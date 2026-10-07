"""Signal engine: market data (+ optional underlying) -> SignalResult."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from app.data.underlying import SpotPrice
from app.kalshi.market_data import MarketSnapshot
from app.strategy.features import PricePoint, compute_features
from app.strategy.scoring import NEUTRAL_BAND, score_features
from app.strategy.signals import Direction, Reason, SignalResult, Validity
from app.utils.logging import log_event
from app.utils.time import utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalFlip:
    ticker: str
    label: str
    previous: SignalResult
    current: SignalResult


class SignalEngine:
    MIN_SAMPLE_SPACING_SEC = 1.0

    def __init__(
        self,
        history_size: int = 240,
        min_confidence: int = 55,
        stale_after_sec: float = 30.0,
        weights: dict[str, float] | None = None,
        min_history_sec: float = 60.0,
    ) -> None:
        self.min_history_sec = min_history_sec
        self.history_size = history_size
        self.min_confidence = min_confidence
        self.stale_after_sec = stale_after_sec
        self.weights = weights
        self._history: dict[str, deque[PricePoint]] = {}
        self.latest: dict[str, SignalResult] = {}
        self._last_directional: dict[str, SignalResult] = {}

    def history(self, ticker: str) -> list[PricePoint]:
        return list(self._history.get(ticker, ()))

    def record(self, snap: MarketSnapshot) -> None:
        mid = snap.yes_mid
        if mid is None:
            return
        hist = self._history.setdefault(snap.ticker, deque(maxlen=self.history_size))
        if hist and (snap.ts - hist[-1].ts).total_seconds() < self.MIN_SAMPLE_SPACING_SEC:
            hist[-1] = PricePoint(snap.ts, mid)
        else:
            hist.append(PricePoint(snap.ts, mid))

    def forget(self, ticker: str) -> None:
        self._history.pop(ticker, None)
        self.latest.pop(ticker, None)
        self._last_directional.pop(ticker, None)

    def evaluate(
        self,
        snap: MarketSnapshot,
        *,
        threshold: int | None = None,
        threshold_label: str = "",
        now: datetime | None = None,
        spot: SpotPrice | None = None,
        spot_history: list[SpotPrice] | None = None,
        record: bool = True,
    ) -> SignalResult:
        now = now or utcnow()
        if record:
            self.record(snap)
        threshold = max(self.min_confidence, threshold or 0)
        hist = self.history(snap.ticker)
        feats = compute_features(snap, hist, now, spot, spot_history or [])

        validity = Validity.VALID
        if snap.time_remaining(now) <= 0 or snap.info.status not in ("", "active", "open", "initialized"):
            validity = Validity.CLOSED
        elif snap.age_sec(now) > self.stale_after_sec:
            validity = Validity.STALE
        elif snap.yes_mid is None:
            validity = Validity.NO_QUOTES
        elif len(hist) < 3 or (hist[-1].ts - hist[0].ts).total_seconds() < self.min_history_sec:
            validity = Validity.INSUFFICIENT_DATA

        sr = score_features(feats, self.weights)
        leaning = Direction.WAIT
        if abs(sr.score) >= NEUTRAL_BAND:
            leaning = Direction.UP if sr.score > 0 else Direction.DOWN
        direction = leaning
        reasons = list(sr.reasons)
        if validity is not Validity.VALID:
            direction = Direction.WAIT
            reasons.insert(0, Reason(False, _validity_text(validity)))
        elif leaning is Direction.WAIT:
            reasons.insert(0, Reason(False, "No clear direction"))
        elif sr.confidence < threshold:
            direction = Direction.WAIT
            tl = f" {threshold_label}" if threshold_label else ""
            reasons.append(Reason(False, f"Confidence below{tl} threshold ({threshold})"))

        result = SignalResult(
            market_ticker=snap.ticker,
            asset=snap.info.asset,
            label=snap.info.label,
            direction=direction,
            leaning=leaning,
            confidence=sr.confidence,
            score=sr.score,
            reasons=tuple(reasons),
            timestamp=now,
            features=feats,
            components=sr.components,
            validity=validity,
            threshold=threshold,
        )
        prev = self.latest.get(snap.ticker)
        self.latest[snap.ticker] = result
        if prev is None or prev.direction != result.direction:
            log_event(log, "SIGNAL", ticker=snap.ticker, direction=result.direction.value,
                      confidence=result.confidence, score=result.score, validity=validity.value)
        return result

    def detect_flip(self, result: SignalResult) -> SignalFlip | None:
        """UP <-> DOWN flips between actionable signals (WAIT is ignored)."""
        if result.direction is Direction.WAIT:
            return None
        prev = self._last_directional.get(result.market_ticker)
        self._last_directional[result.market_ticker] = result
        if prev and prev.direction != result.direction:
            return SignalFlip(result.market_ticker, result.label, prev, result)
        return None


def _validity_text(v: Validity) -> str:
    return {
        Validity.CLOSED: "Market closed",
        Validity.STALE: "Market data stale",
        Validity.NO_QUOTES: "No quotes available",
        Validity.INSUFFICIENT_DATA: "Collecting price history",
    }.get(v, str(v))
