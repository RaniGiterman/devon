"""Chaos harness: inject faults as ordinary-looking git commits to the gitops repo.

  devon-chaos list
  devon-chaos inject oom-limit
  devon-chaos barrage            # one fault per workload, at once
  devon-chaos reset              # restore apps/ to the `baseline` tag (new commit, history kept)
  devon-chaos status
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from agent.config import ROOT, get_settings
from agent.gitrepo import GitRepo

NETPOL = """apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: redis-ingress
spec:
  podSelector:
    matchLabels: { app: redis }
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels: { app: frontend }
      ports:
        - { port: 6379, protocol: TCP }
"""


@dataclass
class Fault:
    workload: str
    message: str           # looks like a normal change; the agent must not be told it's a fault
    edits: list[tuple[str, str, str]] = field(default_factory=list)   # (path, old, new)
    create: dict[str, str] = field(default_factory=dict)
    expected: str = ""      # expected diagnosis -> fix, for humans and scenario tests
    fix_class: str = ""
    needs: str = ""         # precondition note; fault refuses to run unless --force


FAULTS: dict[str, Fault] = {
    "oom-limit": Fault("api", "api: tighten memory to free node capacity",
                       [("apps/api/deployment.yaml",
                         "requests: { cpu: 50m, memory: 128Mi }\n            limits: { memory: 512Mi }",
                         "requests: { cpu: 50m, memory: 64Mi }\n            limits: { memory: 96Mi }")],
                       expected="limit below usage (~120Mi) -> restore 128Mi/512Mi", fix_class="resources"),
    "bad-image": Fault("api", "api: bump image to 1.0.10",
                       [("apps/api/deployment.yaml", "image: devon-demo:1.0.0", "image: devon-demo:1.0.10")],
                       expected="tag does not exist -> back to 1.0.0", fix_class="image"),
    "config-key": Fault("api", "api: rename redis setting for the new client library",
                        [("apps/api/config.env", "REDIS_URL=", "REDIS_URI=")],
                        expected="REDIS_URL missing -> restore key", fix_class="config"),
    "broken-probe": Fault("api", "api: point liveness at /health",
                          [("apps/api/deployment.yaml", "livenessProbe:\n            httpGet: { path: /healthz",
                            "livenessProbe:\n            httpGet: { path: /health")],
                          expected="liveness 404 -> /healthz", fix_class="probe"),
    "selector": Fault("api", "api: move service selector to api-v2 ahead of migration",
                      [("apps/api/service.yaml", "selector: { app: api }", "selector: { app: api-v2 }")],
                      expected="selector matches no pods -> app: api", fix_class="selector"),
    "netpol": Fault("api", "network: restrict redis ingress",
                    [("apps/network/kustomization.yaml", "resources: []", "resources: [redis-ingress.yaml]")],
                    create={"apps/network/redis-ingress.yaml": NETPOL},
                    expected="policy blocks api -> allow app: api", fix_class="networkpolicy"),
    "unschedulable": Fault("api", "api: raise cpu request for peak traffic",
                           [("apps/api/deployment.yaml", "requests: { cpu: 50m, memory: 128Mi }",
                             'requests: { cpu: "64", memory: 128Mi }')],
                           expected="64 CPUs requested -> restore 50m", fix_class="resources"),
    "app-bug": Fault("api", "release api 1.1.0",
                     [("apps/api/deployment.yaml", "image: devon-demo:1.0.0", "image: devon-demo:1.1.0")],
                     expected="code bug in 1.1.0 -> escalate, suggest rollback", fix_class="none"),
    "frontend-bad-image": Fault("frontend", "frontend: bump image to 1.0.1",
                                [("apps/frontend/deployment.yaml", "image: devon-demo:1.0.0", "image: devon-demo:1.0.1")],
                                expected="tag does not exist -> back to 1.0.0", fix_class="image"),
    "redis-bad-image": Fault("redis", "redis: patch release 7.2.00",
                             [("apps/redis/deployment.yaml", "image: redis:7.2.0", "image: redis:7.2.00")],
                             expected="tag does not exist -> back to 7.2.0", fix_class="image"),
    "pvc-fill": Fault("redis", "redis: enable debug snapshots", needs=(
        "needs a CSI driver with volume stats and a size limit (e.g. csi-driver-host-path). With kind's default "
        "local-path provisioner the volume has no quota, so this would fill the laptop's disk."),
        fix_class="pvc_size"),
}
BARRAGE = ["oom-limit", "frontend-bad-image", "redis-bad-image"]


def repo() -> GitRepo:
    s = get_settings()
    token = s.github_token or os.environ.get("GH_TOKEN", "")
    return GitRepo(s.gitops_url, ROOT / ".work/chaos-gitops", s.gitops_branch, token, author=("demo-dev", "demo-dev@localhost"))


def refresh_argocd() -> None:
    """Ask ArgoCD to re-read git now (admin kubeconfig; this is a demo tool, not the agent)."""
    subprocess.run(["kubectl", "--context", "kind-devon", "-n", get_settings().argocd_namespace, "annotate",
                    "applications.argoproj.io", get_settings().argocd_app, "argocd.argoproj.io/refresh=normal",
                    "--overwrite"], capture_output=True)


async def apply(names: list[str], force: bool) -> None:
    r = repo()
    await r.sync()
    for name in names:
        f = FAULTS[name]
        if f.needs and not force:
            sys.exit(f"refusing to inject {name}: {f.needs} (use --force if your storage is set up)")
        files = {}
        for path, old, new in f.edits:
            text = files.get(path) or r.read(path)
            if old not in text:
                sys.exit(f"{name}: expected text not found in {path}; is a fault already active? Run `devon-chaos reset`.")
            files[path] = text.replace(old, new, 1)
        files.update(f.create)
        for path, content in files.items():
            (r.dir / path).parent.mkdir(parents=True, exist_ok=True)
            (r.dir / path).write_text(content)
        await r.git("add", "-A")
        await r.git("commit", "-q", "-m", f.message)
        print(f"injected {name:20s} -> {f.workload}: {f.expected}")
    await r.git("push", "-q", "origin", f"HEAD:{r.branch}")
    refresh_argocd()


async def reset() -> None:
    r = repo()
    await r.sync()
    await r.git("fetch", "-q", "origin", "tag", "baseline")
    shutil.rmtree(r.dir / "apps")
    await r.git("checkout", "baseline", "--", "apps")
    await r.git("add", "-A")
    if not (await r.git("status", "--porcelain")).strip():
        print("apps/ already matches baseline")
        return
    await r.git("commit", "-q", "-m", "reset demo manifests to baseline")
    await r.git("push", "-q", "origin", f"HEAD:{r.branch}")
    refresh_argocd()
    print("reset apps/ to baseline")


async def status() -> None:
    r = repo()
    await r.sync()
    print(await r.git("log", "--oneline", "-8"))
    out = subprocess.run(["kubectl", "--context", "kind-devon", "-n", "argocd", "get", "applications.argoproj.io", "demo",
                          "-o", "jsonpath={.status.sync.status} {.status.health.status} {.status.sync.revision}"],
                         capture_output=True, text=True).stdout
    print("argocd:", out)


def main() -> None:
    p = argparse.ArgumentParser(prog="devon-chaos", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    inj = sub.add_parser("inject")
    inj.add_argument("faults", nargs="+", choices=sorted(FAULTS))
    inj.add_argument("--force", action="store_true")
    sub.add_parser("barrage")
    sub.add_parser("reset")
    sub.add_parser("status")
    a = p.parse_args()
    if a.cmd == "list":
        for n, f in FAULTS.items():
            print(f"{n:20s} {f.workload:9s} {f.fix_class:14s} {f.expected or f.needs}")
    elif a.cmd == "inject":
        asyncio.run(apply(a.faults, a.force))
    elif a.cmd == "barrage":
        asyncio.run(apply(BARRAGE, False))
    elif a.cmd == "reset":
        asyncio.run(reset())
    elif a.cmd == "status":
        asyncio.run(status())


if __name__ == "__main__":
    main()
