"""Reconcile the bot's own records with Kalshi's (live mode only).

* Positions: every open LIVE position the bot tracks is compared with Kalshi's
  GET /portfolio/positions (``position_fp``: positive = YES contracts, negative =
  NO). A mismatch blocks new orders on that market and raises an alert. Positions
  on markets the bot never traded are reported, not blocked (they may be yours).
* Orders: live orders left in a non-final state (``submitting``/``unknown``/
  ``accepted``, e.g. after a crash) are looked up at Kalshi by client_order_id.
  If one turns out to have filled while the bot has no position for it, that
  market is blocked and you are told exactly what to check. The bot never invents
  a position from a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.errors import KalshiError
from app.kalshi.market_data import to_float
from app.trading.live import fill_from_fills, fill_from_order
from app.trading.positions import Position

OPEN_STATES = ("submitting", "unknown", "accepted")


@dataclass(frozen=True)
class Mismatch:
    ticker: str
    local: float  # signed contracts: + YES, - NO
    exchange: float
    tracked: bool  # the bot holds (or thinks it holds) this market

    @property
    def text(self) -> str:
        def fmt(q: float) -> str:
            return "none" if q == 0 else f"{abs(q):g} {'YES' if q > 0 else 'NO'}"
        return f"{self.ticker}: bot {fmt(self.local)} vs Kalshi {fmt(self.exchange)}"


@dataclass
class ReconcileReport:
    at: datetime
    ok: bool = True
    mismatches: list[Mismatch] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)  # order notes
    blocked: set[str] = field(default_factory=set)
    error: str = ""


def signed(side: str, contracts: float) -> float:
    return contracts if side == "yes" else -contracts


def compare_positions(local: list[Position], exchange: list[dict[str, Any]]) -> list[Mismatch]:
    mine: dict[str, float] = {}
    for p in local:
        if p.mode == "live" and p.is_open:
            mine[p.ticker] = mine.get(p.ticker, 0.0) + signed(p.side, p.contracts)
    theirs: dict[str, float] = {}
    for row in exchange:
        q = to_float(row.get("position_fp", row.get("position")))
        if q and row.get("ticker"):
            theirs[str(row["ticker"])] = theirs.get(str(row["ticker"]), 0.0) + q
    out = []
    for t in sorted(set(mine) | set(theirs)):
        a, b = round(mine.get(t, 0.0), 2), round(theirs.get(t, 0.0), 2)
        if a != b:
            out.append(Mismatch(t, a, b, tracked=t in mine))
    return out


async def recover_orders(client: Any, repo: Any) -> tuple[list[str], set[str]]:
    """Resolve live orders left in a non-final state. Returns (notes, tickers to block)."""
    notes: list[str] = []
    block: set[str] = set()
    for row in repo.live_orders(OPEN_STATES):
        coid, ticker = row["client_order_id"], row["ticker"]
        created = row.get("created_at")
        since = int(created.timestamp()) - 300 if isinstance(created, datetime) else None
        try:
            orders = await client.get_orders_all(ticker, min_ts=since)
        except KalshiError as exc:
            notes.append(f"{ticker}: could not check order {coid[:12]}… ({exc}); market blocked")
            block.add(ticker)
            continue
        match = next((o for o in orders if o.get("client_order_id") == coid), None)
        if match is None:
            repo.update_live_order(coid, status="not_placed", error="not found at Kalshi during recovery")
            notes.append(f"{ticker}: {row['action']} order {coid[:12]}… was never placed")
            continue
        oid = str(match.get("order_id", ""))
        fill = None
        try:
            fill = fill_from_fills(await client.get_fills(order_id=oid), row["side"]) if oid else None
        except KalshiError:
            fill = None
        fill = fill if fill is not None and fill.contracts > 0 else (fill_from_order(match, row["side"]) or fill)
        filled = fill.contracts if fill else 0
        status = "no_fill" if filled <= 0 else "filled" if fill and fill.complete else "partial"
        repo.update_live_order(coid, status=status, exchange_order_id=oid, response=match,
                               fee=fill.fee if fill else 0.0)
        if filled > 0 and row.get("position_id") is None and row["action"] == "buy":
            block.add(ticker)
            notes.append(f"⚠️ {ticker}: BUY {row['side'].upper()} order {coid[:12]}… FILLED {filled} contracts "
                         "but the bot has no position for it (crash during submit). Market blocked - check "
                         "Kalshi and close it there if needed.")
        else:
            notes.append(f"{ticker}: {row['action']} order {coid[:12]}… resolved as {status}")
    return notes, block
