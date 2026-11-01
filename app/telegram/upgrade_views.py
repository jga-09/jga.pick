"""Screens added by the upgrade: per-asset cards (/btc /eth /sol /gold), /orders, /models, /kill.

Every card separates signal strength (a score), model probability, market probability and expected value
after Kalshi's rounded-up fee and slippage, and states the exact NO TRADE reason.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from app.strategy.decision_record import DecisionRecord, from_btc, from_legacy
from app.telegram.keyboards import Screen

if TYPE_CHECKING:
    from app.runtime import BotRuntime

HOME = ("🏠 Home", "home")
NA = "—"


def _p(x: float | None) -> str:
    return NA if x is None else f"{x * 100:.1f}%"


def _c(x: float | None, sign: bool = True) -> str:
    return NA if x is None else (f"{x:+.1f}¢" if sign else f"{x:.1f}¢")


def record_for(rt: BotRuntime, asset: str) -> DecisionRecord | None:
    info = rt.discovery.state.current.get(asset)
    if info is None:
        return None
    sig = rt._last_signal.get(info.ticker)
    snap = rt.market_data.latest.get(info.ticker)
    if sig is None or snap is None:
        return None
    s = rt.settings
    if sig.btc is not None:
        return from_btc(sig, snap, s.paper_slippage_cents, s.fee_rate)
    decision = rt.check_risk(sig, snap)
    return from_legacy(sig, snap, decision, s.paper_slippage_cents, s.fee_rate, s.strategy_mode)


def decision_lines(r: DecisionRecord) -> list[str]:
    ages = ", ".join(f"{k} {v:.0f}s" for k, v in r.data_age_sec.items() if v is not None) or NA
    edge = r.edge_pts
    lines = [
        f"<b>{escape(r.ticker)}</b> · {r.t_left_sec / 60:.1f} min left",
        f"Direction: <b>{r.direction}</b>" + (f" ({r.side.upper()})" if r.side else ""),
        f"Signal strength: {r.strength if r.strength is not None else NA}/100 <i>(a score, not a probability)</i>",
        f"Model probability: {_p(r.model_p)}" + (f" (conservative {_p(r.model_p_low)})" if r.model_p_low else ""),
        f"Market probability: {_p(r.market_p)}" + (f" · edge {edge:+.1f} pts" if edge is not None else ""),
        f"Ask: {_c(r.ask, False)} · fee {_c(r.fee_c, False)} · slippage {_c(r.slippage_c, False)}",
        f"Expected value after costs: <b>{_c(r.ev_c)}</b>/contract",
        f"Spread: {_c(r.spread, False)} · liquidity: {NA if r.liquidity is None else f'{r.liquidity:.0f}'} contracts",
        f"Model reliability: {escape(r.reliability)} · data age: {ages}",
        "",
        f"{'✅' if r.approved else '⚪'} <b>{r.verdict}</b>",
    ]
    lines += [f"• {escape(x)}" for x in r.reasons[:6]]
    return lines


def asset_card(rt: BotRuntime, asset: str) -> Screen:
    asset = asset.upper()
    if asset == "GOLD":
        return gold(rt)
    nav = [[("🔄 Refresh", f"asset:{asset}"), ("🤖 Models", "models")], [HOME]]
    title = f"📈 <b>{asset} 15-MIN</b>"
    if asset not in rt.settings.asset_list:
        why = ("The BTC engine (STRATEGY_MODE=btc) only scans BTC." if rt.settings.strategy_mode == "btc"
               else f"{asset} is not in ASSETS.")
        return Screen(f"{title}\n\n{why} {asset} is kept for research comparison: see /models "
                      "(walk-forward results per asset).", nav, route=f"asset:{asset}")
    r = record_for(rt, asset)
    if r is None:
        return Screen(f"{title}\n\nNo current market or no evaluation yet. Is the scanner running (/status)?",
                      nav, route=f"asset:{asset}", refreshable=True)
    return Screen("\n".join([title, "", *decision_lines(r)])[:3900], nav, route=f"asset:{asset}", refreshable=True)


def gold(rt: BotRuntime) -> Screen:
    nav = [[("🔄 Refresh", "asset:GOLD"), ("🤖 Models", "models")], [HOME]]
    title = "🥇 <b>GOLD</b> <i>(research only - never traded)</i>"
    if not rt.settings.gold_enabled:
        return Screen(f"{title}\n\nGold is off. To research it: set <code>GOLD_ENABLED=true</code> and run\n"
                      "<code>python scripts/gold_check.py</code> to confirm Kalshi's Gold series, settlement rules "
                      "and the Pyth price feed id.", nav, route="asset:GOLD")
    st = rt.gold
    if st is None:
        return Screen(f"{title}\n\nChecking Kalshi… refresh in a minute.", nav, route="asset:GOLD", refreshable=True)
    lines = [title, "", f"Series found: {escape(', '.join(s['ticker'] for s in st.series) or 'none')}"]
    if st.market:
        lines += [f"Current: <b>{escape(st.market.ticker)}</b> (closes {st.market.close_time:%H:%M} UTC)",
                  f"Strike: {st.check.strike if st.check else NA} · source: {st.check.source if st.check else NA}",
                  f"YES bid/ask: {_c(st.yes_bid, False)} / {_c(st.yes_ask, False)} · spread {_c(st.spread, False)}"]
    lines.append(f"Gold price: {f'{st.price:,.2f}' if st.price else NA}"
                 + (f" ({st.price_ts:%H:%M:%S} UTC)" if st.price_ts else ""))
    lines += ["", "✅ Research checks passed" if st.ready else "⚠️ Research checks not passed:"]
    lines += [f"• {escape(p)}" for p in st.problems[:8]]
    lines += ["", f"⚪ <b>NO TRADE</b> · {escape(st.trading_reason)}"]
    return Screen("\n".join(lines)[:3900], nav, route="asset:GOLD", refreshable=True)


def orders(rt: BotRuntime) -> Screen:
    nav = [[("🔄 Refresh", "orders"), ("📂 Positions", "pos")], [HOME]]
    lines = ["🧾 <b>LIVE ORDERS</b>"]
    rows = rt.repo.live_orders(limit=12)
    if not rows:
        lines.append("No live orders yet (paper trades are under /trades).")
    for o in rows:
        icon = {"filled": "✅", "partial": "🟡", "no_fill": "⚪", "not_placed": "⚪", "error": "❌",
                "unknown": "❓", "submitting": "❓", "accepted": "❓"}.get(o["status"], "•")
        lines.append(f"{icon} {o['created_at']:%m-%d %H:%M} {o['action'].upper()} {o['side'].upper()} "
                     f"{o['contracts']} @ {o['price']:.0f}¢ · {escape(o['ticker'])} · {o['status']}"
                     + (f" · fee ${o['fee']:.2f}" if o.get("fee") else ""))
    rep = rt.reconcile_report
    lines += ["", "<b>Check vs Kalshi</b>"]
    if rep is None:
        lines.append("Not run (live mode only).")
    else:
        lines.append(f"{'✅ in sync' if rep.ok else '🚨 mismatch'} · {rep.at:%H:%M:%S} UTC"
                     + (f" · {escape(rep.error)}" if rep.error else ""))
        lines += [f"• {escape(m.text)}" for m in rep.mismatches[:6]]
        lines += [f"• {escape(n)}" for n in rep.recovered[:6]]
        if rep.blocked:
            lines.append(f"Blocked markets: {escape(', '.join(sorted(rep.blocked)))}")
    return Screen("\n".join(lines)[:3900], nav, route="orders", refreshable=True)


def models(rt: BotRuntime, path: Path | None = None) -> Screen:
    nav = [[("🧪 Backtest", "bt"), ("📈 BTC", "asset:BTC")], [HOME]]
    path = path or Path("data") / "model_compare.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return Screen("🤖 <b>MODELS</b>\n\nNo model comparison yet. In the terminal:\n"
                      "<code>pip install -r requirements-research.txt</code>\n"
                      "<code>python scripts/replay.py --days 30 --no-trades</code>\n"
                      "<code>python scripts/model_compare.py</code>", nav, route="models")
    lines = ["🤖 <b>MODELS</b> <i>(untouched holdout days; past data, not live results)</i>"]
    for asset, d in data.items():
        lines += ["", f"<b>{escape(asset)}</b>" + (f" · {escape(d['note'])}" if d.get("note") else "")]
        for name, r in (d.get("holdout") or {}).items():
            p, t = r["prob"], r["trades"]
            if not p.get("n_markets"):
                continue
            beats = "✅" if p["brier"] < p["brier_market"] else "➖"
            avg = f"{t['avg'] * 100:+.1f}¢" if t.get("avg") is not None else NA
            lines.append(f"{beats} {escape(name)}: Brier {p['brier']:.3f} vs mkt {p['brier_market']:.3f} · "
                         f"{t['trades']} trades · {avg}/trade")
    lines += ["", "✅ = more accurate than Kalshi's own price. Accuracy alone is not profit: a model needs a "
              "positive average after fees on BOTH walk-forward and holdout (see scripts/model_compare.py)."]
    return Screen("\n".join(lines)[:3900], nav, route="models")
