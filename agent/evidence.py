"""Evidence pack: gather the facts an investigation almost always needs, without the model.

The old design let the model fetch these one tool call at a time, which cost one LLM request
(re-sending the whole conversation) per fetch. They are nearly the same for every incident,
so the agent collects them itself, through the same read-only tools (so each fetch is still
recorded as a step), and the model reads everything in a single request.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from agent.detect import workload_of_pod
from agent.tools.registry import ToolRegistry

# Per-section caps keep one request well under a free-tier per-minute token budget.
CAPS = {"signals": 1500, "pods": 1200, "events": 1200, "logs": 1500, "git_log": 1500, "git_show": 1500,
        "argocd": 800, "metrics": 600, "runbook": 800, "others": 400, "files": 5000}
COMMIT_LOOKBACK_S = 2 * 3600
_COMMIT_LINE = re.compile(r"^([0-9a-f]{7,40}) (\d{4}-\d\d-\d\dT[0-9:.+\-Z]+) ", re.M)

OnStep = Callable[[str, str, str], Awaitable[None]]


def _cap(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + f"\n… [{len(text) - n} more chars]"


def _ts(iso: str) -> float | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def suspect_commits(git_log: str, before: float, lookback: float = COMMIT_LOOKBACK_S, limit: int = 2) -> list[str]:
    """Commits made shortly before the incident started (newest first)."""
    out = []
    for sha, iso in _COMMIT_LINE.findall(git_log):
        t = _ts(iso)
        if t is not None and before - lookback <= t <= before + 60:
            out.append(sha)
    return out[:limit]


def candidate_files(repo, apps_path: str, workload: str) -> list[str]:
    """Manifests a fix may touch: the workload's own directory plus network policies."""
    name = workload.split("/")[-1]
    files = []
    for rel in repo.list_files(apps_path):
        if not rel.endswith((".yaml", ".yml", ".env")):
            continue
        if rel.startswith(f"{apps_path}/{name}/") or rel.startswith(f"{apps_path}/network/"):
            files.append(rel)
        elif re.search(rf"\b(name|claimName):\s*{re.escape(name)}\b", repo.read(rel)):
            files.append(rel)  # an object named after the workload in another directory (e.g. a PVC)
    return files[:10]


async def collect(inc: dict, tools: ToolRegistry, kube, repo, apps_path: str, signal_digest: str,
                  other_incidents: list[dict], on_step: OnStep | None = None) -> tuple[str, dict[str, str]]:
    ns, wl = inc["namespace"] or "default", inc["workload"]
    name = wl.split("/")[-1]
    symptoms = set(re.findall(r"\] (\w+):", signal_digest))

    async def tool(tname: str, **args) -> str:
        result, _ = await tools.call(tname, args)
        if on_step:
            await on_step(tname, str(args), result)
        return result

    sections: list[tuple[str, str]] = [("Detector signals", _cap(signal_digest, CAPS["signals"]))]

    # Pods, events and logs of the workload.
    pods = []
    try:
        import asyncio
        pods = [p for p in await asyncio.to_thread(kube.pods, ns) if workload_of_pod(p) == name]
    except Exception:
        pass
    sections.append(("Pods", _cap(await tool("k8s_get", kind="pod", namespace=ns, label_selector=f"app={name}"), CAPS["pods"])))
    target = max(pods, key=lambda p: sum(c.get("restartCount", 0) for c in (p.get("status") or {}).get("containerStatuses") or []),
                 default=None)
    obj = target["metadata"]["name"] if target else name
    events = await tool("k8s_events", namespace=ns, object_name=obj)
    sections.append((f"Recent events for {obj}", _cap("\n".join(events.splitlines()[-12:]), CAPS["events"])))
    if target:
        cs = ((target.get("status") or {}).get("containerStatuses") or [{}])[0]
        crashed = bool((cs.get("lastState") or {}).get("terminated")) and cs.get("restartCount", 0) > 0
        logs = await tool("k8s_logs", pod=obj, namespace=ns, previous=crashed, tail=30)
        sections.append((f"Logs of {obj}{' (previous, crashed container)' if crashed else ''}", _cap(logs, CAPS["logs"])))

    # What changed: recent commits, and the full diff of those made shortly before the incident.
    log = await tool("git_log", path=apps_path, n=6)
    sections.append(("Recent commits to the gitops repo (newest first)", _cap(log, CAPS["git_log"])))
    for sha in suspect_commits(log, inc["created"]):
        sections.append((f"Diff of commit {sha}", _cap(await tool("git_show", commit=sha), CAPS["git_show"])))

    sections.append(("ArgoCD", _cap(await tool("argocd_status"), CAPS["argocd"])))

    # Metrics only where a symptom needs numbers.
    if "OOMKilled" in symptoms:
        q = (f'max by (container) (max_over_time(container_memory_working_set_bytes{{namespace="{ns}",pod=~"{name}-.*",container!=""}}[1h]))')
        sections.append(("Peak memory in the last hour (bytes)", _cap(await tool("prom_query", promql=q), CAPS["metrics"])))
    if symptoms & {"PersistentVolumeFillingUp", "PVCPending"} or wl.startswith("pvc/"):
        q = f'kubelet_volume_stats_used_bytes{{namespace="{ns}"}} / kubelet_volume_stats_capacity_bytes{{namespace="{ns}"}}'
        sections.append(("PVC usage ratio", _cap(await tool("prom_query", promql=q), CAPS["metrics"])))

    if symptoms:
        sections.append(("Most relevant runbook", _cap(await tool("runbook_search", query=" ".join(sorted(symptoms))), CAPS["runbook"])))

    others = [f"#{o['id']} {o['workload']} [{o['state']}]: {o['title']}" for o in other_incidents]
    sections.append(("Other open incidents", _cap("\n".join(others) or "(none)", CAPS["others"])))

    files: dict[str, str] = {}
    used = 0
    for rel in candidate_files(repo, apps_path, wl):
        content = repo.read(rel)
        if used + len(content) > CAPS["files"]:
            break
        files[rel], used = content, used + len(content)
    sections.append(("Current manifest files (edit these for the fix)",
                     "\n".join(f"=== {p} ===\n{c}" for p, c in files.items()) or "(none)"))

    text = (f"Incident #{inc['id']}: workload '{wl}' in namespace '{ns}', first detected "
            f"{datetime.fromtimestamp(inc['created'], timezone.utc):%Y-%m-%d %H:%M:%S} UTC. "
            f"Now {datetime.fromtimestamp(time.time(), timezone.utc):%H:%M:%S} UTC.\n\n" +
            "\n\n".join(f"## {title}\n{body}" for title, body in sections))
    return text, files
