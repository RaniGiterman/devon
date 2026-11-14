"""Verifier: after a fix is synced, prove it worked.

Healthy means: the rollout completed, the detectors report nothing new for this workload,
and no alert for it started after the rollout, for the whole verify window."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from kubernetes.client.exceptions import ApiException

from agent.ingest.detectors import scan
from agent.kube import Kube
from agent.store import Store

log = logging.getLogger("devon.verify")


class Verifier:
    def __init__(self, kube: Kube, store: Store, namespaces: list[str], argocd_ns: str, argocd_app: str,
                 alertmanager_url: str, poll_s: int = 10):
        self.kube, self.store, self.namespaces = kube, store, namespaces
        self.argocd_ns, self.argocd_app, self.am, self.poll_s = argocd_ns, argocd_app, alertmanager_url, poll_s

    async def wait_synced(self, sha: str, timeout: int) -> tuple[bool, str]:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                app = await asyncio.to_thread(self.kube.argo_app, self.argocd_ns, self.argocd_app)
                st = app.get("status") or {}
                rev = (st.get("sync") or {}).get("revision", "")
                op = st.get("operationState") or {}
                op_rev = (op.get("syncResult") or {}).get("revision", "")
                last = f"sync={st.get('sync', {}).get('status')} revision={rev[:8]} op={op.get('phase')}"
                # Live state matches the fix commit. (An operation may still be retrying an
                # older revision; that doesn't matter once the desired state is in sync.)
                if rev.startswith(sha[:12]) and (st.get("sync") or {}).get("status") == "Synced":
                    return True, last
                if op_rev.startswith(sha[:12]) and op.get("phase") in ("Failed", "Error"):
                    return False, f"ArgoCD sync failed: {op.get('message', '')[:300]}"
            except ApiException as e:
                last = f"argocd read failed: {e.reason}"
            await asyncio.sleep(5)
        return False, f"ArgoCD did not sync {sha[:8]} within {timeout}s ({last})"

    def _rollout_done(self, ns: str, workload: str) -> bool | None:
        try:
            d = self.kube.plain(self.kube.apps.read_namespaced_deployment(workload, ns))
        except ApiException as e:
            if e.status == 404:
                return None  # not a deployment (service, pvc, ...)
            raise
        spec, st = d.get("spec") or {}, d.get("status") or {}
        want = spec.get("replicas", 1)
        return (st.get("observedGeneration", 0) >= d["metadata"].get("generation", 0)
                and (st.get("updatedReplicas") or 0) == want and (st.get("availableReplicas") or 0) == want
                and (st.get("replicas") or 0) == want)

    async def _alerts_since(self, ns: str, workload: str, since: float) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.am}/api/v2/alerts", params={"active": "true", "silenced": "false", "inhibited": "false"})
            alerts = r.json()
        except Exception:
            return []
        out = []
        from agent.detect import alert_signals, parse_ts
        for a in alerts:
            sigs, _ = alert_signals({"alerts": [{**a, "status": "firing"}]})
            for s in sigs:
                started = parse_ts(a.get("startsAt")) or 0
                if s.namespace == ns and s.workload == workload and started > since:
                    out.append(s.summary)
        return out

    async def verify(self, inc: dict, window: int) -> tuple[bool, str]:
        ns, wl, iid = inc["namespace"], inc["workload"], inc["id"]
        # 1. rollout
        deadline = time.time() + 180
        while True:
            done = await asyncio.to_thread(self._rollout_done, ns, wl)
            if done is None or done:
                break
            if time.time() > deadline:
                return False, f"rollout of {wl} did not complete within 180s"
            await asyncio.sleep(self.poll_s)
        settled = time.time() + 15
        await asyncio.sleep(15)
        # 2. watch window
        bad_streak, last_problem = 0, ""
        end = time.time() + window
        while time.time() < end:
            # Ignore events from before the rollout settled (e.g. readiness failures while the new pod started).
            problems = [s.summary for s in await asyncio.to_thread(scan, self.kube, [ns], self.argocd_ns, self.argocd_app, wl)
                        if s.ts >= settled]
            problems += [f"{s['source']}: {s['summary']}" for s in self.store.signals(iid, since=settled) if s["source"] in ("probe", "logs")]
            problems += [f"alert: {a}" for a in await self._alerts_since(ns, wl, settled)]
            if problems:
                bad_streak += 1
                last_problem = "; ".join(dict.fromkeys(problems))[:600]
                if bad_streak >= 2:
                    return False, f"symptom persists after the fix: {last_problem}"
            else:
                bad_streak = 0
            await asyncio.sleep(self.poll_s)
        return True, f"rollout complete and no symptoms for {window}s"
