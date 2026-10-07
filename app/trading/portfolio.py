"""Portfolio: open positions, balances, realized/unrealized P&L and stats."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.database.repository import Repository
from app.kalshi.market_data import MarketSnapshot
from app.risk.manager import RiskContext
from app.trading.positions import Position
from app.utils.logging import log_event
from app.utils.time import utcnow

log = logging.getLogger(__name__)


@dataclass
class PerfStats:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized: float = 0.0
    fees: float = 0.0
    avg_entry: float = 0.0
    avg_result: float = 0.0
    best: float = 0.0
    worst: float = 0.0
    by_asset: dict[str, float] = field(default_factory=dict)
    by_risk: dict[str, float] = field(default_factory=dict)
    by_confidence: dict[str, tuple[int, int, float]] = field(default_factory=dict)  # bucket -> (n, wins, pnl)

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades * 100 if self.trades else None


def confidence_bucket(c: int) -> str:
    if c >= 90:
        return "90+"
    lo = max(50, (c // 10) * 10)
    return f"{lo}-{lo + 9}"


def compute_stats(positions: list[Position]) -> PerfStats:
    st = PerfStats()
    by_asset: dict[str, float] = defaultdict(float)
    by_risk: dict[str, float] = defaultdict(float)
    by_conf: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0.0])
    entries = []
    for p in positions:
        pnl = p.realized_pnl or 0.0
        st.trades += 1
        st.wins += 1 if pnl > 0 else 0
        st.losses += 0 if pnl > 0 else 1
        st.realized += pnl
        st.fees += p.entry_fee + p.exit_fee
        st.best = pnl if st.trades == 1 else max(st.best, pnl)
        st.worst = pnl if st.trades == 1 else min(st.worst, pnl)
        entries.append(p.entry_price)
        by_asset[p.asset] += pnl
        by_risk[p.risk_level] += pnl
        b = by_conf[confidence_bucket(p.confidence)]
        b[0] += 1
        b[1] += 1 if pnl > 0 else 0
        b[2] += pnl
    if st.trades:
        st.avg_entry = sum(entries) / len(entries)
        st.avg_result = st.realized / st.trades
    st.realized = round(st.realized, 2)
    st.by_asset = {k: round(v, 2) for k, v in by_asset.items()}
    st.by_risk = {k: round(v, 2) for k, v in by_risk.items()}
    st.by_confidence = {k: (int(v[0]), int(v[1]), round(v[2], 2)) for k, v in sorted(by_conf.items())}
    return st


class Portfolio:
    def __init__(self, repo: Repository, starting_balance: float) -> None:
        self.repo = repo
        self.starting_balance = starting_balance
        self.positions: dict[int, Position] = {p.id: p for p in repo.open_positions() if p.id}
        self.inflight: set[str] = set()
        self.last_trade_at: dict[str, datetime] = {}
        self.last_loss_at: datetime | None = None
        self.live_balance: float | None = None  # refreshed from Kalshi in live mode

    # ------------------------------------------------------------- queries
    def open_positions(self, mode: str | None = None) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open and (mode is None or p.mode == mode)]

    def paper_balance(self) -> float:
        open_cost = sum(p.cost + p.entry_fee for p in self.open_positions("paper"))
        return round(self.starting_balance + self.repo.realized_total("paper") - open_cost, 2)

    def balance(self, mode: str) -> float:
        if mode == "live":
            return self.live_balance if self.live_balance is not None else 0.0
        return self.paper_balance()

    def exposure(self, mode: str) -> float:
        return round(sum(p.cost + p.entry_fee for p in self.open_positions(mode)), 2)

    def daily_realized(self, mode: str) -> float:
        row = self.repo.daily(mode)
        return row.realized_pnl if row else 0.0

    def unrealized(self, mode: str, snapshots: dict[str, MarketSnapshot]) -> float:
        total = 0.0
        for p in self.open_positions(mode):
            snap = snapshots.get(p.ticker)
            u = p.unrealized_pnl(snap.exit_price(p.side) if snap else None)
            total += u or 0.0
        return round(total, 2)

    def stats(self, mode: str, days: int | None = None) -> PerfStats:
        since = utcnow() - timedelta(days=days) if days is not None else None
        if days == 0:  # "today" in UTC
            now = utcnow()
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return compute_stats(self.repo.closed_positions(mode=mode, since=since, limit=100_000))

    def risk_context(self, *, mode: str, trading_enabled: bool, disabled_reason: str,
                     mode_permitted: bool) -> RiskContext:
        opens = self.open_positions(mode)
        return RiskContext(
            trading_enabled=trading_enabled,
            disabled_reason=disabled_reason,
            mode=mode,
            mode_permitted=mode_permitted,
            balance_usd=self.balance(mode),
            open_positions=len(opens),
            open_exposure_usd=self.exposure(mode),
            open_tickers=frozenset(p.ticker for p in opens),
            inflight_tickers=frozenset(self.inflight),
            daily_realized_pnl=self.daily_realized(mode),
            last_loss_at=self.last_loss_at,
            last_trade_at=dict(self.last_trade_at),
        )

    # ----------------------------------------------------------- mutations
    def add_open(self, p: Position) -> Position:
        self.repo.save_position(p)
        assert p.id is not None
        self.positions[p.id] = p
        self.last_trade_at[p.ticker] = p.opened_at
        self.repo.add_trade(position_id=p.id, mode=p.mode, ticker=p.ticker, action="BUY", side=p.side,
                            contracts=p.contracts, price=p.entry_price, fee=p.entry_fee, ts=p.opened_at)
        log_event(log, "POSITION_OPENED", mode=p.mode, ticker=p.ticker, side=p.side,
                  qty=p.contracts, price=p.entry_price)
        return p

    def close(self, p: Position, exit_price: float, exit_fee: float, reason: str, action: str = "SELL") -> float:
        pnl = p.close(exit_price, exit_fee, reason)
        self.repo.save_position(p)
        self.repo.add_trade(position_id=p.id, mode=p.mode, ticker=p.ticker, action=action, side=p.side,
                            contracts=p.contracts, price=exit_price, fee=exit_fee, ts=p.closed_at)
        self.repo.record_daily(p.mode, pnl, p.entry_fee + p.exit_fee, p.won, p.closed_at)
        if pnl <= 0:
            self.last_loss_at = p.closed_at
        self.positions.pop(p.id, None)  # type: ignore[arg-type]
        log_event(log, "POSITION_CLOSED", mode=p.mode, ticker=p.ticker, pnl=pnl, reason=reason)
        return pnl
