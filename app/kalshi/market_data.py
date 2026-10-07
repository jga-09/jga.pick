"""Normalized internal market-data model + REST polling service.

All prices are stored as YES/NO *cents* (float, so sub-penny ticks survive).
Nothing here is ever fabricated: a missing field stays ``None``.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from app.errors import MalformedResponseError
from app.utils.time import parse_ts, seconds_until, utcnow

log = logging.getLogger(__name__)

ASSET_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("BTC", "BITCOIN"),
    "ETH": ("ETH", "ETHEREUM", "ETHER"),
    "SOL": ("SOL", "SOLANA"),
    "XRP": ("XRP", "RIPPLE"),
    "DOGE": ("DOGE", "DOGECOIN"),
}
ASSET_ICONS = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "XRP": "✕", "DOGE": "Ð"}


def dollars_to_cents(value: Any) -> float | None:
    """'0.6400' -> 64.0. Returns None for missing/invalid values."""
    if value is None or value == "":
        return None
    try:
        return round(float(value) * 100, 4)
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price(raw: dict[str, Any], dollars_key: str, legacy_key: str) -> float | None:
    cents = dollars_to_cents(raw.get(dollars_key))
    if cents is None and isinstance(raw.get(legacy_key), (int, float)):
        cents = float(raw[legacy_key])  # legacy integer-cents field
    return cents


@dataclass(frozen=True)
class MarketInfo:
    """Static/slow-changing market metadata used for discovery and display."""

    ticker: str
    event_ticker: str
    series_ticker: str
    asset: str
    title: str
    subtitle: str
    open_time: datetime | None
    close_time: datetime
    expiration_time: datetime | None
    status: str
    floor_strike: float | None = None
    cap_strike: float | None = None
    strike_type: str | None = None
    result: str = ""

    @property
    def duration_sec(self) -> float | None:
        if self.open_time is None:
            return None
        return (self.close_time - self.open_time).total_seconds()

    @property
    def window_minutes(self) -> int:
        m = re.search(r"(\d+)M$", self.series_ticker.upper())
        if m:
            return int(m.group(1))
        return max(1, round((self.duration_sec or 900) / 60))

    @property
    def label(self) -> str:
        return f"{self.asset} {self.window_minutes}M"

    @property
    def icon(self) -> str:
        return ASSET_ICONS.get(self.asset, "🎯")

    def time_remaining(self, now: datetime | None = None) -> float:
        return max(0.0, seconds_until(self.close_time, now))


@dataclass(frozen=True)
class OrderBook:
    """Kalshi books contain only bids: YES bids and NO bids, as (cents, size)."""

    yes_bids: tuple[tuple[float, float], ...] = ()
    no_bids: tuple[tuple[float, float], ...] = ()

    @property
    def best_yes_bid(self) -> float | None:
        return max((p for p, _ in self.yes_bids), default=None)

    @property
    def best_no_bid(self) -> float | None:
        return max((p for p, _ in self.no_bids), default=None)

    def depth(self, side: str, within_cents: float = 10.0) -> float:
        levels = self.yes_bids if side == "yes" else self.no_bids
        best = max((p for p, _ in levels), default=None)
        if best is None:
            return 0.0
        return sum(s for p, s in levels if p >= best - within_cents)

    def size_at_best(self, side: str) -> float:
        levels = self.yes_bids if side == "yes" else self.no_bids
        best = max((p for p, _ in levels), default=None)
        return sum(s for p, s in levels if p == best) if best is not None else 0.0


@dataclass(frozen=True)
class TradePrint:
    trade_id: str
    yes_price: float  # cents
    count: float
    taker_side: str  # 'yes' or 'no' (outcome side the taker is positioned for)
    ts: datetime


@dataclass(frozen=True)
class MarketSnapshot:
    info: MarketInfo
    ts: datetime
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    last_price: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    orderbook: OrderBook | None = None
    recent_trades: tuple[TradePrint, ...] = ()
    source: str = "rest"

    @property
    def ticker(self) -> str:
        return self.info.ticker

    @property
    def yes_mid(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        return self.last_price

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    def entry_price(self, side: str) -> float | None:
        """Price we would pay to BUY one contract of ``side`` (the ask)."""
        return self.yes_ask if side == "yes" else self.no_ask

    def exit_price(self, side: str) -> float | None:
        """Price we would receive to SELL one contract of ``side`` (the bid)."""
        return self.yes_bid if side == "yes" else self.no_bid

    def ask_liquidity(self, side: str) -> float:
        """Contracts available at the best ask for ``side`` (opposite side's best bid)."""
        if not self.orderbook:
            return 0.0
        return self.orderbook.size_at_best("no" if side == "yes" else "yes")

    def age_sec(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.ts).total_seconds()

    def time_remaining(self, now: datetime | None = None) -> float:
        return self.info.time_remaining(now)


# ---------------------------------------------------------------- parsing
def parse_market_info(raw: dict[str, Any], *, asset: str, series_ticker: str) -> MarketInfo:
    try:
        close_time = parse_ts(raw["close_time"])
        if close_time is None:
            raise KeyError("close_time")
        return MarketInfo(
            ticker=str(raw["ticker"]),
            event_ticker=str(raw.get("event_ticker", "")),
            series_ticker=series_ticker,
            asset=asset,
            title=str(raw.get("title") or ""),
            subtitle=str(raw.get("yes_sub_title") or raw.get("subtitle") or ""),
            open_time=parse_ts(raw.get("open_time")),
            close_time=close_time,
            expiration_time=parse_ts(raw.get("expected_expiration_time") or raw.get("expiration_time")),
            status=str(raw.get("status", "")),
            floor_strike=to_float(raw.get("floor_strike")),
            cap_strike=to_float(raw.get("cap_strike")),
            strike_type=raw.get("strike_type"),
            result=str(raw.get("result") or ""),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise MalformedResponseError(f"bad market payload: {exc}") from exc


def parse_quotes(raw: dict[str, Any]) -> dict[str, float | None]:
    yes_bid = _price(raw, "yes_bid_dollars", "yes_bid")
    yes_ask = _price(raw, "yes_ask_dollars", "yes_ask")
    no_bid = _price(raw, "no_bid_dollars", "no_bid")
    no_ask = _price(raw, "no_ask_dollars", "no_ask")
    # A 0 bid or 100 ask means "no order on that side" - not a real price.
    yes_bid = yes_bid if yes_bid and yes_bid > 0 else None
    no_bid = no_bid if no_bid and no_bid > 0 else None
    yes_ask = yes_ask if yes_ask is not None and 0 < yes_ask < 100 else None
    no_ask = no_ask if no_ask is not None and 0 < no_ask < 100 else None
    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last_price": _price(raw, "last_price_dollars", "last_price") or None,
        "volume": to_float(raw.get("volume_fp", raw.get("volume"))),
        "open_interest": to_float(raw.get("open_interest_fp", raw.get("open_interest"))),
    }


def parse_orderbook(raw: dict[str, Any]) -> OrderBook:
    def levels(key: str) -> tuple[tuple[float, float], ...]:
        out = []
        for lvl in raw.get(key) or []:
            if not isinstance(lvl, (list, tuple)) or len(lvl) != 2:
                raise MalformedResponseError(f"bad orderbook level in {key}")
            price, size = dollars_to_cents(lvl[0]), to_float(lvl[1])
            if price is not None and size is not None and size > 0:
                out.append((price, size))
        return tuple(sorted(out))

    return OrderBook(yes_bids=levels("yes_dollars"), no_bids=levels("no_dollars"))


def parse_trade(raw: dict[str, Any]) -> TradePrint | None:
    price = _price(raw, "yes_price_dollars", "yes_price")
    count = to_float(raw.get("count_fp", raw.get("count")))
    side = raw.get("taker_outcome_side") or raw.get("taker_side")
    ts = parse_ts(raw.get("created_time") or raw.get("ts"))
    if price is None or count is None or side not in ("yes", "no") or ts is None:
        return None
    return TradePrint(str(raw.get("trade_id", f"{ts.timestamp()}-{price}")), price, count, side, ts)


def build_snapshot(
    info: MarketInfo,
    market_raw: dict[str, Any],
    book: OrderBook | None,
    trades: tuple[TradePrint, ...],
    ts: datetime | None = None,
) -> MarketSnapshot:
    q = parse_quotes(market_raw)
    # Derive missing quotes from the book when possible (YES ask = 100 - best NO bid).
    if book:
        if q["yes_bid"] is None and book.best_yes_bid is not None:
            q["yes_bid"] = book.best_yes_bid
        if q["no_bid"] is None and book.best_no_bid is not None:
            q["no_bid"] = book.best_no_bid
        if q["yes_ask"] is None and book.best_no_bid is not None:
            q["yes_ask"] = 100 - book.best_no_bid
        if q["no_ask"] is None and book.best_yes_bid is not None:
            q["no_ask"] = 100 - book.best_yes_bid
    status = str(market_raw.get("status") or info.status)
    if status != info.status or market_raw.get("result"):
        info = replace(info, status=status, result=str(market_raw.get("result") or info.result))
    return MarketSnapshot(info=info, ts=ts or utcnow(), orderbook=book, recent_trades=trades, **q)


# ---------------------------------------------------------------- service
@dataclass
class _TradeBuffer:
    seen: set[str] = field(default_factory=set)
    trades: deque[TradePrint] = field(default_factory=lambda: deque(maxlen=300))

    def add(self, t: TradePrint) -> bool:
        if t.trade_id in self.seen:
            return False
        self.seen.add(t.trade_id)
        self.trades.append(t)
        if len(self.seen) > 2000:
            self.seen = {x.trade_id for x in self.trades}
        return True


class MarketDataService:
    """Keeps the latest snapshot per tracked market (REST polling + WS updates)."""

    def __init__(self, client: Any, orderbook_depth: int = 10) -> None:
        self.client = client
        self.depth = orderbook_depth
        self.latest: dict[str, MarketSnapshot] = {}
        self._trades: dict[str, _TradeBuffer] = {}
        self.last_ws_update: datetime | None = None

    def recent_trades(self, ticker: str, window_sec: float = 300) -> tuple[TradePrint, ...]:
        buf = self._trades.get(ticker)
        if not buf:
            return ()
        cutoff = utcnow().timestamp() - window_sec
        return tuple(t for t in buf.trades if t.ts.timestamp() >= cutoff)

    def add_trades(self, ticker: str, trades: list[TradePrint]) -> None:
        buf = self._trades.setdefault(ticker, _TradeBuffer())
        for t in sorted(trades, key=lambda t: t.ts):
            buf.add(t)

    async def poll(self, info: MarketInfo) -> MarketSnapshot:
        """Fetch market quotes, order book and recent trades for one market."""
        market_raw = await self.client.get_market(info.ticker)
        book = parse_orderbook(await self.client.get_orderbook(info.ticker, self.depth))
        min_ts = int(utcnow().timestamp()) - 300
        raw_trades = await self.client.get_trades(info.ticker, limit=100, min_ts=min_ts)
        self.add_trades(info.ticker, [t for t in (parse_trade(r) for r in raw_trades) if t])
        snap = build_snapshot(info, market_raw, book, self.recent_trades(info.ticker))
        self.latest[info.ticker] = snap
        return snap

    def apply_ws_ticker(self, msg: dict[str, Any]) -> MarketSnapshot | None:
        """Merge a WebSocket ``ticker`` message into the latest snapshot (if tracked)."""
        ticker = msg.get("market_ticker")
        prev = self.latest.get(ticker or "")
        if prev is None:
            return None
        q = parse_quotes(msg)
        last = _price(msg, "price_dollars", "price")
        updates = {k: v for k, v in q.items() if v is not None}
        if last is not None:
            updates["last_price"] = last
        if not updates:
            return None
        snap = replace(prev, ts=utcnow(), source="ws", **updates)
        if snap.yes_bid is not None and snap.no_ask is None:
            snap = replace(snap, no_ask=100 - snap.yes_bid)
        if snap.yes_ask is not None and snap.no_bid is None:
            snap = replace(snap, no_bid=100 - snap.yes_ask)
        self.latest[ticker] = snap  # type: ignore[index]
        self.last_ws_update = snap.ts
        return snap

    def apply_ws_trade(self, msg: dict[str, Any]) -> None:
        ticker = msg.get("market_ticker")
        if ticker in self.latest:
            t = parse_trade(msg)
            if t:
                self.add_trades(ticker, [t])

    def drop(self, ticker: str) -> None:
        self.latest.pop(ticker, None)
        self._trades.pop(ticker, None)
