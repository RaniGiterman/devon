from agent.signals import Signal
from agent.verify import relevant_problems


def ev(pod, ts, summary="api: Unhealthy: Readiness probe failed"):
    return Signal("sweep", "demo", "api", "ProbeFailing", summary, ts=ts, obj=f"Pod/{pod}")


def test_terminating_old_pod_does_not_fail_verification():
    """Regression: an old pod failing readiness while it shut down made the verifier revert a correct fix."""
    live = {"api-6bc4657858-5pqkm"}
    assert relevant_problems([ev("api-689b55585c-jpdwv", ts=200)], live, settled=100) == []


def test_live_pod_problem_counts():
    live = {"api-6bc4657858-5pqkm"}
    assert relevant_problems([ev("api-6bc4657858-5pqkm", ts=200)], live, settled=100) == ["api: Unhealthy: Readiness probe failed"]


def test_events_before_settle_are_ignored_and_state_signals_count():
    live = {"api-6bc4657858-5pqkm"}
    state = Signal("sweep", "demo", "api", "CrashLoopBackOff", "api: CrashLoopBackOff", ts=200)
    assert relevant_problems([ev("api-6bc4657858-5pqkm", ts=50), state], live, settled=100) == ["api: CrashLoopBackOff"]
