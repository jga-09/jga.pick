"""Structured key=value logging with secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),  # Telegram bot token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(KALSHI-ACCESS-SIGNATURE['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9+/=]+", re.I),
    re.compile(r"(bot)[0-9]{6,12}:[A-Za-z0-9_-]{20,}"),
]
_REDACT_KEYS = {"token", "secret", "signature", "private_key", "password", "api_key"}
_extra_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a literal secret string that must never appear in logs."""
    if value and len(value) >= 6:
        _extra_secrets.add(value)


def redact(text: str) -> str:
    for secret in _extra_secrets:
        text = text.replace(secret, "***")
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "***REDACTED***", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S")
        msg = record.getMessage()
        line = f"{ts} {record.levelname:<7} {record.name}: {msg}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return redact(line)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    # Quiet chatty libraries (httpx logs full URLs incl. the bot token at INFO).
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _fmt_value(key: str, value: Any) -> str:
    if key.lower() in _REDACT_KEYS:
        return "***"
    if isinstance(value, float):
        value = f"{value:.4f}".rstrip("0").rstrip(".")
    text = str(value)
    return f'"{text}"' if " " in text else text


def log_event(logger: logging.Logger, event: str, /, level: int = logging.INFO, **fields: Any) -> None:
    """Emit ``EVENT key=value ...`` lines, e.g. ``SIGNAL direction=UP confidence=84``."""
    parts = " ".join(f"{k}={_fmt_value(k, v)}" for k, v in fields.items())
    logger.log(level, f"{event} {parts}".rstrip())
