"""Real-time BTC/USD market data (Coinbase Exchange public API) with health status.

* ``/products/BTC-USD/ticker``  -> last trade price, best bid/ask, 24h volume and the
  exchange timestamp of that trade (used for staleness, never our own clock).
* ``/products/BTC-USD/candles?granularity=60`` -> recent 1-minute OHLCV bars, used
  for volatility, momentum, trend, volume and market-structure features.

Coinbase is a *proxy*: Kalshi BTC contracts settle on CF Benchmarks' BRTI, which
aggregates several exchanges. The probability model carries an explicit basis
uncertainty for that reason.

Nothing is fabricated. Invalid prints (non-positive, crossed book, implausible
jumps) are rejected and the feed reports ``STALE``/``DOWN`` so the decision engine
blocks new trades until reliable data returns.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.data.underlying import SpotPrice
from app.utils.time import parse_ts, utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MinuteBar:
    ts: datetime  # END of the 1-minute period (the bar is complete at ts)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class FeedStatus:
    state: str  # CONNECTED | STALE | DOWN | DISABLED | FIXTURE
    source: str
    last_ts: datetime | None
    age_sec: float | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.state in ("CONNECTED", "FIXTURE")

    @property
    def badge(self) -> str:
        return {"CONNECTED": "🟢 CONNECTED", "FIXTURE": "🧪 FIXTURE", "STALE": "🟡 STALE",
                "DOWN": "🔴 DISCONNECTED", "DISABLED": "⚪ DISABLED"}.get(self.state, self.state)


def parse_candles(rows: Any) -> list[MinuteBar]:
    """Coinbase candles: [time, low, high, open, close, volume] (time = period START)."""
    bars: dict[int, MinuteBar] = {}
    if not isinstance(rows, list):
        return []
    for r in rows:
        if not isinstance(r, list) or len(r) < 6:
            continue
        try:
            t, lo, hi, op, cl, vol = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        except (TypeError, ValueError):
            continue
        if min(lo, hi, op, cl) <= 0 or lo > hi or vol < 0:
            continue
        bars[t] = MinuteBar(datetime.fromtimestamp(t + 60, UTC), op, hi, lo, cl, vol)
    return [bars[t] for t in sorted(bars)]


def bars_to_spot(bars: list[MinuteBar], asset: str = "BTC", source: str = "coinbase") -> list[SpotPrice]:
    return [SpotPrice(asset, b.close, b.ts, source) for b in bars]


class BTCFeed:
    """Coinbase Exchange BTC-USD feed. Implements the UnderlyingPriceProvider protocol."""

    name = "coinbase_exchange"
    BASE = "https://api.exchange.coinbase.com/products/BTC-USD"
    MAX_JUMP = 0.05  # reject a print >5% away from the previous one (data error, not a market move)

    def __init__(self, stale_after_sec: float = 15.0, timeout: float = 5.0, bar_refresh_sec: float = 30.0,
                 http: httpx.AsyncClient | None = None, clock: Any = utcnow) -> None:
        self.stale_after_sec = stale_after_sec
        self.bar_refresh_sec = bar_refresh_sec
        self._http = http or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "jgapicks-bot"})
        self.clock = clock
        self._last: SpotPrice | None = None
        self._bid: float | None = None
        self._ask: float | None = None
        self._ticks: deque[SpotPrice] = deque(maxlen=900)
        self._bars: deque[MinuteBar] = deque(maxlen=360)
        self._bars_at: datetime | None = None
        self._error: str | None = None
        self._failures = 0

    # ------------------------------------------------------------ fetching
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = await self._http.get(self.BASE + path, params=params)
        r.raise_for_status()
        return r.json()

    async def refresh_bars(self, force: bool = False) -> None:
        now = self.clock()
        if not force and self._bars_at and (now - self._bars_at).total_seconds() < self.bar_refresh_sec:
            return
        try:
            bars = parse_candles(await self._get("/candles", {"granularity": 60}))
        except (httpx.HTTPError, ValueError) as exc:
            self._error = f"candles: {type(exc).__name__}"
            log.warning("BTC_CANDLES_UNAVAILABLE error=%s", type(exc).__name__)
            return
        complete = [b for b in bars if b.ts <= now]  # drop the still-forming minute
        known = {b.ts for b in self._bars}
        for b in complete:
            if b.ts not in known:
                self._bars.append(b)
        self._bars = deque(sorted(self._bars, key=lambda b: b.ts), maxlen=self._bars.maxlen)
        self._bars_at = now

    async def get_price(self, asset: str) -> SpotPrice | None:
        if asset.upper() != "BTC":
            return None
        await self.refresh_bars()
        try:
            data = await self._get("/ticker")
            price = float(data["price"])
            bid = float(data["bid"]) if data.get("bid") else None
            ask = float(data["ask"]) if data.get("ask") else None
            ts = parse_ts(data.get("time")) or None
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            self._failures += 1
            self._error = f"ticker: {type(exc).__name__}"
            log.warning("BTC_TICKER_UNAVAILABLE error=%s", type(exc).__name__)
            return self.current()
        problem = self.validate(price, bid, ask, ts)
        if problem:
            self._error = problem
            log.warning("BTC_TICK_REJECTED reason=%s", problem)
            return self.current()
        assert ts is not None
        sp = SpotPrice("BTC", price, ts, "coinbase")
        self._last, self._bid, self._ask = sp, bid, ask
        self._ticks.append(sp)
        self._failures, self._error = 0, None
        return sp

    def validate(self, price: float, bid: float | None, ask: float | None, ts: datetime | None) -> str | None:
        if not math.isfinite(price) or price <= 0:
            return "non-positive price"
        if ts is None:
            return "missing exchange timestamp"
        if bid is not None and ask is not None and bid > ask:
            return "crossed bid/ask"
        if self._last is not None and abs(price / self._last.price - 1) > self.MAX_JUMP:
            return f"implausible jump {price / self._last.price - 1:+.1%}"
        if (ts - self.clock()).total_seconds() > 5:
            return "timestamp in the future"
        return None

    # ------------------------------------------------------------- reading
    def current(self) -> SpotPrice | None:
        """Last good price, or None once it is older than the staleness limit."""
        if self._last is None:
            return None
        return self._last if (self.clock() - self._last.ts).total_seconds() <= self.stale_after_sec else None

    def history(self, asset: str = "BTC") -> list[SpotPrice]:
        """Completed 1-minute closes followed by recent ticks (time-ordered, no duplicates)."""
        if asset.upper() != "BTC":
            return []
        out = bars_to_spot(list(self._bars))
        last_bar = out[-1].ts if out else None
        out += [t for t in self._ticks if last_bar is None or t.ts > last_bar]
        return out

    def bars(self) -> list[MinuteBar]:
        return list(self._bars)

    @property
    def spread(self) -> float | None:
        return (self._ask - self._bid) if self._ask is not None and self._bid is not None else None

    def status(self) -> FeedStatus:
        now = self.clock()
        if self._last is None:
            return FeedStatus("DOWN", "Coinbase", None, None, self._error or "no data yet")
        age = (now - self._last.ts).total_seconds()
        if age > self.stale_after_sec:
            return FeedStatus("STALE" if self._failures < 3 else "DOWN", "Coinbase", self._last.ts, age, self._error)
        return FeedStatus("CONNECTED", "Coinbase", self._last.ts, age, self._error)

    async def close(self) -> None:
        await self._http.aclose()


class FixtureBTCFeed:
    """Deterministic synthetic BTC feed for DATA_SOURCE=fixture (always labelled 🧪)."""

    name = "fixture_btc"

    def __init__(self, clock: Any = utcnow, start_price: float = 60000.0, drift_per_min: float = 0.0001,
                 vol_per_min: float = 0.0008, minutes: int = 240) -> None:
        self.clock = clock
        self.start_price = start_price
        self.drift = drift_per_min
        self.vol = vol_per_min
        self.minutes = minutes

    def _price_at(self, ts: datetime) -> float:
        m = ts.timestamp() / 60
        wiggle = self.vol * (math.sin(m * 1.7) + 0.6 * math.sin(m * 0.37) + 0.3 * math.sin(m * 5.3))
        return self.start_price * math.exp(self.drift * (m % 600) + wiggle)

    def bars(self) -> list[MinuteBar]:
        end = self.clock().replace(second=0, microsecond=0)
        out = []
        for i in range(self.minutes, 0, -1):
            t = end - timedelta(minutes=i - 1)
            o, c = self._price_at(t - timedelta(minutes=1)), self._price_at(t)
            out.append(MinuteBar(t, o, max(o, c) * 1.0002, min(o, c) * 0.9998, c, 5 + (i % 7)))
        return out

    async def get_price(self, asset: str) -> SpotPrice | None:
        if asset.upper() != "BTC":
            return None
        now = self.clock()
        return SpotPrice("BTC", self._price_at(now), now, "fixture")

    def current(self) -> SpotPrice | None:
        now = self.clock()
        return SpotPrice("BTC", self._price_at(now), now, "fixture")

    def history(self, asset: str = "BTC") -> list[SpotPrice]:
        return bars_to_spot(self.bars(), source="fixture") if asset.upper() == "BTC" else []

    spread = None

    def status(self) -> FeedStatus:
        return FeedStatus("FIXTURE", "synthetic", self.clock(), 0.0)

    async def close(self) -> None:
        return None
