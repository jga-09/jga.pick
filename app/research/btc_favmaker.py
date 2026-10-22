"""'Favourite maker' test: post a bid on the FAVOURITE side (no model), hold to settlement.

Idea from an outside source (Pondletter's results table: "crypto favourite maker, with the filter",
1,459 trades, +2.07c/trade). Their filter is not public, so this tests simple, pre-registered
versions - fixed rules chosen before looking at results, never tuned:

* At the first minute with >= 2 min left where the favourite side's mid is within [lo, hi],
  rest a bid at that side's current best bid. Cancel 60 s before close. One order per market.
* Fill rules from the real trade tape (trades AFTER the order is placed only):
  - THROUGH (conservative, used for decisions): a trade printed strictly beyond our price, so
    every order at our price must have been filled;
  - TOUCH (optimistic, reference only): a seller traded at our price (we might have been in the queue).
* Maker fee 0: KXBTC15M is a 'quadratic' series (no maker fee; verified against the published
  fee code). Settlement pays 100c if the side wins, else 0.
* Evaluated on development days and once on the untouched final holdout, with bootstrap 95%
  intervals and a 30-trade minimum, using the same summaries as the profitability audit.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.kalshi.market_data import TradePrint
from app.research.btc_audit import MIN_N, ATrade, summarize
from app.research.btc_backtest import Dataset, Step
from app.research.replay import _record_trades

VARIANTS = ((60.0, 80.0), (70.0, 90.0), (80.0, 95.0))


Row = tuple[float, float, str]  # (unix time, YES price in cents, taker side "yes"/"no")


def _rows(rec: dict[str, Any]) -> list[Row]:
    """Light-weight trade rows for one cached market (no per-trade objects: tapes can be huge)."""
    if rec.get("v") == 2:
        return sorted((float(r[0]), float(r[1]), "yes" if r[3] == "y" else "no") for r in rec.get("trades") or [])
    return sorted((t.ts.timestamp(), t.yes_price, t.taker_side) for t in _record_trades(rec))


def compute_fills(cache: Path, ds: Dataset) -> tuple[dict[str, dict[tuple[float, float, str], bool]], int]:
    """Stream the cache ONE market at a time and decide every variant's fill for it.

    Returns ({ticker: {(lo, hi, rule): filled}}, markets with a trade tape). Memory stays flat."""
    out: dict[str, dict[tuple[float, float, str], bool]] = {}
    with_tape = 0
    for f in sorted(cache.glob("KXBTC*.json.gz")):
        try:
            rec = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        ticker = (rec.get("market") or {}).get("ticker")
        steps = ds.markets.get(ticker or "")
        if not steps:
            continue
        rows = _rows(rec)
        del rec
        with_tape += bool(rows)
        res: dict[tuple[float, float, str], bool] = {}
        for lo, hi in VARIANTS:
            o = place(steps, lo, hi)
            for rule in ("through", "touch"):
                res[(lo, hi, rule)] = o is not None and filled_rows(o, rows, rule)
        out[ticker] = res
    return out, with_tape


@dataclass(frozen=True)
class Order:
    step: Step
    side: str
    price: float  # cents for our side


def place(steps: Sequence[Step], lo: float, hi: float, min_t_left: float = 120.0) -> Order | None:
    for st in steps:
        if st.t_left < min_t_left or st.yes_mid is None or st.yes_bid is None or st.yes_ask is None:
            continue
        side = "yes" if st.yes_mid >= 50 else "no"
        fav_mid = st.yes_mid if side == "yes" else 100 - st.yes_mid
        if lo <= fav_mid <= hi:
            bid = st.yes_bid if side == "yes" else st.no_bid
            return Order(st, side, bid) if bid is not None and bid >= 1 else None
    return None


def filled_rows(order: Order, rows: Sequence[Row], rule: str, cancel_before: float = 60.0) -> bool:
    start = order.step.ts.timestamp()
    end = (order.step.close - timedelta(seconds=cancel_before)).timestamp()
    for ts, yes_price, taker in rows:
        if ts <= start:
            continue
        if ts > end:
            break
        if order.side == "yes":
            if yes_price < order.price or (rule == "touch" and yes_price <= order.price and taker == "no"):
                return True
        else:
            no_price = 100 - yes_price
            if no_price < order.price or (rule == "touch" and no_price <= order.price and taker == "yes"):
                return True
    return False


def filled(order: Order, trades: Sequence[TradePrint], rule: str, cancel_before: float = 60.0) -> bool:
    rows = sorted((t.ts.timestamp(), t.yes_price, t.taker_side) for t in trades)
    return filled_rows(order, rows, rule, cancel_before)


def run_variant(markets: dict[str, list[Step]], fills: dict[str, dict[tuple[float, float, str], bool]],
                lo: float, hi: float, rule: str) -> tuple[list[ATrade], int]:
    out, orders = [], 0
    for ticker, steps in markets.items():
        o = place(steps, lo, hi)
        if o is None:
            continue
        orders += 1
        if not fills.get(ticker, {}).get((lo, hi, rule)):
            continue
        won = o.step.outcome == o.side
        out.append(ATrade(ticker, o.step.day, o.side, o.price, 0.0, 0.0, 100.0 if won else 0.0, won,
                          o.step.regime.value, o.step.t_left, None, None))
    return out, orders


def run_favmaker(ds: Dataset, fills: dict[str, dict[tuple[float, float, str], bool]], holdout_days: int = 7,
                 with_tape: int = 0) -> dict[str, Any]:
    days = ds.days
    if len(days) <= holdout_days + 5:
        raise ValueError(f"need more than {holdout_days + 5} days of data (have {len(days)})")
    by_day = ds.by_day()
    dev_days, hold_days = days[:-holdout_days], days[-holdout_days:]
    dev = {k: v for d in dev_days for k, v in by_day[d].items()}
    hold = {k: v for d in hold_days for k, v in by_day[d].items()}
    rows = []
    for lo, hi in VARIANTS:
        for rule in ("through", "touch"):
            td, od = run_variant(dev, fills, lo, hi, rule)
            th, oh = run_variant(hold, fills, lo, hi, rule)
            sd, sh = summarize(td), summarize(th)
            decision = None
            if rule == "through":
                ok = sd["trades"] >= MIN_N and sd.get("ev_95") and sd["ev_95"][0] > 0 and sh["net_pnl"] > 0
                decision = "SUPPORTED (paper-test next)" if ok else "NOT SUPPORTED"
            rows.append({"variant": f"favourite {lo:.0f}-{hi:.0f}c", "rule": rule,
                         "dev": sd, "dev_orders": od, "holdout": sh, "holdout_orders": oh, "decision": decision})
    supported = [r for r in rows if r["decision"] and r["decision"].startswith("SUPPORTED")]
    verdict = ("SUPPORTED: " + ", ".join(r["variant"] for r in supported) +
               " passed on development AND the untouched holdout with the conservative fill rule. "
               "Next step is paper trading only." if supported else
               "NOT SUPPORTED on this data: no pre-registered favourite-maker version shows positive net EV with "
               "30+ conservative fills and a profitable holdout. Retain the baseline.")
    return {"label": "FAVOURITE-MAKER TEST (post a bid on the favourite; real trade tape; maker fee 0)",
            "data": {"markets": len(ds.markets), "markets_with_trade_tape": with_tape,
                     "dev_days": [dev_days[0], dev_days[-1]], "holdout_days": [hold_days[0], hold_days[-1]]},
            "rows": rows, "verdict": verdict}


def format_favmaker(r: dict[str, Any]) -> str:
    d = r["data"]
    L = [r["label"], f"Markets {d['markets']} ({d['markets_with_trade_tape']} with trade tape). Development "
         f"{d['dev_days'][0]}..{d['dev_days'][1]}; holdout {d['holdout_days'][0]}..{d['holdout_days'][1]}.",
         "THROUGH = conservative fills (decides); TOUCH = optimistic queue fills (reference only).", ""]
    for x in r["rows"]:
        s, h = x["dev"], x["holdout"]
        ci = f" (95% {s['ev_95'][0]:+.1f}..{s['ev_95'][1]:+.1f})" if s.get("ev_95") else ""
        ev = f"{s['ev_cents']:+.2f}c" if s["ev_cents"] is not None else "—"
        fill = f"{s['trades']}/{x['dev_orders']}" if x["dev_orders"] else "0/0"
        line = (f"{x['variant']:<18} {x['rule']:<8} dev fills {fill:<9} EV {ev}{ci} net {s['net_pnl']:+.2f}$ "
                f"win {s.get('win_rate', 0) * 100:.0f}% | holdout {h['trades']} trades net {h['net_pnl']:+.2f}$")
        if x["decision"]:
            line += f"  -> {x['decision']}"
        L.append(line)
    L += ["", "VERDICT: " + r["verdict"]]
    return "\n".join(L)
