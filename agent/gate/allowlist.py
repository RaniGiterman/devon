"""Deterministic fix allowlist: compare old vs new manifests field by field and reject
any change outside the allowed fix classes. The LLM proposes; this decides."""

from __future__ import annotations

import re

import yaml

ALLOWED_PATHS = {
    ("Deployment", "StatefulSet"): [
        r"spec\.replicas",
        r"spec\.template\.spec\.containers\[\*\]\.resources(\..*)?",
        r"spec\.template\.spec\.containers\[\*\]\.image",
        r"spec\.template\.spec\.containers\[\*\]\.(env|envFrom)(\[\*\])?(\..*)?",
        r"spec\.template\.spec\.containers\[\*\]\.(livenessProbe|readinessProbe|startupProbe)(\..*)?",
    ],
    ("Service",): [r"spec\.selector(\..*)?", r"spec\.ports\[\*\]\.targetPort"],
    ("ConfigMap",): [r"data(\..*)?"],
    ("NetworkPolicy",): [r"spec(\..*)?", r"metadata\.labels(\..*)?"],
    ("PersistentVolumeClaim",): [r"spec\.resources\.requests\.storage"],
    ("Kustomization",): [r"resources(\[\*\])?"],
}
CREATABLE_KINDS = {"NetworkPolicy"}
DELETABLE_KINDS = {"NetworkPolicy"}

_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}


def parse_quantity(q) -> float:
    m = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", str(q).strip())
    if not m:
        raise ValueError(f"bad quantity {q!r}")
    num, unit = float(m.group(1)), m.group(2)
    if unit == "m":
        return num / 1000
    return num * _UNITS.get(unit, 1)


def _docs(text: str) -> dict[tuple[str, str], dict]:
    out = {}
    for d in yaml.safe_load_all(text):
        if isinstance(d, dict):
            kind = d.get("kind") or ("Kustomization" if "resources" in d or "configMapGenerator" in d else "?")
            name = (d.get("metadata") or {}).get("name", "")
            out[(kind, name)] = d
    return out


def diff_paths(a, b, prefix: str = "") -> list[str]:
    """Leaf paths that differ, with list indexes normalised to [*]."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in set(a) | set(b):
            p = f"{prefix}.{k}" if prefix else str(k)
            if k not in a or k not in b:
                out.append(p)
            else:
                out += diff_paths(a[k], b[k], p)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{prefix}[*]"]
        out = []
        for x, y in zip(a, b):
            out += diff_paths(x, y, f"{prefix}[*]")
        return out
    return [] if a == b else [prefix]


def _allowed(kind: str, path: str) -> bool:
    for kinds, patterns in ALLOWED_PATHS.items():
        if kind in kinds and any(re.fullmatch(p, path) for p in patterns):
            return True
    return False


def check_fix(old_files: dict[str, str | None], new_files: dict[str, str], apps_path: str = "apps") -> list[str]:
    """Return a list of violations (empty = allowed)."""
    problems: list[str] = []
    for path, new in new_files.items():
        old = old_files.get(path)
        if not path.startswith(apps_path + "/"):
            problems.append(f"{path}: only files under {apps_path}/ may change")
            continue
        if path.endswith(".env"):
            if old is None:
                problems.append(f"{path}: new .env files are not allowed")
            continue  # config values: allowed (fix_class config)
        if not path.endswith((".yaml", ".yml")):
            problems.append(f"{path}: unsupported file type")
            continue
        try:
            new_docs = _docs(new)
            old_docs = _docs(old) if old is not None else {}
        except yaml.YAMLError as e:
            problems.append(f"{path}: invalid YAML: {e}")
            continue
        if path.endswith("kustomization.yaml") and not path.startswith(f"{apps_path}/network/"):
            problems.append(f"{path}: kustomization changes are only allowed in {apps_path}/network/")
            continue
        for key, doc in new_docs.items():
            kind = key[0]
            if key not in old_docs:
                if kind not in CREATABLE_KINDS:
                    problems.append(f"{path}: creating {kind}/{key[1]} is not allowed")
                continue
            for p in diff_paths(old_docs[key], doc):
                if not _allowed(kind, p):
                    problems.append(f"{path}: change to {kind}/{key[1]} field '{p}' is outside the fix allowlist")
            if kind == "PersistentVolumeClaim":
                try:
                    o = parse_quantity(old_docs[key]["spec"]["resources"]["requests"]["storage"])
                    n = parse_quantity(doc["spec"]["resources"]["requests"]["storage"])
                    if n > 2 * o:
                        problems.append(f"{path}: PVC growth limited to 2x ({o / 2**30:.1f}Gi -> {n / 2**30:.1f}Gi)")
                    if n < o:
                        problems.append(f"{path}: PVCs cannot shrink")
                except (KeyError, TypeError, ValueError) as e:
                    problems.append(f"{path}: could not read PVC size: {e}")
        for key in old_docs:
            if key not in new_docs and key[0] not in DELETABLE_KINDS:
                problems.append(f"{path}: deleting {key[0]}/{key[1]} is not allowed")
    return problems
