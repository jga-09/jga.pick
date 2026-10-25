"""AstroTick's 'reddit_time_delay' strategy on REAL cached BTC 15-minute markets.

Rules as in AstroTick's defaults (config.py): arm once <= 14 minutes remain; buy the side whose
price has reached 90c (taker at the ask + 1c slippage + taker fee); sell early if that side's bid
falls to 40c (bid - 1c, taker fee on the exit); otherwise hold to settlement; one trade per window.
Fixed rules, not tuned. Variants: R1 = as specified, R0 = same entries without the 40c stop.
Development days, then an untouched holdout (used once); bootstrap 95% interval; 30-trade minimum.
"""

from __future__ import annotations

from typing import Any

from app.research.btc_audit import MIN_N, ATrade, summarize
from app.research.btc_backtest import Dataset, SimCosts, Step, fee_per_contract

TRIGGER = 90.0
EXIT = 40.0
ARM_T_LEFT = 14 * 60


def _ask(st: Step, side: str) -> float | None:
    return st.yes_ask if side == "yes" else st.no_ask


def _bid(st: Step, side: str) -> float | None:
    return st.yes_bid if side == "yes" else st.no_bid


def trade_market(steps: list[Step], costs: SimCosts, stop: bool) -> ATrade | None:
    for i, st in enumerate(steps):
        if st.t_left > ARM_T_LEFT or st.t_left < 60:
            continue
        side = next((s for s in ("yes", "no") if (_ask(st, s) or 0) >= TRIGGER and (_ask(st, s) or 100) <= 99), None)
        if side is None:
            continue
        ask = _ask(st, side)
        fill = min(99.0, ask + costs.slippage_cents)
        fee = fee_per_contract(fill, costs)
        won = st.outcome == side
        payout, slip, exited = (100.0 if won else 0.0), fill - ask, False
        if stop:
            for later in steps[i + 1:]:
                b = _bid(later, side)
                if b is not None and b <= EXIT and later.t_left >= 30:
                    sell = max(1.0, b - costs.slippage_cents)
                    payout, exited = sell, True
                    slip += b - sell
                    fee += fee_per_contract(sell, costs)
                    break
        return ATrade(st.ticker, st.day, side, ask, slip, fee, payout, won, st.regime.value, st.t_left, None, None,
                      exited)
    return None


def run_reddit(ds: Dataset, holdout_days: int = 7, costs: SimCosts | None = None) -> dict[str, Any]:
    costs = costs or SimCosts()
    days = ds.days
    if len(days) <= holdout_days + 5:
        raise ValueError(f"need more than {holdout_days + 5} days of data (have {len(days)})")
    hold_set = set(days[-holdout_days:])
    rows = []
    for key, label, stop in (("R1", "as specified (90c trigger, 40c stop)", True),
                             ("R0", "same entries, no stop (hold to settlement)", False)):
        dev, hold = [], []
        for steps in ds.markets.values():
            t = trade_market(steps, costs, stop)
            if t is not None:
                (hold if t.day in hold_set else dev).append(t)
        sd, sh = summarize(sorted(dev, key=lambda t: t.day)), summarize(sorted(hold, key=lambda t: t.day))
        ok = sd["trades"] >= MIN_N and sd.get("ev_95") and sd["ev_95"][0] > 0 and sh["net_pnl"] > 0
        rows.append({"key": key, "label": label, "dev": sd, "holdout": sh,
                     "decision": "SUPPORTED (paper-test next)" if ok else "NOT SUPPORTED"})
    sup = [r["key"] for r in rows if r["decision"].startswith("SUPPORTED")]
    verdict = (f"SUPPORTED: {', '.join(sup)}. Paper-test only." if sup else
               "NOT SUPPORTED: reddit_time_delay does not show positive net EV with 30+ trades and a profitable "
               "holdout. Retain the baseline.")
    return {"label": "ASTROTICK 'reddit_time_delay' TEST (buy the side that reaches 90c)",
            "data": {"markets": len(ds.markets), "dev_days": [days[0], days[-holdout_days - 1]],
                     "holdout_days": [days[-holdout_days], days[-1]]},
            "rows": rows, "verdict": verdict}


def format_reddit(r: dict[str, Any]) -> str:
    d = r["data"]
    L = [r["label"], f"{d['markets']} markets. Development {d['dev_days'][0]}..{d['dev_days'][1]}; holdout "
         f"{d['holdout_days'][0]}..{d['holdout_days'][1]}. 1 contract per trade, taker fees + 1c slippage.", ""]
    for x in r["rows"]:
        s, h = x["dev"], x["holdout"]
        if not s["trades"]:
            L.append(f"{x['key']} {x['label']}: 0 trades -> {x['decision']}")
            continue
        ci = f" (95% {s['ev_95'][0]:+.1f}..{s['ev_95'][1]:+.1f})" if s.get("ev_95") else ""
        L.append(f"{x['key']} {x['label']}")
        L.append(f"   dev {s['trades']} trades · win {s['settle_win_rate'] * 100:.1f}% at avg {s['avg_entry_cents']:.1f}c "
                 f"(break-even {s['breakeven_win_rate'] * 100:.1f}%) · stopped out {s['exited']} · "
                 f"EV {s['ev_cents']:+.2f}c{ci} · net {s['net_pnl']:+.2f}$ | holdout {h['trades']} trades "
                 f"net {h['net_pnl']:+.2f}$ -> {x['decision']}")
    L += ["", "VERDICT: " + r["verdict"]]
    return "\n".join(L)
