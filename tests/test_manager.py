"""State machine with every external dependency faked."""

import asyncio
from pathlib import Path

import pytest

from agent.config import Settings
from agent.gate.validate import GateResult
from agent.manager import IncidentManager
from agent.signals import Signal
from agent.store import Store

DIAG = {"root_cause": "limit too low", "evidence": ["OOMKilled"], "suspect_commit": "abc1234", "suspect_change": "x",
        "confidence": 0.9, "fix_class": "resources", "fix_hint": "restore", "escalate_reason": None}


FIX = {"title": "api: restore limit", "summary": "s", "risk": "low",
       "files": [{"path": "apps/api/deployment.yaml", "content": "new\n"}]}


class FakeLLM:
    """One combined call (run_tools) returns triage + diagnosis + fix; json() is only the FIXER retry."""

    def __init__(self, diag=DIAG, triage=None):
        self.diag, self.triage = diag, triage or {"actionable": True, "severity": "critical", "duplicate_of": None}
        self.feedback, self.fixer_calls = [], 0

    async def json(self, system, user, smart=False, usage=None):
        self.fixer_calls += 1
        return dict(FIX)

    async def run_tools(self, system, user, tools, max_calls=15, usage=None, on_step=None):
        self.feedback.append(user)
        fix = FIX if self.diag.get("fix_class") not in (None, "none") else {"files": []}
        return {**self.diag, **self.triage, "fix": fix}

    async def text(self, *a, **k):
        return "text"


class FakeRepo:
    dir = Path("/tmp")

    async def sync(self): return "sha"
    def list_files(self, sub): return ["apps/api/deployment.yaml"]
    def read(self, p): return "old\n api \n"
    async def show(self, sha): return "diff"
    async def commit_branch(self, *a): return "branchsha"
    async def revert(self, sha, msg): return "revertsha"
    async def commit_main(self, *a): return "pmsha"


class FakeHost:
    def __init__(self): self.closed, self.merged = [], []
    async def open_pr(self, branch, title, body): return 7, "https://example/pr/7"
    async def merge_pr(self, n, title): self.merged.append(n); return "mergesha"
    async def close_pr(self, n, comment=""): self.closed.append((n, comment))


class FakeGate:
    def __init__(self, ok=True): self.ok = ok
    async def run(self, repo_dir, old, new):
        r = GateResult()
        r.add("allowlist", self.ok, "ok" if self.ok else "nope")
        return r


class FakeNotifier:
    def __init__(self): self.approvals, self.updates = [], []
    async def update(self, inc, text): self.updates.append(text)
    async def request_approval(self, inc): self.approvals.append(inc["id"])


class FakeVerifier:
    def __init__(self, ok=True): self.ok = ok
    async def wait_synced(self, sha, timeout): return True, "synced"
    async def verify(self, inc, window): return self.ok, "healthy" if self.ok else "still crashing"


@pytest.fixture
def make(monkeypatch):
    # The pre-triage recheck scans the cluster; pretend the problem is still there.
    monkeypatch.setattr("agent.manager.scan", lambda *a, **k: [Signal("sweep", "demo", "api", "OOMKilled", "x")])

    async def fake_collect(inc, tools, kube, repo, apps, digest, others, on_step=None):
        other = "\n".join(f"#{o['id']} {o['workload']}" for o in others)
        return f"evidence for {inc['workload']}\n{other}", {"apps/api/deployment.yaml": "old\n api \n"}
    monkeypatch.setattr("agent.manager.collect_evidence", fake_collect)

    def _make(llm=None, gate=None, verifier=None):
        s = Settings(_env_file=None, debounce_s=0, max_fix_attempts=2)
        store = Store(":memory:")
        host, notifier = FakeHost(), FakeNotifier()
        m = IncidentManager(s, store, llm or FakeLLM(), None, FakeRepo(), host, gate or FakeGate(), notifier,
                            kube=type("K", (), {"pods": lambda self, ns: []})(), verifier=verifier or FakeVerifier())
        return m, store, host, notifier
    return _make


async def wait_state(store, iid, states, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        inc = store.get(iid)
        if inc and inc["state"] in states:
            return inc
        await asyncio.sleep(0.02)
    raise AssertionError(f"state stuck at {store.get(iid)['state']}")


def sig(wl="api"):
    return Signal("watch", "demo", wl, "OOMKilled", f"{wl}: OOMKilled", "critical", ["e"])


async def test_happy_path_resolves(make):
    m, store, host, notifier = make()
    await m.on_signal(sig())
    await wait_state(store, 1, {"AWAITING_APPROVAL"})
    assert notifier.approvals == [1]
    assert "approved" in await m.approve(1, "alice")
    inc = await wait_state(store, 1, {"RESOLVED", "ESCALATED"})
    assert inc["state"] == "RESOLVED" and host.merged == [7] and inc["approver"] == "alice"
    states = [t["to_state"] for t in store.transitions(1)]
    assert states == ["DETECTED", "TRIAGED", "INVESTIGATING", "FIX_PROPOSED", "AWAITING_APPROVAL", "APPLYING", "VERIFYING", "RESOLVED"]


async def test_signals_for_same_workload_join_one_incident(make):
    m, store, *_ = make()
    await m.on_signal(sig())
    await m.on_signal(Signal("probe", "demo", "api", "ProbeFailed", "api: probe", "critical", ["x"], stateful=False))
    assert len(store.list()) == 1
    assert {s["source"] for s in store.signals(1)} == {"watch", "probe"}


async def test_reject_feeds_reason_back_then_escalates(make):
    llm = FakeLLM()
    m, store, host, _ = make(llm=llm)
    await m.on_signal(sig())
    await wait_state(store, 1, {"AWAITING_APPROVAL"})
    await m.reject(1, "bob", "limit should be 1Gi")
    await asyncio.sleep(0.1)
    await wait_state(store, 1, {"AWAITING_APPROVAL"})
    assert "limit should be 1Gi" in llm.feedback[-1] and host.closed[0][0] == 7
    await m.reject(1, "bob", "still no")
    inc = await wait_state(store, 1, {"ESCALATED"})
    assert "no acceptable fix" in store.transitions(1)[-1]["note"]
    assert inc["state"] == "ESCALATED"


async def test_failed_verification_rolls_back(make):
    m, store, *_ = make(verifier=FakeVerifier(ok=False))
    await m.on_signal(sig())
    await wait_state(store, 1, {"AWAITING_APPROVAL"})
    await m.approve(1, "alice")
    await wait_state(store, 1, {"ESCALATED"})
    states = [t["to_state"] for t in store.transitions(1)]
    assert states[-2:] == ["ROLLED_BACK", "ESCALATED"] and "reverted" in store.transitions(1)[-1]["note"]


async def test_low_confidence_escalates_without_pr(make):
    m, store, host, notifier = make(llm=FakeLLM(diag={**DIAG, "confidence": 0.3}))
    await m.on_signal(sig())
    await wait_state(store, 1, {"ESCALATED"})
    assert notifier.approvals == [] and host.merged == []


async def test_gate_failure_never_reaches_a_human(make):
    m, store, host, notifier = make(gate=FakeGate(ok=False))
    await m.on_signal(sig())
    await wait_state(store, 1, {"ESCALATED"})
    assert notifier.approvals == []


async def test_downstream_duplicate_is_dropped_and_stays_quiet(make):
    m, store, *_ = make()
    await m.on_signal(sig("api"))
    await wait_state(store, 1, {"AWAITING_APPROVAL"})
    m.llm.triage = {"actionable": True, "severity": "critical", "duplicate_of": 1, "reason": "downstream"}
    await m.on_signal(sig("frontend"))
    await wait_state(store, 2, {"DROPPED"})
    m._seen.clear()
    await m.on_signal(sig("frontend"))
    assert len(store.list()) == 2  # no new incident while #1 is open


async def test_downstream_of_escalated_incident_stays_quiet(make):
    """Regression: a frontend symptom of an escalated (still failing) api incident must not
    open a fresh investigation."""
    m, store, *_ = make(llm=FakeLLM(diag={**DIAG, "fix_class": "none", "escalate_reason": "code bug"}))
    await m.on_signal(sig("api"))
    await wait_state(store, 1, {"ESCALATED"})
    m.llm.triage = {"actionable": True, "severity": "critical", "duplicate_of": 1, "reason": "downstream"}
    await m.on_signal(sig("frontend"))
    await wait_state(store, 2, {"DROPPED"})
    m._seen.clear()
    await m.on_signal(sig("frontend"))
    assert len(store.list()) == 2  # still attached to #2, no new incident


async def test_approve_when_not_waiting_is_refused(make):
    m, store, *_ = make()
    assert "not awaiting" in await m.approve(99, "x")


async def test_restart_does_not_mute_new_problems(make):
    """Regression: incidents interrupted by a restart were marked ESCALATED, and the escalation
    mute then swallowed the next fault on that workload."""
    m, store, *_ = make()
    iid = store.create("demo/api", "demo", "api", "critical", "old")
    m.recover_after_restart()
    assert store.get(iid)["state"] == "DROPPED"
    await m.on_signal(sig("api"))
    assert len(store.list()) == 2


async def test_new_symptom_on_escalated_workload_opens_new_incident(make):
    m, store, *_ = make(llm=FakeLLM(diag={**DIAG, "fix_class": "none", "escalate_reason": "code bug"}))
    await m.on_signal(Signal("logs", "demo", "api", "LogErrors", "api: errors", "warning", ["KeyError"], stateful=False))
    await wait_state(store, 1, {"ESCALATED"})
    m._seen.clear()
    await m.on_signal(Signal("logs", "demo", "api", "LogErrors", "api: errors", "warning", ["KeyError"], stateful=False))
    assert len(store.list()) == 1          # same ongoing problem stays with the escalation
    await m.on_signal(sig("api"))          # OOMKilled: a different fault
    assert len(store.list()) == 2


async def test_happy_path_uses_one_model_call(make):
    llm = FakeLLM()
    m, store, *_ = make(llm=llm)
    await m.on_signal(sig())
    await wait_state(store, 1, {"AWAITING_APPROVAL"})
    assert len(llm.feedback) == 1 and llm.fixer_calls == 0  # triage + diagnosis + fix in one call


class FlakyGate(FakeGate):
    """Fails the first proposal, accepts the corrected one."""
    def __init__(self): self.runs = 0
    async def run(self, repo_dir, old, new):
        self.runs += 1
        r = GateResult()
        r.add("dry-run", self.runs > 1, "invalid" if self.runs == 1 else "ok")
        return r


async def test_gate_failure_costs_exactly_one_fixer_call(make):
    llm = FakeLLM()
    m, store, *_ = make(llm=llm, gate=FlakyGate())
    await m.on_signal(sig())
    await wait_state(store, 1, {"AWAITING_APPROVAL"})
    assert len(llm.feedback) == 1 and llm.fixer_calls == 1


async def test_postmortem_needs_no_model(make):
    llm = FakeLLM()
    m, store, *_ = make(llm=llm)
    await m.on_signal(sig())
    await wait_state(store, 1, {"AWAITING_APPROVAL"})
    await m.approve(1, "alice")
    await wait_state(store, 1, {"RESOLVED"})
    md = m.postmortem_markdown(1)
    assert "## Root cause" in md and "limit too low" in md and "abc1234" in md and "RESOLVED" in md
    assert len(llm.feedback) == 1 and llm.fixer_calls == 0


async def test_argocd_echo_is_dropped_without_a_model_call(make):
    llm = FakeLLM()
    m, store, *_ = make(llm=llm)
    await m.on_signal(sig("api"))
    await wait_state(store, 1, {"AWAITING_APPROVAL"})
    await m.on_signal(Signal("watch", "demo", "argocd/demo", "AppDegraded", "ArgoCD app demo Degraded", "warning", ["x"]))
    inc = await wait_state(store, 2, {"DROPPED"})
    assert "incident #1" in store.transitions(2)[-1]["note"]
    assert len(llm.feedback) == 1  # only #1's call
