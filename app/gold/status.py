"""Live Gold status for /gold: discovered series, current market, rule check, quotes, price feed.

Research only. ``GoldStatus.ready`` says whether every precondition for Gold research data passed; trading
Gold is ALWAYS disabled here (``trading_reason``), because no Gold pricing model has been validated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.errors import KalshiError, MalformedResponseError
from app.gold.markets import (
    GoldCheck,
    discover_gold_series,
    frequency_minutes,
    validate_gold_market,
)
from app.kalshi.market_data import MarketInfo, parse_market_info, parse_quotes
from app.utils.time import utcnow

MAX_SPREAD_CENTS = 6.0
TRADING_REASON = ("Gold is research-only: no Gold pricing model has been validated on Kalshi's settled Gold "
                  "markets yet (run scripts/gold_check.py)")


@dataclass
class GoldStatus:
    at: datetime
    series: list[dict[str, Any]] = field(default_factory=list)
    market: MarketInfo | None = None
    check: GoldCheck | None = None
    yes_bid: float | None = None
    yes_ask: float | None = None
    price: float | None = None
    price_ts: datetime | None = None
    price_problem: str = ""
    problems: list[str] = field(default_factory=list)
    trading_reason: str = TRADING_REASON

    @property
    def spread(self) -> float | None:
        return None if self.yes_bid is None or self.yes_ask is None else self.yes_ask - self.yes_bid

    @property
    def ready(self) -> bool:
        """All research preconditions passed (discovery, rules, quotes, fresh price). Trading is still off."""
        return not self.problems


async def gold_status(client: Any, provider: Any, categories: list[str], now: datetime | None = None) -> GoldStatus:
    now = now or utcnow()
    st = GoldStatus(at=now)
    st.series, errors = await discover_gold_series(client, categories)
    st.problems.extend(errors)
    if not st.series:
        st.problems.append("no Gold series found on Kalshi")
        return st
    # prefer the shortest-window series (15-minute if it exists)
    ordered = sorted(st.series, key=lambda s: frequency_minutes(s) or 10**9)
    for s in ordered:
        try:
            raws = await client.get_markets(series_ticker=s["ticker"], status="open")
        except KalshiError as exc:
            st.problems.append(f"{s['ticker']}: {exc}")
            continue
        infos: list[tuple[MarketInfo, dict[str, Any]]] = []
        for raw in raws:
            try:
                info = parse_market_info(raw, asset="GOLD", series_ticker=str(s["ticker"]))
            except MalformedResponseError:
                continue
            if info.close_time > now and (info.open_time is None or info.open_time <= now):
                infos.append((info, raw))
        if infos:
            info, raw = min(infos, key=lambda x: x[0].close_time)
            st.market = info
            q = parse_quotes(raw)
            st.yes_bid, st.yes_ask = q["yes_bid"], q["yes_ask"]
            break
    if st.market is None:
        st.problems.append("no open Gold market right now (gold markets may be closed outside trading hours)")
        return st
    provider_source = "pyth" if provider is not None and getattr(provider, "name", "") == "pyth_gold" else None
    st.check = validate_gold_market(st.market, provider_source, now)
    st.problems.extend(st.check.problems)
    if st.spread is None:
        st.problems.append("no two-sided quote")
    elif st.spread > MAX_SPREAD_CENTS:
        st.problems.append(f"spread {st.spread:.0f}c wider than {MAX_SPREAD_CENTS:.0f}c")
    if provider is None:
        st.price_problem = "no Gold price provider configured (set GOLD_PYTH_FEED_ID)"
    else:
        spot = await provider.get_price("GOLD")
        if spot is None:
            st.price_problem = f"Gold price unavailable ({getattr(provider, 'last_error', '') or 'no data'})"
        else:
            st.price, st.price_ts = spot.price, spot.ts
    if st.price_problem:
        st.problems.append(st.price_problem)
    return st
