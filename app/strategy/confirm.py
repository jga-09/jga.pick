"""Experimental "confirmation" strategy (V4 in the walk-forward research).

Trade only when momentum, trend, order book AND the underlying spot price all
point the same way. The backtest and the live risk manager both call this one
function, so a forward paper test is exactly the rule that was backtested.
"""

from __future__ import annotations

RULES = {"momentum": 0.3, "trend": 0.3, "orderbook": 0.1, "underlying": 0.2}


def confirm_side(components: dict[str, float], require_orderbook: bool = True) -> tuple[str | None, list[str]]:
    """Return ('yes'|'no'|None, reasons-it-failed) from YES-signed component scores.

    ``require_orderbook=False`` is the V4L variant used for historical replays,
    where Kalshi provides no order-book history."""
    m, t = components.get("momentum"), components.get("trend")
    if m is None or t is None:
        return None, ["Momentum/trend unavailable"]
    if m >= RULES["momentum"] and t >= RULES["trend"]:
        side, sign = "yes", 1
    elif m <= -RULES["momentum"] and t <= -RULES["trend"]:
        side, sign = "no", -1
    else:
        return None, ["Momentum and trend do not agree strongly"]
    reasons = []
    ob = components.get("orderbook")
    if require_orderbook and (ob is None or ob * sign < RULES["orderbook"]):
        reasons.append("Order book does not confirm")
    u = components.get("underlying")
    if u is None:
        reasons.append("Underlying price feed unavailable")
    elif u * sign < RULES["underlying"]:
        reasons.append("Underlying does not confirm")
    return (None, reasons) if reasons else (side, [])
