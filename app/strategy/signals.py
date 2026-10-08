"""Signal result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.strategy.quality import SetupAnalysis


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    WAIT = "WAIT"

    @property
    def emoji(self) -> str:
        return {"UP": "🟢", "DOWN": "🔴", "WAIT": "⚪"}[self.value]

    @property
    def side(self) -> str | None:
        """Kalshi outcome side to buy for this direction."""
        return {"UP": "yes", "DOWN": "no"}.get(self.value)


class Validity(StrEnum):
    VALID = "VALID"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NO_QUOTES = "NO_QUOTES"
    STALE = "STALE"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class Reason:
    positive: bool
    text: str

    def __str__(self) -> str:
        return f"{'+' if self.positive else '-'} {self.text}"


@dataclass(frozen=True)
class SignalResult:
    market_ticker: str
    asset: str
    label: str
    direction: Direction
    leaning: Direction  # raw direction before the confidence threshold is applied
    confidence: int  # 0-100 signal strength; NOT a calibrated win probability
    score: float  # composite score in [-1, 1]
    reasons: tuple[Reason, ...]
    timestamp: datetime
    features: dict[str, float | None] = field(default_factory=dict)
    components: dict[str, float] = field(default_factory=dict)
    validity: Validity = Validity.VALID
    threshold: int = 0
    analysis: SetupAnalysis | None = None

    @property
    def quality(self) -> int | None:
        return self.analysis.quality if self.analysis else None

    @property
    def grade(self) -> str:
        return self.analysis.grade if self.analysis else "NO_TRADE"

    @property
    def recommended_action(self) -> str:
        if self.validity is not Validity.VALID:
            return "NONE"
        return {"UP": "BUY_YES", "DOWN": "BUY_NO"}.get(self.direction.value, "NONE")

    @property
    def is_actionable(self) -> bool:
        return self.recommended_action != "NONE"

    # --- compact labels for the Telegram card -------------------------
    def _label(self, key: str, pos: str, neg: str, neutral: str, weak: float = 0.15) -> str:
        v = self.components.get(key)
        if v is None:
            return "⚪ n/a"
        if v >= weak:
            return f"🟢 {pos}"
        if v <= -weak:
            return f"🔴 {neg}"
        return f"⚪ {neutral}"

    @property
    def momentum_label(self) -> str:
        v = self.components.get("momentum")
        if v is None:
            return "⚪ n/a"
        mag = abs(v)
        strength = "Strong" if mag >= 0.5 else "Moderate" if mag >= 0.2 else "Weak"
        if strength == "Weak":
            return "⚪ Weak"
        return f"{'🟢' if v > 0 else '🔴'} {strength}"

    @property
    def trend_label(self) -> str:
        return self._label("trend", "Up", "Down", "Flat")

    @property
    def book_label(self) -> str:
        return self._label("orderbook", "Buyers", "Sellers", "Mixed", weak=0.2)
