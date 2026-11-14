"""Deterministic detection rules. Pure functions over plain dicts (API objects as
returned by `ApiClient.sanitize_for_serialization`), so they are easy to test and
shared by the watch, the sweep, and the pre-triage recheck.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from agent.signals import Signal

BAD_WAITING = {
    "CrashLoopBackOff": "critical",
    "ImagePullBackOff": "critical",
    "ErrImagePull": "critical",
    "CreateContainerConfigError": "critical",
    "CreateContainerError": "critical",
    "InvalidImageName": "critical",
    "RunContainerError": "critical",
}
WARNING_EVENT_REASONS = {
    "FailedScheduling", "FailedMount", "FailedAttachVolume", "Unhealthy", "BackOff",
    "FailedCreate", "Evicted", "OOMKilling", "FailedCreatePodSandBox", "ProvisioningFailed",
    "VolumeResizeFailed",
}
PENDING_GRACE_S = 60
NOT_READY_GRACE_S = 60
EVENT_FRESH_S = 180
RECENT_OOM_S = 600

_HASH_SUFFIX = re.compile(r"-[a-z0-9]{8,10}$")
_POD_SUFFIX = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$")


def parse_ts(v) -> float | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime):
        return v.timestamp()
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _age(v, now: float) -> float | None:
    t = parse_ts(v)
    return None if t is None else now - t


def workload_of_pod(pod: dict) -> str:
    """Owning workload name: app label, else owner ReplicaSet minus its hash."""
    meta = pod.get("metadata", {})
    labels = meta.get("labels") or {}
    if labels.get("app"):
        return labels["app"]
    for ref in meta.get("ownerReferences") or []:
        if ref.get("kind") == "ReplicaSet":
            return _HASH_SUFFIX.sub("", ref["name"])
        if ref.get("kind") in ("StatefulSet", "DaemonSet", "Job"):
            return ref["name"]
    return _POD_SUFFIX.sub("", meta.get("name", "unknown"))


def workload_of_object(kind: str, name: str) -> str:
    if kind == "Pod":
        return _POD_SUFFIX.sub("", name)
    if kind == "ReplicaSet":
        return _HASH_SUFFIX.sub("", name)
    return name


# --- pods ------------------------------------------------------------------

def pod_signals(pod: dict, now: float | None = None) -> list[Signal]:
    now = now or time.time()
    meta, status = pod.get("metadata", {}), pod.get("status") or {}
    ns, name = meta.get("namespace", ""), meta.get("name", "")
    if meta.get("deletionTimestamp"):
        return []
    wl = workload_of_pod(pod)
    out: list[Signal] = []

    for cs in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
        waiting = (cs.get("state") or {}).get("waiting") or {}
        reason = waiting.get("reason")
        last_term = (cs.get("lastState") or {}).get("terminated") or {}
        if reason in BAD_WAITING:
            ev = [f"pod {name} container {cs['name']}: {reason}: {waiting.get('message', '')}".strip(": ")]
            if last_term:
                ev.append(f"last terminated: reason={last_term.get('reason')} exitCode={last_term.get('exitCode')}")
            ev.append(f"restartCount={cs.get('restartCount', 0)}")
            symptom = "OOMKilled" if last_term.get("reason") == "OOMKilled" else reason
            out.append(Signal("watch", ns, wl, symptom, f"{wl}: container {cs['name']} {symptom}",
                              BAD_WAITING[reason], ev))
        elif last_term.get("reason") == "OOMKilled":
            age = _age(last_term.get("finishedAt"), now)
            if age is not None and age < RECENT_OOM_S:
                out.append(Signal("watch", ns, wl, "OOMKilled",
                                  f"{wl}: container {cs['name']} was OOMKilled {int(age)}s ago", "critical",
                                  [f"pod {name} container {cs['name']} lastState.terminated.reason=OOMKilled",
                                   f"restartCount={cs.get('restartCount', 0)}"]))

    phase = status.get("phase")
    if phase == "Pending":
        age = _age(meta.get("creationTimestamp"), now) or 0
        if age > PENDING_GRACE_S and not out:
            conds = {c.get("type"): c for c in status.get("conditions") or []}
            sched = conds.get("PodScheduled", {})
            msg = sched.get("message") or "pod not started"
            out.append(Signal("watch", ns, wl, "Pending", f"{wl}: pod {name} Pending for {int(age)}s",
                              "critical", [f"pod {name} Pending: {sched.get('reason', '')} {msg}".strip()]))
    elif phase == "Running" and not out:
        for c in status.get("conditions") or []:
            if c.get("type") == "Ready" and c.get("status") != "True":
                age = _age(c.get("lastTransitionTime"), now) or 0
                if age > NOT_READY_GRACE_S:
                    out.append(Signal("watch", ns, wl, "NotReady", f"{wl}: pod {name} not Ready for {int(age)}s",
                                      "warning", [f"pod {name} Ready=False: {c.get('reason', '')} {c.get('message', '')}".strip()]))
    elif phase == "Failed":
        out.append(Signal("watch", ns, wl, "PodFailed", f"{wl}: pod {name} Failed", "warning",
                          [f"pod {name} phase=Failed reason={status.get('reason')} {status.get('message', '')}".strip()]))
    return out


# --- events ----------------------------------------------------------------

def event_signal(ev: dict, now: float | None = None) -> Signal | None:
    now = now or time.time()
    if ev.get("type") != "Warning" or ev.get("reason") not in WARNING_EVENT_REASONS:
        return None
    last = ev.get("lastTimestamp") or ev.get("eventTime") or (ev.get("series") or {}).get("lastObservedTime") \
        or ev.get("metadata", {}).get("creationTimestamp")
    age = _age(last, now)
    if age is None or age > EVENT_FRESH_S:
        return None
    obj = ev.get("involvedObject") or ev.get("regarding") or {}
    kind, name = obj.get("kind", ""), obj.get("name", "")
    ns = obj.get("namespace") or ev.get("metadata", {}).get("namespace", "")
    wl = workload_of_object(kind, name)
    reason, msg = ev["reason"], (ev.get("message") or ev.get("note") or "").strip()
    symptom = {"Unhealthy": "ProbeFailing", "BackOff": "CrashLoopBackOff"}.get(reason, reason)
    sev = "critical" if reason in ("FailedScheduling", "Evicted", "OOMKilling", "FailedMount") else "warning"
    count = ev.get("count") or (ev.get("series") or {}).get("count") or 1
    return Signal("watch", ns, wl, symptom, f"{wl}: {reason}: {msg[:140]}", sev,
                  [f"event {reason} on {kind}/{name} (x{count}): {msg[:400]}"], ts=parse_ts(last) or now)


# --- workloads -------------------------------------------------------------

def deployment_signals(dep: dict) -> list[Signal]:
    meta, spec, status = dep.get("metadata", {}), dep.get("spec") or {}, dep.get("status") or {}
    ns, name = meta.get("namespace", ""), meta.get("name", "")
    out = []
    for c in status.get("conditions") or []:
        if c.get("type") == "Progressing" and c.get("status") == "False":
            out.append(Signal("watch", ns, name, "RolloutStuck", f"{name}: rollout stuck ({c.get('reason')})",
                              "critical", [f"deployment {name} Progressing=False: {c.get('reason')}: {c.get('message', '')}"]))
    want = spec.get("replicas", 1)
    avail = status.get("availableReplicas") or 0
    if want > 0 and avail == 0 and status.get("observedGeneration", 0) >= meta.get("generation", 0):
        out.append(Signal("watch", ns, name, "Unavailable", f"{name}: 0/{want} replicas available", "critical",
                          [f"deployment {name} availableReplicas=0 desired={want}"]))
    return out


def endpointslice_signal(es: dict, pods_exist_for_service: bool | None = None) -> Signal | None:
    meta = es.get("metadata", {})
    svc = (meta.get("labels") or {}).get("kubernetes.io/service-name")
    if not svc:
        return None
    ns = meta.get("namespace", "")
    eps = es.get("endpoints") or []
    ready = [e for e in eps if (e.get("conditions") or {}).get("ready")]
    if ready:
        return None
    detail = "no endpoints at all (selector matches no pods)" if not eps else f"{len(eps)} endpoints, none ready"
    return Signal("watch", ns, svc, "NoEndpoints", f"service {svc} has no ready endpoints", "critical",
                  [f"EndpointSlice {meta.get('name')}: {detail}"])


def node_signals(node: dict) -> list[Signal]:
    name = node.get("metadata", {}).get("name", "")
    out = []
    for c in (node.get("status") or {}).get("conditions") or []:
        t, s = c.get("type"), c.get("status")
        if (t == "Ready" and s != "True") or (t in ("MemoryPressure", "DiskPressure", "PIDPressure") and s == "True"):
            out.append(Signal("watch", "", f"node/{name}", t if t != "Ready" else "NodeNotReady",
                              f"node {name}: {t}={s}", "critical", [f"node {name} {t}={s}: {c.get('message', '')}"]))
    return out


def pvc_signal(pvc: dict, now: float | None = None) -> Signal | None:
    now = now or time.time()
    meta, status = pvc.get("metadata", {}), pvc.get("status") or {}
    if status.get("phase") != "Pending":
        return None
    age = _age(meta.get("creationTimestamp"), now) or 0
    if age < PENDING_GRACE_S:
        return None
    name = meta.get("name", "")
    return Signal("watch", meta.get("namespace", ""), f"pvc/{name}", "PVCPending", f"PVC {name} Pending for {int(age)}s",
                  "critical", [f"pvc {name} phase=Pending"])


def argo_app_signal(app: dict, watch_namespaces: list[str]) -> Signal | None:
    status = app.get("status") or {}
    health = (status.get("health") or {}).get("status")
    op = status.get("operationState") or {}
    name = app.get("metadata", {}).get("name", "")
    ns = (app.get("spec") or {}).get("destination", {}).get("namespace", "")
    if ns not in watch_namespaces:
        return None
    failed = [r for r in (op.get("syncResult") or {}).get("resources") or [] if r.get("status") == "SyncFailed"]
    if failed and op.get("phase") in ("Running", "Failed", "Error"):
        r = failed[0]
        # Attribute to the workload whose manifest the API server rejected.
        return Signal("watch", r.get("namespace") or ns, r.get("name", f"argocd/{name}"), "SyncFailed",
                      f"{r.get('name')}: ArgoCD cannot apply {r.get('kind')} ({op.get('phase')})", "critical",
                      [f"argocd sync of revision {str((op.get('syncResult') or {}).get('revision', ''))[:8]}: "
                       f"{r.get('kind')}/{r.get('name')}: {r.get('message', '')[:400]}"])
    if op.get("phase") in ("Failed", "Error"):
        return Signal("watch", ns, f"argocd/{name}", "SyncFailed", f"ArgoCD app {name} sync {op.get('phase')}",
                      "critical", [f"argocd app {name}: {op.get('message', '')[:400]}"])
    if health == "Degraded":
        bad = [f"{r.get('kind')}/{r.get('name')}: {(r.get('health') or {}).get('message', '')}"
               for r in status.get("resources") or [] if (r.get("health") or {}).get("status") == "Degraded"]
        # Degraded resources are reported by the pod/deployment rules with better
        # workload attribution; only signal the app when nothing else explains it.
        if not bad:
            return Signal("watch", ns, f"argocd/{name}", "AppDegraded", f"ArgoCD app {name} Degraded", "warning",
                          [f"argocd app {name} health=Degraded"])
    return None


# --- alerts ----------------------------------------------------------------

def alert_signals(payload: dict) -> tuple[list[Signal], bool]:
    """Alertmanager webhook payload -> (signals, saw_watchdog)."""
    out, watchdog = [], False
    for a in payload.get("alerts") or []:
        labels, ann = a.get("labels") or {}, a.get("annotations") or {}
        name = labels.get("alertname", "Alert")
        if name == "Watchdog":
            watchdog = True
            continue
        if a.get("status") != "firing" or name == "InfoInhibitor":
            continue
        ns = labels.get("namespace", "")
        if labels.get("persistentvolumeclaim"):
            wl = f"pvc/{labels['persistentvolumeclaim']}"
        elif labels.get("svc"):
            wl = labels["svc"]
        elif labels.get("deployment"):
            wl = labels["deployment"]
        elif labels.get("pod"):
            wl = workload_of_object("Pod", labels["pod"])
        elif labels.get("node"):
            wl = f"node/{labels['node']}"
        else:
            wl = name
        sev = labels.get("severity", "warning")
        sev = sev if sev in ("info", "warning", "critical") else "warning"
        summary = ann.get("summary") or ann.get("description") or name
        symptom = name.removeprefix("Demo").removeprefix("Kube")
        out.append(Signal("alert", ns, wl, symptom, f"{wl}: {summary}"[:200], sev,
                          [f"alert {name} firing since {a.get('startsAt', '?')}: {ann.get('description') or summary}"[:500]],
                          stateful=False))
    return out, watchdog


# --- logs ------------------------------------------------------------------

ERROR_PATTERNS = re.compile(
    r'("level":\s*"(error|fatal)")|\b(ERROR|FATAL|Traceback|Exception|panic:)\b|connection refused|timed out|'
    r'no route to host|could not connect',
    re.IGNORECASE)


def log_error_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ERROR_PATTERNS.search(ln)]
