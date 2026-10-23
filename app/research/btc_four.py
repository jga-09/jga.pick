"""Test of the four strategies (Hammer, Drift, Surge, Fade) on REAL cached Kalshi BTC 15-minute markets.

The rules are ported unchanged from the Kalshi AI project (shared/kalshi_ai/strategies/engine.py,
default StrategyParams, min edge 5c). They were written before these results were seen and are
NOT tuned here. Mapping to the data available in a historical replay:

* model probability = the BTC engine's point estimate P(YES) (the only probability model replayable);
* "confidence" (Surge >= 0.70, platform >= 0.50) = 1 - width of the model's probability range;
* trend toward YES (Hammer) = average of: 5-min BTC return (0.2% = full), EMA trend strength,
  strong-trend regime (+/-1), all signed by the contract direction, and the Kalshi 5-min price move
  (5c = full). There is no order-book history, so the order-book term is omitted;
* Fade "overreaction" = the side's Kalshi price fell >= 8c in 5 min, or BTC is stretched >= 2 sd
  (15-min momentum z) against the side (RSI is not available in the replay);
* takers (Hammer, Surge, Fade) pay the ask + 1c slippage + taker fee; Drift rests a bid one tick
  inside the spread for up to 5 min, filled only if the REAL trade tape prints through it (maker fee 0
  on KXBTC15M), held to settlement;
* one entry per market per strategy at its first qualifying minute with >= 2 min left; in the
  combined portfolio, markets where strategies disagree on the side are skipped (as in the original).

Development days first, then an untouched holdout (used once). Bootstrap 95% intervals, 30-trade minimum.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.research.btc_audit import MIN_N, ATrade, summarize
from app.research.btc_backtest import Dataset, SimCosts, Step, fee_per_contract
from app.research.btc_favmaker import Row, _rows
from app.strategy.btc_model import BTCRegime

NY = ZoneInfo("America/New_York")
MIN_EDGE = 5.0  # cents (Kalshi AI min_edge 0.05)
HAMMER_MIN_TREND = 0.25
FADE_MAX_PRICE = 35.0
FADE_MIN_DROP = 8.0
FADE_Z = 2.0
SURGE_EDGE = MIN_EDGE * 1.5
SURGE_CONF = 0.70
PLATFORM_CONF = 0.50
DRIFT_TTL = timedelta(minutes=5)
STRATEGIES = ("hammer", "drift", "surge", "fade")


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _ask(st: Step, side: str) -> float | None:
    return st.yes_ask if side == "yes" else st.no_ask


def _bid(st: Step, side: str) -> float | None:
    return st.yes_bid if side == "yes" else st.no_bid


def _p(st: Step, side: str) -> float | None:
    p = st.estimate.p_yes
    return None if p is None else (p if side == "yes" else 1 - p) * 100


def confidence(st: Step) -> float | None:
    w = st.estimate.width
    return None if w is None else 1 - w


def trend_toward_yes(st: Step) -> float | None:
    f, parts = st.features, []
    direction = 1.0 if st.terms.kind == "above" else -1.0 if st.terms.kind == "below" else None
    if direction is not None:
        if f.ret_5m_bps is not None:
            parts.append(_clamp(f.ret_5m_bps / 20.0) * direction)
        if f.trend_strength is not None:
            parts.append(_clamp(f.trend_strength) * direction)
        reg = {BTCRegime.STRONG_UPTREND: 1.0, BTCRegime.STRONG_DOWNTREND: -1.0}.get(st.regime, 0.0)
        parts.append(reg * direction)
    if st.kalshi_move_5m is not None:
        parts.append(_clamp(st.kalshi_move_5m / 5.0))
    return sum(parts) / len(parts) if parts else None


def stretched_against(st: Step, side: str) -> bool:
    sign = 1.0 if side == "yes" else -1.0
    if st.kalshi_move_5m is not None and st.kalshi_move_5m * sign <= -FADE_MIN_DROP:
        return True
    direction = 1.0 if st.terms.kind == "above" else -1.0 if st.terms.kind == "below" else None
    z = st.features.mom_z_15m
    return direction is not None and z is not None and z * direction * sign <= -FADE_Z


def surge_blackout(st: Step) -> bool:
    t = st.ts.astimezone(NY)
    return t.weekday() < 5 and 12 <= t.hour < 18


def signals(st: Step) -> dict[str, tuple[str, float]]:
    """{strategy: (side, entry price in cents)} that fire at this minute (before the conflict rule)."""
    out: dict[str, tuple[str, float]] = {}
    conf = confidence(st)
    if conf is None or conf < PLATFORM_CONF or not st.estimate.ok:
        return out
    edges = {}
    for side in ("yes", "no"):
        a, p = _ask(st, side), _p(st, side)
        if a is not None and p is not None and 0 < a < 100:
            edges[side] = (p - a, a)
    tyes = trend_toward_yes(st)
    best = None
    for side, (e, a) in edges.items():
        tr = None if tyes is None else (tyes if side == "yes" else -tyes)
        if e >= MIN_EDGE and tr is not None and tr >= HAMMER_MIN_TREND and (best is None or e > best[1]):
            best = (side, e, a)
    if best:
        out["hammer"] = (best[0], best[2])
    drift = None
    for side in ("yes", "no"):
        b, p = _bid(st, side), _p(st, side)
        if b is None or p is None or side not in edges:
            continue
        a = edges[side][1]
        limit = min(b + 1, a - 1)
        if not 1 <= limit < 100:
            continue
        e_lim = p - limit
        if e_lim >= MIN_EDGE and edges[side][0] >= 0 and (drift is None or e_lim > drift[2]):
            drift = (side, limit, e_lim)
    if drift:
        out["drift"] = (drift[0], drift[1])
        e_ask, a = edges[drift[0]]
        if e_ask >= SURGE_EDGE and conf >= SURGE_CONF and not surge_blackout(st):
            out["surge"] = (drift[0], a)
    fade = None
    for side, (e, a) in edges.items():
        if a <= FADE_MAX_PRICE and e >= MIN_EDGE and stretched_against(st, side) and (fade is None or e > fade[1]):
            fade = (side, e, a)
    if fade:
        out["fade"] = (fade[0], fade[2])
    return out


def _filled(st: Step, side: str, price: float, rows: Sequence[Row]) -> bool:
    start, end = st.ts.timestamp(), min((st.ts + DRIFT_TTL), st.close - timedelta(seconds=60)).timestamp()
    for ts, yp, _ in rows:
        if ts <= start:
            continue
        if ts > end:
            break
        if (yp < price) if side == "yes" else (100 - yp < price):
            return True
    return False


def market_trades(steps: Sequence[Step], rows: Sequence[Row], costs: SimCosts) -> dict[str, ATrade]:
    """First entry per strategy in one market, plus 'combined' (conflict rule)."""
    out: dict[str, ATrade] = {}
    combined_sides: set[str] = set()
    for st in steps:
        if st.t_left < costs.min_t_left:
            continue
        sig = signals(st)
        for name, (side, price) in sig.items():
            if name in out:
                continue
            won = st.outcome == side
            if name == "drift":
                if not _filled(st, side, price, rows):
                    out[name] = None  # type: ignore[assignment]  # order placed, never filled
                    continue
                t = ATrade(st.ticker, st.day, side, price, 0.0, 0.0, 100.0 if won else 0.0, won,
                           st.regime.value, st.t_left, _p(st, side), None)
            else:
                fill = min(99.0, price + costs.slippage_cents)
                t = ATrade(st.ticker, st.day, side, price, fill - price, fee_per_contract(fill, costs),
                           100.0 if won else 0.0, won, st.regime.value, st.t_left, _p(st, side), None)
            out[name] = t
            combined_sides.add(side)
    real = {k: v for k, v in out.items() if v is not None}
    if real and len(combined_sides) == 1:
        first = min(real.values(), key=lambda t: -t.t_left)  # earliest entry
        real["combined"] = first
    return real


def run_four(cache: Path, ds: Dataset, holdout_days: int = 7, costs: SimCosts | None = None) -> dict[str, Any]:
    costs = costs or SimCosts()
    days = ds.days
    if len(days) <= holdout_days + 5:
        raise ValueError(f"need more than {holdout_days + 5} days of data (have {len(days)})")
    hold_set = set(days[-holdout_days:])
    dev: dict[str, list[ATrade]] = defaultdict(list)
    hold: dict[str, list[ATrade]] = defaultdict(list)
    for f in sorted(cache.glob("KXBTC*.json.gz")):  # one market at a time: memory stays flat
        try:
            rec = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        steps = ds.markets.get((rec.get("market") or {}).get("ticker") or "")
        if not steps:
            continue
        rows = _rows(rec)
        del rec
        for name, t in market_trades(steps, rows, costs).items():
            (hold if steps[0].day in hold_set else dev)[name].append(t)
    rows_out = []
    for name in (*STRATEGIES, "combined"):
        td = sorted(dev[name], key=lambda t: t.day)
        th = sorted(hold[name], key=lambda t: t.day)
        sd, sh = summarize(td), summarize(th)
        ok = sd["trades"] >= MIN_N and sd.get("ev_95") and sd["ev_95"][0] > 0 and sh["net_pnl"] > 0
        rows_out.append({"strategy": name, "dev": sd, "holdout": sh,
                         "decision": "SUPPORTED (paper-test next)" if ok else "NOT SUPPORTED"})
    sup = [r["strategy"] for r in rows_out if r["decision"].startswith("SUPPORTED")]
    verdict = (f"SUPPORTED: {', '.join(sup)} passed development AND the untouched holdout. Paper-test only."
               if sup else "NOT SUPPORTED: none of Hammer, Drift, Surge, Fade (alone or combined) shows positive net EV "
               "with 30+ trades and a profitable holdout. Retain the baseline.")
    return {"label": "FOUR-STRATEGY TEST (Hammer / Drift / Surge / Fade, rules ported unchanged)",
            "data": {"markets": len(ds.markets), "dev_days": [days[0], days[-holdout_days - 1]],
                     "holdout_days": [days[-holdout_days], days[-1]]},
            "rows": rows_out, "verdict": verdict}


def format_four(r: dict[str, Any]) -> str:
    d = r["data"]
    L = [r["label"], f"{d['markets']} markets. Development {d['dev_days'][0]}..{d['dev_days'][1]}; "
         f"holdout {d['holdout_days'][0]}..{d['holdout_days'][1]}. 1 contract per trade.", ""]
    for x in r["rows"]:
        s, h = x["dev"], x["holdout"]
        if not s["trades"]:
            L.append(f"{x['strategy']:<9} dev 0 trades | holdout {h['trades']} trades net {h['net_pnl']:+.2f}$  -> {x['decision']}")
            continue
        ci = f" (95% {s['ev_95'][0]:+.1f}..{s['ev_95'][1]:+.1f})" if s.get("ev_95") else ""
        L.append(f"{x['strategy']:<9} dev {s['trades']:>4} trades  win {s['win_rate'] * 100:3.0f}% "
                 f"(break-even {s['breakeven_win_rate'] * 100:.0f}%)  EV {s['ev_cents']:+.2f}c{ci}  net {s['net_pnl']:+.2f}$ "
                 f"maxDD {s['max_drawdown']:.2f}$ | holdout {h['trades']} trades net {h['net_pnl']:+.2f}$  -> {x['decision']}")
    L += ["", "VERDICT: " + r["verdict"]]
    return "\n".join(L)
