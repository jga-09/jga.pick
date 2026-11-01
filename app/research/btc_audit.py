"""Profitability audit of the BTC 15-minute strategy (historical replay + paper records).

Rules this audit follows (and the report states):

* Only REAL cached Kalshi markets and REAL recorded paper positions are used. Nothing is invented.
* Every decision at a minute uses only data available at that minute (the same replay as the
  backtest). Outcomes are used only to score trades after the fact.
* Strategies are FIXED rules written before looking at results (no parameter fitting), so a
  chronological day-by-day evaluation has no training leakage. The only "selection" is which
  pre-registered change to accept; that is guarded by a final HOLDOUT period that is used once.
* Each candidate changes exactly ONE thing versus the unchanged baseline and states its hypothesis.
* A change is ACCEPTED only if, on the development period, it has enough trades, positive expected
  value, and a day-paired improvement whose 95% interval is above zero, AND it does not lose on the
  untouched holdout. Otherwise the baseline is retained.
* Uncertainty: bootstrap 95% intervals for EV per trade and for the paired daily difference;
  minimum-sample flags on every segment. Short-term gains are not treated as proof.
"""

from __future__ import annotations

import math
import random
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from app.research.btc_backtest import Dataset, SimCosts, Step, _inputs, fee_per_contract, params_for
from app.research.btc_calibration import calibration_report
from app.risk.profiles import RiskLevel, load_profiles
from app.strategy.btc_decision import UNCALIBRATED, BTCParams, decide, side_edges
from app.strategy.btc_model import BTCRegime

MIN_N = 30  # minimum trades before any EV claim
SEGMENT_MIN_N = 30


# ------------------------------------------------------------------ trades
@dataclass(frozen=True)
class ATrade:
    ticker: str
    day: str
    side: str
    entry: float  # cents, ask at entry (before slippage)
    slippage: float  # cents
    fee: float  # cents per contract (entry, + exit fee if sold early)
    payout: float  # cents received: 100/0 at settlement or exit bid
    won: bool  # side won at settlement (even if exited early)
    regime: str
    t_left: float
    p_side: float | None
    spread: float | None
    exited: bool = False

    @property
    def net(self) -> float:  # dollars per contract
        return (self.payout - self.entry - self.slippage - self.fee) / 100

    @property
    def gross(self) -> float:  # dollars, before fee and slippage
        return (self.payout - self.entry) / 100


Rule = Callable[[Step], str | None]


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    rule: Rule
    exit_p: float | None = None  # early exit when model P(our side) falls below this
    hypothesis: str = ""
    change: str = ""


def _ask(st: Step, side: str) -> float | None:
    return st.yes_ask if side == "yes" else st.no_ask


def _bid(st: Step, side: str) -> float | None:
    return st.yes_bid if side == "yes" else st.no_bid


def run_strategy(markets: dict[str, list[Step]], strat: Strategy, costs: SimCosts) -> list[ATrade]:
    """One contract per market at the first qualifying minute; taker entry at ask + slippage."""
    out = []
    for steps in markets.values():
        for i, st in enumerate(steps):
            if st.t_left < costs.min_t_left:
                continue
            side = strat.rule(st)
            if side is None:
                continue
            ask = _ask(st, side)
            if ask is None or not 1 <= ask <= 99:
                break
            fill = min(99.0, ask + costs.slippage_cents)
            fee = fee_per_contract(fill, costs)
            probs = st.estimate.p_side(side)
            won = st.outcome == side
            payout, exited, slip = (100.0 if won else 0.0), False, fill - ask
            if strat.exit_p is not None:
                for later in steps[i + 1:]:
                    pr = later.estimate.p_side(side)
                    bid = _bid(later, side)
                    if pr is None or bid is None or later.t_left < 45 or bid < 5:
                        continue
                    if pr[0] < strat.exit_p:
                        sell = max(1.0, bid - costs.slippage_cents)
                        payout, exited = sell, True
                        slip += bid - sell
                        fee += fee_per_contract(sell, costs)
                        break
            spread = None if st.yes_ask is None or st.yes_bid is None else st.yes_ask - st.yes_bid
            out.append(ATrade(st.ticker, st.day, side, ask, slip, fee, payout, won, st.regime.value, st.t_left,
                              probs[0] if probs else None, spread, exited))
            break
    return out


# ------------------------------------------------------------------ stats
def _boot(xs: Sequence[float], n: int = 3000, seed: int = 7) -> tuple[float, float]:
    rng = random.Random(seed)
    k = len(xs)
    means = sorted(sum(rng.choices(xs, k=k)) / k for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def summarize(trades: Sequence[ATrade]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "net_pnl": 0.0, "ev_cents": None, "ev_95": None, "sufficient": False}
    nets = [t.net for t in trades]
    wins = sum(1 for t in trades if t.payout > t.entry + t.slippage + t.fee)
    eq = peak = dd = 0.0
    for x in nets:
        eq += x
        peak, dd = max(peak, eq), max(dd, peak - eq)
    avg_price = sum(t.entry + t.slippage for t in trades) / n
    fee_c = sum(t.fee for t in trades) / n
    out = {
        "trades": n, "net_pnl": round(sum(nets), 2), "gross_pnl": round(sum(t.gross for t in trades), 2),
        "fees": round(sum(t.fee for t in trades) / 100, 2), "slippage": round(sum(t.slippage for t in trades) / 100, 2),
        "ev_cents": 100 * sum(nets) / n, "win_rate": wins / n,
        "settle_win_rate": sum(1 for t in trades if t.won) / n,
        "avg_entry_cents": avg_price, "avg_fee_cents": fee_c,
        "breakeven_win_rate": (avg_price + fee_c) / 100,
        "max_drawdown": round(dd, 2), "sufficient": n >= MIN_N,
        "exited": sum(1 for t in trades if t.exited),
    }
    p = [t.p_side for t in trades if t.p_side is not None]
    if p:
        out["avg_model_p"] = sum(p) / len(p)  # what the model promised on the trades it picked
    if n >= 10:
        lo, hi = _boot(nets)
        out["ev_95"] = [100 * lo, 100 * hi]
    else:
        out["ev_95"] = None
    return out


def segments(trades: Sequence[ATrade]) -> dict[str, dict[str, dict[str, Any]]]:
    def bucket(v: float | None, edges: Sequence[float], fmt: str) -> str:
        if v is None:
            return "n/a"
        for lo, hi in zip(edges, edges[1:], strict=False):
            if lo <= v < hi:
                return fmt.format(lo, hi)
        return fmt.format(edges[-2], edges[-1])

    keys: dict[str, Callable[[ATrade], str]] = {
        "regime": lambda t: t.regime,
        "entry_price": lambda t: bucket(t.entry, (0, 30, 45, 55, 65, 75, 85, 101), "{:.0f}-{:.0f}c"),
        "minutes_left": lambda t: bucket(t.t_left / 60, (0, 3, 6, 9, 12, 16), "{:.0f}-{:.0f}m"),
        "model_p": lambda t: bucket(t.p_side, (0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01), "{:.1f}-{:.1f}"),
        "side": lambda t: t.side,
        "spread": lambda t: bucket(t.spread, (0, 2, 4, 6, 100), "{:.0f}-{:.0f}c"),
    }
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for name, key in keys.items():
        groups: dict[str, list[ATrade]] = defaultdict(list)
        for t in trades:
            groups[key(t)].append(t)
        out[name] = {}
        for k in sorted(groups):
            s = summarize(groups[k])
            s["verdict"] = ("insufficient sample" if s["trades"] < SEGMENT_MIN_N else
                            "positive (95% above 0)" if s["ev_95"] and s["ev_95"][0] > 0 else
                            "negative (95% below 0)" if s["ev_95"] and s["ev_95"][1] < 0 else "not distinguishable from 0")
            out[name][k] = s
    return out


def daily(trades: Sequence[ATrade], days: Sequence[str]) -> list[float]:
    d = defaultdict(float)
    for t in trades:
        d[t.day] += t.net
    return [d[x] for x in days]


def paired(cand: Sequence[ATrade], base: Sequence[ATrade], days: Sequence[str]) -> dict[str, Any]:
    a, b = daily(cand, days), daily(base, days)
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    if len(diffs) < 5:
        return {"days": len(diffs), "mean_daily_diff": None, "ci95": None}
    lo, hi = _boot(diffs)
    return {"days": len(diffs), "mean_daily_diff": sum(diffs) / len(diffs), "ci95": [lo, hi]}


# ------------------------------------------------------------------ calibration
def calibration(markets: dict[str, list[Step]], traded: Sequence[ATrade] = ()) -> dict[str, Any]:
    obs = []
    for steps in markets.values():
        for st in steps:
            if st.estimate.p_yes is not None:
                obs.append(_O(st.ticker, st.estimate.p_yes, st.yes_mid, st.outcome, st.t_left))
    rep = calibration_report(obs, MIN_N, 0.05)
    by_time: dict[str, Any] = {}
    for lo, hi in ((0, 3), (3, 6), (6, 10), (10, 16)):
        sub = [o for o in obs if lo <= o.t_left / 60 < hi]
        if sub:
            r = calibration_report(sub, MIN_N, 0.05).state
            by_time[f"{lo}-{hi}m"] = {"brier_model": r.brier_model, "brier_market": r.brier_market, "ece": r.ece}
    # overconfidence on the favoured side: mean(predicted - realised), per-prediction, market-weighted
    fav = [(max(o.p, 1 - o.p), (1 if (o.p >= 0.5) == (o.outcome == "yes") else 0)) for o in obs]
    over = sum(p - y for p, y in fav) / len(fav) if fav else None
    sel = [t for t in traded if t.p_side is not None]
    out = {
        "markets": rep.markets, "status": rep.state.status, "ece": rep.state.ece,
        "brier_model": rep.state.brier_model, "brier_market": rep.state.brier_market,
        "buckets": [{"label": b.label, "markets": b.markets, "predicted": b.predicted, "actual": b.actual}
                    for b in rep.buckets],
        "favoured_side_overconfidence": over, "by_minutes_left": by_time,
    }
    if sel:  # winner's-curse check on the trades a strategy actually picked
        out["traded_promised"] = sum(t.p_side for t in sel) / len(sel)
        out["traded_realised"] = sum(1 for t in sel if t.won) / len(sel)
    return out


@dataclass(frozen=True)
class _O:
    ticker: str
    p: float
    mid: float | None
    outcome: str
    t_left: float

    @property
    def features(self) -> dict[str, float]:
        f = {"p_yes": self.p}
        if self.mid is not None:
            f["yes_mid"] = self.mid  # cents, as in the backtest calibration
        return f


# ------------------------------------------------------------------ strategies
def _mom_side(st: Step, z: float = 1.0) -> str | None:
    m = st.features.mom_z_5m
    if m is None or abs(m) < z:
        return None
    return st.terms.side_for_direction("UP" if m > 0 else "DOWN")


def build_strategies(prm: BTCParams) -> tuple[list[Strategy], Strategy, list[Strategy]]:
    """(baselines, the baseline candidates are measured against, pre-registered candidates)."""
    ungated = replace(prm, require_calibration=False)

    def current(st: Step) -> str | None:
        sig = decide(_inputs(st, UNCALIBRATED), ungated)
        return sig.side if sig.approved else None

    def edge_rule(p: BTCParams, blend: float = 0.0, min_gap: float = 0.0,
                  max_price: float | None = None, min_t: float | None = None,
                  skip_regimes: frozenset[str] = frozenset()) -> Rule:
        def rule(st: Step) -> str | None:
            if not st.estimate.ok or st.regime.value in skip_regimes:
                return None
            if min_t is not None and st.t_left < min_t:
                return None
            edges = side_edges(_inputs(st, UNCALIBRATED), p)
            best = max(edges, key=lambda e: e.ev_low_cents) if edges else None
            if best is None:
                return None
            ev = best.ev_low_cents
            mid = st.yes_mid
            if blend or min_gap:
                if mid is None:
                    return None
                m_side = mid if best.side == "yes" else 100 - mid
                p_low = best.p_low * 100
                if blend:
                    ev = (1 - blend) * p_low + blend * m_side - best.cost_cents
                if min_gap and best.p_point * 100 - m_side < min_gap:
                    return None
            if ev < p.min_ev_cents:
                return None
            if max_price is not None and best.ask > max_price:
                return None
            return best.side
        return rule

    no_trade = Strategy("B0", "no trade", lambda st: None)
    simple = Strategy("B1", "simple baseline: 5-min momentum (z>=1)", _mom_side)
    cur = Strategy("B2", "current BTC engine (profile filters, calibration gate off)", current)
    base = Strategy("REF", "model edge (net EV at conservative probability >= min edge)", edge_rule(prm))
    hv = frozenset({BTCRegime.HIGH_VOLATILITY.value, BTCRegime.RAPID_REVERSAL.value})
    cands = [
        Strategy("C1", "REF + entry price cap 65c", edge_rule(prm, max_price=65.0),
                 hypothesis="Expensive entries need very high win rates; paper trades at 70c+ lost money.",
                 change="max entry price 65c"),
        Strategy("C2", "REF + at least 5 min left", edge_rule(prm, min_t=300.0),
                 hypothesis="Late entries pay near-certain prices, leaving no edge after fees.",
                 change="min time left 300s"),
        Strategy("C3", "REF + blend 50/50 with market price", edge_rule(prm, blend=0.5),
                 hypothesis="The market mid is better calibrated (lower Brier), so blending removes false edges.",
                 change="probability = 50% model + 50% market mid"),
        Strategy("C4", "REF + model must disagree with market by 5+ pts", edge_rule(prm, min_gap=5.0),
                 hypothesis="Only large disagreements are large enough to survive spread and fees.",
                 change="model - market >= 5 percentage points"),
        Strategy("C5", "REF + skip high-volatility / reversal regimes", edge_rule(prm, skip_regimes=hv),
                 hypothesis="Volatility estimates lag during shocks, so the model is least reliable then.",
                 change="no trades in HIGH_VOLATILITY / RAPID_REVERSAL"),
        Strategy("C6", "REF + early exit when model odds < 40%", edge_rule(prm), exit_p=0.40,
                 hypothesis="Cutting trades that turn against us reduces large losses by more than the exit costs.",
                 change="sell at bid when model P(side) < 0.40"),
    ]
    return [no_trade, simple, cur], base, cands


# ------------------------------------------------------------------ paper records
def paper_records(db_paths: Sequence[Path]) -> dict[str, Any]:
    """Closed PAPER positions from the bot database and its backups (de-duplicated). Read-only."""
    rows: dict[tuple, dict[str, Any]] = {}
    for p in db_paths:
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            for r in con.execute("SELECT * FROM positions WHERE mode='paper' AND status='closed'"):
                d = dict(r)
                rows[(d.get("client_order_id") or "", d.get("ticker"), str(d.get("opened_at")))] = d
            con.close()
        except sqlite3.Error:
            continue
    trades = []
    for d in rows.values():
        try:
            opened = datetime.fromisoformat(str(d["opened_at"]))
            close = datetime.fromisoformat(str(d["close_time"])) if d.get("close_time") else None
        except ValueError:
            continue
        t_left = (close - opened).total_seconds() if close else None
        pnl = float(d.get("realized_pnl") or 0.0)
        n = int(d.get("contracts") or 0) or 1
        entry = float(d.get("entry_price") or 0)
        reason = str(d.get("close_reason") or "")
        trades.append({"day": opened.strftime("%Y-%m-%d"), "entry": entry, "pnl": pnl, "contracts": n,
                       "pnl_per_contract": pnl / n, "t_left": t_left, "side": d.get("side"),
                       "risk": d.get("risk_level"), "settled": reason.startswith("settled"), "reason": reason,
                       "won": pnl > 0})
    trades.sort(key=lambda t: t["day"])
    out: dict[str, Any] = {"trades": len(trades), "sources": [str(p) for p in db_paths]}
    if not trades:
        return out

    def stats(ts: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(ts)
        per = [t["pnl_per_contract"] for t in ts]
        s = {"trades": n, "net_pnl": round(sum(t["pnl"] for t in ts), 2), "win_rate": sum(t["won"] for t in ts) / n,
             "avg_entry_cents": sum(t["entry"] for t in ts) / n, "ev_cents_per_contract": 100 * sum(per) / n,
             "sufficient": n >= MIN_N}
        s["ev_95"] = [100 * x for x in _boot(per)] if n >= 10 else None
        return s

    out["overall"] = stats(trades)
    groups: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for t in trades:
        e = t["entry"]
        groups["entry_price"]["<45c" if e < 45 else "45-55c" if e < 55 else "55-65c" if e < 65 else
                              "65-75c" if e < 75 else "75c+"].append(t)
        m = None if t["t_left"] is None else t["t_left"] / 60
        groups["minutes_left"]["n/a" if m is None else "<3m" if m < 3 else "3-6m" if m < 6 else
                               "6-10m" if m < 10 else "10m+"].append(t)
        groups["side"][str(t["side"])].append(t)
        groups["risk"][str(t["risk"])].append(t)
        groups["exit"]["settled" if t["settled"] else "sold early"].append(t)
        groups["day"][t["day"]].append(t)
    out["segments"] = {g: {k: stats(v) for k, v in sorted(d.items())} for g, d in groups.items()}
    return out


# ------------------------------------------------------------------ audit
def run_audit(ds: Dataset, costs: SimCosts | None = None, holdout_days: int = 7, risk: RiskLevel = RiskLevel.LOW,
              paper_dbs: Sequence[Path] = ()) -> dict[str, Any]:
    costs = costs or SimCosts()
    prof = load_profiles()[risk]
    prm = params_for(prof, costs)
    days = ds.days
    if len(days) <= holdout_days + 5:
        raise ValueError(f"need more than {holdout_days + 5} days of data (have {len(days)})")
    dev_days, hold_days = days[:-holdout_days], days[-holdout_days:]
    by_day = ds.by_day()
    dev = {k: v for d in dev_days for k, v in by_day[d].items()}
    hold = {k: v for d in hold_days for k, v in by_day[d].items()}
    baselines, base, cands = build_strategies(prm)

    res: dict[str, Any] = {"dev": {}, "holdout": {}}
    trades_dev: dict[str, list[ATrade]] = {}
    trades_hold: dict[str, list[ATrade]] = {}
    for s in [*baselines, base, *cands]:
        trades_dev[s.key] = run_strategy(dev, s, costs)
        trades_hold[s.key] = run_strategy(hold, s, costs)
        res["dev"][s.key] = {"name": s.name, **summarize(trades_dev[s.key])}
        res["holdout"][s.key] = {"name": s.name, **summarize(trades_hold[s.key])}

    decisions = []
    for c in cands:
        dv, bv = res["dev"][c.key], res["dev"][base.key]
        pr = paired(trades_dev[c.key], trades_dev[base.key], dev_days)
        hp = paired(trades_hold[c.key], trades_hold[base.key], hold_days)
        hd, hb = res["holdout"][c.key], res["holdout"][base.key]
        reasons = []
        if dv["trades"] < MIN_N:
            reasons.append(f"only {dv['trades']} development trades (need {MIN_N})")
        if not (dv.get("ev_95") and dv["ev_95"][0] > 0):
            reasons.append("development EV not shown above zero")
        if not (pr["ci95"] and pr["ci95"][0] > 0):
            reasons.append("improvement over baseline not shown (paired daily 95% interval includes 0)")
        if hd["net_pnl"] < hb["net_pnl"] or hd["net_pnl"] < 0:
            reasons.append("worse than baseline or losing on the untouched holdout")
        decisions.append({"key": c.key, "change": c.change, "hypothesis": c.hypothesis,
                          "dev_ev_cents": dv["ev_cents"], "dev_trades": dv["trades"],
                          "base_ev_cents": bv["ev_cents"], "base_trades": bv["trades"],
                          "paired_dev": pr, "paired_holdout": hp,
                          "holdout_net": hd["net_pnl"], "holdout_base_net": hb["net_pnl"],
                          "decision": "ACCEPT" if not reasons else "REJECT (retain baseline)", "reasons": reasons})

    sens = {}
    for slip in (0.0, 1.0, 2.0):
        c2 = replace(costs, slippage_cents=slip)
        sens[f"slippage {slip:g}c"] = summarize(run_strategy(dev, base, c2))
    sens["no fees"] = summarize(run_strategy(dev, base, replace(costs, fee_rate=0.0)))

    accepted = [d for d in decisions if d["decision"] == "ACCEPT"]
    ref_dev, ref_hold = res["dev"][base.key], res["holdout"][base.key]
    edge_shown = bool(ref_dev.get("ev_95") and ref_dev["ev_95"][0] > 0 and ref_hold["net_pnl"] > 0)
    if accepted:
        verdict = ("SUPPORTED CHANGE(S): " + ", ".join(f"{d['key']} ({d['change']})" for d in accepted)
                   + ". Paper-trade before any further step; live stays disabled.")
    elif edge_shown:
        verdict = "No candidate beat the baseline; baseline itself shows positive EV. Retain baseline (paper only)."
    else:
        verdict = ("NO DEMONSTRATED EDGE: neither the baseline nor any single change shows positive net expectancy "
                   "with enough samples and a holdout that agrees. Retain the baseline; the no-trade baseline "
                   "(B0) is the best validated choice for real money.")
    return {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": "PROFITABILITY AUDIT (historical replay + paper records; no invented data, no future data)",
        "data": {"markets": len(ds.markets), "days": len(days), "dev_days": [dev_days[0], dev_days[-1]],
                 "holdout_days": [hold_days[0], hold_days[-1]], "risk_profile": risk.value},
        "assumptions": {"entry": "taker at the 1-minute ask + slippage, one contract per market, first qualifying minute",
                        "fee_rate": costs.fee_rate, "slippage_cents": costs.slippage_cents, "min_minutes_left": costs.min_t_left / 60,
                        "min_trades_for_claims": MIN_N, "uncertainty": "bootstrap 95% intervals",
                        "strategies": "fixed rules, no parameter fitting; holdout used once"},
        "results": res, "baseline_for_candidates": base.key, "decisions": decisions,
        "segments_dev": {"REF": segments(trades_dev[base.key]), "B1": segments(trades_dev["B1"])},
        "calibration_dev": calibration(dev, trades_dev[base.key]),
        "cost_sensitivity_dev": sens,
        "paper": paper_records(paper_dbs) if paper_dbs else {"trades": 0},
        "verdict": verdict,
    }


# ------------------------------------------------------------------ text
def _f(v: float | None, fmt: str = "{:+.1f}") -> str:
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else fmt.format(v)


def format_audit(r: dict[str, Any]) -> str:
    d = r["data"]
    L = [f"# {r['label']}", "",
         f"Data: {d['markets']} markets, {d['days']} days. Development {d['dev_days'][0]}..{d['dev_days'][1]}; "
         f"untouched holdout {d['holdout_days'][0]}..{d['holdout_days'][1]}. Profile: {d['risk_profile']}.",
         f"Costs: taker fee rate {r['assumptions']['fee_rate']}, slippage {r['assumptions']['slippage_cents']}c. "
         f"Claims need {r['assumptions']['min_trades_for_claims']}+ trades.", "",
         "## Baselines and candidates (1 contract per trade)", "",
         "| | strategy | dev trades | dev EV c/contract (95%) | dev net $ | dev max DD $ | fees $ | win% (break-even%) | holdout trades | holdout net $ |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for k, v in r["results"]["dev"].items():
        h = r["results"]["holdout"][k]
        ci = f" ({_f(v['ev_95'][0])}..{_f(v['ev_95'][1])})" if v.get("ev_95") else ""
        wr = (f"{v['win_rate'] * 100:.0f} ({v['breakeven_win_rate'] * 100:.0f})" if v["trades"] else "—")
        L.append(f"| {k} | {v['name']} | {v['trades']} | {_f(v['ev_cents'])}{ci} | {v['net_pnl']:+.2f} | "
                 f"{v.get('max_drawdown', 0):.2f} | {v.get('fees', 0):.2f} | {wr} | {h['trades']} | {h['net_pnl']:+.2f} |")
    L += ["", f"## One change at a time (each vs unchanged baseline {r['baseline_for_candidates']})", ""]
    for c in r["decisions"]:
        pd_ = c["paired_dev"]
        ci = f"{_f(pd_['ci95'][0], '{:+.3f}')}..{_f(pd_['ci95'][1], '{:+.3f}')}" if pd_["ci95"] else "n/a"
        L += [f"**{c['key']} – {c['change']}**: {c['decision']}",
              f"- Hypothesis: {c['hypothesis']}",
              f"- Dev: {c['dev_trades']} trades, EV {_f(c['dev_ev_cents'])}c vs baseline {c['base_trades']} trades, "
              f"EV {_f(c['base_ev_cents'])}c; paired daily diff 95% {ci} $/day",
              f"- Holdout net: {c['holdout_net']:+.2f}$ vs baseline {c['holdout_base_net']:+.2f}$"]
        if c["reasons"]:
            L.append("- Why not accepted: " + "; ".join(c["reasons"]))
        L.append("")
    cal = r["calibration_dev"]
    L += ["## Calibration (development period, every market-minute)", "",
          f"Status {cal['status']} on {cal['markets']} markets · ECE {_f(cal['ece'], '{:.3f}')} · "
          f"Brier model {_f(cal['brier_model'], '{:.4f}')} vs market {_f(cal['brier_market'], '{:.4f}')}",
          f"Favoured-side overconfidence (predicted − realised): {_f(cal['favoured_side_overconfidence'], '{:+.3f}')}"]
    if "traded_promised" in cal:
        L.append(f"On trades the baseline picked: model promised {cal['traded_promised'] * 100:.1f}% wins, "
                 f"realised {cal['traded_realised'] * 100:.1f}% (gap = selection overconfidence).")
    for b in cal["buckets"]:
        if b["markets"]:
            L.append(f"- said {b['label']}: happened {b['actual'] * 100:.1f}% ({b['markets']} markets)")
    for k, v in cal["by_minutes_left"].items():
        L.append(f"- {k} left: Brier model {_f(v['brier_model'], '{:.4f}')} vs market {_f(v['brier_market'], '{:.4f}')}")
    L += ["", "## Where the baseline makes / loses money (development; segments need 30+ trades)", ""]
    for g, groups in r["segments_dev"]["REF"].items():
        L.append(f"- by {g}: " + "; ".join(
            f"{k} n={s['trades']} EV {_f(s['ev_cents'])}c [{s['verdict']}]" for k, s in groups.items()))
    L += ["", "## Execution-cost sensitivity (baseline, development)", ""]
    for k, s in r["cost_sensitivity_dev"].items():
        L.append(f"- {k}: {s['trades']} trades, EV {_f(s['ev_cents'])}c, net {s['net_pnl']:+.2f}$")
    p = r["paper"]
    L += ["", "## Paper-trading records", ""]
    if not p.get("trades"):
        L.append("No closed paper trades found.")
    else:
        o = p["overall"]
        ci = f" (95% {_f(o['ev_95'][0])}..{_f(o['ev_95'][1])})" if o.get("ev_95") else ""
        L.append(f"{o['trades']} trades · net {o['net_pnl']:+.2f}$ · win {o['win_rate'] * 100:.0f}% at avg entry "
                 f"{o['avg_entry_cents']:.1f}c · EV {_f(o['ev_cents_per_contract'])}c/contract{ci}")
        for g, groups in p["segments"].items():
            if g == "day":
                continue
            L.append(f"- by {g}: " + "; ".join(
                f"{k} n={s['trades']} EV {_f(s['ev_cents_per_contract'])}c win {s['win_rate'] * 100:.0f}%"
                for k, s in groups.items()))
    L += ["", "## VERDICT", "", r["verdict"], "",
          "_Live trading is not changed by this audit and remains disabled unless you enable it in .env._"]
    return "\n".join(L)
