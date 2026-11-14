"""Notifier interface. Telegram today; Mattermost / web page on the closed network.

Approvals flow back through `ApprovalHandler`, implemented by the incident manager.
"""

from __future__ import annotations

import html
import logging
from typing import Protocol

log = logging.getLogger("devon.notify")

STATE_ICONS = {
    "DETECTED": "🔴", "TRIAGED": "🟠", "INVESTIGATING": "🔎", "FIX_PROPOSED": "🛠", "AWAITING_APPROVAL": "🙋",
    "APPLYING": "🚀", "VERIFYING": "⏱", "RESOLVED": "✅", "ROLLED_BACK": "↩️", "ESCALATED": "🧑‍🚒", "DROPPED": "⚪",
}


class ApprovalHandler(Protocol):
    async def approve(self, iid: int, who: str) -> str: ...
    async def reject(self, iid: int, who: str, reason: str) -> str: ...
    async def explain(self, iid: int) -> str: ...


class Notifier(Protocol):
    async def start(self, handler: ApprovalHandler) -> None: ...
    async def stop(self) -> None: ...
    async def update(self, inc: dict, text: str) -> None: ...
    async def request_approval(self, inc: dict) -> None: ...


def approval_card(inc: dict, web_url: str = "") -> str:
    """HTML (Telegram subset) approval message."""
    d, fix, gate = inc.get("diagnosis") or {}, inc.get("fix") or {}, inc.get("gate") or {}
    e = html.escape
    lines = [
        f"🙋 <b>Incident #{inc['id']}: {e(inc['workload'])}</b> needs approval",
        "",
        f"<b>Root cause:</b> {e(d.get('root_cause', '?'))}",
    ]
    if d.get("suspect_commit"):
        lines.append(f"<b>Caused by:</b> commit <code>{e(str(d['suspect_commit'])[:8])}</code>: {e(d.get('suspect_change') or '')}")
    ev = d.get("evidence") or []
    if ev:
        lines += ["", "<b>Evidence:</b>"] + [f"• {e(str(x))[:200]}" for x in ev[:3]]
    lines += ["", f"<b>Fix:</b> {e(fix.get('title', ''))} (risk: {e(fix.get('risk', '?'))})"]
    if inc.get("pr_url"):
        lines.append(f"<b>PR:</b> {e(inc['pr_url'])}")
    checks = " ".join(f"{'✅' if c['ok'] else '❌'}{e(c['name'])}" for c in gate.get("checks", []))
    if checks:
        lines.append(f"<b>Checks:</b> {checks}")
    if gate.get("blast_radius"):
        lines.append(f"<b>Blast radius:</b> {e('; '.join(gate['blast_radius']))}")
    lines.append(f"<b>Confidence:</b> {d.get('confidence', '?')}")
    if web_url:
        lines.append(f"<b>Details:</b> {e(web_url)}")
    return "\n".join(lines)


class ConsoleNotifier:
    """Logs updates; approvals happen on the scoreboard's web page (POST /api/incidents/{id}/approve)."""

    async def start(self, handler: ApprovalHandler) -> None:
        log.info("console notifier active: approve incidents from the scoreboard page")

    async def stop(self) -> None:
        pass

    async def update(self, inc: dict, text: str) -> None:
        log.info("[#%s %s] %s", inc["id"], inc["workload"], text)

    async def request_approval(self, inc: dict) -> None:
        log.info("[#%s] awaiting approval: %s (%s)", inc["id"], (inc.get("fix") or {}).get("title"), inc.get("pr_url"))
