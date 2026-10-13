"""Build the python-telegram-bot Application around a BotRuntime."""

from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from app import DESCRIPTION, DISPLAY_NAME, SHORT_DESCRIPTION
from app.runtime import BotRuntime
from app.telegram.callbacks import CallbackRouter
from app.telegram.dashboard import AlertManager, DashboardManager
from app.telegram.handlers import COMMAND_ROUTES, KEY_COMMANDS, Handlers

log = logging.getLogger(__name__)

COMMANDS = [
    ("menu", "Dashboard"), ("btc", "BTC 15-min signal"), ("analytics", "Analytics"),
    ("backtest", "Backtest report"), ("calibration", "Model calibration"), ("status", "Bot status"), ("markets", "Active markets"), ("signals", "Signals"),
    ("risk", "Risk profile"), ("config", "Configuration"), ("positions", "Open positions"),
    ("trades", "Recent trades"), ("pnl", "P&L"), ("history", "Performance history"), ("strategy", "Strategy"),
    ("pause", "Pause trading"), ("resume", "Resume trading"), ("stop", "Stop bot"),
    ("estop", "Emergency stop"), ("setkey", "Connect Kalshi API key (message auto-deleted)"),
    ("removekey", "Remove saved Kalshi key"), ("health", "Health check"), ("help", "Help"),
]


async def _set_profile(app: Application) -> None:
    """Show the bot as "JGA Picks" in Telegram. Only changes what differs (Telegram rate-limits renames)."""
    bot = app.bot
    try:
        if (await bot.get_my_name()).name != DISPLAY_NAME:
            await bot.set_my_name(DISPLAY_NAME)
        if (await bot.get_my_short_description()).short_description != SHORT_DESCRIPTION:
            await bot.set_my_short_description(SHORT_DESCRIPTION)
        if (await bot.get_my_description()).description != DESCRIPTION:
            await bot.set_my_description(DESCRIPTION)
    except TelegramError as e:  # cosmetic only; never block startup
        log.warning("BOT_PROFILE not updated: %s", e)


def build_application(rt: BotRuntime, token: str) -> Application:
    admin_ids = rt.settings.admin_ids
    router = CallbackRouter(rt, admin_ids)
    tasks: list[asyncio.Task] = []

    async def post_init(app: Application) -> None:
        dashboard = DashboardManager(app.bot, router, rt.settings.dashboard_refresh_sec)
        handlers = Handlers(router, dashboard)
        rt.notifier = AlertManager(app.bot, dashboard, admin_ids, rt.settings.alert_cooldown_sec)
        names = list(COMMAND_ROUTES) + list(KEY_COMMANDS) + ["help", "health", "debug"]
        app.add_handler(CommandHandler(names, handlers.command))
        app.add_handler(CallbackQueryHandler(handlers.callback))
        app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.Document.ALL
                                       | filters.CAPTION, handlers.text))
        app.add_error_handler(handlers.error)
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        await _set_profile(app)
        tasks.append(asyncio.create_task(dashboard.refresh_loop(), name="dashboard-refresh"))
        log.info("TELEGRAM_READY admins=%d", len(admin_ids))
        ok, why = rt.should_auto_start()
        if ok:
            await rt.start()
            log.info("AUTO_START scanner started")
        elif rt.settings.auto_start:
            log.warning("AUTO_START skipped: %s", why)

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
