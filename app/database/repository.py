"""Repository: every database read/write goes through here."""

from __future__ import annotations

import json
import logging
from dataclasses import fields
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.database.db import SCHEMA_VERSION, Database
from app.database.models import (
    BotEventRow,
    ObservationRow,
    TradeContextRow,
    DailyPnlRow,
    LiveOrderRow,
    MarketRow,
    MarketSnapshotRow,
    PaperOrderRow,
    PositionRow,
    RiskDecisionRow,
    SettingRow,
    SignalRow,
    StrategySettingRow,
    TradeRow,
)
from app.kalshi.market_data import MarketInfo, MarketSnapshot
from app.trading.positions import Position
from app.utils.time import day_key, utcnow

log = logging.getLogger(__name__)
_POSITION_FIELDS = [f.name for f in fields(Position)]


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def init(self) -> None:
        self.db.create_all()
        if self.get_setting("schema_version") is None:
            self.set_setting("schema_version", SCHEMA_VERSION)

    # ------------------------------------------------------------ settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.db.session() as s:
            row = s.get(SettingRow, key)
            return json.loads(row.value) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.db.session() as s:
            row = s.get(SettingRow, key)
            if row:
                row.value = json.dumps(value)
            else:
                s.add(SettingRow(key=key, value=json.dumps(value)))

    def get_strategy_settings(self) -> dict[str, Any]:
        with self.db.session() as s:
            return {r.key: json.loads(r.value) for r in s.scalars(select(StrategySettingRow))}

    def set_strategy_setting(self, key: str, value: Any) -> None:
        with self.db.session() as s:
            row = s.get(StrategySettingRow, key)
            if row:
                row.value = json.dumps(value)
            else:
                s.add(StrategySettingRow(key=key, value=json.dumps(value)))

    # ------------------------------------------------------------- markets
    def upsert_market(self, m: MarketInfo) -> None:
        with self.db.session() as s:
            row = s.get(MarketRow, m.ticker)
            if row is None:
                row = MarketRow(ticker=m.ticker)
                s.add(row)
            row.event_ticker, row.series_ticker, row.asset = m.event_ticker, m.series_ticker, m.asset
            row.title, row.open_time, row.close_time = m.title, m.open_time, m.close_time
            row.status, row.result, row.floor_strike = m.status, m.result, m.floor_strike

    def add_snapshot(self, snap: MarketSnapshot, book_imbalance: float | None = None) -> None:
        with self.db.session() as s:
            s.add(MarketSnapshotRow(
                ticker=snap.ticker, ts=snap.ts, yes_bid=snap.yes_bid, yes_ask=snap.yes_ask,
                no_bid=snap.no_bid, no_ask=snap.no_ask, last_price=snap.last_price,
                volume=snap.volume, open_interest=snap.open_interest,
                book_imbalance=book_imbalance, source=snap.source,
            ))

    def add_signal(self, sig: Any) -> None:
        with self.db.session() as s:
            s.add(SignalRow(
                ticker=sig.market_ticker, ts=sig.timestamp, direction=sig.direction.value,
                leaning=sig.leaning.value, confidence=sig.confidence, score=sig.score,
                validity=sig.validity.value, threshold=sig.threshold,
                reasons=json.dumps([str(r) for r in sig.reasons]),
                features=json.dumps({k: v for k, v in sig.features.items() if v is not None}),
            ))

    def add_risk_decision(self, ticker: str, d: Any) -> None:
        with self.db.session() as s:
            s.add(RiskDecisionRow(
                ticker=ticker, ts=d.timestamp, approved=d.approved, reason=d.reason,
                risk_level=d.risk_level.value, side=d.side, contracts=d.position_size,
                price=d.price_cents, failures=json.dumps(list(d.failures)),
            ))

    # -------------------------------------------------------------- orders
    def client_order_exists(self, client_order_id: str) -> bool:
        with self.db.session() as s:
            for model in (PaperOrderRow, LiveOrderRow):
                if s.scalar(select(model.id).where(model.client_order_id == client_order_id)):
                    return True
        return False

    def add_paper_order(self, **kw: Any) -> None:
        with self.db.session() as s:
            s.add(PaperOrderRow(**kw))

    def add_live_order(self, **kw: Any) -> int:
        for k in ("request", "response"):
            if k in kw and not isinstance(kw[k], str):
                kw[k] = json.dumps(kw[k])
        with self.db.session() as s:
            row = LiveOrderRow(**kw)
            s.add(row)
            s.flush()
            return row.id

    def update_live_order(self, client_order_id: str, **kw: Any) -> None:
        if "response" in kw and not isinstance(kw["response"], str):
            kw["response"] = json.dumps(kw["response"])
        with self.db.session() as s:
            row = s.scalar(select(LiveOrderRow).where(LiveOrderRow.client_order_id == client_order_id))
            if row:
                for k, v in kw.items():
                    setattr(row, k, v)

    # ----------------------------------------------------------- positions
    def save_position(self, p: Position) -> int:
        with self.db.session() as s:
            row = s.get(PositionRow, p.id) if p.id else None
            if row is None:
                row = PositionRow()
                s.add(row)
            for name in _POSITION_FIELDS:
                if name != "id":
                    setattr(row, name, getattr(p, name))
            s.flush()
            p.id = row.id
            return row.id

    def _to_position(self, row: PositionRow) -> Position:
        return Position(**{n: getattr(row, n) for n in _POSITION_FIELDS})

    def open_positions(self, mode: str | None = None) -> list[Position]:
        with self.db.session() as s:
            q = select(PositionRow).where(PositionRow.status == "open")
            if mode:
                q = q.where(PositionRow.mode == mode)
            return [self._to_position(r) for r in s.scalars(q.order_by(PositionRow.opened_at))]

    def closed_positions(
        self, *, mode: str | None = None, since: datetime | None = None, limit: int = 20, offset: int = 0
    ) -> list[Position]:
        with self.db.session() as s:
            q = select(PositionRow).where(PositionRow.status == "closed")
            if mode:
                q = q.where(PositionRow.mode == mode)
            if since:
                q = q.where(PositionRow.closed_at >= since)
            q = q.order_by(PositionRow.closed_at.desc()).limit(limit).offset(offset)
            return [self._to_position(r) for r in s.scalars(q)]

    def count_closed(self, mode: str | None = None) -> int:
        with self.db.session() as s:
            q = select(func.count(PositionRow.id)).where(PositionRow.status == "closed")
            if mode:
                q = q.where(PositionRow.mode == mode)
            return int(s.scalar(q) or 0)

    def realized_total(self, mode: str) -> float:
        with self.db.session() as s:
            q = select(func.coalesce(func.sum(PositionRow.realized_pnl), 0.0)).where(
                PositionRow.status == "closed", PositionRow.mode == mode
            )
            return float(s.scalar(q) or 0.0)

    def add_trade(self, **kw: Any) -> None:
        with self.db.session() as s:
            s.add(TradeRow(**kw))

    # ---------------------------------------------------------- daily pnl
    def record_daily(self, mode: str, pnl: float, fees: float, won: bool, when: datetime | None = None) -> None:
        key = day_key(when)
        with self.db.session() as s:
            row = s.get(DailyPnlRow, (key, mode))
            if row is None:
                row = DailyPnlRow(day=key, mode=mode, realized_pnl=0.0, fees=0.0, trades=0, wins=0, losses=0)
                s.add(row)
            row.realized_pnl = round(row.realized_pnl + pnl, 2)
            row.fees = round(row.fees + fees, 2)
            row.trades += 1
            row.wins += 1 if won else 0
            row.losses += 0 if won else 1

    def daily(self, mode: str, day: str | None = None) -> DailyPnlRow | None:
        with self.db.session() as s:
            return s.get(DailyPnlRow, (day or day_key(), mode))

    # -------------------------------------------------------- observations
    def add_observation(self, obs: Any) -> bool:
        """Insert unless this (ticker, minute, source) is already recorded."""
        with self.db.session() as s:
            exists = s.scalar(select(ObservationRow.id).where(
                ObservationRow.ticker == obs.ticker, ObservationRow.minute == obs.minute,
                ObservationRow.source == obs.source))
            if exists:
                return False
            s.add(ObservationRow(**obs.to_row()))
            return True

    def add_observations(self, observations: list[Any]) -> None:
        with self.db.session() as s:
            s.add_all([ObservationRow(**o.to_row()) for o in observations])

    def unlabeled_tickers(self, before: datetime, limit: int = 20) -> list[str]:
        with self.db.session() as s:
            q = (select(ObservationRow.ticker).where(ObservationRow.outcome.is_(None),
                                                     ObservationRow.close_time < before)
                 .group_by(ObservationRow.ticker).limit(limit))
            return list(s.scalars(q))

    def label_observations(self, ticker: str, outcome: str) -> int:
        with self.db.session() as s:
            rows = s.scalars(select(ObservationRow).where(ObservationRow.ticker == ticker,
                                                          ObservationRow.outcome.is_(None))).all()
            for r in rows:
                r.outcome = outcome
            return len(rows)

    def observations(self, *, source: str | None = "live", settled_only: bool = True,
                     since: datetime | None = None) -> list[Any]:
        from app.research.dataset import Observation

        with self.db.session() as s:
            q = select(ObservationRow)
            if source:
                q = q.where(ObservationRow.source == source)
            if settled_only:
                q = q.where(ObservationRow.outcome.is_not(None))
            if since:
                q = q.where(ObservationRow.ts >= since)
            return [Observation.from_row(r) for r in s.scalars(q.order_by(ObservationRow.ts))]

    def count_observations(self, source: str = "live") -> tuple[int, int]:
        """(total, settled)"""
        with self.db.session() as s:
            total = s.scalar(select(func.count(ObservationRow.id)).where(ObservationRow.source == source)) or 0
            settled = s.scalar(select(func.count(ObservationRow.id)).where(
                ObservationRow.source == source, ObservationRow.outcome.is_not(None))) or 0
            return int(total), int(settled)

    # ------------------------------------------------------- trade context
    def save_trade_context(self, position_id: int, ticker: str, **kw: Any) -> None:
        for k in ("entry", "exit", "tags"):
            if k in kw and not isinstance(kw[k], str):
                kw[k] = json.dumps(kw[k])
        with self.db.session() as s:
            row = s.get(TradeContextRow, position_id)
            if row is None:
                row = TradeContextRow(position_id=position_id, ticker=ticker)
                s.add(row)
            for k, v in kw.items():
                setattr(row, k, v)

    def trade_contexts(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.scalars(select(TradeContextRow).where(TradeContextRow.won.is_not(None))
                             .order_by(TradeContextRow.updated_at.desc()).limit(limit))
            return [{"position_id": r.position_id, "ticker": r.ticker, "entry": json.loads(r.entry),
                     "exit": json.loads(r.exit), "tags": json.loads(r.tags), "won": r.won,
                     "summary": r.summary, "ts": r.updated_at} for r in rows]

    def trade_context(self, position_id: int) -> dict[str, Any] | None:
        with self.db.session() as s:
            r = s.get(TradeContextRow, position_id)
            if r is None:
                return None
            return {"entry": json.loads(r.entry), "exit": json.loads(r.exit), "tags": json.loads(r.tags),
                    "won": r.won, "summary": r.summary}

    # ---------------------------------------------------------- paper reset
    def reset_paper(self) -> dict[str, int]:
        """Delete all PAPER trading history (positions, trades, orders, daily P&L, trade notes).

        Research data (observations, signals, snapshots) and settings are kept.
        """
        with self.db.session() as s:
            ids = list(s.scalars(select(PositionRow.id).where(PositionRow.mode == "paper")))
            out = {
                "positions": len(ids),
                "trades": s.execute(delete(TradeRow).where(TradeRow.mode == "paper")).rowcount or 0,
                "paper_orders": s.execute(delete(PaperOrderRow)).rowcount or 0,
                "daily_pnl": s.execute(delete(DailyPnlRow).where(DailyPnlRow.mode == "paper")).rowcount or 0,
                "trade_notes": s.execute(delete(TradeContextRow).where(
                    TradeContextRow.position_id.in_(ids))).rowcount or 0 if ids else 0,
            }
            s.execute(delete(PositionRow).where(PositionRow.mode == "paper"))
        return out

    # -------------------------------------------------------------- events
    def add_event(self, event: str, details: str = "", level: str = "INFO") -> None:
        try:
            with self.db.session() as s:
                s.add(BotEventRow(event=event, details=details[:2000], level=level))
        except SQLAlchemyError:
            log.exception("EVENT_PERSIST_FAILED event=%s", event)

    # ----------------------------------------------------------- retention
    def purge(self, snapshot_days: int, signal_days: int, event_days: int,
              observation_days: int = 365) -> dict[str, int]:
        now = utcnow()
        out: dict[str, int] = {}
        with self.db.session() as s:
            for name, model, col, days in (
                ("snapshots", MarketSnapshotRow, MarketSnapshotRow.ts, snapshot_days),
                ("signals", SignalRow, SignalRow.ts, signal_days),
                ("risk_decisions", RiskDecisionRow, RiskDecisionRow.ts, signal_days),
                ("events", BotEventRow, BotEventRow.ts, event_days),
                ("observations", ObservationRow, ObservationRow.ts, observation_days),
            ):
                res = s.execute(delete(model).where(col < now - timedelta(days=days)))
                out[name] = res.rowcount or 0
        return out
