"""Incident manager: owns the state machine and drives each incident through
detect -> triage -> investigate -> fix -> approve -> apply -> verify -> resolve | roll back | escalate."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from agent.config import Settings
from agent.evidence import collect as collect_evidence
from agent.gate.validate import Gate
from agent.githost.github import GitHost
from agent.gitrepo import GitRepo
from agent.ingest.detectors import scan
from agent.kube import Kube
from agent.llm import prompts
from agent.llm.client import LLM, LLMError, Usage
from agent.notify.base import Notifier
from agent.signals import SEVERITY_ORDER, Signal
from agent.store import TERMINAL, Store
from agent.tools.registry import ToolRegistry
from agent.verify import Verifier

log = logging.getLogger("devon.manager")
APPROVAL_TIMEOUT_S = 1800
ESCALATED_QUIET_S = 300  # an escalated problem that keeps signalling stays with its incident
SIGNAL_DEDUP_S = 60


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Escalate(Exception):
    pass


class IncidentManager:
    def __init__(self, s: Settings, store: Store, llm: LLM, tools: ToolRegistry, repo: GitRepo, githost: GitHost,
                 gate: Gate, notifier: Notifier, kube: Kube, verifier: Verifier, web_url: str = ""):
        self.s, self.store, self.llm, self.tools, self.repo = s, store, llm, tools, repo
        self.githost, self.gate, self.notifier, self.kube, self.verifier = githost, gate, notifier, kube, verifier
        self.web_url = web_url
        self.tasks: dict[int, asyncio.Task] = {}
        self.decisions: dict[int, asyncio.Future] = {}
        self._seen: dict[tuple, float] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ intake
    def emit(self, sig: Signal) -> None:
        """Sync entry point for detectors (must be called on the event loop)."""
        asyncio.get_running_loop().create_task(self.on_signal(sig))

    async def on_signal(self, sig: Signal) -> None:
        async with self._lock:
            inc = self.store.open_for_key(sig.key)
            if inc is None:
                last = self.store.last_closed_for_key(sig.key)
                if last and last["state"] == "ESCALATED" and self._still_noisy(last["id"], sig.symptom):
                    self._attach(last["id"], sig)  # same ongoing problem; a human owns it now
                    return
                if self._open_parent(last):
                    self._attach(last["id"], sig)  # still a downstream symptom of an open incident
                    return
                iid = self.store.create(sig.key, sig.namespace, sig.workload, sig.severity, sig.summary)
                self._attach(iid, sig)
                inc = self.store.get(iid)
                log.info("incident #%d opened: %s", iid, sig.summary)
                await self.notifier.update(inc, f"detected by {sig.source}: {sig.summary}")
                self.tasks[iid] = asyncio.create_task(self._run(iid), name=f"incident-{iid}")
            else:
                self._attach(inc["id"], sig)
                if SEVERITY_ORDER.get(sig.severity, 0) > SEVERITY_ORDER.get(inc["severity"] or "", 0):
                    self.store.update(inc["id"], severity=sig.severity)

    def _still_noisy(self, iid: int, symptom: str | None = None) -> bool:
        """True while the escalated problem keeps signalling. With `symptom`, only if that symptom is
        part of the escalated problem: a different symptom is a new fault and gets its own incident."""
        sigs = self.store.signals(iid, since=time.time() - ESCALATED_QUIET_S)
        if symptom is not None:
            return any(s["symptom"] == symptom for s in sigs)
        return bool(sigs)

    def _parent_active(self, parent: dict | None) -> bool:
        """A root-cause incident is still active while it is open, or escalated and still signalling
        (a human is on it; its downstream symptoms should stay quiet too)."""
        if not parent:
            return False
        if parent["state"] not in TERMINAL:
            return True
        return parent["state"] == "ESCALATED" and self._still_noisy(parent["id"])

    def _open_parent(self, last: dict | None) -> dict | None:
        if not last or last["state"] != "DROPPED":
            return None
        dup = (last.get("triage") or {}).get("duplicate_of")
        parent = self.store.get(int(dup)) if dup else None
        return parent if self._parent_active(parent) else None

    def _attach(self, iid: int, sig: Signal) -> None:
        k = (iid, sig.source, sig.symptom, sig.summary)
        now = time.time()
        if now - self._seen.get(k, 0) < SIGNAL_DEDUP_S:
            return
        self._seen[k] = now
        self.store.add_signal(iid, sig)

    def recover_after_restart(self) -> None:
        for inc in self.store.open_incidents():
            # Closed as DROPPED, not ESCALATED: an escalation mutes later signals for the
            # workload, and a restart must not hide problems that are still happening.
            self.store.transition(inc["id"], "DROPPED", "agent restarted; re-detected if still failing")

    # ------------------------------------------------------------- approvals
    async def approve(self, iid: int, who: str) -> str:
        fut = self.decisions.get(iid)
        inc = self.store.get(iid)
        if not inc or not fut or fut.done():
            return f"incident #{iid} is not awaiting approval ({inc['state'] if inc else 'unknown'})"
        fut.set_result(("approve", who, ""))
        return f"approved by {who}; merging"

    async def reject(self, iid: int, who: str, reason: str) -> str:
        fut = self.decisions.get(iid)
        if not fut or fut.done():
            return f"incident #{iid} is not awaiting approval"
        fut.set_result(("reject", who, reason or "no reason given"))
        return f"rejected by {who}; re-investigating with your feedback"

    async def explain(self, iid: int) -> str:
        inc = self.store.get(iid)
        if not inc:
            return "unknown incident"
        data = {k: inc.get(k) for k in ("workload", "state", "diagnosis", "fix")}
        data["gate"] = {k: (inc.get("gate") or {}).get(k) for k in ("checks", "blast_radius", "diff")}
        try:
            return await self.llm.text(prompts.EXPLAIN, json.dumps(data, indent=1)[:12000])
        except LLMError as e:
            return f"could not generate an explanation: {e}"

    # ----------------------------------------------------------- state machine
    async def _to(self, iid: int, state: str, note: str = "", notify: bool = True) -> dict:
        self.store.transition(iid, state, note)
        inc = self.store.get(iid)
        log.info("#%d -> %s %s", iid, state, note)
        if notify and note:
            await self.notifier.update(inc, f"{state.lower().replace('_', ' ')}: {note}")
        return inc

    async def _run(self, iid: int) -> None:
        usage = Usage()
        try:
            await self._pipeline(iid, usage)
        except Escalate as e:
            await self._to(iid, "ESCALATED", str(e))
        except LLMError as e:
            await self._to(iid, "ESCALATED", f"LLM unavailable: {e}")
        except Exception as e:
            log.exception("incident #%d crashed", iid)
            await self._to(iid, "ESCALATED", f"agent error: {type(e).__name__}: {e}")
        finally:
            self.store.add_usage(iid, usage.tokens, usage.ms, usage.calls)
            self.decisions.pop(iid, None)
            self.tasks.pop(iid, None)

    async def _pipeline(self, iid: int, usage: Usage) -> None:
        await asyncio.sleep(self.s.debounce_s)
        inc = self.store.get(iid)

        # Triage is deterministic: is it still happening after the debounce? (No LLM call.)
        if not await self._still_happening(inc):
            await self._to(iid, "DROPPED", "recovered by itself before triage", notify=False)
            return
        await self._to(iid, "TRIAGED", f"still failing after {self.s.debounce_s}s", notify=False)
        # "ArgoCD app Degraded" just echoes a broken workload in that app; if one is already being
        # handled in the same namespace, don't spend a model call on the echo.
        if inc["workload"].startswith("argocd/"):
            root = next((o for o in self.store.open_incidents()
                         if o["id"] != iid and o["namespace"] == inc["namespace"] and not o["workload"].startswith("argocd/")), None)
            if root:
                await self._to(iid, "DROPPED", f"app-level symptom of incident #{root['id']}", notify=False)
                self.store.update(iid, triage={"duplicate_of": root["id"]})
                return

        feedback = ""
        for attempt in range(1, self.s.max_fix_attempts + 1):
            self.store.update(iid, attempts=attempt)
            # One model call: triage verdict + diagnosis + first fix proposal.
            diagnosis, old_files = await self._investigate(iid, feedback, usage)
            dup = diagnosis.get("duplicate_of")
            if dup and str(dup).isdigit() and int(dup) != iid and self._parent_active(self.store.get(int(dup))):
                await self._to(iid, "DROPPED", f"downstream symptom of incident #{dup}")
                return
            if diagnosis.get("actionable") is False:
                await self._to(iid, "DROPPED", f"not actionable: {diagnosis.get('root_cause', '')}", notify=False)
                return
            if (diagnosis.get("fix_class") in (None, "none") or diagnosis.get("escalate_reason")
                    or float(diagnosis.get("confidence") or 0) < self.s.min_confidence):
                reason = diagnosis.get("escalate_reason") or (
                    f"confidence {diagnosis.get('confidence')} below {self.s.min_confidence}"
                    if float(diagnosis.get("confidence") or 0) < self.s.min_confidence else "no safe config fix")
                raise Escalate(f"{diagnosis.get('root_cause', '')} Needs a human: {reason}")
            if self.store.prs_since(time.time() - 3600) >= self.s.max_prs_per_hour:
                raise Escalate(f"PR rate limit reached ({self.s.max_prs_per_hour}/hour). Diagnosis: {diagnosis.get('root_cause')}")

            fix, gate = await self._propose_fix(iid, diagnosis, old_files, usage)
            if fix is None:
                feedback = f"Previous fix attempt failed validation: {json.dumps(gate.get('checks'))[:1500]}"
                continue

            branch = f"devon/incident-{iid}-{attempt}"
            await self.repo.commit_branch(branch, fix["new_files"], f"{fix['title']}\n\nIncident #{iid}: {diagnosis.get('root_cause', '')}")
            number, url = await self.githost.open_pr(branch, fix["title"], self._pr_body(iid, diagnosis, fix, gate))
            self.store.update(iid, pr_number=number, pr_url=url)
            inc = await self._to(iid, "AWAITING_APPROVAL", f"PR #{number} opened", notify=False)
            fut = asyncio.get_running_loop().create_future()
            self.decisions[iid] = fut
            await self.notifier.request_approval(inc)
            try:
                decision, who, reason = await asyncio.wait_for(fut, APPROVAL_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise Escalate(f"no decision on PR #{number} within {APPROVAL_TIMEOUT_S // 60} min")
            if decision == "reject":
                self.store.update(iid, approver=who, reject_reason=reason)
                await self.githost.close_pr(number, f"Rejected by {who}: {reason}")
                feedback = f"A human rejected the previous fix ({fix['title']}). Their reason: {reason}"
                await self._to(iid, "INVESTIGATING", f"rejected by {who}: {reason}")
                continue
            self.store.update(iid, approver=who)
            await self._apply_and_verify(iid, number, fix)
            return
        raise Escalate(f"no acceptable fix after {self.s.max_fix_attempts} attempts")

    async def _still_happening(self, inc: dict) -> bool:
        recent = [s for s in self.store.signals(inc["id"], since=time.time() - 90) if s["source"] in ("probe", "logs", "alert")]
        if recent:
            return True
        current = await asyncio.to_thread(scan, self.kube, [inc["namespace"]] if inc["namespace"] else self.s.watch_namespaces,
                                          self.s.argocd_namespace, self.s.argocd_app, inc["workload"])
        return bool(current)

    def _signal_digest(self, iid: int, limit: int = 15) -> str:
        sigs = self.store.signals(iid)
        lines, seen = [], set()
        for s in sigs[-60:]:
            k = (s["source"], s["symptom"])
            if k in seen:
                continue
            seen.add(k)
            lines.append(f"- [{s['source']}] {s['symptom']}: {s['summary']} | {' / '.join(s['evidence'][:2])}")
        return "\n".join(lines[-limit:])

    async def _investigate(self, iid: int, feedback: str, usage: Usage) -> tuple[dict, dict[str, str]]:
        """Collect the evidence pack (no LLM), then one model call for triage + diagnosis + fix."""
        inc = await self._to(iid, "INVESTIGATING", "", notify=False)
        await self.notifier.update(inc, "collecting evidence and investigating…")

        async def on_step(name: str, args: str, result: str) -> None:
            self.store.add_step(iid, "tool", name, f"{args}\n---\n{result[:1500]}")

        await self.repo.sync()
        others = [o for o in self.store.open_incidents() if o["id"] != iid]
        pack, old_files = await collect_evidence(inc, self.tools, self.kube, self.repo, self.s.gitops_apps_path,
                                                 self._signal_digest(iid), others, on_step)
        if feedback:
            pack += f"\n\n## IMPORTANT, from the previous attempt\n{feedback}"
        self.store.add_step(iid, "evidence", "pack", f"{len(pack)} chars, {len(old_files)} manifest files")
        result = await self.llm.run_tools(prompts.INVESTIGATE_AND_FIX, pack, self.tools, self.s.max_tool_calls, usage, on_step)
        triage = {k: result.get(k) for k in ("actionable", "duplicate_of", "severity")}
        diagnosis = {k: v for k, v in result.items() if k != "fix"}
        diagnosis["proposed_fix"] = result.get("fix") or {}
        self.store.update(iid, triage=triage, severity=result.get("severity") or inc["severity"],
                          diagnosis={k: v for k, v in diagnosis.items() if k != "proposed_fix"})
        self.store.add_step(iid, "diagnosis", "investigate+fix", json.dumps(result)[:4000])
        return diagnosis, old_files

    async def _propose_fix(self, iid: int, diagnosis: dict, old_files: dict[str, str],
                           usage: Usage) -> tuple[dict | None, dict]:
        """Validate the fix from the combined call; only on a gate failure ask FIXER to correct it."""
        inc = self.store.get(iid)
        feedback, gate_res = "", {}
        for attempt in range(self.s.max_fix_attempts):
            fix = diagnosis.get("proposed_fix") if attempt == 0 else None
            if not fix or not fix.get("files"):
                user = (f"Diagnosis:\n{json.dumps({k: v for k, v in diagnosis.items() if k != 'proposed_fix'}, indent=1)}\n\n"
                        "Current files:\n" + "\n".join(f"=== {p} ===\n{c}" for p, c in old_files.items()) +
                        (f"\n\nYour previous proposal failed validation:\n{feedback}\nFix that." if feedback else ""))
                fix = await self.llm.json(prompts.FIXER, user, smart=True, usage=usage)
            fix = dict(fix)
            new_files = {}
            for f in fix.get("files") or []:
                p, c = f.get("path", ""), f.get("content", "")
                if not c.endswith("\n"):
                    c += "\n"
                if p in old_files and old_files[p] == c:
                    continue
                if p in old_files or p.startswith(f"{self.s.gitops_apps_path}/network/"):
                    new_files[p] = c
            fix["new_files"] = new_files
            fix.setdefault("title", f"{inc['workload']}: fix {diagnosis.get('fix_class')}")
            gate = await self.gate.run(self.repo.dir, {p: old_files.get(p) for p in new_files}, new_files)
            gate_res = gate.to_dict()
            self.store.update(iid, fix={k: v for k, v in fix.items() if k != "files"}, gate=gate_res)
            self.store.add_step(iid, "gate", "validation", json.dumps(gate.checks))
            if gate.passed:
                await self._to(iid, "FIX_PROPOSED", fix["title"], notify=False)
                return fix, gate_res
            feedback = "\n".join(f"{c['name']}: {c['detail']}" for c in gate.checks if not c["ok"])
            log.info("#%d gate failed (attempt %d): %s", iid, attempt + 1, feedback[:300])
        return None, gate_res

    def _pr_body(self, iid: int, d: dict, fix: dict, gate: dict) -> str:
        ev = "\n".join(f"- {e}" for e in d.get("evidence", [])[:6])
        checks = "\n".join(f"- {'✅' if c['ok'] else '❌'} **{c['name']}**: {c['detail'].splitlines()[0] if c['detail'] else ''}"
                           for c in gate.get("checks", []))
        blast = "\n".join(f"- {b}" for b in gate.get("blast_radius", []))
        link = f"\n\nIncident page: {self.web_url}/#incident-{iid}" if self.web_url else ""
        return (f"## Incident #{iid}\n\n**Root cause:** {d.get('root_cause')}\n\n"
                f"**Suspect change:** `{d.get('suspect_commit') or 'n/a'}` {d.get('suspect_change') or ''}\n\n"
                f"**Evidence:**\n{ev}\n\n**Fix:** {fix.get('summary', '')} (risk: {fix.get('risk', '?')})\n\n"
                f"**Validation gate:**\n{checks}\n\n**Blast radius:**\n{blast}\n\n"
                f"Confidence: {d.get('confidence')}{link}\n\n---\nOpened by the devon agent. Approve or reject in Telegram.")

    async def _apply_and_verify(self, iid: int, number: int, fix: dict) -> None:
        await self._to(iid, "APPLYING", f"merging PR #{number}")
        sha = await self.githost.merge_pr(number, f"{fix['title']} (#{number})")
        self.store.update(iid, merge_sha=sha)
        ok, detail = await self.verifier.wait_synced(sha, self.s.sync_timeout_s)
        if not ok:
            raise Escalate(detail)
        inc = await self._to(iid, "VERIFYING", f"ArgoCD synced {sha[:8]}; watching for {self.s.verify_window_s}s")
        ok, detail = await self.verifier.verify(inc, self.s.verify_window_s)
        if ok:
            inc = await self._to(iid, "RESOLVED", f"{detail}. Time to resolve: {int(time.time() - inc['created'])}s")
            self.store.update(iid, outcome="fixed")
            asyncio.create_task(self._postmortem(iid))
            return
        await self._to(iid, "ROLLED_BACK", detail)
        try:
            rsha = await self.repo.revert(sha, f"Revert \"{fix['title']}\"\n\nIncident #{iid}: fix did not resolve the problem: {detail}")
            note = f"reverted fix with commit {rsha[:8]}"
        except Exception as e:
            note = f"automatic revert FAILED ({e}); revert {sha[:8]} manually"
        raise Escalate(f"{note}. {detail}")

    def postmortem_markdown(self, iid: int) -> str:
        """Built from the incident record; no model call needed."""
        inc = self.store.get(iid)
        d, fix = inc.get("diagnosis") or {}, inc.get("fix") or {}
        tr = self.store.transitions(iid)
        timeline = "\n".join(f"- {utc(t['ts'])}  **{t['to_state']}**" + (f": {t['note']}" if t["note"] else "") for t in tr)
        sigs = self.store.signals(iid)
        detectors = ", ".join(sorted({s["source"] for s in sigs})) or "n/a"
        first = "; ".join(dict.fromkeys(s["summary"] for s in sigs[:3])) or inc["title"]
        evidence = "\n".join(f"- {e}" for e in d.get("evidence") or []) or "- n/a"
        took = int((inc["resolved"] or time.time()) - inc["created"])
        return (f"# Postmortem: incident #{iid} ({inc['workload']})\n\n"
                f"## Summary\n{inc['workload']} in `{inc['namespace']}` was unhealthy for {took}s. "
                f"{d.get('root_cause', '')}\n\n"
                f"## Timeline (UTC)\n{timeline}\n\n"
                f"## Root cause\n{d.get('root_cause', 'n/a')}\n\n"
                f"Caused by commit `{d.get('suspect_commit') or 'n/a'}`: {d.get('suspect_change') or 'n/a'}\n\n"
                f"Evidence:\n{evidence}\n\n"
                f"## Fix\n{fix.get('title', 'n/a')} ({inc.get('pr_url') or 'no PR'}), approved by {inc.get('approver') or 'n/a'}, "
                f"merged as `{(inc.get('merge_sha') or '')[:8]}`. {fix.get('summary', '')}\n\n"
                f"## Detection\nDetectors: {detectors}. First signals: {first}\n\n"
                f"## Prevention\n- Review changes of class `{d.get('fix_class', 'n/a')}` carefully; this one passed review and broke {inc['workload']}.\n"
                f"- Consider a policy or CI check that would have caught it before merge.\n")

    async def _postmortem(self, iid: int) -> None:
        inc = self.store.get(iid)
        try:
            md = self.postmortem_markdown(iid)
            day = datetime.fromtimestamp(inc["created"], timezone.utc).strftime("%Y-%m-%d")
            path = f"runbooks/postmortems/{day}-incident-{iid}-{inc['workload'].replace('/', '-')}.md"
            await self.repo.commit_main({path: md}, f"postmortem: incident #{iid} ({inc['workload']})")
            self.store.add_step(iid, "postmortem", path, md[:2000])
        except Exception as e:
            log.warning("postmortem for #%d failed: %s", iid, e)
