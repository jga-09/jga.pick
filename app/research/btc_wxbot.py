"""The BTC Up/Down strategy from the open-source 'polymarket-kalshi-weather-bot' (MIT), on Kalshi.

Ported unchanged from backend/core/signals.py + backend/data/crypto.py (its defaults), applied to
Kalshi's 15-minute BTC markets (Kalshi has no 5-minute series). From the last 60 Coinbase 1-minute bars:
  RSI(14) mean-reversion signal, momentum blend (0.5*1m + 0.35*5m + 0.15*15m, 0.10% = full),
  VWAP deviation (30 bars, 0.05% = full), SMA5 vs SMA15 (0.03% = full), market-skew fade (x4);
  weights 0.20/0.35/0.20/0.15/0.10; convergence = 2 of the 4 indicators agree (|s| > 0.05);
  P(UP) = 0.50 + 0.15 * composite, clipped to 0.35..0.65; edge = P - market (mid);
  trade when edge >= 2%, entry price <= 55c, >= 60 s left; one trade per market.
Differences, stated: Kalshi taker fees and 1c slippage ARE charged (the original simulator charges
none); entry is at the Kalshi ask. Development days, then an untouched holdout used once.
"""

from __future__ import annotations


from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.data.btc_feed import MinuteBar
from app.research.btc_audit import MIN_N, Strategy, run_strategy, summarize
from app.research.btc_backtest import Dataset, SimCosts, Step, load_bars, window

W = {"rsi": 0.20, "mom": 0.35, "vwap": 0.20, "sma": 0.15, "skew": 0.10}
MIN_EDGE = 0.02
MAX_ENTRY = 55.0


def _rsi(closes: Sequence[float], n: int = 14) -> float:
    if len(closes) < n + 1:
        return 50.0
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:], strict=True):
        d = b - a
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def indicators(bars: Sequence[MinuteBar]) -> dict[str, float] | None:
    if len(bars) < 20:
        return None
    closes = [b.close for b in bars]
    cur = closes[-1]

    def pct(k: int) -> float:
        return (cur - closes[-1 - k]) / closes[-1 - k] * 100 if len(closes) > k and closes[-1 - k] > 0 else 0.0

    vw = bars[-min(30, len(bars)):]
    tv = sum(b.volume for b in vw)
    vwap = sum((b.high + b.low + b.close) / 3 * b.volume for b in vw) / tv if tv > 0 else cur
    sma5 = sum(closes[-5:]) / 5
    sma15 = sum(closes[-15:]) / 15
    return {"rsi": _rsi(closes), "m1": pct(1), "m5": pct(5), "m15": pct(15),
            "vwap_dev": (cur - vwap) / vwap * 100 if vwap > 0 else 0.0,
            "sma_x": (sma5 - sma15) / cur * 100 if cur > 0 else 0.0}


def p_up(ind: dict[str, float], market_up: float) -> tuple[float, bool]:
    """(model P(UP), convergence) exactly as the original."""
    r = ind["rsi"]
    if r < 30:
        rs = 0.5 + (30 - r) / 30
    elif r > 70:
        rs = -0.5 - (r - 70) / 30
    elif r < 45:
        rs = (45 - r) / 30
    elif r > 55:
        rs = -(r - 55) / 30
    else:
        rs = 0.0
    rs = max(-1.0, min(1.0, rs))
    mom = max(-1.0, min(1.0, (ind["m1"] * 0.5 + ind["m5"] * 0.35 + ind["m15"] * 0.15) / 0.10))
    vw = max(-1.0, min(1.0, ind["vwap_dev"] / 0.05))
    sm = max(-1.0, min(1.0, ind["sma_x"] / 0.03))
    sk = max(-1.0, min(1.0, -(market_up - 0.50) * 4))
    signs = [rs, mom, vw, sm]
    conv = sum(s > 0.05 for s in signs) >= 2 or sum(s < -0.05 for s in signs) >= 2
    comp = rs * W["rsi"] + mom * W["mom"] + vw * W["vwap"] + sm * W["sma"] + sk * W["skew"]
    return max(0.35, min(0.65, 0.50 + comp * 0.15)), conv


def make_rule(bars: Sequence[MinuteBar]):
    keys = [b.ts for b in bars]

    def rule(st: Step) -> str | None:
        if st.yes_mid is None or st.terms.kind is None:
            return None
        hist = window(bars, keys, st.ts, 60)
        if not hist or (st.ts - hist[-1].ts).total_seconds() > 120:
            return None
        ind = indicators(hist)
        if ind is None:
            return None
        up_side = st.terms.side_for_direction("UP")
        mid_up = st.yes_mid if up_side == "yes" else 100 - st.yes_mid
        m_up = mid_up / 100
        if not 0.02 <= m_up <= 0.98:
            return None
        p, conv = p_up(ind, m_up)
        up_edge, down_edge = p - m_up, (1 - p) - (1 - m_up)
        direction, edge = ("UP", up_edge) if up_edge >= down_edge else ("DOWN", down_edge)
        side = st.terms.side_for_direction(direction)
        ask = st.yes_ask if side == "yes" else st.no_ask
        if not conv or edge < MIN_EDGE or ask is None or ask > MAX_ENTRY:
            return None
        return side
    return rule


def run_wxbot(ds: Dataset, cache: Path, holdout_days: int = 7, costs: SimCosts | None = None) -> dict[str, Any]:
    costs = replace(costs or SimCosts(), min_t_left=60.0)
    days = ds.days
    if len(days) <= holdout_days + 5:
        raise ValueError(f"need more than {holdout_days + 5} days of data (have {len(days)})")
    by_day = ds.by_day()
    dev = {k: v for d in days[:-holdout_days] for k, v in by_day[d].items()}
    hold = {k: v for d in days[-holdout_days:] for k, v in by_day[d].items()}
    strat = Strategy("WX", "weather-bot BTC rules", make_rule(load_bars(cache)))
    rows = []
    for label, c in (("as charged (taker fee + 1c slippage)", costs),
                     ("no fees, no slippage (the original's assumption)", replace(costs, fee_rate=0.0, slippage_cents=0.0))):
        sd, sh = summarize(run_strategy(dev, strat, c)), summarize(run_strategy(hold, strat, c))
        rows.append({"costs": label, "dev": sd, "holdout": sh})
    main = rows[0]
    ok = main["dev"]["trades"] >= MIN_N and main["dev"].get("ev_95") and main["dev"]["ev_95"][0] > 0 \
        and main["holdout"]["net_pnl"] > 0
    verdict = ("SUPPORTED: positive net EV on development and the untouched holdout. Paper-test only." if ok else
               "NOT SUPPORTED: the weather-bot BTC rules do not show positive net EV after Kalshi costs with 30+ "
               "trades and a profitable holdout. Retain the baseline.")
    return {"label": "WEATHER-BOT BTC STRATEGY ON KALSHI 15-MIN (rules ported unchanged)",
            "data": {"markets": len(ds.markets), "dev_days": [days[0], days[-holdout_days - 1]],
                     "holdout_days": [days[-holdout_days], days[-1]]},
            "rows": rows, "verdict": verdict}


def format_wxbot(r: dict[str, Any]) -> str:
    d = r["data"]
    L = [r["label"], f"{d['markets']} markets. Development {d['dev_days'][0]}..{d['dev_days'][1]}; holdout "
         f"{d['holdout_days'][0]}..{d['holdout_days'][1]}. 1 contract per trade.", ""]
    for x in r["rows"]:
        s, h = x["dev"], x["holdout"]
        if not s["trades"]:
            L.append(f"{x['costs']}: 0 trades")
            continue
        ci = f" (95% {s['ev_95'][0]:+.1f}..{s['ev_95'][1]:+.1f})" if s.get("ev_95") else ""
        L.append(f"{x['costs']}:")
        L.append(f"   dev {s['trades']} trades · win {s['settle_win_rate'] * 100:.1f}% at avg {s['avg_entry_cents']:.1f}c "
                 f"(break-even {s['breakeven_win_rate'] * 100:.1f}%) · EV {s['ev_cents']:+.2f}c{ci} · net "
                 f"{s['net_pnl']:+.2f}$ · maxDD {s['max_drawdown']:.2f}$ | holdout {h['trades']} trades net {h['net_pnl']:+.2f}$")
    L += ["", "VERDICT: " + r["verdict"]]
    return "\n".join(L)



