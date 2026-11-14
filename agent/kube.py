"""Kubernetes access for the read-only identity, plus a small kubectl runner."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import yaml
from kubernetes import client, config

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?$")


class Kube:
    def __init__(self, kubeconfig: Path):
        self.kubeconfig = Path(kubeconfig)
        self.api_client = config.new_client_from_config(config_file=str(self.kubeconfig))
        self.core = client.CoreV1Api(self.api_client)
        self.apps = client.AppsV1Api(self.api_client)
        self.discovery = client.DiscoveryV1Api(self.api_client)
        self.custom = client.CustomObjectsApi(self.api_client)

    def plain(self, obj) -> dict:
        """API model -> plain dict with camelCase keys, like `kubectl -o json`."""
        return self.api_client.sanitize_for_serialization(obj)

    def plain_list(self, lst) -> list[dict]:
        return [self.plain(i) for i in lst.items]

    # snapshot helpers (sync; call via asyncio.to_thread)
    def pods(self, ns: str, selector: str | None = None) -> list[dict]:
        return self.plain_list(self.core.list_namespaced_pod(ns, label_selector=selector or ""))

    def deployments(self, ns: str) -> list[dict]:
        return self.plain_list(self.apps.list_namespaced_deployment(ns))

    def endpointslices(self, ns: str) -> list[dict]:
        return self.plain_list(self.discovery.list_namespaced_endpoint_slice(ns))

    def events(self, ns: str) -> list[dict]:
        return self.plain_list(self.core.list_namespaced_event(ns))

    def nodes(self) -> list[dict]:
        return self.plain_list(self.core.list_node())

    def pvcs(self, ns: str) -> list[dict]:
        return self.plain_list(self.core.list_namespaced_persistent_volume_claim(ns))

    def argo_app(self, ns: str, name: str) -> dict:
        return self.custom.get_namespaced_custom_object("argoproj.io", "v1alpha1", ns, "applications", name)

    def service_get(self, ns: str, svc: str, path: str, timeout: float = 5.0) -> tuple[int, str]:
        """GET through the API server's service proxy, so the prober needs no port-forward."""
        # Non-2xx responses raise ApiException (handled by the prober).
        r = self.core.connect_get_namespaced_service_proxy_with_path(
            svc, ns, path.lstrip("/") or "/", _preload_content=False, _request_timeout=timeout)
        return r.status, r.data.decode(errors="replace")[:500]


async def kubectl(kubeconfig: Path, *args: str, stdin: str | None = None, timeout: float = 30) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "kubectl", "--kubeconfig", str(kubeconfig), *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin else None), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"kubectl timed out after {timeout}s"
    return proc.returncode, out.decode(errors="replace")


def strip_noise(doc: dict) -> dict:
    """Remove fields that waste LLM tokens."""
    meta = doc.get("metadata") or {}
    meta.pop("managedFields", None)
    meta.pop("uid", None)
    meta.pop("resourceVersion", None)
    ann = meta.get("annotations") or {}
    for k in list(ann):
        if k.startswith("kubectl.kubernetes.io/last-applied") or k.startswith("argocd.argoproj.io/tracking"):
            ann.pop(k)
    if not ann:
        meta.pop("annotations", None)
    return doc


def compact_yaml(text: str) -> str:
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return text
    out = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        if d.get("kind", "").endswith("List") and "items" in d:
            d["items"] = [strip_noise(i) for i in d["items"]]
        else:
            strip_noise(d)
        out.append(yaml.safe_dump(d, sort_keys=False, width=120))
    return "---\n".join(out)
