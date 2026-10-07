"""python-telegram-bot handlers (thin transport layer over CallbackRouter)."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.telegram import messages as M
from app.telegram.callbacks import CallbackGuard, CallbackRouter
from app.telegram.dashboard import DashboardManager
from app.utils.logging import log_event

log = logging.getLogger(__name__)

# command -> callback route rendered as a fresh dashboard message
COMMAND_ROUTES = {
    "start": "home", "menu": "home", "status": "status", "markets": "signals", "signals": "signals",
    "risk": "risk", "config": "cfg", "positions": "pos", "trades": "trades:0", "pnl": "pnl",
    "history": "hist:today", "strategy": "strat", "pause": "pause", "resume": "resume", "stop": "stop",
    "estop": "estop",
}


class Handlers:
    def __init__(self, router: CallbackRouter, dashboard: DashboardManager) -> None:
        self.router = router
        self.dashboard = dashboard
        self.guard = CallbackGuard()

    async def command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.effective_message, update.effective_user
        if msg is None or msg.text is None:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        uid = user.id if user else None
        if not self.router.is_admin(uid):
            log_event(log, "UNAUTHORIZED_COMMAND", logging.WARNING, user=uid, cmd=cmd[:20])
            await msg.reply_text(M.unauthorized().text)
            return
        assert uid is not None
        if cmd == "help":
            await msg.reply_text(M.help_text(), parse_mode=ParseMode.HTML)
            return
        if cmd in ("health", "debug"):
            await msg.reply_text(self._health_text(debug=cmd == "debug"), parse_mode=ParseMode.HTML)
            return
        route = COMMAND_ROUTES.get(cmd)
        if route is None:
            await msg.reply_text("Unknown command. Try /help")
            return
        resp = await self.router.handle(uid, route)
        if resp.screen:
            await self.dashboard.show(msg.chat_id, uid, resp.screen, new=True)
        elif resp.toast:
            await msg.reply_text(resp.toast)

    def _health_text(self, debug: bool) -> str:
        rt = self.router.rt
        h = rt.health()
        lines = ["🩺 <b>HEALTH</b>"] + [f"{k}: <code>{M.fmt_any(v)}</code>" for k, v in h.items()]
        if debug:
            s = rt.settings
            lines += ["", "<b>DEBUG</b> (no secrets)",
                      f"env: {s.kalshi_env} · data: {s.data_source}",
                      f"rest: <code>{M.fmt_any(s.rest_url)}</code>",
                      f"creds loaded: {s.has_kalshi_credentials}",
                      f"live allowed: {s.live_allowed} · live autotrade: {s.live_autotrade_allowed}",
                      f"tracked markets: {len(rt.discovery.state.by_ticker)}",
                      f"series: {M.fmt_any(', '.join(rt.discovery._series) or '—')}"]
        return "\n".join(lines)

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        uid = query.from_user.id if query.from_user else None
        if not self.guard.accept(query.id, uid or 0, query.data or ""):
            await query.answer("⏳ Already processing…")
            return
        resp = await self.router.handle(uid, query.data)
        await query.answer(resp.toast, show_alert=resp.alert)
        msg = query.message
        if msg is None or uid is None or not self.router.is_admin(uid):
            return
        chat_id = msg.chat.id
        if resp.clear:
            await self.dashboard.clear(chat_id)
        if resp.screen:
            await self.dashboard.show(chat_id, uid, resp.screen, message_id=msg.message_id)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.effective_message and user and self.router.is_admin(user.id):
            await update.effective_message.reply_text("Use /menu to open the dashboard.")

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("TELEGRAM_HANDLER_ERROR error=%r", context.error, exc_info=context.error)
