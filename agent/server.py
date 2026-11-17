"""HTTP: Alertmanager webhook, scoreboard + incident API, web approvals."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent.detect import alert_signals
from agent.store import TERMINAL

log = logging.getLogger("devon.server")
SCOREBOARD = Path(__file__).resolve().parent.parent / "scoreboard" / "index.html"


class Decision(BaseModel):
    who: str = "web"
    reason: str = ""


def create_app(manager, store, health, settings, notifier_name: str) -> FastAPI:
    app = FastAPI(title="devon agent")

    @app.post("/alerts")
    async def alerts(req: Request):
        payload = await req.json()
        sigs, watchdog = alert_signals(payload)
        if watchdog:
            health.last_watchdog = time.time()
        health.beat("alerts")
        # Only alerts about watched namespaces (or nodes) open incidents.
        sigs = [x for x in sigs if x.namespace in settings.watch_namespaces or x.workload.startswith("node/")]
        for x in sigs:
            await manager.on_signal(x)
        return {"accepted": len(sigs)}

    def summary(inc: dict) -> dict:
        tr = store.transitions(inc["id"])
        d = inc.get("diagnosis") or {}
        return {
            "id": inc["id"], "workload": inc["workload"], "namespace": inc["namespace"], "state": inc["state"],
            "severity": inc["severity"], "title": inc["title"], "created": inc["created"], "resolved": inc["resolved"],
            "duration": (inc["resolved"] or time.time()) - inc["created"], "pr_url": inc["pr_url"],
            "root_cause": d.get("root_cause"), "fix_title": (inc.get("fix") or {}).get("title"),
            "detectors": sorted({s["source"] for s in store.signals(inc["id"])}),
            "tokens": inc["tokens"], "llm_calls": inc.get("llm_calls") or 0, "approver": inc["approver"],
            "transitions": [{"to": t["to_state"], "ts": t["ts"], "note": t["note"]} for t in tr],
        }

    @app.get("/api/incidents")
    async def incidents(limit: int = 50):
        items = [summary(i) for i in store.list(limit)]
        done = [i for i in items if i["state"] in TERMINAL and i["state"] != "DROPPED"]
        fixed = [i for i in done if i["state"] == "RESOLVED"]
        return {
            "incidents": items,
            "stats": {
                "open": sum(1 for i in items if i["state"] not in TERMINAL),
                "resolved": len(fixed), "escalated": sum(1 for i in done if i["state"] == "ESCALATED"),
                "dropped": sum(1 for i in items if i["state"] == "DROPPED"),
                "mttr_s": (sum(i["duration"] for i in fixed) / len(fixed)) if fixed else None,
                "tokens": sum(i["tokens"] or 0 for i in items),
            },
        }

    @app.get("/api/incidents/{iid}")
    async def incident(iid: int):
        inc = store.get(iid)
        if not inc:
            raise HTTPException(404, "no such incident")
        return {**summary(inc), "diagnosis": inc.get("diagnosis"), "fix": inc.get("fix"), "gate": inc.get("gate"),
                "triage": inc.get("triage"), "signals": store.signals(iid)[-40:], "steps": store.steps(iid)}

    @app.post("/api/incidents/{iid}/approve")
    async def approve(iid: int, d: Decision):
        return {"message": await manager.approve(iid, d.who or "web")}

    @app.post("/api/incidents/{iid}/reject")
    async def reject(iid: int, d: Decision):
        return {"message": await manager.reject(iid, d.who or "web", d.reason)}

    @app.post("/api/incidents/{iid}/explain")
    async def explain(iid: int):
        return {"message": await manager.explain(iid)}

    @app.get("/api/health")
    async def healthz():
        now = time.time()
        return {
            "detectors": {k: round(now - v) for k, v in health.last_run.items()},
            "watchdog_age_s": round(now - health.last_watchdog) if health.last_watchdog else None,
            "llm": {"base_url": settings.llm_base_url, "fast": settings.model_fast, "smart": settings.model_smart},
            "notifier": notifier_name, "verify_window_s": settings.verify_window_s,
        }

    @app.get("/")
    async def board():
        return FileResponse(SCOREBOARD)

    return app
