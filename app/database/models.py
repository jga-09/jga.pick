"""SQLAlchemy ORM tables. All datetimes are stored as UTC."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils.time import utcnow


class UTCDateTime(TypeDecorator):
    """Stores naive UTC in SQLite, always returns aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


class MarketRow(Base):
    __tablename__ = "markets"
    ticker: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_ticker: Mapped[str] = mapped_column(String(80), index=True)
    series_ticker: Mapped[str] = mapped_column(String(40))
    asset: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(Text, default="")
    open_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    close_time: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(20), default="")
    result: Mapped[str] = mapped_column(String(10), default="")
    floor_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(80))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    yes_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    yes_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)
    book_imbalance: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(10), default="rest")
    __table_args__ = (Index("ix_snap_ticker_ts", "ticker", "ts"),)


class SignalRow(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(80))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    direction: Mapped[str] = mapped_column(String(8))
    leaning: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    validity: Mapped[str] = mapped_column(String(24))
    threshold: Mapped[int] = mapped_column(Integer, default=0)
    reasons: Mapped[str] = mapped_column(Text, default="[]")
    features: Mapped[str] = mapped_column(Text, default="{}")
    __table_args__ = (Index("ix_sig_ticker_ts", "ticker", "ts"),)


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    approved: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    risk_level: Mapped[str] = mapped_column(String(10))
    side: Mapped[str | None] = mapped_column(String(4), nullable=True)
    contracts: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    failures: Mapped[str] = mapped_column(Text, default="[]")


class _OrderMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    ticker: Mapped[str] = mapped_column(String(80), index=True)
    side: Mapped[str] = mapped_column(String(4))
    action: Mapped[str] = mapped_column(String(8))
    contracts: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16))
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PaperOrderRow(_OrderMixin, Base):
    __tablename__ = "paper_orders"


class LiveOrderRow(_OrderMixin, Base):
    __tablename__ = "live_orders"
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request: Mapped[str] = mapped_column(Text, default="{}")
    response: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PositionRow(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(8))
    ticker: Mapped[str] = mapped_column(String(80), index=True)
    event_ticker: Mapped[str] = mapped_column(String(80), default="")
    asset: Mapped[str] = mapped_column(String(10))
    label: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(4))
    contracts: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_fee: Mapped[float] = mapped_column(Float, default=0.0)
    risk_level: Mapped[str] = mapped_column(String(10))
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    close_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fee: Mapped[float] = mapped_column(Float, default=0.0)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    client_order_id: Mapped[str] = mapped_column(String(64), default="")


class TradeRow(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(Integer, index=True)
    mode: Mapped[str] = mapped_column(String(8))
    ticker: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(8))  # BUY / SELL / SETTLE
    side: Mapped[str] = mapped_column(String(4))
    contracts: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DailyPnlRow(Base):
    __tablename__ = "daily_pnl"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)


class BotEventRow(Base):
    __tablename__ = "bot_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    event: Mapped[str] = mapped_column(String(40))
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    details: Mapped[str] = mapped_column(Text, default="")


class SettingRow(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class StrategySettingRow(Base):
    __tablename__ = "strategy_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ObservationRow(Base):
    """One graded setup per market per minute, labelled with the market result later.

    This is the research dataset: calibration, regime/time/price analytics,
    feature importance and walk-forward backtests are all computed from it.
    """

    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(8), default="live")  # live | sim
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    ticker: Mapped[str] = mapped_column(String(80), index=True)
    asset: Mapped[str] = mapped_column(String(10))
    close_time: Mapped[datetime] = mapped_column(UTCDateTime)
    minute: Mapped[int] = mapped_column(Integer)  # whole minutes remaining
    time_remaining: Mapped[float] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(String(5))
    confidence: Mapped[int] = mapped_column(Integer)
    quality: Mapped[int] = mapped_column(Integer)
    grade: Mapped[str] = mapped_column(String(10))
    regime: Mapped[str] = mapped_column(String(20))
    accel_state: Mapped[str] = mapped_column(String(14))
    underlying_state: Mapped[str] = mapped_column(String(12))
    stability: Mapped[float | None] = mapped_column(Float, nullable=True)
    yes_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    no_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    hard_flags: Mapped[str] = mapped_column(Text, default="[]")
    soft_flags: Mapped[str] = mapped_column(Text, default="[]")
    components: Mapped[str] = mapped_column(Text, default="{}")
    features: Mapped[str] = mapped_column(Text, default="{}")
    outcome: Mapped[str | None] = mapped_column(String(4), nullable=True, index=True)  # yes | no
    __table_args__ = (Index("ux_obs_ticker_minute", "ticker", "minute", "source", unique=True),)


class TradeContextRow(Base):
    """Entry/exit conditions and win/loss analysis for each position."""

    __tablename__ = "trade_context"
    position_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(80))
    entry: Mapped[str] = mapped_column(Text, default="{}")
    exit: Mapped[str] = mapped_column(Text, default="{}")
    tags: Mapped[str] = mapped_column(Text, default="[]")
    won: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
