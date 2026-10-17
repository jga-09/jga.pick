"""Backtest of "post your own bid" (maker) entries for the BTC 15-minute engine.

Instead of paying the ask, the bot rests a limit bid ``offset`` cents below it. Fill rule
(deliberately conservative, because there is no order-book history):

* the bid is FILLED only if, within ``max_wait_min`` minutes, a later 1-minute quote shows the
  ask for that side at or below our bid (someone offered at our price);
* a seller merely hitting the bid is NOT counted (queue position is unknown);
* otherwise the order expires unfilled - missed winners are simply missed.

Fills tend to happen when the price moves toward us, i.e. against our side; that adverse
selection is captured because every fill is scored against the market's real result.
The same walk-forward as the main backtest is used: parameters are picked on earlier days,
checked on validation days, then tested on the next unseen day.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.research.btc_backtest import (
    MOM_GRID,
    Dataset,
    SimCosts,
    Step,
    Trade,
    _mom,
    _side_dir,
    fee_per_contract,
    metrics,
    simulate,
    v_mom_model,
    walk_forward,
)
from app.strategy.btc_decision import UNCALIBRATED, CalibrationState

EDGE_GRID = (0.0, 1.0, 2.0, 3.0, 5.0)


@dataclass(frozen=True)
class MakerCosts:
    offset_cents: float = 2.0
    max_wait_min: float = 5.0
    maker_fee_rate: float = 0.0175  # conservative; set 0 if your markets charge no maker fee
    min_fill_t_left: float = 60.0


@dataclass
class FillStats:
    attempts: int = 0
    fills: int = 0
    missed_winners: int = 0  # unfilled orders whose side won (profit we did not get)

    @property
    def fill_rate(self) -> float | None:
        return self.fills / self.attempts if self.attempts else None


def _ask(st: Step, side: str) -> float | None:
    return st.yes_ask if side == "yes" else st.no_ask


def make_simulator(mc: MakerCosts, stats: FillStats | None = None) -> Callable[..., list[Trade]]:
    fee_costs = SimCosts(fee_rate=mc.maker_fee_rate, slippage_cents=0.0)

    def sim(markets: dict[str, list[Step]], select: Callable, param: float, costs: SimCosts,
            calibration: CalibrationState = UNCALIBRATED, variant: str = "") -> list[Trade]:
        trades = []
        for steps in markets.values():
            for i, st in enumerate(steps):
                if st.t_left < costs.min_t_left:
                    continue
                side, strength = select(st, param, calibration)
                if side is None:
                    continue
                ask = _ask(st, side)
                if ask is None or not 2 <= ask <= 99:
                    break
                bid = ask - mc.offset_cents
                if bid < 1:
                    break
                if stats is not None:
                    stats.attempts += 1
                filled = None
                for later in steps[i + 1:]:
                    if later.ts - st.ts > timedelta(minutes=mc.max_wait_min) or later.t_left < mc.min_fill_t_left:
                        break
                    a2 = _ask(later, side)
                    if a2 is not None and a2 <= bid:
                        filled = later
                        break
                if filled is None:
                    if stats is not None and st.outcome == side:
                        stats.missed_winners += 1
                    break
                if stats is not None:
                    stats.fills += 1
                probs = st.estimate.p_side(side)
                trades.append(Trade(filled, side, bid, 0.0, fee_per_contract(bid, fee_costs), 1, variant,
                                    probs[0] if probs else None, strength))
                break  # one order per market
        return sorted(trades, key=lambda t: t.step.ts)
    return sim


def v_maker_mom_model(mc: MakerCosts) -> Callable:
    """Momentum direction, only when the model's point EV at OUR bid (after maker fee) is positive."""
    fee_costs = SimCosts(fee_rate=mc.maker_fee_rate, slippage_cents=0.0)

    def sel(st: Step, z: float, _: CalibrationState) -> tuple[str | None, int | None]:
        d = _mom(st, z)
        if d is None or not st.estimate.ok:
            return None, None
        side = _side_dir(st, d)
        ask = _ask(st, side) if side else None
        probs = st.estimate.p_side(side) if side else None
        if ask is None or probs is None:
            return None, None
        bid = ask - mc.offset_cents
        if probs[0] * 100 - bid - fee_per_contract(bid, fee_costs) <= 0:
            return None, None
        return side, None
    return sel


def v_maker_edge(mc: MakerCosts) -> Callable:
    """Side with the best CONSERVATIVE edge at our bid: 100*p_low - bid - maker fee >= margin."""
    fee_costs = SimCosts(fee_rate=mc.maker_fee_rate, slippage_cents=0.0)

    def sel(st: Step, margin: float, _: CalibrationState) -> tuple[str | None, int | None]:
        if not st.estimate.ok:
            return None, None
        best, best_ev = None, None
        for side in ("yes", "no"):
            ask, probs = _ask(st, side), st.estimate.p_side(side)
            if ask is None or probs is None:
                continue
            bid = ask - mc.offset_cents
            if bid < 1:
                continue
            ev = probs[1] * 100 - bid - fee_per_contract(bid, fee_costs)
            if best_ev is None or ev > best_ev:
                best, best_ev = side, ev
        return (best, None) if best is not None and best_ev is not None and best_ev >= margin else (None, None)
    return sel


@dataclass
class MakerVariant:
    name: str
    trades: list[Trade]
    stats: FillStats = field(default_factory=FillStats)
    days_traded: int = 0  # out-of-sample days where a profitable setting was found


def run_maker(ds: Dataset, costs: SimCosts | None = None, train_days: int = 3, val_days: int = 1,
              offsets: tuple[float, ...] = (1.0, 2.0, 3.0), max_wait_min: float = 5.0,
              maker_fee_rate: float = 0.0175, capital: float = 100.0) -> dict[str, Any]:
    costs = costs or SimCosts()
    out_vars: list[MakerVariant] = []
    ref = walk_forward(ds, "taker: momentum+probability model", v_mom_model(costs), MOM_GRID, costs,
                       train_days, val_days, sim=simulate)
    out_vars.append(MakerVariant(ref.name, ref.trades, days_traded=sum(1 for _, g in ref.chosen if g is not None)))
    for off in offsets:
        mc = MakerCosts(off, max_wait_min, maker_fee_rate)
        for label, sel, grid in (("momentum+probability model", v_maker_mom_model(mc), MOM_GRID),
                                 ("probability model + edge", v_maker_edge(mc), EDGE_GRID)):
            stats = FillStats()
            name = f"maker -{off:g}c: {label}"
            wf = walk_forward(ds, name, sel, grid, costs, train_days, val_days, sim=make_simulator(mc, stats))
            out_vars.append(MakerVariant(name, wf.trades, stats, sum(1 for _, g in wf.chosen if g is not None)))
    variants = {}
    for v in out_vars:
        m = metrics(v.trades, capital)
        m["fill_rate"] = v.stats.fill_rate
        m["orders"] = v.stats.attempts
        m["missed_winners"] = v.stats.missed_winners
        m["days_traded"] = v.days_traded
        variants[v.name] = m
    best_name, best = None, None
    for name, m in variants.items():
        if name.startswith("maker") and m["trades"] >= 30 and (best is None or
                                                               (m["avg_profit_per_trade"] or -1) > (best["avg_profit_per_trade"] or -1)):
            best_name, best = name, m
    makers = {k: m for k, m in variants.items() if k.startswith("maker")}
    if best is None and all(m["days_traded"] == 0 for m in makers.values()):
        verdict = ("NO EVIDENCE OF EDGE: posting bids was never profitable on earlier days, so no maker version "
                   "traded out of sample. Do not trade this live.")
    elif best is None:
        verdict = ("INSUFFICIENT DATA: some maker versions traded out of sample but none reached 30 fills, "
                   "so the result could be luck. Download more days.")
    elif best.get("ev_95") and best["ev_95"][0] > 0:
        verdict = f"POSITIVE: '{best_name}' is profitable out of sample with the whole 95% range above zero. Next: paper-trade it live."
    elif (best["avg_profit_per_trade"] or 0) > 0:
        verdict = (f"POSITIVE ON AVERAGE ('{best_name}') but the 95% range includes losses - could be luck. "
                   "Paper-trade only; collect more days.")
    else:
        verdict = "NO EDGE: posting bids did not make money out of sample either. Do not trade this live."
    days = ds.days
    return {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": "MAKER BACKTEST (post your own bid; historical simulation, out-of-sample walk-forward)",
        "data": {"markets": len(ds.markets), "days": len(days), "first_day": days[0] if days else None,
                 "last_day": days[-1] if days else None, "test_days": len(days[train_days + val_days:])},
        "assumptions": {"fill_rule": "filled only if a later 1-minute ask for our side is at or below our bid",
                        "max_wait_min": max_wait_min, "maker_fee_rate": maker_fee_rate,
                        "taker_fee_rate": costs.fee_rate, "taker_slippage_cents": costs.slippage_cents},
        "variants": variants, "best": best_name, "verdict": verdict,
    }


def format_maker_report(r: dict[str, Any]) -> str:
    d = r["data"]
    lines = [r["label"], f"Data: {d['markets']} markets over {d['days']} days ({d['first_day']} .. {d['last_day']}); "
             f"out-of-sample test days: {d['test_days']}",
             f"Fill rule: {r['assumptions']['fill_rule']}; orders expire after {r['assumptions']['max_wait_min']:g} min; "
             f"maker fee rate {r['assumptions']['maker_fee_rate']:g}", ""]
    for name, m in r["variants"].items():
        if not m["trades"]:
            fr = f"  (orders {m['orders']}, fill rate {m['fill_rate']:.0%})" if m.get("fill_rate") is not None else ""
            lines.append(f"{name:<46}    0 fills (never profitable on earlier days){fr}" if not m.get("days_traded")
                         else f"{name:<46}    0 fills{fr}")
            continue
        pf = m["profit_factor"]
        pf_s = "inf" if pf == math.inf else ("n/a" if pf is None else f"{pf:.2f}")
        fr = f"  fill {m['fill_rate']:.0%}" if m.get("fill_rate") is not None else ""
        ci = f"  95% {m['ev_95'][0] * 100:+.1f}..{m['ev_95'][1] * 100:+.1f}c" if m.get("ev_95") else ""
        lines.append(f"{name:<46} {m['trades']:>4} trades  WR {m['win_rate']:.0%}  net {m['net_pnl']:+.2f}$  "
                     f"EV {m['avg_profit_per_trade'] * 100:+.1f}c/contract{ci}  PF {pf_s}{fr}")
    lines += ["", "VERDICT: " + r["verdict"]]
    return "\n".join(lines)
