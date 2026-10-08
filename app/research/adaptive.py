"""Daily adaptive mode: NORMAL / CAUTION / PAUSED.

Reacts to abnormal *conditions* (volatility far outside its history, bad data,
performance far below what the calibrated model expected over a meaningful
sample) - never to a couple of wins or losses.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.research.stats import wilson


@dataclass(frozen=True)
class AdaptiveState:
    mode: str  # NORMAL | CAUTION | PAUSED
    reason: str = ""

    @property
    def badge(self) -> str:
        return {"NORMAL": "🟢 NORMAL", "CAUTION": "🟡 CAUTION MODE", "PAUSED": "🔴 TRADING PAUSED"}[self.mode]


def percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def assess(current_vols: list[float], historical_vols: list[float], recent_results: list[tuple[bool, float]],
           data_ok: bool, min_history: int = 200, min_recent: int = 20) -> AdaptiveState:
    """recent_results: (won, expected win probability at entry) for the latest closed trades."""
    if not data_ok:
        return AdaptiveState("PAUSED", "Market data unavailable or stale")
    if current_vols and len(historical_vols) >= min_history:
        hist = sorted(historical_vols)
        cur = sorted(current_vols)[len(current_vols) // 2]
        if cur > percentile(hist, 0.99):
            return AdaptiveState("PAUSED", f"Volatility {cur:.2f}¢ above the 99th percentile of history")
        if cur > percentile(hist, 0.95):
            return AdaptiveState("CAUTION", f"Volatility {cur:.2f}¢ above the 95th percentile of history")
    if len(recent_results) >= min_recent:
        recent = recent_results[-min_recent:]
        wins = sum(1 for w, _ in recent if w)
        expected = sum(p for _, p in recent) / len(recent)
        _, hi = wilson(wins, len(recent), z=1.64)
        if hi < expected:
            return AdaptiveState("CAUTION", f"Last {len(recent)} trades won {wins} - well below the expected "
                                            f"{expected:.0%}")
    return AdaptiveState("NORMAL")
