"""End-to-end fault scenarios against the live kind cluster and a running agent.

    make up            # cluster
    make agent         # in another terminal (real LLM), or `make agent-offline` (fake LLM)
    make scenarios     # DEVON_SCENARIOS=1 pytest -m scenario

Each scenario: reset to baseline -> inject the fault -> wait for the agent -> approve -> expect RESOLVED
(or ESCALATED for faults that need a human).
"""

import os
import subprocess
import time

import httpx
import pytest

from chaos.cli import FAULTS

AGENT = os.environ.get("DEVON_AGENT_URL", "http://localhost:8088")
pytestmark = [pytest.mark.scenario,
              pytest.mark.skipif(os.environ.get("DEVON_SCENARIOS") != "1", reason="set DEVON_SCENARIOS=1 to run")]
SCENARIOS = ["oom-limit", "bad-image", "config-key", "broken-probe", "selector", "netpol", "unschedulable", "app-bug",
             "frontend-bad-image", "redis-bad-image"]


def chaos(*args: str) -> None:
    subprocess.run(["uv", "run", "devon-chaos", *args], check=True)


def incidents() -> list[dict]:
    return httpx.get(f"{AGENT}/api/incidents", timeout=10).json()["incidents"]


def wait_for(pred, timeout: float, what: str):
    end = time.time() + timeout
    while time.time() < end:
        hit = pred()
        if hit:
            return hit
        time.sleep(5)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


@pytest.mark.parametrize("fault", SCENARIOS)
def test_fault(fault):
    f = FAULTS[fault]
    chaos("reset")
    time.sleep(60)  # let ArgoCD sync baseline and old incidents settle
    start_id = max([i["id"] for i in incidents()] or [0])
    chaos("inject", fault)

    def decided():
        for i in incidents():
            if i["id"] > start_id and i["workload"] == f.workload and i["state"] in ("AWAITING_APPROVAL", "ESCALATED", "RESOLVED"):
                return i
    inc = wait_for(decided, 480, f"{fault}: agent decision")

    if f.fix_class == "none":
        assert inc["state"] == "ESCALATED", f"{fault} should be escalated to a human, got {inc['state']}"
        return
    assert inc["state"] == "AWAITING_APPROVAL", f"{fault}: expected a proposed fix, got {inc['state']}: {inc['transitions'][-1]}"
    detail = httpx.get(f"{AGENT}/api/incidents/{inc['id']}", timeout=10).json()
    assert detail["gate"]["passed"]
    assert detail["diagnosis"]["fix_class"] == f.fix_class, detail["diagnosis"]
    httpx.post(f"{AGENT}/api/incidents/{inc['id']}/approve", json={"who": "scenario-test"}, timeout=10)

    final = wait_for(lambda: next((i for i in incidents() if i["id"] == inc["id"] and i["state"] in ("RESOLVED", "ESCALATED")), None),
                     600, f"{fault}: resolution")
    assert final["state"] == "RESOLVED", final["transitions"][-1]
