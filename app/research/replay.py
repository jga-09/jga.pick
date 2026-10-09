"""Historical replay: download settled Kalshi 15-minute markets and run them
through the real signal engine to create research observations.

What history exists (official API): settled markets, 1-minute candlesticks
(YES bid/ask OHLC, trade price, volume) and individual trades with taker side.
What does NOT exist: order-book history. So replayed observations have no
order-book component and only 1-minute quote resolution. Strategies that need
the order book (V3, V4) cannot be replayed; V4L (V4 without the order book)
can. Underlying spot comes from Coinbase public 1-minute candles (a proxy -
Kalshi settles on its own index).

Raw downloads are cached as JSON so re-runs are instant.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.data.underlying import SpotPrice
from app.errors import KalshiError, KalshiHTTPError, MalformedResponseError
from app.kalshi.market_data import MarketInfo, MarketSnapshot, TradePrint, dollars_to_cents, parse_market_info, parse_trade, to_float
from app.research.dataset import Observation, from_signal
from app.strategy.engine import SignalEngine
from app.utils.time import parse_ts

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ parsing
def _dist(c: dict[str, Any], key: str, part: str = "close") -> float | None:
    """Candlestick distributions use '<part>_dollars' (live) or '<part>' (historical)."""
    d = c.get(key)
    if not isinstance(d, dict):
        return None
    return dollars_to_cents(d.get(f"{part}_dollars", d.get(part)))


@dataclass(frozen=True)
class Candle:
    ts: datetime  # end of the 1-minute period
    yes_bid: float | None
    yes_ask: float | None
    price: float | None
    volume: float | None


def parse_candles(raw: list[dict[str, Any]]) -> list[Candle]:
    out = []
    for c in raw:
        ts = parse_ts(c.get("end_period_ts"))
        if ts is None:
            continue
        bid, ask = _dist(c, "yes_bid"), _dist(c, "yes_ask")
        bid = bid if bid and bid > 0 else None  # 0 bid / 100 ask = no quote on that side
        ask = ask if ask is not None and 0 < ask < 100 else None
        out.append(Candle(ts, bid, ask, _dist(c, "price"),
                          to_float(c.get("volume_fp", c.get("volume")))))
    return sorted(out, key=lambda c: c.ts)


def parse_coinbase(raw: list[list[float]], asset: str) -> list[SpotPrice]:
    """Coinbase Exchange candles: [time, low, high, open, close, volume]; use the close.

    Request windows are inclusive at both ends, so adjacent chunks can repeat a
    minute - de-duplicate by timestamp."""
    by_ts: dict[int, SpotPrice] = {}
    for row in raw:
        if isinstance(row, list) and len(row) >= 5:
            t = int(row[0])
            by_ts[t] = SpotPrice(asset, float(row[4]), datetime.fromtimestamp(t + 60, UTC), "coinbase")
    return [by_ts[t] for t in sorted(by_ts)]


# ---------------------------------------------------------------- downloader
class HistoryDownloader:
    def __init__(self, client: Any, cache_dir: str | Path, concurrency: int = 6) -> None:
        self.client = client
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.sem = asyncio.Semaphore(concurrency)

    async def settled_markets(self, series: str, since: datetime, until: datetime,
                              max_pages: int = 40) -> list[dict[str, Any]]:
        """Settled markets of a series closing in [since, until] (live + historical endpoints)."""
        found: dict[str, dict[str, Any]] = {}
        try:
            for m in await self.client.get_markets(series_ticker=series, status="settled", limit=1000,
                                                   max_pages=max_pages):
                found[m["ticker"]] = m
        except KalshiError as exc:
            log.warning("REPLAY_LIST_FAILED series=%s error=%s", series, exc)
        cursor: str | None = None
        for _ in range(max_pages):  # older markets live behind the historical cutoff
            try:
                data = await self.client.get("/historical/markets",
                                             {"series_ticker": series, "limit": 1000, "cursor": cursor})
            except KalshiError as exc:
                log.warning("REPLAY_HISTORICAL_LIST_FAILED series=%s error=%s", series, exc)
                break
            page = [m for m in data.get("markets") or [] if isinstance(m, dict)]
            for m in page:
                found.setdefault(m.get("ticker", ""), m)
            closes = [parse_ts(m.get("close_time")) for m in page]
            cursor = data.get("cursor") or None
            if not cursor or not page or all(c is not None and c < since for c in closes):
                break
        out = []
        for m in found.values():
            ct = parse_ts(m.get("close_time"))
            if ct is not None and since <= ct <= until and m.get("result") in ("yes", "no"):
                out.append(m)
        return sorted(out, key=lambda m: m["close_time"])

    async def market_data(self, series: str, market: dict[str, Any]) -> dict[str, Any] | None:
        ticker = market["ticker"]
        path = self.cache / f"{ticker}.json"
        if path.exists():
            return json.loads(path.read_text())
        async with self.sem:
            open_t, close_t = parse_ts(market.get("open_time")), parse_ts(market.get("close_time"))
            if open_t is None or close_t is None:
                return None
            start, end = int(open_t.timestamp()), int(close_t.timestamp())
            try:
                candles = await self._candles(series, ticker, start, end)
                trades = await self._trades(ticker, start, end)
            except KalshiError as exc:
                log.warning("REPLAY_DOWNLOAD_FAILED ticker=%s error=%s", ticker, exc)
                return None
        record = {"market": market, "series": series, "candles": candles, "trades": trades}
        path.write_text(json.dumps(record))
        return record

    async def _candles(self, series: str, ticker: str, start: int, end: int) -> list[dict[str, Any]]:
        params = {"start_ts": start - 60, "end_ts": end, "period_interval": 1}
        try:
            data = await self.client.get(f"/series/{series}/markets/{ticker}/candlesticks", params)
            if data.get("candlesticks"):
                return data["candlesticks"]
        except KalshiHTTPError as exc:
            if exc.status_code not in (400, 404):
                raise
        data = await self.client.get(f"/historical/markets/{ticker}/candlesticks", params)
        return data.get("candlesticks") or []

    async def _trades(self, ticker: str, start: int, end: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in ("/markets/trades", "/historical/trades"):
            cursor: str | None = None
            for _ in range(20):
                try:
                    data = await self.client.get(path, {"ticker": ticker, "min_ts": start, "max_ts": end,
                                                        "limit": 1000, "cursor": cursor})
                except KalshiHTTPError as exc:
                    if exc.status_code in (400, 404):
                        break
                    raise
                out.extend(t for t in data.get("trades") or [] if isinstance(t, dict))
                cursor = data.get("cursor") or None
                if not cursor:
                    break
            if out:
                return out
        return out


class CoinbaseHistory:
    """Public Coinbase Exchange 1-minute candles (max 300 per request), cached per asset/day."""

    URL = "https://api.exchange.coinbase.com/products/{asset}-USD/candles"

    def __init__(self, cache_dir: str | Path, http: httpx.AsyncClient | None = None) -> None:
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.http = http or httpx.AsyncClient(timeout=15, headers={"User-Agent": "jgapicks-research"})
        self.available = True

    async def day(self, asset: str, day: datetime) -> list[SpotPrice]:
        key = self.cache / f"spot_{asset}_{day:%Y%m%d}.json"
        if key.exists():
            return parse_coinbase(json.loads(key.read_text()), asset)
        if not self.available:
            return []
        rows: list[list[float]] = []
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(0, 1440, 300):
            a, b = start + timedelta(minutes=i), start + timedelta(minutes=min(i + 300, 1440))
            try:
                r = await self.http.get(self.URL.format(asset=asset), params={
                    "granularity": 60, "start": a.isoformat(), "end": b.isoformat()})
                r.raise_for_status()
                rows.extend(r.json())
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("SPOT_HISTORY_UNAVAILABLE asset=%s error=%s", asset, type(exc).__name__)
                self.available = False
                return []
            await asyncio.sleep(0.35)  # stay well under the public rate limit
        if day.date() < datetime.now(UTC).date():  # only cache complete days
            key.write_text(json.dumps(rows))
        return parse_coinbase(rows, asset)

    async def close(self) -> None:
        await self.http.aclose()


# ------------------------------------------------------------------- replay
@dataclass
class ReplayStats:
    markets: int = 0
    observations: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def replay_market(record: dict[str, Any], asset: str, spot: list[SpotPrice],
                  engine_factory: Callable[[], SignalEngine] = SignalEngine) -> list[Observation]:
    """Replay one settled market minute by minute through the real engine."""
    market = record["market"]
    try:
        info: MarketInfo = parse_market_info(dict(market, status="active"), asset=asset,
                                             series_ticker=record["series"])
    except MalformedResponseError:
        return []
    result = market.get("result")
    candles = [c for c in parse_candles(record.get("candles") or [])
               if info.open_time is None or info.open_time < c.ts <= info.close_time]
    trades: list[TradePrint] = sorted((t for t in (parse_trade(x) for x in record.get("trades") or []) if t),
                                      key=lambda t: t.ts)
    engine = engine_factory()
    out: list[Observation] = []
    seen: set[int] = set()
    for c in candles:
        if c.yes_bid is None and c.yes_ask is None:
            continue
        window = tuple(t for t in trades if c.ts - timedelta(seconds=300) < t.ts <= c.ts)
        snap = MarketSnapshot(
            info=info, ts=c.ts, yes_bid=c.yes_bid, yes_ask=c.yes_ask,
            no_bid=(100 - c.yes_ask) if c.yes_ask is not None else None,
            no_ask=(100 - c.yes_bid) if c.yes_bid is not None else None,
            last_price=c.price, volume=c.volume, orderbook=None, recent_trades=window, source="replay",
        )
        hist = [s for s in spot if c.ts - timedelta(seconds=300) <= s.ts <= c.ts]
        sig = engine.evaluate(snap, now=c.ts, spot=hist[-1] if hist else None, spot_history=hist)
        o = from_signal(sig, snap, c.ts, source="replay")
        if o is not None and o.minute not in seen:
            seen.add(o.minute)
            o.outcome = result
            out.append(o)
    return out


def replay_engine() -> SignalEngine:
    # 1-minute candles: require 2 minutes of history before a signal counts as valid.
    return SignalEngine(min_history_sec=120, stale_after_sec=120)
