"""Scripted OpenAI-compatible LLM for offline end-to-end tests.

It speaks the same Chat Completions protocol as Groq/Ollama (including tool calls), but
its "reasoning" is a lookup against the chaos fault catalog: it finds the fault's commit
in `git_log` output and proposes the reverse edit. Use it to exercise the whole loop
(PR -> approve -> merge -> ArgoCD sync -> verify) without an API key:

    uv run python -m tests.fake_llm --port 8099
    LLM_BASE_URL=http://localhost:8099/v1 LLM_API_KEY=fake uv run devon-agent
"""

from __future__ import annotations

import argparse
import json
import re
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request

from chaos.cli import FAULTS

app = FastAPI(title="fake llm")


def _reply(content: str = "", tool_calls: list | None = None, model: str = "fake") -> dict:
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600}}


def _fault_for_message(msg: str):
    for name, f in FAULTS.items():
        if f.message == msg.strip():
            return name, f
    return None, None


def triage(user: str) -> dict:
    m = re.search(r"Incident #(\d+) on [\w-]+/([\w/-]+)", user)
    wl = m.group(2) if m else ""
    dup = None
    if wl == "frontend":
        for line in user.split("Other open incidents:")[-1].splitlines():
            mm = re.match(r"#(\d+) (api|redis) ", line.strip())
            if mm:
                dup = int(mm.group(1))
    return {"actionable": True, "category": "other", "severity": "critical", "duplicate_of": dup,
            "reason": f"{wl} is failing now" + (f"; likely caused by incident #{dup}" if dup else "")}


def investigate(messages: list[dict]) -> dict:
    tool_results = [m["content"] for m in messages if m.get("role") == "tool"]
    if not tool_results:
        return {"tool_call": ("git_log", {"path": "apps", "n": 10})}
    log = tool_results[-1]
    for m in re.finditer(r"^([0-9a-f]{7,12}) \S+ [^\n]*\n\s{4}(.+)$", log, re.M):
        sha, subject = m.group(1), m.group(2)
        name, f = _fault_for_message(subject)
        if f:
            d = {"root_cause": f"Commit {sha} ('{subject}') broke {f.workload}: {f.expected.split('->')[0].strip()}.",
                 "evidence": [f"git: {sha} {subject}", "detector signals started right after this commit was synced"],
                 "suspect_commit": sha, "suspect_change": subject, "confidence": 0.9,
                 "fix_class": f.fix_class, "fix_hint": f.expected.split("->")[-1].strip(), "escalate_reason": None}
            if f.fix_class == "none":
                d.update(fix_class="none", escalate_reason="application code bug; roll back the release")
            return d
    return {"root_cause": "unknown", "evidence": [], "suspect_commit": None, "suspect_change": None, "confidence": 0.2,
            "fix_class": "none", "fix_hint": "", "escalate_reason": "could not identify the change"}


def fix(user: str) -> dict:
    files = dict(re.findall(r"=== (\S+) ===\n(.*?)(?=\n=== \S+ ===\n|\n\nYour previous|\n\n## |\Z)", user, re.S))
    out, title = [], "revert faulty change"
    for path, content in files.items():
        new = content
        for name, f in FAULTS.items():
            for p, old, bad in f.edits:
                if p == path and bad in new and old not in new:
                    new = new.replace(bad, old)
                    title = f"revert: {f.message}"
        if new != content:
            out.append({"path": path, "content": new})
    return {"title": title, "summary": "Restore the value from before the faulty commit.", "risk": "low",
            "files": out, "notes": ""}


def investigate_and_fix(pack: str) -> dict:
    """One-call mode: everything needed is in the evidence pack."""
    wl = (re.search(r"workload '([\w/-]+)'", pack) or [None, ""])[1]
    others = pack.split("## Other open incidents", 1)[-1].split("\n## ", 1)[0]
    dup = None
    if wl == "frontend":
        m = re.search(r"#(\d+) (api|redis) ", others)
        dup = int(m.group(1)) if m else None
    git = pack.split("## Recent commits", 1)[-1].split("\n## ", 1)[0]
    d = investigate([{"role": "tool", "content": git}])
    d.update(actionable=True, duplicate_of=dup, severity="critical")
    d["fix"] = fix(pack) if d.get("fix_class") not in (None, "none") else {"title": "", "summary": "", "risk": "low", "files": []}
    return d


@app.post("/v1/chat/completions")
async def completions(req: Request):
    body = await req.json()
    msgs = body["messages"]
    system, user = msgs[0]["content"], next((m["content"] for m in msgs if m["role"] == "user"), "")
    if system.startswith("You are an expert Kubernetes SRE handling an incident"):
        return _reply(json.dumps(investigate_and_fix(user)))
    if system.startswith("You are the triage step"):
        return _reply(json.dumps(triage(user)))
    if system.startswith("You are an expert Kubernetes SRE"):
        res = investigate(msgs)
        if "tool_call" in res and body.get("tools"):
            name, args = res["tool_call"]
            return _reply("", [{"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}])
        return _reply(json.dumps(res if "tool_call" not in res else investigate([{"role": "tool", "content": ""}])))
    if system.startswith("You write the smallest possible fix"):
        return _reply(json.dumps(fix(user)))
    if system.startswith("Write a short blameless postmortem"):
        return _reply("## Summary\nScripted postmortem from the fake LLM.\n")
    return _reply("Scripted explanation from the fake LLM.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    uvicorn.run(app, host="127.0.0.1", port=ap.parse_args().port, log_level="warning")
