"""Timezone-aware time helpers. Everything internal is UTC."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_ts(value: str | int | float | datetime | None) -> datetime | None:
    """Parse an ISO-8601 string / unix seconds / datetime into an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 10**11 else value  # tolerate ms timestamps
        return datetime.fromtimestamp(secs, UTC)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def seconds_until(target: datetime | None, now: datetime | None = None) -> float:
    if target is None:
        return 0.0
    return (target - (now or utcnow())).total_seconds()


def fmt_countdown(seconds: float) -> str:
    """08:42 style countdown; clamps at zero; adds hours when needed."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def day_key(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(UTC).strftime("%Y-%m-%d")
