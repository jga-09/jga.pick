"""Position domain object (shared by paper and live)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.utils.time import utcnow


@dataclass
class Position:
    mode: str
    ticker: str
    event_ticker: str
    asset: str
    label: str
    side: str  # 'yes' | 'no'
    contracts: int
    entry_price: float  # cents
    entry_fee: float  # USD
    risk_level: str
    confidence: int = 0
    close_time: datetime | None = None
    opened_at: datetime = field(default_factory=utcnow)
    status: str = "open"  # open | closed
    exit_price: float | None = None
    exit_fee: float = 0.0
    closed_at: datetime | None = None
    realized_pnl: float | None = None
    close_reason: str | None = None
    client_order_id: str = ""
    id: int | None = None

    @property
    def cost(self) -> float:
        return round(self.contracts * self.entry_price / 100, 2)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def mark_value(self, exit_price_cents: float | None) -> float | None:
        if exit_price_cents is None:
            return None
        return round(self.contracts * exit_price_cents / 100, 2)

    def unrealized_pnl(self, exit_price_cents: float | None) -> float | None:
        mv = self.mark_value(exit_price_cents)
        return None if mv is None else round(mv - self.cost - self.entry_fee, 2)

    def close(self, exit_price: float, exit_fee: float, reason: str, when: datetime | None = None) -> float:
        self.exit_price = exit_price
        self.exit_fee = exit_fee
        self.closed_at = when or utcnow()
        self.status = "closed"
        self.close_reason = reason
        self.realized_pnl = round(
            self.contracts * (exit_price - self.entry_price) / 100 - self.entry_fee - exit_fee, 2
        )
        return self.realized_pnl

    @property
    def won(self) -> bool:
        return (self.realized_pnl or 0) > 0
