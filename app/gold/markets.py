"""Find Kalshi's Gold series and verify each contract's settlement rules before anything uses it.

Nothing here assumes a ticker. Series are found from Kalshi's own /series list
(category filter configurable, default "Commodities", plus a full-list scan),
by the word gold/XAU in the ticker or title. A market is usable for research
only when ``validate_gold_market`` passes:
  * the rules text mentions gold / XAU,
  * the strike type is a single threshold (above / below) with a published strike,
  * the rules' direction matches the strike type,
  * the settlement SOURCE named in the rules is identified, and matches the price
    provider the bot would compare it with (e.g. Pyth rules + Pyth feed),
  * expiration is after close, and the market's window is what the series claims.
Trading Gold stays disabled regardless (no validated Gold pricing model yet).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.errors import KalshiError
from app.kalshi.contract import ABOVE_TYPES, BELOW_TYPES
from app.kalshi.market_data import MarketInfo

GOLD_WORDS = re.compile(r"\b(gold|xau)\b|KXGOLD", re.IGNORECASE)
_ABOVE = re.compile(r"\b(above|greater|higher|at or above|or more|exceeds?|over)\b", re.IGNORECASE)
_BELOW = re.compile(r"\b(below|less than|lower|under|or less)\b", re.IGNORECASE)
SOURCES = (("pyth", re.compile(r"\bpyth\b", re.IGNORECASE)),
           ("comex", re.compile(r"\b(comex|cme|gc\s*futures?|gold futures)\b", re.IGNORECASE)),
           ("lbma", re.compile(r"\blbma\b", re.IGNORECASE)))


def is_gold_series(series: dict[str, Any]) -> bool:
    text = f"{series.get('ticker', '')} {series.get('title', '')}"
    return bool(GOLD_WORDS.search(text)) and "silver" not in text.lower()


def frequency_minutes(series: dict[str, Any]) -> int | None:
    """Window length implied by the series (ticker suffix or frequency text). None if not stated."""
    t = str(series.get("ticker", "")).upper()
    m = re.search(r"(\d+)M$", t)
    if m:
        return int(m.group(1))
    freq = str(series.get("frequency", "")).lower()
    return {"fifteen_min": 15, "hourly": 60, "daily": 1440, "weekly": 10080}.get(freq)


async def discover_gold_series(client: Any, categories: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """(gold series found, errors). Searches the given categories, then the full series list."""
    found: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for cat in [*categories, None]:
        try:
            for s in await client.get_series_list(category=cat):
                if is_gold_series(s) and s.get("ticker"):
                    found.setdefault(str(s["ticker"]).upper(), s)
        except KalshiError as exc:
            errors.append(f"series list ({cat or 'all'}): {exc}")
    return sorted(found.values(), key=lambda s: str(s.get("ticker"))), errors


def settlement_source(rules: str) -> str | None:
    for name, rx in SOURCES:
        if rx.search(rules):
            return name
    return None


@dataclass(frozen=True)
class GoldCheck:
    ticker: str
    verified: bool
    kind: str | None  # above | below
    strike: float | None
    source: str | None  # pyth | comex | lbma
    problems: tuple[str, ...] = field(default_factory=tuple)


def validate_gold_market(info: MarketInfo, provider_source: str | None, now: datetime | None = None,
                         series_sources: list[str] | None = None) -> GoldCheck:
    """``series_sources``: the series' own settlement_sources from Kalshi (used when the market's rules text
    does not name the source, e.g. "the close price of the 1-minute candlestick for Gold")."""
    problems: list[str] = []
    rules = f"{info.rules_primary} {info.rules_secondary}".strip()
    st = (info.strike_type or "").lower()
    kind, strike = None, None
    if st in ABOVE_TYPES:
        kind, strike = "above", info.floor_strike
    elif st in BELOW_TYPES:
        kind, strike = "below", info.cap_strike
    else:
        problems.append(f"strike type {info.strike_type!r} not supported (need a single above/below threshold)")
    if kind and (strike is None or strike <= 0):
        problems.append("strike not published")
    if not rules:
        problems.append("settlement rules text missing - cannot verify what YES means")
        source = None
    else:
        if not GOLD_WORDS.search(rules):
            problems.append("rules text does not mention gold / XAU")
        up, down = bool(_ABOVE.search(rules)), bool(_BELOW.search(rules))
        if kind == "above" and down and not up:
            problems.append("rules describe BELOW but strike type says ABOVE")
        if kind == "below" and up and not down:
            problems.append("rules describe ABOVE but strike type says BELOW")
        source = settlement_source(rules)
        if source is None and series_sources:
            source = settlement_source(" ".join(series_sources))
        if source is None:
            problems.append("settlement source not identified in the rules")
        elif provider_source is None:
            problems.append(f"no price provider configured to match the settlement source ({source})")
        elif source != provider_source:
            problems.append(f"settlement source is {source} but the price provider is {provider_source}")
    if info.expiration_time and info.expiration_time < info.close_time:
        problems.append("expiration is before close")
    if now and info.close_time <= now:
        problems.append("market already closed")
    return GoldCheck(info.ticker, not problems, kind, strike, source, tuple(problems))
