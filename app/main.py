"""Entry point: ``python -m app.main``."""

from __future__ import annotations

import argparse
import logging
import sys

from app import APP_NAME, __version__
from app.config import Settings
from app.data.underlying import build_provider
from app.database.db import Database
from app.database.repository import Repository
from app.errors import ConfigError, KalshiAuthError
from app.kalshi.auth import KalshiSigner
from app.kalshi.client import KalshiClient
from app.kalshi.fixtures import FixtureKalshiClient
from app.risk.profiles import load_profiles
from app.runtime import BotRuntime
from app.utils.logging import log_event, register_secret, setup_logging

log = logging.getLogger("app")


def build_runtime(settings: Settings) -> BotRuntime:
    if settings.data_source == "fixture":
        client = FixtureKalshiClient()
    else:
        signer = None
        if settings.has_kalshi_credentials:
            signer = KalshiSigner.from_file(settings.kalshi_api_key_id or "", settings.kalshi_private_key_path or "")
        client = KalshiClient(settings, signer)
    repo = Repository(Database(settings.database_url))
    repo.init()
    return BotRuntime(settings, repo, client, underlying=build_provider(settings.underlying_provider),
                      profiles=load_profiles())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} - Kalshi 15M directional bot")
    parser.add_argument("--fixture", action="store_true", help="use synthetic fixture data (no Kalshi access)")
    parser.add_argument("--check-config", action="store_true", help="validate configuration and exit")
    args = parser.parse_args(argv)

    try:
        settings = Settings()
        if args.fixture:
            settings = settings.model_copy(update={"data_source": "fixture"})
        setup_logging(settings.log_level)
        if settings.telegram_bot_token:
            register_secret(settings.telegram_bot_token.get_secret_value())
        warnings = settings.validate_runtime(require_telegram=True)
        load_profiles()  # validates LOW_/MEDIUM_/HIGH_ overrides against hard limits
    except (ConfigError, ValueError) as exc:
        setup_logging("INFO")
        log.critical("CONFIG_INVALID %s", exc)
        return 2
    for w in warnings:
        log.warning("CONFIG_WARNING %s", w)
    mode = "LIVE" if settings.live_allowed else "PAPER"
    log_event(log, "BOOT", version=__version__, mode=mode, data=settings.data_source, env=settings.kalshi_env,
              assets=",".join(settings.asset_list))
    if args.check_config:
        print("Configuration OK")
        return 0

    try:
        rt = build_runtime(settings)
    except KalshiAuthError as exc:
        log.critical("KALSHI_CREDENTIALS_INVALID %s", exc)
        return 2

    from app.telegram.bot import build_application

    app = build_application(rt, settings.telegram_bot_token.get_secret_value())  # type: ignore[union-attr]
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
