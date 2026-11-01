"""Pre-registered LATE-ENTRY test (written 2026-10-23, before looking at any market after 2026-10-12).

Hypothesis (from two earlier observations on data up to 2026-10-12):
  * in the last 3 minutes the model's probabilities were MORE accurate than the Kalshi price
    (Brier 0.0667 vs 0.0707) - the only time window where that happened;
  * paper trades entered with < 6 minutes left did better than earlier entries.
Earlier backtests never traded with < 2 minutes left, so this window was untested.

Rules (fixed, not tuned):
  * buy the side whose conservative net EV (P_low x 100 - ask - 1c slippage - taker fee) >= 3c,
    entering at the first qualifying minute inside the window; one contract per market;
  * the model abstains inside the 60 s settlement-averaging window, so no entry below 60 s left;
  * L3 = 60-180 s left, L6 = 60-360 s left, ALL = control (any time >= 60 s left).

Decision: ONLY the FRESH period (markets after 2026-10-12, never examined when the hypothesis was
formed) counts. A window is SUPPORTED if it has >= 30 fresh trades, fresh EV whose 95% interval is
above 0, and it beats the control on the fresh period. Earlier markets are shown for context only.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.research.btc_audit import MIN_N, Strategy, run_strategy, summarize
from app.research.btc_backtest import Dataset, SimCosts, Step, _inputs, params_for
from app.risk.profiles import RiskLevel, load_profiles
from app.strategy.btc_decision import UNCALIBRATED, side_edges

FRESH_AFTER = "2026-10-12"
MIN_EV = 3.0
WINDOWS = (("L3", "last 60-180 s", 180.0), ("L6", "last 60-360 s", 360.0), ("ALL", "control: any time", 1e9))


def _rule(prm: Any, max_t: float):
    def rule(st: Step) -> str | None:
        if st.t_left > max_t or not st.estimate.ok:
            return None
        edges = side_edges(_inputs(st, UNCALIBRATED), prm)
        best = max(edges, key=lambda e: e.ev_low_cents) if edges else None
        return best.side if best is not None and best.ev_low_cents >= MIN_EV else None
    return rule


def run_late(ds: Dataset, costs: SimCosts | None = None, fresh_after: str = FRESH_AFTER) -> dict[str, Any]:
    costs = replace(costs or SimCosts(), min_t_left=60.0)
    prm = params_for(load_profiles(read_env=False)[RiskLevel.LOW], costs)
    by_day = ds.by_day()
    seen = {k: v for d in ds.days if d <= fresh_after for k, v in by_day[d].items()}
    fresh = {k: v for d in ds.days if d > fresh_after for k, v in by_day[d].items()}
    fresh_days = [d for d in ds.days if d > fresh_after]
    rows = []
    res_fresh = {}
    for key, label, max_t in WINDOWS:
        strat = Strategy(key, label, _rule(prm, max_t))
        s_seen = summarize(run_strategy(seen, strat, costs))
        s_fresh = summarize(run_strategy(fresh, strat, costs))
        s_fresh2 = summarize(run_strategy(fresh, strat, replace(costs, slippage_cents=2.0)))
        res_fresh[key] = s_fresh
        rows.append({"key": key, "window": label, "seen": s_seen, "fresh": s_fresh, "fresh_slip2": s_fresh2})
    ctrl = res_fresh["ALL"]
    for r in rows:
        if r["key"] == "ALL":
            r["decision"] = "control"
            continue
        f = r["fresh"]
        ok = (f["trades"] >= MIN_N and f.get("ev_95") and f["ev_95"][0] > 0
              and (ctrl["ev_cents"] is None or (f["ev_cents"] or -1e9) > ctrl["ev_cents"]))
        r["decision"] = "SUPPORTED (paper-test next)" if ok else "NOT SUPPORTED"
    sup = [r["key"] for r in rows if r["decision"].startswith("SUPPORTED")]
    if not fresh_days:
        verdict = f"NO FRESH DATA: download markets after {fresh_after} (scripts/replay.py) and re-run."
    elif sup:
        verdict = (f"SUPPORTED on fresh data: {', '.join(sup)}. Next: paper-trade late entries only; "
                   "real money stays off until paper agrees.")
    else:
        verdict = ("NOT SUPPORTED: late entries do not show positive net EV on the fresh (never-examined) markets "
                   "with 30+ trades. Retain the baseline.")
    return {"label": "LATE-ENTRY TEST (pre-registered; decision on fresh markets only)",
            "data": {"markets_seen": len(seen), "markets_fresh": len(fresh), "fresh_after": fresh_after,
                     "fresh_days": [fresh_days[0], fresh_days[-1]] if fresh_days else None},
            "rows": rows, "verdict": verdict}


def format_late(r: dict[str, Any]) -> str:
    d = r["data"]
    fd = f"{d['fresh_days'][0]}..{d['fresh_days'][1]}" if d["fresh_days"] else "none"
    L = [r["label"], f"Fresh (decides): {d['markets_fresh']} markets after {d['fresh_after']} ({fd}). "
         f"Earlier (context only): {d['markets_seen']} markets.", ""]

    def cell(s: dict[str, Any]) -> str:
        if not s["trades"]:
            return "0 trades"
        ci = f" (95% {s['ev_95'][0]:+.1f}..{s['ev_95'][1]:+.1f})" if s.get("ev_95") else ""
        return (f"{s['trades']} trades win {s['win_rate'] * 100:.0f}% (b/e {s['breakeven_win_rate'] * 100:.0f}%) "
                f"EV {s['ev_cents']:+.2f}c{ci} net {s['net_pnl']:+.2f}$")

    for x in r["rows"]:
        L.append(f"{x['key']:<4} {x['window']}")
        L.append(f"     FRESH : {cell(x['fresh'])}  -> {x['decision']}")
        L.append(f"     fresh, 2c slippage: {cell(x['fresh_slip2'])}")
        L.append(f"     earlier (context): {cell(x['seen'])}")
    L += ["", "VERDICT: " + r["verdict"]]
    return "\n".join(L)
