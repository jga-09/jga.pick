"""One-shot trade tickets: what the user confirms in Telegram.

A ticket can be consumed exactly once - this is the core protection against
duplicate callbacks / double-tapped confirm buttons producing duplicate orders.
"""

from __future__ import annotations

import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.utils.time import utcnow

TICKET_TTL_SEC = 60


@dataclass(frozen=True)
class TradeTicket:
    ticker: str
    label: str
    side: str
    direction: str
    contracts: int
    limit_price_cents: float  # max price accepted (ask + tolerance)
    quoted_price_cents: float  # ask when the ticket was created
    cost_usd: float
    fee_usd: float
    risk_level: str
    confidence: int
    mode: str
    origin: str = "manual"  # manual | auto
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    client_order_id: str = field(default_factory=lambda: f"db-{uuid.uuid4().hex[:24]}")
    created_at: datetime = field(default_factory=utcnow)

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=TICKET_TTL_SEC)

    def expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.expires_at

    @property
    def max_cost(self) -> float:
        return round(self.contracts * self.limit_price_cents / 100 + self.fee_usd, 2)


class TicketStore:
    def __init__(self) -> None:
        self._tickets: dict[str, TradeTicket] = {}
        self._lock = threading.Lock()

    def add(self, t: TradeTicket) -> TradeTicket:
        with self._lock:
            self._purge()
            self._tickets[t.id] = t
        return t

    def get(self, ticket_id: str) -> TradeTicket | None:
        with self._lock:
            t = self._tickets.get(ticket_id)
            return None if t is None or t.expired() else t

    def consume(self, ticket_id: str) -> TradeTicket | None:
        """Atomically remove and return the ticket (None if missing/expired/already used)."""
        with self._lock:
            t = self._tickets.pop(ticket_id, None)
            return None if t is None or t.expired() else t

    def discard(self, ticket_id: str) -> None:
        with self._lock:
            self._tickets.pop(ticket_id, None)

    def _purge(self) -> None:
        now = utcnow()
        for k in [k for k, t in self._tickets.items() if t.expired(now)]:
            del self._tickets[k]
