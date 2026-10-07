"""Build the python-telegram-bot Application around a BotRuntime."""

from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from app.runtime import BotRuntime
from app.telegram.callbacks import CallbackRouter
from app.telegram.dashboard import AlertManager, DashboardManager
from app.telegram.handlers import COMMAND_ROUTES, Handlers

log = logging.getLogger(__name__)

COMMANDS = [
    ("menu", "Dashboard"), ("status", "Bot status"), ("markets", "Active markets"), ("signals", "Signals"),
    ("risk", "Risk profile"), ("config", "Configuration"), ("positions", "Open positions"),
    ("trades", "Recent trades"), ("pnl", "P&L"), ("history", "Performance history"), ("strategy", "Strategy"),
    ("pause", "Pause trading"), ("resume", "Resume trading"), ("stop", "Stop bot"),
    ("estop", "Emergency stop"), ("health", "Health check"), ("help", "Help"),
]


def build_application(rt: BotRuntime, token: str) -> Application:
    admin_ids = rt.settings.admin_ids
    router = CallbackRouter(rt, admin_ids)
    tasks: list[asyncio.Task] = []

    async def post_init(app: Application) -> None:
        dashboard = DashboardManager(app.bot, router, rt.settings.dashboard_refresh_sec)
        handlers = Handlers(router, dashboard)
        rt.notifier = AlertManager(app.bot, dashboard, admin_ids, rt.settings.alert_cooldown_sec)
        names = list(COMMAND_ROUTES) + ["help", "health", "debug"]
        app.add_handler(CommandHandler(names, handlers.command))
        app.add_handler(CallbackQueryHandler(handlers.callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text))
        app.add_error_handler(handlers.error)
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        tasks.append(asyncio.create_task(dashboard.refresh_loop(), name="dashboard-refresh"))
        log.info("TELEGRAM_READY admins=%d", len(admin_ids))

    async def post_shutdown(app: Application) -> None:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await rt.shutdown()

    return (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
