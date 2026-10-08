"""Observation records: what the engine saw at decision time, plus the market result."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.strategy.quality import price_bucket, time_bucket


@dataclass
class Observation:
    ts: datetime
    ticker: str
    asset: str
    close_time: datetime
    minute: int
    time_remaining: float
    direction: str  # UP / DOWN
    confidence: int
    quality: int
    grade: str
    regime: str
    accel_state: str
    underlying_state: str
    stability: float | None
    yes_ask: float | None
    no_ask: float | None
    spread: float | None
    hard_flags: list[str] = field(default_factory=list)
    soft_flags: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    features: dict[str, float] = field(default_factory=dict)
    outcome: str | None = None  # "yes" | "no" once the market settles
    source: str = "live"
    id: int | None = None

    # --- derived -----------------------------------------------------------
    @property
    def side(self) -> str | None:
        return {"UP": "yes", "DOWN": "no"}.get(self.direction)

    @property
    def entry_price(self) -> float | None:
        return self.yes_ask if self.side == "yes" else self.no_ask if self.side == "no" else None

    @property
    def settled(self) -> bool:
        return self.outcome in ("yes", "no")

    @property
    def won(self) -> bool | None:
        return None if not self.settled or self.side is None else self.outcome == self.side

    @property
    def time_bucket(self) -> str:
        return time_bucket(self.time_remaining)

    @property
    def price_bucket(self) -> str:
        return price_bucket(self.entry_price)

    @property
    def flags(self) -> list[str]:
        return [*self.hard_flags, *self.soft_flags]

    def to_row(self) -> dict[str, Any]:
        return {
            "source": self.source, "ts": self.ts, "ticker": self.ticker, "asset": self.asset,
            "close_time": self.close_time, "minute": self.minute, "time_remaining": self.time_remaining,
            "direction": self.direction, "confidence": self.confidence, "quality": self.quality,
            "grade": self.grade, "regime": self.regime, "accel_state": self.accel_state,
            "underlying_state": self.underlying_state, "stability": self.stability, "yes_ask": self.yes_ask,
            "no_ask": self.no_ask, "spread": self.spread, "hard_flags": json.dumps(self.hard_flags),
            "soft_flags": json.dumps(self.soft_flags),
            "components": json.dumps({k: round(v, 4) for k, v in self.components.items()}),
            "features": json.dumps({k: round(v, 5) for k, v in self.features.items() if v is not None}),
            "outcome": self.outcome,
        }

    @classmethod
    def from_row(cls, r: Any) -> Observation:
        return cls(
            id=r.id, source=r.source, ts=r.ts, ticker=r.ticker, asset=r.asset, close_time=r.close_time,
            minute=r.minute, time_remaining=r.time_remaining, direction=r.direction, confidence=r.confidence,
            quality=r.quality, grade=r.grade, regime=r.regime, accel_state=r.accel_state,
            underlying_state=r.underlying_state, stability=r.stability, yes_ask=r.yes_ask, no_ask=r.no_ask,
            spread=r.spread, hard_flags=json.loads(r.hard_flags or "[]"),
            soft_flags=json.loads(r.soft_flags or "[]"), components=json.loads(r.components or "{}"),
            features=json.loads(r.features or "{}"), outcome=r.outcome,
        )


def from_signal(sig: Any, snap: Any, now: datetime, source: str = "live") -> Observation | None:
    """Build an observation from a SignalResult + snapshot (None if not a directional setup)."""
    a = sig.analysis
    if a is None or sig.leaning.value == "WAIT" or sig.validity.value != "VALID":
        return None
    tr = snap.time_remaining(now)
    keep = ("mom_30s", "mom_60s", "mom_180s", "prob_move_300s", "move_since_start", "slope_cpm", "accel",
            "volatility", "efficiency", "book_imbalance", "book_near_imbalance", "book_imbalance_delta",
            "trade_flow", "trade_count_120s", "volume_accel", "strike_dist_pct", "spot_mom_pct", "yes_mid",
            "zmove_60s", "zmove_180s", "zmove_300s", "accel_z", "vol_ratio")
    return Observation(
        ts=now, ticker=snap.ticker, asset=snap.info.asset, close_time=snap.info.close_time, minute=int(tr // 60),
        time_remaining=tr, direction=sig.leaning.value, confidence=sig.confidence, quality=a.quality,
        grade=a.grade, regime=a.regime.value, accel_state=a.accel_state, underlying_state=a.underlying_state,
        stability=a.stability.score, yes_ask=snap.yes_ask, no_ask=snap.no_ask, spread=snap.spread,
        hard_flags=list(a.hard_flags), soft_flags=list(a.soft_flags), components=dict(a.components),
        features={k: sig.features.get(k) for k in keep if sig.features.get(k) is not None}, source=source,
    )
