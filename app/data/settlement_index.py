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
    if not out and isinstance(details, dict):
        return _generic_series(details)  # other live-data types (e.g. Gold / Pyth) use a different layout
    return [out[t] for t in sorted(out)]


_START_KEYS = ("open_ts_ms", "open_ts", "start_ts", "start_time")
_POINT_KEYS = ("ts_ms", "ts", "timestamp", "time", "t", "end_ts", "end_period_ts", "publish_time")
_PRICE_KEYS = ("close", "price", "value", "c", "p", "y", "close_price")


def _to_unix(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        from app.utils.time import parse_ts

        d = parse_ts(v) if isinstance(v, str) else None
        return d.timestamp() if d else None
    return x / 1000 if x > 1e12 else x


def _generic_series(details: dict[str, Any]) -> list[MinuteBar]:
    """Find the longest list of (time, price) points anywhere in ``details``. Candle START times are shifted
    to the END of the minute; point times are used as given. [] when nothing recognisable is found."""
    best: dict[int, MinuteBar] = {}

    def walk(node: Any) -> None:
        nonlocal best
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
            return
        if not isinstance(node, list) or not node:
            return
        found: dict[int, MinuteBar] = {}
        for item in node:
            t = price = None
            shift = 0
            if isinstance(item, dict):
                k = next((k for k in _START_KEYS if k in item), None)
                if k:
                    t, shift = _to_unix(item[k]), 60
                else:
                    k = next((k for k in _POINT_KEYS if k in item), None)
                    t = _to_unix(item[k]) if k else None
                pk = next((k for k in _PRICE_KEYS if k in item), None)
                price = _float(item.get(pk)) if pk else None
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                t, price = _to_unix(item[0]), _float(item[-1] if len(item) == 2 else item[4] if len(item) > 4 else item[1])
            if t and price and price > 0:
                end = int(t + shift)
                found[end] = MinuteBar(datetime.fromtimestamp(end, UTC), price, price, price, price, 0.0)
        if len(found) > len(best):
            best = found
        for item in node:
            if isinstance(item, (dict, list)):
                walk(item)

    walk(details)
    return [best[t] for t in sorted(best)]


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
