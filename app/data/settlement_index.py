"""Kalshi's own settlement index for crypto 15-minute markets (CF Benchmarks, e.g. BRTI for BTC).

GET /live_data/events/{event_ticker} returns the index each event settles on as 1-minute candles
(live_data.details.candlesticks["1M"], each {open_ts_ms, open, high, low, close}). Using it instead
of Coinbase removes the basis difference between the exchange price and the settlement number.
Idea and response shape from the open-source AstroTick bot (index_feed.py).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.data.btc_feed import MinuteBar
from app.data.underlying import SpotPrice

log = logging.getLogger(__name__)


def parse_index_bars(resp: Any) -> list[MinuteBar]:
    """1-minute index candles, ts = END of the minute. [] for an unrecognised shape."""
    if not isinstance(resp, dict):
        return []
    inner = resp.get("live_data", resp)
    details = inner.get("details") if isinstance(inner, dict) else None
    candles = ((details or {}).get("candlesticks") or {}).get("1M") if isinstance(details, dict) else None
    out: dict[int, MinuteBar] = {}
    for c in candles or []:
        if not isinstance(c, dict):
            continue
        try:
            t = int(c["open_ts_ms"]) // 1000
            op, hi, lo, cl = (float(c[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            continue
        if min(op, hi, lo, cl) <= 0:
            continue
        out[t] = MinuteBar(datetime.fromtimestamp(t + 60, UTC), op, hi, lo, cl, 0.0)
    return [out[t] for t in sorted(out)]


def compact(bars: list[MinuteBar]) -> list[list[float]]:
    """Cache format: [[end_unix_seconds, close], ...]."""
    return [[b.ts.timestamp(), b.close] for b in bars]


class SettlementIndexFeed:
    """Latest settlement-index value for an event, cached briefly per event."""

    def __init__(self, ttl_sec: float = 5.0, stale_sec: float = 120.0) -> None:
        self.ttl = ttl_sec
        self.stale = timedelta(seconds=stale_sec)
        self._cache: dict[str, tuple[float, list[MinuteBar]]] = {}
        self.last_error: str | None = None

    async def bars(self, client: Any, event_ticker: str) -> list[MinuteBar]:
        hit = self._cache.get(event_ticker)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        try:
            bars = parse_index_bars(await client.get_event_live_data(event_ticker))
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - any failure falls back to Coinbase
            self.last_error = type(exc).__name__
            log.debug("SETTLEMENT_INDEX_UNAVAILABLE event=%s error=%s", event_ticker, exc)
            bars = []
        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[event_ticker] = (time.monotonic(), bars)
        return bars

    async def latest(self, client: Any, event_ticker: str, now: datetime, asset: str = "BTC") -> SpotPrice | None:
        bars = await self.bars(client, event_ticker)
        if not bars:
            return None
        last = bars[-1]
        start = last.ts - timedelta(seconds=60)
        if now - start > self.stale:  # nothing recent: let the caller fall back
            return None
        return SpotPrice(asset, last.close, min(now, last.ts), "cf_index")
