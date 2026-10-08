"""Signal consistency: how stable has the leaning been over recent evaluations?"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Stability:
    score: float | None  # 0..1, None when there are too few samples
    flips: int  # UP<->DOWN changes in the window
    samples: int
    held_sec: float  # how long the current leaning has persisted
    confidence_trend: float  # confidence change per minute over the window


class StabilityTracker:
    SAMPLE_SPACING_SEC = 10.0

    def __init__(self, window_sec: float = 180.0) -> None:
        self.window = window_sec
        self._hist: dict[str, deque[tuple[datetime, str, int]]] = {}

    def record(self, ticker: str, ts: datetime, leaning: str, confidence: int) -> None:
        h = self._hist.setdefault(ticker, deque(maxlen=120))
        # Keep committed samples >= spacing apart; the newest slot is refreshed in place.
        if len(h) >= 2 and (ts - h[-2][0]).total_seconds() < self.SAMPLE_SPACING_SEC:
            h[-1] = (ts, leaning, confidence)
        else:
            h.append((ts, leaning, confidence))

    def forget(self, ticker: str) -> None:
        self._hist.pop(ticker, None)

    def history(self, ticker: str) -> list[tuple[datetime, str, int]]:
        return list(self._hist.get(ticker, ()))

    def measure(self, ticker: str, leaning: str, now: datetime) -> Stability:
        win = [x for x in self._hist.get(ticker, ()) if (now - x[0]).total_seconds() <= self.window]
        if len(win) < 3 or leaning == "WAIT":
            return Stability(None, 0, len(win), 0.0, 0.0)
        directional = [x[1] for x in win if x[1] != "WAIT"]
        flips = sum(1 for a, b in zip(directional, directional[1:], strict=False) if a != b)
        agree = sum(1.0 if x[1] == leaning else 0.5 if x[1] == "WAIT" else 0.0 for x in win) / len(win)
        held = 0.0
        for ts, lean, _ in reversed(win):
            if lean != leaning:
                break
            held = (now - ts).total_seconds()
        span_min = max(1e-6, (win[-1][0] - win[0][0]).total_seconds() / 60)
        conf_trend = (win[-1][2] - win[0][2]) / span_min
        score = max(0.0, min(1.0, agree - 0.25 * flips + (0.05 if conf_trend > 0 else 0)))
        return Stability(round(score, 3), flips, len(win), held, conf_trend)
