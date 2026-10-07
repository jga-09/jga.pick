"""Deterministic fixture market data for dry runs, smoke tests and unit tests.

This is *synthetic* data and is always labelled as such in the UI
("🧪 FIXTURE DATA"). It is never used when DATA_SOURCE=kalshi.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from app.kalshi.market_data import (
    MarketInfo,
    MarketSnapshot,
    OrderBook,
    TradePrint,
    build_snapshot,
)
from app.utils.time import utcnow

WINDOW_SEC = 900
FIXTURE_TRENDS = {"BTC": "up", "ETH": "flat", "SOL": "down"}


def window_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    return start, start + timedelta(seconds=WINDOW_SEC)


def fixture_mid(trend: str, t: float) -> float:
    """YES mid (cents) as a function of seconds since window open."""
    if trend == "up":
        v = 50 + 40 * (t / WINDOW_SEC) + 0.4 * math.sin(t / 7)
    elif trend == "down":
        v = 50 - 40 * (t / WINDOW_SEC) + 0.4 * math.sin(t / 7)
    else:
        v = 51 + 1.2 * math.sin(t / 25)
    return max(3.0, min(97.0, round(v)))


def fixture_info(asset: str, now: datetime) -> MarketInfo:
    start, end = window_bounds(now)
    series = f"KX{asset}15M"
    stamp = end.strftime("%y%b%d%H%M").upper()
    return MarketInfo(
        ticker=f"{series}-{stamp}", event_ticker=f"{series}-{stamp}", series_ticker=series, asset=asset,
        title=f"{asset} price up in next 15 mins?", subtitle="Fixture market", open_time=start,
        close_time=end, expiration_time=end, status="active",
    )


def fixture_market_raw(info: MarketInfo, mid: float, spread: float = 2.0) -> dict[str, Any]:
    yes_bid, yes_ask = mid - spread / 2, mid + spread / 2
    return {
        "ticker": info.ticker, "event_ticker": info.event_ticker, "title": info.title,
        "yes_sub_title": info.subtitle, "open_time": info.open_time.isoformat() if info.open_time else None,
        "close_time": info.close_time.isoformat(), "status": info.status,
        "yes_bid_dollars": f"{yes_bid / 100:.4f}", "yes_ask_dollars": f"{yes_ask / 100:.4f}",
        "no_bid_dollars": f"{(100 - yes_ask) / 100:.4f}", "no_ask_dollars": f"{(100 - yes_bid) / 100:.4f}",
        "last_price_dollars": f"{mid / 100:.4f}", "volume_fp": "1520.00", "open_interest_fp": "830.00",
        "result": "",
    }


def fixture_book(mid: float, trend: str, spread: float = 2.0) -> OrderBook:
    yes_w, no_w = {"up": (3.0, 1.0), "down": (1.0, 3.0)}.get(trend, (1.0, 1.0))
    yes_best, no_best = mid - spread / 2, 100 - (mid + spread / 2)
    yes = tuple((yes_best - i, 40 * yes_w) for i in range(5))
    no = tuple((no_best - i, 40 * no_w) for i in range(5))
    return OrderBook(yes_bids=tuple(sorted(yes)), no_bids=tuple(sorted(no)))


def fixture_trades(trend: str, now: datetime, n: int = 8) -> tuple[TradePrint, ...]:
    out = []
    for i in range(n):
        if trend == "up":
            side = "yes" if i % 4 else "no"
        elif trend == "down":
            side = "no" if i % 4 else "yes"
        else:
            side = "yes" if i % 2 else "no"
        ts = now - timedelta(seconds=10 * i)
        out.append(TradePrint(f"fx-{int(ts.timestamp())}-{i}", 50.0, 10.0, side, ts))
    return tuple(sorted(out, key=lambda t: t.ts))


def make_snapshot(asset: str, trend: str, now: datetime, *, info: MarketInfo | None = None,
                  spread: float = 2.0, ts: datetime | None = None) -> MarketSnapshot:
    info = info or fixture_info(asset, now)
    t = (now - info.open_time).total_seconds() if info.open_time else 0
    mid = fixture_mid(trend, t)
    raw = fixture_market_raw(info, mid, spread)
    return build_snapshot(info, raw, fixture_book(mid, trend, spread), fixture_trades(trend, now), ts=ts or now)


class FixtureKalshiClient:
    """Drop-in replacement for KalshiClient's read API. Cannot place orders.

    Markets for the current 15-minute window are generated per asset; tests can
    also ``register`` custom markets. After close a market reports a result
    derived from its fixture path (finalized).
    """

    signer = None
    auth_failed = False
    consecutive_failures = 0

    def __init__(self, clock: Any = utcnow) -> None:
        self.clock = clock
        self.last_success: float | None = None
        self._markets: dict[str, tuple[MarketInfo, str]] = {}

    @property
    def healthy(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    def register(self, info: MarketInfo, trend: str) -> None:
        self._markets[info.ticker] = (info, trend)

    def _t(self, info: MarketInfo, now: datetime) -> float:
        return (now - info.open_time).total_seconds() if info.open_time else 0.0

    async def get_series_list(self, category: str | None = None) -> list[dict[str, Any]]:
        return [{"ticker": f"KX{a}15M", "title": f"{a} price up/down 15 min", "frequency": "15 min"}
                for a in FIXTURE_TRENDS]

    async def get_markets(self, *, series_ticker: str | None = None, **_: Any) -> list[dict[str, Any]]:
        now = self.clock()
        out = []
        for asset, trend in FIXTURE_TRENDS.items():
            if series_ticker and series_ticker != f"KX{asset}15M":
                continue
            info = fixture_info(asset, now)
            self.register(info, trend)
            out.append(fixture_market_raw(info, fixture_mid(trend, self._t(info, now))))
        return out

    def _lookup(self, ticker: str) -> tuple[MarketInfo, str, datetime]:
        if ticker not in self._markets:
            from app.errors import KalshiHTTPError

            raise KalshiHTTPError(404, "market not found", f"/markets/{ticker}")
        info, trend = self._markets[ticker]
        return info, trend, self.clock()

    async def get_market(self, ticker: str) -> dict[str, Any]:
        info, trend, now = self._lookup(ticker)
        if now >= info.close_time:
            final = fixture_mid(trend, (info.close_time - info.open_time).total_seconds() if info.open_time else 0)
            return {**fixture_market_raw(info, final), "status": "finalized",
                    "result": "yes" if final > 50 else "no"}
        return fixture_market_raw(info, fixture_mid(trend, self._t(info, now)))

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        info, trend, now = self._lookup(ticker)
        book = fixture_book(fixture_mid(trend, self._t(info, now)), trend)
        return {
            "yes_dollars": [[f"{p / 100:.4f}", f"{s:.2f}"] for p, s in book.yes_bids],
            "no_dollars": [[f"{p / 100:.4f}", f"{s:.2f}"] for p, s in book.no_bids],
        }

    async def get_trades(self, ticker: str, **_: Any) -> list[dict[str, Any]]:
        _, trend, now = self._lookup(ticker)
        return [{"trade_id": t.trade_id, "yes_price_dollars": "0.5000", "count_fp": "10.00",
                 "taker_outcome_side": t.taker_side, "created_time": t.ts.isoformat()}
                for t in fixture_trades(trend, now)]

    async def get_balance(self) -> dict[str, Any]:
        raise NotImplementedError("fixture client has no account")

    async def create_order_v2(self, body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("fixture client can never submit orders")
