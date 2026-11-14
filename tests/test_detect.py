import time
from datetime import datetime, timezone

from agent import detect


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pod(waiting=None, last_term=None, phase="Running", created_ago=300, labels=None, ready=True):
    now = time.time()
    cs = {"name": "api", "restartCount": 3, "ready": ready, "state": {"waiting": waiting} if waiting else {"running": {}}}
    if last_term:
        cs["lastState"] = {"terminated": last_term}
    return {"metadata": {"name": "api-689b55585c-abcde", "namespace": "demo", "labels": labels or {"app": "api"},
                         "creationTimestamp": iso(now - created_ago)},
            "status": {"phase": phase, "containerStatuses": [cs], "conditions": []}}


def test_crashloop_with_oom_is_reported_as_oom():
    sigs = detect.pod_signals(pod({"reason": "CrashLoopBackOff", "message": "back-off"},
                                  {"reason": "OOMKilled", "exitCode": 137, "finishedAt": iso(time.time())}))
    assert [s.symptom for s in sigs] == ["OOMKilled"]
    assert sigs[0].workload == "api" and sigs[0].severity == "critical"


def test_image_pull_backoff():
    sigs = detect.pod_signals(pod({"reason": "ImagePullBackOff", "message": "not found"}))
    assert sigs[0].symptom == "ImagePullBackOff"


def test_healthy_pod_has_no_signals():
    assert detect.pod_signals(pod()) == []


def test_pending_needs_grace_period():
    young = pod(phase="Pending", created_ago=10)
    old = pod(phase="Pending", created_ago=120)
    for p in (young, old):
        p["status"]["containerStatuses"] = []
    assert detect.pod_signals(young) == []
    assert detect.pod_signals(old)[0].symptom == "Pending"


def test_workload_from_replicaset_owner_when_no_app_label():
    p = pod(labels={"x": "y"})
    p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "checkout-7d8b848c6"}]
    assert detect.workload_of_pod(p) == "checkout"


def test_event_filtering_and_freshness():
    now = time.time()
    ev = {"type": "Warning", "reason": "FailedScheduling", "message": "0/1 nodes: Insufficient cpu",
          "lastTimestamp": iso(now - 5), "involvedObject": {"kind": "Pod", "name": "api-689b55585c-abcde", "namespace": "demo"}}
    s = detect.event_signal(ev, now)
    assert s and s.workload == "api" and s.symptom == "FailedScheduling"
    assert abs(s.ts - (now - 5)) < 2  # event time, not detection time
    assert detect.event_signal({**ev, "lastTimestamp": iso(now - 3600)}, now) is None
    assert detect.event_signal({**ev, "type": "Normal"}, now) is None


def test_endpointslice_without_ready_endpoints():
    es = {"metadata": {"name": "api-x", "namespace": "demo", "labels": {"kubernetes.io/service-name": "api"}}, "endpoints": []}
    s = detect.endpointslice_signal(es)
    assert s.workload == "api" and "selector matches no pods" in s.evidence[0]
    es["endpoints"] = [{"conditions": {"ready": True}}]
    assert detect.endpointslice_signal(es) is None


def test_argocd_sync_failed_is_attributed_to_the_rejected_workload():
    app = {"metadata": {"name": "demo"}, "spec": {"destination": {"namespace": "demo"}},
           "status": {"operationState": {"phase": "Running", "message": "one or more objects failed to apply",
                                         "syncResult": {"revision": "2f25d2b5", "resources": [
                                             {"kind": "Deployment", "name": "api", "namespace": "demo", "status": "SyncFailed",
                                              "message": "requests must be <= limit"}]}}}}
    s = detect.argo_app_signal(app, ["demo"])
    assert s.workload == "api" and s.symptom == "SyncFailed"


def test_alert_signals_and_watchdog():
    payload = {"alerts": [
        {"status": "firing", "labels": {"alertname": "Watchdog"}},
        {"status": "firing", "labels": {"alertname": "DemoHighErrorRate", "namespace": "demo", "svc": "api", "severity": "critical"},
         "annotations": {"summary": "api 5xx ratio is 100%"}, "startsAt": "2026-11-14T10:00:00Z"},
        {"status": "firing", "labels": {"alertname": "KubePersistentVolumeFillingUp", "namespace": "demo",
                                        "persistentvolumeclaim": "redis-data", "severity": "warning"}, "annotations": {}},
        {"status": "resolved", "labels": {"alertname": "DemoHighErrorRate", "namespace": "demo", "svc": "api"}},
    ]}
    sigs, watchdog = detect.alert_signals(payload)
    assert watchdog
    assert [(s.workload, s.symptom) for s in sigs] == [("api", "HighErrorRate"), ("pvc/redis-data", "PersistentVolumeFillingUp")]
    assert not sigs[0].stateful


def test_log_error_lines():
    text = '{"level": "info", "msg": "ok"}\n{"level": "error", "msg": "redis connection failed"}\nTraceback (most recent call last):'
    assert len(detect.log_error_lines(text)) == 2
