"""The deterministic detectors. None of them calls an LLM.

watch    Kubernetes API watch streams (pods, events, deployments, endpointslices, pvcs, nodes, ArgoCD apps)
sweep    full scan every few minutes (catches missed events, pre-existing faults, drift); also used to re-check
logs     polls recent container logs and counts error lines
probe    synthetic HTTP checks through the API server's service proxy
(alerts arrive via the Alertmanager webhook in server.py)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from kubernetes import watch
from kubernetes.client.exceptions import ApiException

from agent import detect
from agent.kube import Kube
from agent.signals import Signal

log = logging.getLogger("devon.detect")
Emit = Callable[[Signal], None]


@dataclass
class DetectorHealth:
    last_run: dict[str, float] = field(default_factory=dict)
    last_watchdog: float = 0.0

    def beat(self, name: str) -> None:
        self.last_run[name] = time.time()


# --- sweep / recheck -------------------------------------------------------

def scan(kube: Kube, namespaces: list[str], argocd_ns: str, argocd_app: str, workload: str | None = None) -> list[Signal]:
    """Current-state problems. Synchronous; run in a thread."""
    now = time.time()
    out: list[Signal] = []
    for ns in namespaces:
        for p in kube.pods(ns):
            out += detect.pod_signals(p, now)
        for d in kube.deployments(ns):
            out += detect.deployment_signals(d)
        for es in kube.endpointslices(ns):
            s = detect.endpointslice_signal(es)
            if s:
                out.append(s)
        for pvc in kube.pvcs(ns):
            s = detect.pvc_signal(pvc, now)
            if s:
                out.append(s)
        for ev in kube.events(ns):
            s = detect.event_signal(ev, now)
            if s:
                out.append(s)
    for n in kube.nodes():
        out += detect.node_signals(n)
    try:
        s = detect.argo_app_signal(kube.argo_app(argocd_ns, argocd_app), namespaces)
        if s:
            out.append(s)
    except ApiException:
        pass
    for s in out:
        s.source = "sweep"
    if workload:
        out = [s for s in out if s.workload == workload]
    return out


async def run_sweep(kube: Kube, namespaces: list[str], argocd_ns: str, argocd_app: str, interval: int,
                    emit: Emit, health: DetectorHealth) -> None:
    while True:
        try:
            for s in await asyncio.to_thread(scan, kube, namespaces, argocd_ns, argocd_app):
                emit(s)
            health.beat("sweep")
        except Exception as e:
            log.warning("sweep failed: %s", e)
        await asyncio.sleep(interval)


# --- watch -----------------------------------------------------------------

class Watcher:
    def __init__(self, kube: Kube, namespaces: list[str], argocd_ns: str, emit: Emit, health: DetectorHealth,
                 loop: asyncio.AbstractEventLoop):
        self.kube, self.namespaces, self.argocd_ns = kube, namespaces, argocd_ns
        self.emit_async, self.health, self.loop = emit, health, loop
        self.stop = threading.Event()

    def _emit(self, sigs) -> None:
        for s in sigs:
            if s:
                self.loop.call_soon_threadsafe(self.emit_async, s)

    def _stream(self, name: str, fn, args: tuple, handle) -> None:
        while not self.stop.is_set():
            w = watch.Watch()
            try:
                for ev in w.stream(fn, *args, timeout_seconds=300):
                    if self.stop.is_set():
                        break
                    obj = ev["object"]
                    obj = obj if isinstance(obj, dict) else self.kube.plain(obj)
                    if ev["type"] in ("ADDED", "MODIFIED"):
                        self._emit(handle(obj))
                    self.health.beat("watch")
            except ApiException as e:
                if e.status != 410:
                    log.warning("watch %s: %s", name, e.reason)
                    time.sleep(3)
            except Exception as e:
                log.warning("watch %s error: %s", name, e)
                time.sleep(3)
            finally:
                w.stop()

    def start(self) -> None:
        k = self.kube
        streams = []
        for ns in self.namespaces:
            streams += [
                (f"pods/{ns}", k.core.list_namespaced_pod, (ns,), lambda o: detect.pod_signals(o)),
                (f"events/{ns}", k.core.list_namespaced_event, (ns,), lambda o: [detect.event_signal(o)]),
                (f"deploy/{ns}", k.apps.list_namespaced_deployment, (ns,), detect.deployment_signals),
                (f"eps/{ns}", k.discovery.list_namespaced_endpoint_slice, (ns,), lambda o: [detect.endpointslice_signal(o)]),
                (f"pvc/{ns}", k.core.list_namespaced_persistent_volume_claim, (ns,), lambda o: [detect.pvc_signal(o)]),
            ]
        streams.append(("nodes", k.core.list_node, (), detect.node_signals))
        streams.append(("argocd", k.custom.list_namespaced_custom_object,
                        ("argoproj.io", "v1alpha1", self.argocd_ns, "applications"),
                        lambda o: [detect.argo_app_signal(o, self.namespaces)]))
        for name, fn, args, handle in streams:
            threading.Thread(target=self._stream, args=(name, fn, args, handle), daemon=True, name=f"watch-{name}").start()
        log.info("watching %d streams", len(streams))


# --- logs ------------------------------------------------------------------

async def run_log_tail(kube: Kube, namespaces: list[str], interval: int, threshold: int, emit: Emit,
                       health: DetectorHealth, window: int = 60) -> None:
    def poll() -> list[Signal]:
        out = []
        for ns in namespaces:
            per_wl: dict[str, list[str]] = {}
            for p in kube.pods(ns):
                if (p.get("status") or {}).get("phase") != "Running":
                    continue
                wl = detect.workload_of_pod(p)
                for c in p["spec"]["containers"]:
                    try:
                        text = kube.core.read_namespaced_pod_log(p["metadata"]["name"], ns, container=c["name"],
                                                                 since_seconds=window, _request_timeout=5)
                    except Exception:
                        continue
                    per_wl.setdefault(wl, []).extend(detect.log_error_lines(text or ""))
            for wl, lines in per_wl.items():
                if len(lines) >= threshold:
                    out.append(Signal("logs", ns, wl, "LogErrors",
                                      f"{wl}: {len(lines)} error log lines in the last {window}s", "warning",
                                      [ln[:300] for ln in lines[-5:]], stateful=False))
        return out

    while True:
        try:
            for s in await asyncio.to_thread(poll):
                emit(s)
            health.beat("logs")
        except Exception as e:
            log.warning("log tail failed: %s", e)
        await asyncio.sleep(interval)


# --- synthetic prober ------------------------------------------------------

@dataclass
class ProbeTarget:
    namespace: str
    service: str   # "name:port"
    path: str
    workload: str

    @classmethod
    def parse(cls, spec: str) -> "ProbeTarget":
        """'demo/api:80/deps=api'"""
        left, wl = spec.split("=", 1)
        ns, rest = left.split("/", 1)
        svc, _, path = rest.partition("/")
        return cls(ns, svc, "/" + path, wl)


async def run_prober(kube: Kube, targets: list[ProbeTarget], interval: int, failures_needed: int, emit: Emit,
                     health: DetectorHealth) -> None:
    fails: dict[str, int] = {}

    def check(t: ProbeTarget) -> tuple[bool, str]:
        try:
            code, body = kube.service_get(t.namespace, t.service, t.path, timeout=5)
            return 200 <= code < 400, f"GET {t.service}{t.path} -> {code} {body.strip()[:160]}"
        except ApiException as e:
            body = (e.body or b"")
            body = body.decode(errors="replace") if isinstance(body, bytes) else str(body)
            return False, f"GET {t.service}{t.path} -> {e.status} {body.strip()[:160]}"
        except Exception as e:
            return False, f"GET {t.service}{t.path} -> {type(e).__name__}: {str(e)[:160]}"

    while True:
        for t in targets:
            ok, detail = await asyncio.to_thread(check, t)
            key = f"{t.namespace}/{t.service}{t.path}"
            fails[key] = 0 if ok else fails.get(key, 0) + 1
            if fails[key] >= failures_needed:
                emit(Signal("probe", t.namespace, t.workload, "ProbeFailed",
                            f"{t.workload}: synthetic check {t.service}{t.path} failing ({fails[key]}x)", "critical",
                            [detail], stateful=False))
        health.beat("probe")
        await asyncio.sleep(interval)
