import time
from datetime import datetime, timezone

from agent import evidence
from agent.tools.registry import Tool, ToolRegistry, obj


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class Repo:
    files = {"apps/api/deployment.yaml": "kind: Deployment\nmetadata: {name: api}\n",
             "apps/api/config.env": "REDIS_URL=redis://redis:6379\n",
             "apps/network/kustomization.yaml": "resources: []\n",
             "apps/frontend/deployment.yaml": "env: [{name: API_URL, value: http://api}]\n",
             "apps/redis/deployment.yaml": "kind: Deployment\nmetadata: {name: redis}\n"}
    def list_files(self, sub): return sorted(self.files)
    def read(self, p): return self.files[p]


class Kube:
    def __init__(self, restarted=True):
        self.restarted = restarted
    def pods(self, ns):
        cs = {"name": "api", "restartCount": 4 if self.restarted else 0,
              "lastState": {"terminated": {"reason": "OOMKilled"}} if self.restarted else {}}
        return [{"metadata": {"name": "api-7b5797cc4-zjvmj", "labels": {"app": "api"}}, "status": {"containerStatuses": [cs]}}]


def tools(calls, now):
    log = (f"a319a04 {iso(now - 300)} demo-dev\n    api: tighten memory\n apps/api/deployment.yaml | 4 ++--\n"
           f"0c9bb0c {iso(now - 86400)} devon-bootstrap\n    seed demo manifests\n")

    def t(name, result):
        async def fn(**kw):
            calls.append((name, kw))
            return result(kw) if callable(result) else result
        return Tool(name, name, obj({k: {"type": "string"} for k in
                                     ("kind", "namespace", "label_selector", "object_name", "pod", "previous", "tail",
                                      "path", "n", "commit", "promql", "query")}), fn)
    return ToolRegistry([
        t("k8s_get", "NAME READY STATUS\napi-7b5797cc4-zjvmj 0/1 CrashLoopBackOff"),
        t("k8s_events", "Warning BackOff pod/api-7b5797cc4-zjvmj"),
        t("k8s_logs", lambda kw: "x" * 5000),
        t("git_log", log),
        t("git_show", lambda kw: f"diff of {kw['commit']}"),
        t("argocd_status", "sync=Synced health=Degraded"),
        t("prom_query", "{container=api} 125829120"),
        t("runbook_search", "### runbooks/oomkilled.md"),
    ])


async def test_pack_contains_the_standard_evidence_in_order():
    now, calls = time.time(), []
    inc = {"id": 3, "namespace": "demo", "workload": "api", "created": now}
    digest = "- [watch] OOMKilled: api: container api OOMKilled | restartCount=4"
    pack, files = await evidence.collect(inc, tools(calls, now), Kube(), Repo(), "apps", digest,
                                         [{"id": 2, "workload": "frontend", "state": "INVESTIGATING", "title": "502s"}])
    order = ["Detector signals", "Pods", "Recent events", "Logs of", "Recent commits", "Diff of commit a319a04",
             "ArgoCD", "Peak memory", "Most relevant runbook", "Other open incidents", "Current manifest files"]
    positions = [pack.index(f"## {h}") for h in order]
    assert positions == sorted(positions)
    assert "diff of 0c9bb0c" not in pack            # only commits from shortly before the incident
    assert "#2 frontend" in pack
    assert set(files) == {"apps/api/deployment.yaml", "apps/api/config.env", "apps/network/kustomization.yaml"}
    assert len(pack) < 16000                          # budgeted (5000-char logs were capped)
    assert ("k8s_logs", {"pod": "api-7b5797cc4-zjvmj", "namespace": "demo", "previous": True, "tail": 30}) in calls


async def test_metrics_only_when_a_symptom_needs_them():
    now, calls = time.time(), []
    inc = {"id": 4, "namespace": "demo", "workload": "api", "created": now}
    pack, _ = await evidence.collect(inc, tools(calls, now), Kube(restarted=False), Repo(), "apps",
                                     "- [probe] ProbeFailed: api: synthetic check failing", [])
    assert not any(name == "prom_query" for name, _ in calls)
    assert ("k8s_logs", {"pod": "api-7b5797cc4-zjvmj", "namespace": "demo", "previous": False, "tail": 30}) in calls


def test_suspect_commits_window():
    now = time.time()
    log = f"aaa1111 {iso(now - 600)} x\n    recent\nbbb2222 {iso(now - 10 * 3600)} y\n    old\n"
    assert evidence.suspect_commits(log, now) == ["aaa1111"]
