"""Validation gate: every proposed fix must pass these checks before a human sees it.

1. allowlist      field-level diff against the fix allowlist
2. render         kustomize build of the whole app
3. schema         kubeconform (if installed in .work/bin)
4. dry-run        server-side apply --dry-run=server with the gate identity
5. policy         kyverno CLI (if installed) else built-in checks
6. blast radius   which objects change and which pods will restart
"""

from __future__ import annotations

import asyncio
import difflib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agent.gate.allowlist import check_fix, parse_quantity
from agent.kube import kubectl


@dataclass
class GateResult:
    passed: bool = True
    checks: list[dict] = field(default_factory=list)
    blast_radius: list[str] = field(default_factory=list)
    diff: str = ""

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail[:1500]})
        self.passed = self.passed and ok

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks, "blast_radius": self.blast_radius, "diff": self.diff}


async def _run(*cmd: str, stdin: str | None = None, timeout: float = 60) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE if stdin else None,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin else None), timeout)
    return proc.returncode, out.decode(errors="replace")


def unified_diff(old_files: dict[str, str | None], new_files: dict[str, str]) -> str:
    chunks = []
    for path, new in new_files.items():
        old = old_files.get(path) or ""
        chunks += difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                       f"a/{path}", f"b/{path}", n=2)
    return "".join(chunks)


def _index(rendered: str) -> dict[tuple[str, str], dict]:
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(rendered) if isinstance(d, dict)}


def builtin_policy(rendered: str) -> list[str]:
    problems = []
    for d in yaml.safe_load_all(rendered):
        if not isinstance(d, dict) or d.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet"):
            continue
        for c in d["spec"]["template"]["spec"].get("containers", []):
            where = f"{d['kind']}/{d['metadata']['name']} container {c['name']}"
            img = c.get("image", "")
            if img.endswith(":latest") or ":" not in img.split("/")[-1]:
                problems.append(f"{where}: image must be pinned to a tag other than latest")
            if (c.get("securityContext") or {}).get("privileged"):
                problems.append(f"{where}: privileged containers are not allowed")
            res = c.get("resources") or {}
            lim, req = (res.get("limits") or {}).get("memory"), (res.get("requests") or {}).get("memory")
            if not lim:
                problems.append(f"{where}: memory limit required")
            elif req and parse_quantity(lim) < parse_quantity(req):
                problems.append(f"{where}: memory limit {lim} below request {req}")
    return problems


class Gate:
    def __init__(self, gate_kubeconfig: Path, namespace: str, policies_dir: Path, bin_dir: Path, apps_path: str = "apps"):
        self.kubeconfig, self.ns, self.policies, self.bin = gate_kubeconfig, namespace, policies_dir, bin_dir
        self.apps_path = apps_path

    async def run(self, repo_dir: Path, old_files: dict[str, str | None], new_files: dict[str, str]) -> GateResult:
        r = GateResult(diff=unified_diff(old_files, new_files))
        if not new_files:
            r.add("allowlist", False, "fix changes no files")
            return r
        problems = check_fix(old_files, new_files, self.apps_path)
        r.add("allowlist", not problems, "\n".join(problems) or "all changed fields are in allowed fix classes")
        if problems:
            return r

        with tempfile.TemporaryDirectory(prefix="devon-gate-") as tmp:
            work = Path(tmp) / "repo"
            shutil.copytree(repo_dir, work, ignore=shutil.ignore_patterns(".git"))
            for rel, content in new_files.items():
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                (work / rel).write_text(content)

            rc_old, old_render = await _run("kubectl", "kustomize", str(repo_dir / self.apps_path))
            rc, new_render = await _run("kubectl", "kustomize", str(work / self.apps_path))
            r.add("render", rc == 0, "kustomize build ok" if rc == 0 else new_render)
            if rc != 0:
                return r

            kc = self.bin / "kubeconform"
            if kc.exists():
                rc, out = await _run(str(kc), "-strict", "-ignore-missing-schemas", "-summary", "-", stdin=new_render)
                r.add("schema", rc == 0, out.strip())
            else:
                r.add("schema", True, "kubeconform not installed (make tools); skipped")

            namespaced = "---\n".join(yaml.safe_dump(d) for d in yaml.safe_load_all(new_render)
                                      if isinstance(d, dict) and d.get("kind") != "Namespace")
            rc, out = await kubectl(self.kubeconfig, "apply", "--server-side", "--dry-run=server", "--force-conflicts",
                                    "--field-manager=devon-gate", "-n", self.ns, "-f", "-", stdin=namespaced, timeout=60)
            r.add("dry-run", rc == 0, out.strip()[-1500:])

            kyv = self.bin / "kyverno"
            if kyv.exists() and self.policies.exists():
                rendered = Path(tmp) / "rendered.yaml"
                rendered.write_text(new_render)
                rc, out = await _run(str(kyv), "apply", str(self.policies), "--resource", str(rendered))
                r.add("policy", rc == 0 and "fail: 0" in out, out.strip()[-1500:])
            else:
                problems = builtin_policy(new_render)
                r.add("policy", not problems, "\n".join(problems) or "built-in policies pass (kyverno CLI not installed)")

            if rc_old == 0:
                r.blast_radius = blast_radius(old_render, new_render)
        return r


def blast_radius(old_render: str, new_render: str) -> list[str]:
    old, new = _index(old_render), _index(new_render)
    out = []
    for key, doc in new.items():
        if key not in old:
            # ConfigMap renamed by the generator hash is reported via the deployment restart.
            if not (key[0] == "ConfigMap" and any(k[0] == "ConfigMap" and k not in new for k in old)):
                out.append(f"create {key[0]}/{key[1]}")
        elif doc != old[key]:
            if key[0] in ("Deployment", "StatefulSet") and doc["spec"].get("template") != old[key]["spec"].get("template"):
                n = doc["spec"].get("replicas", 1)
                out.append(f"rolling restart of {key[0]}/{key[1]} ({n} pod{'s' if n != 1 else ''})")
            else:
                out.append(f"update {key[0]}/{key[1]}")
    for key in old:
        if key not in new and not (key[0] == "ConfigMap" and any(k[0] == "ConfigMap" and k not in old for k in new)):
            out.append(f"delete {key[0]}/{key[1]}")
    return out or ["no live objects change"]
