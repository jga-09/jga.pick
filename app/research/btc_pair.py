"""'Pair completion' test: rest a bid on BOTH YES and NO so the pair costs less than 100c.

Idea from an outside source (Pondletter's paper table, simulated fills, top row +$231). Their exact
rules are not public, so these are simple pre-registered versions, fixed before any result:

* P1  join both bids at the first minute with >= 10 min left (pair cost = 100 - spread).
* P2  as P1, but only when the spread is >= 2c (so a completed pair earns >= 2c).
* P3  as P1, and as soon as one leg fills, BUY the other side at the ask (taker) if that still locks
      a profit after slippage and the taker fee; otherwise keep the other bid resting.

Both bids rest until 60 s before close. Fills come from the REAL trade tape, only trades AFTER the
order: THROUGH (a trade printed beyond our price; conservative, decides) and TOUCH (a seller traded at
our price; optimistic, reference). Maker fee 0 on KXBTC15M. Outcomes per market:
  both legs filled -> 100 - (yes bid + no bid), regardless of who wins;
  one leg filled   -> that side's settlement value minus its price (an unhedged bet);
  none             -> no trade.
Development days, then an untouched holdout used once; bootstrap 95% interval; 30-market minimum.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.research.btc_audit import MIN_N, _boot
from app.research.btc_backtest import Dataset, SimCosts, Step, fee_per_contract
from app.research.btc_favmaker import Row, _rows

VARIANTS = (("P1", "join both bids, >=10 min left"), ("P2", "P1 but spread >= 2c"),
            ("P3", "P1 + complete the 2nd leg at the ask when it still locks a profit"))


def fill_time(side: str, price: float, rows: Sequence[Row], start: float, end: float, rule: str) -> float | None:
    for ts, yp, taker in rows:
        if ts <= start:
            continue
        if ts > end:
            return None
        if side == "yes":
            if yp < price or (rule == "touch" and yp <= price and taker == "no"):
                return ts
        else:
            npx = 100 - yp
            if npx < price or (rule == "touch" and npx <= price and taker == "yes"):
                return ts
    return None


def market_result(steps: Sequence[Step], rows: Sequence[Row], variant: str, rule: str,
                  costs: SimCosts) -> dict[str, Any] | None:
    st = next((s for s in steps if s.t_left >= 600 and s.yes_bid is not None and s.yes_ask is not None), None)
    if st is None:
        return None
    yb, nb = st.yes_bid, st.no_bid
    spread = st.yes_ask - st.yes_bid
    if yb is None or nb is None or yb < 1 or nb < 1 or yb + nb >= 100:
        return None
    if variant == "P2" and spread < 2:
        return None
    start = st.ts.timestamp()
    end = (st.close - timedelta(seconds=60)).timestamp()
    ty = fill_time("yes", yb, rows, start, end, rule)
    tn = fill_time("no", nb, rows, start, end, rule)
    win_yes = st.outcome == "yes"
    if variant == "P3" and (ty is not None or tn is not None):
        # leg that filled first; try to lock the pair by taking the other side before its bid fills
        if tn is None or (ty is not None and ty <= tn):
            t1, first_px, other, other_fill = ty, yb, "no", tn
        else:
            t1, first_px, other, other_fill = tn, nb, "yes", ty
        for s in steps:
            ts = s.ts.timestamp()
            if ts < t1 or s.t_left < 60:
                continue
            if other_fill is not None and other_fill <= ts:
                break  # the resting bid completed the pair first (handled below)
            ask = s.no_ask if other == "no" else s.yes_ask
            if ask is None:
                continue
            fill = min(99.0, ask + costs.slippage_cents)
            cost = first_px + fill + fee_per_contract(fill, costs)
            if cost < 100:
                return {"kind": "both (taker completion)", "net": (100 - cost) / 100, "day": st.day}
    if ty is not None and tn is not None:
        return {"kind": "both", "net": (100 - yb - nb) / 100, "day": st.day}
    if ty is not None:
        return {"kind": "yes only", "net": ((100 if win_yes else 0) - yb) / 100, "day": st.day}
    if tn is not None:
        return {"kind": "no only", "net": ((0 if win_yes else 100) - nb) / 100, "day": st.day}
    return {"kind": "none", "net": None, "day": st.day}


def _summary(res: list[dict[str, Any]]) -> dict[str, Any]:
    traded = [r for r in res if r["net"] is not None]
    nets = [r["net"] for r in traded]
    n = len(nets)
    both = [r for r in traded if r["kind"].startswith("both")]
    one = [r for r in traded if not r["kind"].startswith("both")]
    eq = peak = dd = 0.0
    for x in nets:
        eq += x
        peak, dd = max(peak, eq), max(dd, peak - eq)
    out = {"orders": len(res), "trades": n, "net_pnl": round(sum(nets), 2),
           "ev_cents": 100 * sum(nets) / n if n else None, "max_drawdown": round(dd, 2),
           "both_filled": len(both), "one_leg": len(one),
           "both_ev_cents": 100 * sum(r["net"] for r in both) / len(both) if both else None,
           "one_leg_ev_cents": 100 * sum(r["net"] for r in one) / len(one) if one else None,
           "ev_95": [100 * x for x in _boot(nets)] if n >= 10 else None}
    return out


def run_pair(cache: Path, ds: Dataset, holdout_days: int = 7, costs: SimCosts | None = None) -> dict[str, Any]:
    costs = costs or SimCosts()
    days = ds.days
    if len(days) <= holdout_days + 5:
        raise ValueError(f"need more than {holdout_days + 5} days of data (have {len(days)})")
    hold_set = set(days[-holdout_days:])
    keys = [(v, r) for v, _ in VARIANTS for r in ("through", "touch")]
    dev: dict[tuple[str, str], list] = {k: [] for k in keys}
    hold: dict[tuple[str, str], list] = {k: [] for k in keys}
    for f in sorted(cache.glob("KXBTC*.json.gz")):  # one market at a time
        try:
            rec = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        steps = ds.markets.get((rec.get("market") or {}).get("ticker") or "")
        if not steps:
            continue
        rows = _rows(rec)
        del rec
        for v, r in keys:
            res = market_result(steps, rows, v, r, costs)
            if res is not None:
                (hold if steps[0].day in hold_set else dev)[(v, r)].append(res)
    out_rows = []
    for v, label in VARIANTS:
        for r in ("through", "touch"):
            sd, sh = _summary(dev[(v, r)]), _summary(hold[(v, r)])
            dec = None
            if r == "through":
                ok = sd["trades"] >= MIN_N and sd["ev_95"] and sd["ev_95"][0] > 0 and sh["net_pnl"] > 0
                dec = "SUPPORTED (paper-test next)" if ok else "NOT SUPPORTED"
            out_rows.append({"variant": v, "label": label, "rule": r, "dev": sd, "holdout": sh, "decision": dec})
    sup = sorted({x["variant"] for x in out_rows if x["decision"] and x["decision"].startswith("SUPPORTED")})
    verdict = (f"SUPPORTED: {', '.join(sup)} with conservative fills on development AND the holdout. Paper-test only."
               if sup else "NOT SUPPORTED: pair completion does not show positive net EV with conservative fills, "
               "30+ markets and a profitable holdout. Retain the baseline.")
    return {"label": "PAIR-COMPLETION TEST (bids on both YES and NO; real trade tape; maker fee 0)",
            "data": {"markets": len(ds.markets), "dev_days": [days[0], days[-holdout_days - 1]],
                     "holdout_days": [days[-holdout_days], days[-1]]},
            "rows": out_rows, "verdict": verdict}


def format_pair(r: dict[str, Any]) -> str:
    d = r["data"]
    L = [r["label"], f"{d['markets']} markets. Development {d['dev_days'][0]}..{d['dev_days'][1]}; "
         f"holdout {d['holdout_days'][0]}..{d['holdout_days'][1]}. 1 contract per leg.",
         "THROUGH = conservative fills (decides); TOUCH = optimistic (reference).", ""]

    def f(v: float | None, fmt: str = "{:+.2f}") -> str:
        return "—" if v is None else fmt.format(v)

    for x in r["rows"]:
        s, h = x["dev"], x["holdout"]
        ci = f" (95% {s['ev_95'][0]:+.1f}..{s['ev_95'][1]:+.1f})" if s["ev_95"] else ""
        line = (f"{x['variant']} {x['rule']:<8} dev {s['trades']}/{s['orders']} filled: both {s['both_filled']} "
                f"(avg {f(s['both_ev_cents'])}c), one leg {s['one_leg']} (avg {f(s['one_leg_ev_cents'])}c) "
                f"-> EV {f(s['ev_cents'])}c{ci} net {s['net_pnl']:+.2f}$ | holdout net {h['net_pnl']:+.2f}$")
        if x["decision"]:
            line += f"  -> {x['decision']}"
        L.append(line)
    L += ["", "VERDICT: " + r["verdict"]]
    return "\n".join(L)
