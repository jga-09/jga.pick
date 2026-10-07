"""Application configuration loaded from environment / .env.

Risk-profile specific variables (``LOW_*``, ``MEDIUM_*``, ``HIGH_*``) are
loaded in :mod:`app.risk.profiles`.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ConfigError

KALSHI_HOSTS = {
    # From the official Kalshi OpenAPI spec (kalshi_python_sync host settings).
    "prod": (
        "https://api.elections.kalshi.com/trade-api/v2",
        "wss://api.elections.kalshi.com/trade-api/ws/v2",
    ),
    "demo": (
        "https://demo-api.kalshi.co/trade-api/v2",
        "wss://demo-api.kalshi.co/trade-api/ws/v2",
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram -------------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_admin_ids: str = ""

    # --- Kalshi ---------------------------------------------------------
    kalshi_env: Literal["prod", "demo"] = "prod"
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: Path | None = None
    kalshi_rest_url: str | None = None
    kalshi_ws_url: str | None = None
    kalshi_read_rps: float = Field(8.0, gt=0, le=50)
    kalshi_write_rps: float = Field(4.0, gt=0, le=20)
    http_timeout_sec: float = Field(10.0, gt=0, le=60)

    # --- Execution mode (paper is the default and the safe fallback) -----
    paper_trading: bool = True
    live_trading: bool = False
    auto_trading: bool = False
    allow_live_autotrade: bool = False

    default_risk_level: Literal["low", "medium", "high"] = "low"

    # --- Paper simulation -----------------------------------------------
    paper_starting_balance: float = Field(1000.0, gt=0)
    paper_slippage_cents: int = Field(1, ge=0, le=10)
    # Limit price = ask + tolerance (paper and live). Sizing uses this worst case.
    order_price_tolerance_cents: int = Field(2, ge=0, le=10)
    fee_rate: float = Field(0.07, ge=0, le=0.5)

    # --- Data source: "kalshi" (real) or "fixture" (synthetic, for dry runs) ---
    data_source: Literal["kalshi", "fixture"] = "kalshi"

    # --- Market discovery -----------------------------------------------
    assets: str = "BTC,ETH,SOL"
    series_tickers: str = ""
    auto_discover_series: bool = True
    market_duration_minutes: int = Field(15, ge=1, le=240)
    duration_tolerance_sec: int = Field(120, ge=0)
    discovery_interval_sec: float = Field(30.0, ge=5)

    # --- Market data ----------------------------------------------------
    poll_interval_sec: float = Field(5.0, ge=1)
    orderbook_depth: int = Field(10, ge=1, le=100)
    use_websocket: bool = True
    stale_data_sec: float = Field(30.0, ge=5)
    underlying_provider: Literal["none", "coinbase"] = "none"

    # --- Signal engine --------------------------------------------------
    signal_min_confidence: int = Field(55, ge=0, le=100)
    signal_min_history_sec: float = Field(60.0, ge=0, le=900)
    signal_history_size: int = Field(240, ge=20, le=5000)

    # --- Telegram UI / alerts -------------------------------------------
    dashboard_refresh_sec: float = Field(15.0, ge=3)
    alert_cooldown_sec: float = Field(120.0, ge=0)
    strong_signal_alert_min_confidence: int = Field(80, ge=0, le=100)
    large_loss_alert_usd: float = Field(5.0, ge=0)

    # --- Persistence ----------------------------------------------------
    database_url: str = "sqlite:///data/bot.db"
    snapshot_interval_sec: float = Field(30.0, ge=1)
    snapshot_retention_days: int = Field(7, ge=1)
    signal_retention_days: int = Field(30, ge=1)
    event_retention_days: int = Field(30, ge=1)

    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid LOG_LEVEL {v!r}")
        return v

    # --- Derived values -------------------------------------------------
    @property
    def admin_ids(self) -> frozenset[int]:
        ids: set[int] = set()
        for part in self.telegram_admin_ids.replace(" ", "").split(","):
            if not part:
                continue
            if not part.lstrip("-").isdigit():
                raise ConfigError(f"TELEGRAM_ADMIN_IDS contains a non-numeric id: {part!r}")
            ids.add(int(part))
        return frozenset(ids)

    @property
    def asset_list(self) -> list[str]:
        return [a.strip().upper() for a in self.assets.split(",") if a.strip()]

    @property
    def series_list(self) -> list[str]:
        return [s.strip().upper() for s in self.series_tickers.split(",") if s.strip()]

    @property
    def rest_url(self) -> str:
        return (self.kalshi_rest_url or KALSHI_HOSTS[self.kalshi_env][0]).rstrip("/")

    @property
    def ws_url(self) -> str:
        return self.kalshi_ws_url or KALSHI_HOSTS[self.kalshi_env][1]

    @property
    def has_kalshi_credentials(self) -> bool:
        return bool(self.kalshi_api_key_id and self.kalshi_private_key_path)

    @property
    def live_allowed(self) -> bool:
        """Live trading is permitted only with BOTH flags set deliberately."""
        return self.live_trading and not self.paper_trading

    @property
    def live_autotrade_allowed(self) -> bool:
        return self.live_allowed and self.allow_live_autotrade

    def validate_runtime(self, *, require_telegram: bool = True) -> list[str]:
        """Fail-safe validation. Raises ConfigError on fatal problems, returns warnings."""
        errors: list[str] = []
        warnings: list[str] = []
        if require_telegram:
            token = self.telegram_bot_token.get_secret_value() if self.telegram_bot_token else ""
            if not token:
                errors.append("TELEGRAM_BOT_TOKEN is required")
            elif not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,64}", token):
                # Never echo the token itself.
                errors.append("TELEGRAM_BOT_TOKEN looks malformed (expected <digits>:<letters/digits>, "
                              "no spaces, quotes or trailing punctuation)")
            if not self.admin_ids:
                errors.append("TELEGRAM_ADMIN_IDS must list at least one numeric Telegram user id")
        if self.live_trading and self.paper_trading:
            warnings.append("LIVE_TRADING=true but PAPER_TRADING=true -> running in PAPER mode")
        if self.live_allowed:
            if not self.has_kalshi_credentials:
                errors.append("Live trading requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
            elif not Path(self.kalshi_private_key_path).is_file():  # type: ignore[arg-type]
                errors.append("KALSHI_PRIVATE_KEY_PATH does not point to a readable file")
        if self.data_source == "fixture" and self.live_allowed:
            errors.append("DATA_SOURCE=fixture can never be combined with live trading")
        if self.allow_live_autotrade and not self.live_allowed:
            warnings.append("ALLOW_LIVE_AUTOTRADE ignored because live trading is not enabled")
        if not self.asset_list and not self.series_list:
            errors.append("Configure ASSETS and/or SERIES_TICKERS")
        if errors:
            raise ConfigError("; ".join(errors))
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
