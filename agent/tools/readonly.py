"""Read-only tools the investigator can call. None of them can change anything:
kubectl runs with the read-only identity and only `get`/`logs` verbs are exposed."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from agent.gitrepo import GitRepo
from agent.kube import NAME_RE, compact_yaml, kubectl
from agent.tools.registry import Tool, ToolRegistry, obj

KINDS = {
    "pod": "pods", "pods": "pods", "deployment": "deployments", "deploy": "deployments",
    "replicaset": "replicasets", "statefulset": "statefulsets", "service": "services", "svc": "services",
    "endpointslice": "endpointslices", "configmap": "configmaps", "pvc": "persistentvolumeclaims",
    "persistentvolumeclaim": "persistentvolumeclaims", "pv": "persistentvolumes", "node": "nodes",
    "networkpolicy": "networkpolicies", "storageclass": "storageclasses", "job": "jobs",
    "event": "events", "application": "applications.argoproj.io",
}


def _name_ok(*names: str) -> None:
    for n in names:
        if n and not NAME_RE.match(n):
            raise ValueError(f"invalid name {n!r}")


def build_tools(kubeconfig: Path, repo: GitRepo, prometheus_url: str, apps_path: str,
                argocd_ns: str, argocd_app: str, default_ns: str) -> ToolRegistry:

    async def k8s_get(kind: str, name: str = "", namespace: str = default_ns, label_selector: str = "") -> str:
        res = KINDS.get(kind.lower())
        if not res:
            return f"error: kind must be one of {sorted(set(KINDS))}"
        _name_ok(name, namespace)
        if label_selector and not all(c.isalnum() or c in "=-_.,!/" for c in label_selector):
            return "error: invalid label selector"
        args = ["get", res]
        if name:
            args += [name, "-o", "yaml"]
        else:
            args += ["-o", "wide"] + (["-l", label_selector] if label_selector else [])
        if res not in ("nodes", "persistentvolumes", "storageclasses"):
            args += ["-n", namespace]
        rc, out = await kubectl(kubeconfig, *args)
        return compact_yaml(out) if name and rc == 0 else out

    async def k8s_events(namespace: str = default_ns, object_name: str = "") -> str:
        _name_ok(namespace, object_name)
        args = ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"]
        if object_name:
            args += ["--field-selector", f"involvedObject.name={object_name}"]
        rc, out = await kubectl(kubeconfig, *args)
        lines = out.splitlines()
        return "\n".join(lines[:1] + lines[-40:])

    async def k8s_logs(pod: str, namespace: str = default_ns, container: str = "", previous: bool = False,
                       tail: int = 80) -> str:
        _name_ok(pod, namespace, container)
        args = ["logs", pod, "-n", namespace, f"--tail={min(int(tail), 200)}"]
        if container:
            args += ["-c", container]
        if previous:
            args.append("--previous")
        rc, out = await kubectl(kubeconfig, *args)
        return out or "(no log output)"

    async def prom_query(promql: str) -> str:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{prometheus_url}/api/v1/query", params={"query": promql})
        data = r.json()
        if data.get("status") != "success":
            return f"error: {data.get('error', r.text[:300])}"
        res = data["data"]["result"]
        if not res:
            return "(empty result)"
        lines = []
        for s in res[:30]:
            labels = ",".join(f"{k}={v}" for k, v in s["metric"].items() if k not in ("__name__", "endpoint", "service", "job", "instance", "uid", "prometheus"))
            val = s.get("value", [None, None])[1]
            lines.append(f"{{{labels}}} {val}")
        return "\n".join(lines)

    async def git_log(path: str = "", n: int = 10) -> str:
        await repo.sync()
        return await repo.log(path or apps_path, n) or "(no commits)"

    async def git_show(commit: str) -> str:
        return await repo.show(commit)

    async def read_manifest(path: str) -> str:
        if not path.startswith((apps_path + "/", "runbooks/")):
            return f"error: path must be under {apps_path}/ or runbooks/"
        return repo.read(path)

    async def argocd_status() -> str:
        rc, out = await kubectl(kubeconfig, "get", "applications.argoproj.io", argocd_app, "-n", argocd_ns, "-o", "json")
        if rc != 0:
            return out
        app = json.loads(out)
        st = app.get("status", {})
        hist = [f"  {h.get('deployedAt')} revision={str(h.get('revision', ''))[:8]}" for h in (st.get("history") or [])[-5:]]
        degraded = [f"  {r.get('kind')}/{r.get('name')}: {(r.get('health') or {}).get('status')} {(r.get('health') or {}).get('message', '')}"
                    for r in st.get("resources") or [] if (r.get("health") or {}).get("status") not in (None, "Healthy")]
        op = st.get("operationState") or {}
        return "\n".join([
            f"sync={st.get('sync', {}).get('status')} revision={str(st.get('sync', {}).get('revision', ''))[:8]}",
            f"health={st.get('health', {}).get('status')}",
            f"last operation: {op.get('phase')} {op.get('message', '')[:300]}",
            "recent deploys (oldest first):", *hist,
            "unhealthy resources:", *(degraded or ["  none"]),
        ])

    async def runbook_search(query: str) -> str:
        await repo.sync()
        words = [w.lower() for w in query.replace(",", " ").split() if len(w) > 2]
        scored = []
        for rel in repo.list_files("runbooks"):
            if not rel.endswith(".md"):
                continue
            text = repo.read(rel)
            score = sum(text.lower().count(w) for w in words)
            if score:
                scored.append((score, rel, text))
        scored.sort(reverse=True)
        if not scored:
            return "no matching runbooks"
        return "\n\n".join(f"### {rel} (score {s})\n{text[:1500]}" for s, rel, text in scored[:3])

    s = {"type": "string"}
    return ToolRegistry([
        Tool("k8s_get", "Get Kubernetes objects (read-only). With name: full YAML. Without name: a table. "
             "Kinds: pod, deployment, replicaset, service, endpointslice, configmap, pvc, pv, node, networkpolicy, "
             "storageclass, job, application.",
             obj({"kind": s, "name": s, "namespace": s, "label_selector": s}, ["kind"]), k8s_get),
        Tool("k8s_events", "Recent events in a namespace, optionally for one object name.",
             obj({"namespace": s, "object_name": s}), k8s_events),
        Tool("k8s_logs", "Container logs. Use previous=true for the crashed container's logs.",
             obj({"pod": s, "namespace": s, "container": s, "previous": {"type": "boolean"},
                  "tail": {"type": "integer"}}, ["pod"]), k8s_logs),
        Tool("prom_query", "Instant PromQL query, e.g. max_over_time(container_memory_working_set_bytes{namespace=\"demo\"}[1h]) "
             "or kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes.",
             obj({"promql": s}, ["promql"]), prom_query),
        Tool("git_log", "Recent commits in the gitops repo with changed files. Optional path filter, e.g. apps/api.",
             obj({"path": s, "n": {"type": "integer"}}), git_log),
        Tool("git_show", "Full diff of one commit (use a sha from git_log).", obj({"commit": s}, ["commit"]), git_show),
        Tool("read_manifest", f"Read a file from the gitops repo under {apps_path}/ or runbooks/.",
             obj({"path": s}, ["path"]), read_manifest),
        Tool("argocd_status", "ArgoCD sync/health status, last operation and recent deploy history.", obj({}), argocd_status),
        Tool("runbook_search", "Search team runbooks and past postmortems by keywords.",
             obj({"query": s}, ["query"]), runbook_search),
    ])
