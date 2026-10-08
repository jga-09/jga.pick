"""Win / loss analysis: why did a trade win or lose?

Tags compare the conditions recorded at entry with the last conditions the bot
saw before the market closed. They are heuristics for spotting recurring
patterns, not proof of causation.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

LOSS_TEXT = {
    "momentum_reversal": "Momentum reversed after entry",
    "orderbook_reversal": "Order book flipped against the position",
    "underlying_divergence": "Underlying moved against the position",
    "late_entry": "Late entry (< 3 min left)",
    "volatility_spike": "Volatility spiked after entry",
    "spread_problem": "Wide spread at entry",
    "low_liquidity": "Thin liquidity at entry",
    "regime_mismatch": "Entered in a weak regime",
    "false_breakout": "False breakout (extended move reverted)",
    "model_error": "No clear cause - model error / noise",
}
WIN_TEXT = {
    "strong_momentum": "Strong momentum",
    "rising_volume": "Rising volume",
    "underlying_confirmed": "Underlying confirmed",
    "book_confirmed": "Order book confirmed",
    "stable_signal": "Signal stable",
    "strong_trend": "Strong-trend regime",
    "accelerating": "Momentum accelerating",
}


def entry_snapshot(sig: Any, snap: Any, now: Any) -> dict[str, Any]:
    """Compact, JSON-safe description of the conditions at entry."""
    a = sig.analysis
    f = sig.features
    return {
        "direction": sig.direction.value if sig.direction.value != "WAIT" else sig.leaning.value,
        "confidence": sig.confidence, "quality": a.quality if a else None, "grade": a.grade if a else None,
        "regime": a.regime.value if a else None, "accel_state": a.accel_state if a else None,
        "underlying_state": a.underlying_state if a else None,
        "stability": a.stability.score if a else None, "time_remaining": snap.time_remaining(now),
        "time_bucket": a.time_bucket if a else None, "spread": snap.spread,
        "components": {k: round(v, 3) for k, v in (a.components if a else {}).items()},
        "volatility": f.get("volatility"), "volume_accel": f.get("volume_accel"),
        "prob_move": f.get("prob_move_300s"), "soft_flags": list(a.soft_flags) if a else [],
    }


def exit_snapshot(sig: Any | None) -> dict[str, Any]:
    if sig is None:
        return {}
    a = sig.analysis
    return {
        "components": {k: round(v, 3) for k, v in (a.components if a else sig.components).items()},
        "volatility": sig.features.get("volatility"), "regime": a.regime.value if a else None,
    }


def analyze_trade(entry: dict[str, Any], exit_: dict[str, Any], won: bool) -> list[str]:
    sign = 1 if entry.get("direction") == "UP" else -1
    ec, xc = entry.get("components", {}), exit_.get("components", {})

    def aligned(c: dict[str, float], k: str) -> float | None:
        return None if k not in c else c[k] * sign

    if won:
        tags = []
        if (aligned(ec, "momentum") or 0) >= 0.5:
            tags.append("strong_momentum")
        if (entry.get("volume_accel") or 0) > 1.2:
            tags.append("rising_volume")
        if entry.get("underlying_state") == "CONFIRMED":
            tags.append("underlying_confirmed")
        if (aligned(ec, "orderbook") or 0) >= 0.2:
            tags.append("book_confirmed")
        if (entry.get("stability") or 0) >= 0.8:
            tags.append("stable_signal")
        if entry.get("regime") == "STRONG_TREND":
            tags.append("strong_trend")
        if entry.get("accel_state") == "ACCELERATING":
            tags.append("accelerating")
        return tags

    tags = []
    if (aligned(xc, "momentum") or 0) <= -0.2:
        tags.append("momentum_reversal")
    if (aligned(ec, "orderbook") or 0) > 0.1 and (aligned(xc, "orderbook") or 0) < -0.1:
        tags.append("orderbook_reversal")
    if entry.get("underlying_state") == "CONFLICT" or (aligned(xc, "underlying") or 0) <= -0.2:
        tags.append("underlying_divergence")
    if (entry.get("time_remaining") or 999) < 180:
        tags.append("late_entry")
    ev, xv = entry.get("volatility"), exit_.get("volatility")
    if ev and xv and xv > 2 * ev and xv > 2.5:
        tags.append("volatility_spike")
    if (entry.get("spread") or 0) > 6:
        tags.append("spread_problem")
    if "low_liquidity" in entry.get("soft_flags", []):
        tags.append("low_liquidity")
    if entry.get("regime") in ("SIDEWAYS", "CHAOTIC", "HIGH_VOLATILITY", "LOW_LIQUIDITY"):
        tags.append("regime_mismatch")
    if (entry.get("prob_move") or 0) * sign >= 8:
        tags.append("false_breakout")
    return tags or ["model_error"]


def describe(tags: list[str], won: bool) -> str:
    table = WIN_TEXT if won else LOSS_TEXT
    return "; ".join(table.get(t, t) for t in tags)


def pattern_counts(contexts: list[dict[str, Any]], won: bool) -> list[tuple[str, int]]:
    c: Counter[str] = Counter()
    for ctx in contexts:
        if ctx.get("won") is won:
            c.update(ctx.get("tags", []))
    table = WIN_TEXT if won else LOSS_TEXT
    return [(table.get(k, k), v) for k, v in c.most_common()]
