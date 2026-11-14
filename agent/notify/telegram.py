"""Telegram bot notifier: long polling (no public URL needed), inline Approve / Reject /
Explain buttons, approver allowlist by Telegram user ID."""

from __future__ import annotations

import html
import io
import logging

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from agent.notify.base import STATE_ICONS, ApprovalHandler, approval_card

log = logging.getLogger("devon.telegram")
REJECT_PROMPT = "Reply to this message with the reason for rejecting incident #"
MAX_MSG = 3800


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, approvers: list[str], web_url: str = ""):
        # Accept numeric user IDs and @usernames. IDs are safer: usernames can be changed or given up.
        self.chat_id, self.web_url = chat_id, web_url
        self.approvers = {str(a).strip().lstrip("@").lower() for a in approvers if str(a).strip()}
        self.app = Application.builder().token(token).build()
        self.handler: ApprovalHandler | None = None

    async def start(self, handler: ApprovalHandler) -> None:
        self.handler = handler
        self.app.add_handler(CallbackQueryHandler(self._on_button))
        self.app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, self._on_reply))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram bot polling; approvers=%s", sorted(self.approvers) or "ANYONE (set TELEGRAM_APPROVERS)")

    async def stop(self) -> None:
        if self.app.updater.running:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    async def _send(self, text: str, **kw) -> None:
        try:
            await self.app.bot.send_message(self.chat_id, text[:4096], parse_mode=ParseMode.HTML,
                                            disable_web_page_preview=True, **kw)
        except Exception as e:  # never let chat problems break the loop
            log.warning("telegram send failed: %s", e)

    async def update(self, inc: dict, text: str) -> None:
        icon = STATE_ICONS.get(inc.get("state", ""), "•")
        await self._send(f"{icon} <b>#{inc['id']} {html.escape(inc['workload'])}</b>: {html.escape(text)}")

    async def request_approval(self, inc: dict) -> None:
        iid = inc["id"]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{iid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{iid}"),
            InlineKeyboardButton("💬 Explain", callback_data=f"explain:{iid}"),
        ]])
        card = approval_card(inc, f"{self.web_url}/#incident-{iid}" if self.web_url else "")
        diff = (inc.get("gate") or {}).get("diff", "")
        if diff and len(card) + len(diff) < MAX_MSG:
            card += f"\n\n<pre>{html.escape(diff)}</pre>"
        elif diff:
            try:
                await self.app.bot.send_document(self.chat_id, io.BytesIO(diff.encode()),
                                                 filename=f"incident-{iid}.diff", caption=f"Diff for incident #{iid}")
            except Exception as e:
                log.warning("telegram document send failed: %s", e)
        await self._send(card, reply_markup=kb)

    def _authorized(self, user) -> bool:
        if not self.approvers:
            return True
        if not user:
            return False
        return str(user.id) in self.approvers or bool(user.username and user.username.lower() in self.approvers)

    async def _on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        action, _, iid_s = (q.data or "").partition(":")
        iid = int(iid_s) if iid_s.isdigit() else -1
        who = (q.from_user.username or str(q.from_user.id)) if q.from_user else "unknown"
        if action in ("approve", "reject") and not self._authorized(q.from_user):
            await q.answer("You are not on the approver list.", show_alert=True)
            return
        await q.answer()
        if action == "approve":
            msg = await self.handler.approve(iid, who)
            await q.edit_message_reply_markup(None)
            await self._send(f"#{iid}: {html.escape(msg)}")
        elif action == "reject":
            await self.app.bot.send_message(self.chat_id, f"{REJECT_PROMPT}{iid}", reply_markup=ForceReply(selective=True))
        elif action == "explain":
            await self._send(f"💬 <b>#{iid}</b>\n{html.escape(await self.handler.explain(iid))}")

    async def _on_reply(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        parent = msg.reply_to_message.text if msg.reply_to_message else ""
        if not parent or not parent.startswith(REJECT_PROMPT):
            return
        if not self._authorized(update.effective_user):
            return
        iid = int(parent.removeprefix(REJECT_PROMPT).strip() or -1)
        who = update.effective_user.username or str(update.effective_user.id)
        res = await self.handler.reject(iid, who, msg.text)
        await self._send(f"#{iid}: {html.escape(res)}")
