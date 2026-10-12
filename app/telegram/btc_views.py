"""Telegram screens for the BTC 15-minute engine (STRATEGY_MODE=btc).

Rules: never show a fabricated price or statistic (``N/A`` when unknown), never
show an approved trade button when the RiskManager rejected the setup, and keep
backtest / paper / live numbers strictly separate and labelled.
"""

from __future__ import annotations

import math
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app import APP_NAME
from app.research.btc_backtest import load_report
from app.telegram.keyboards import BACK, BTC_MENU, Button, Screen, grid
from app.trading.positions import Position
from app.utils.time import fmt_countdown

if TYPE_CHECKING:
    from app.runtime import BotRuntime, MarketView

SEP = "━━━━━━━━━━━━━━━━━━"
NA = "N/A"
SCORE_NOTE = "<i>Strength is a 0-100 evidence score, not a win probability.</i>"


def _usd(v: float | None, signed: bool = True) -> str:
    if v is None:
        return NA
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else ""
    return f"{sign}${abs(v):,.2f}"


def _c(v: float | None) -> str:
    return NA if v is None else (f"{v:.0f}¢" if abs(v - round(v)) < 0.05 else f"{v:.1f}¢")


def _pct(v: float | None, digits: int = 0) -> str:
    return NA if v is None else f"{v * 100:.{digits}f}%"


def _dot(ok: bool | None, yes: str, no: str) -> str:
    return f"🟢 {yes}" if ok else f"🔴 {no}"


def btc_view(rt: BotRuntime) -> MarketView | None:
    for v in rt.markets():
        if v.info.asset == "BTC":
            return v
    return None


def _statuses(rt: BotRuntime) -> list[str]:
    st, h = rt.state, rt.health()
    bot = ("🚨 E-STOP" if st.emergency_stop else "⏸ PAUSED" if st.paused and st.running
           else _dot(st.running, "RUNNING", "STOPPED"))
    if h["fixture"]:
        kalshi = "🧪 FIXTURE"
    elif h["auth_failed"]:
        kalshi = "🔴 AUTH FAILED"
    elif not st.running:
        kalshi = "⚪ IDLE"
    else:
        kalshi = _dot(h["kalshi"] and h["live_data"], "CONNECTED", "DISCONNECTED")
    feed = h.get("btc_feed")
    btc = feed.badge if feed is not None else "⚪ NOT CONFIGURED"
    if feed is not None and not st.running and feed.state == "DOWN" and feed.last_ts is None:
        btc = "⚪ IDLE"
    return ["📡 <b>BTC DIRECTIONAL BOT</b>", f"Bot: {bot}", f"Kalshi: {kalshi}", f"BTC Feed: {btc}"]


# ------------------------------------------------------------------ home
def home(rt: BotRuntime) -> Screen:
    st = rt.state
    v = btc_view(rt)
    b = v.signal.btc if v and v.signal else None
    d = v.decision if v else None
    approved = bool(d and d.approved and b is not None)
    lines = [*_statuses(rt), "", SEP, "", "₿ <b>BITCOIN · 15 MIN</b>", ""]
    spot = b.features.spot if b else None
    lines += [
        f"BTC Price: {_usd(spot, signed=False)}",
        f"Market: {escape(v.info.ticker) if v else 'searching…'}",
        f"Reference: {_usd(b.terms.threshold, signed=False) if b and b.terms.threshold else NA}",
        f"Time Left: {fmt_countdown(v.info.time_remaining()) if v else NA}",
        "",
    ]
    if b is None:
        lines += ["Signal: ⚪ WAIT", "", "Press 🟢 Start to begin." if not st.running else "Collecting data…"]
    else:
        e = b.edge
        signal = b.label.replace("NO TRADE", "WAIT") if approved else "⚪ WAIT"
        model = (f"{_pct(e.p_point)} ({'calibrated' if b.calibration.calibrated else 'uncalibrated'})"
                 if e else NA)
        lines += [
            f"Signal: {signal}",
            "",
            f"Model Estimate: {model}",
            f"Market Price: {f'{e.side.upper()} {_c(e.ask)}' if e else NA}",
            f"Net Edge: {f'{e.ev_low_cents:+.1f}¢ (conservative)' if e else NA}",
            "",
            f"Regime: {b.regime.badge}",
        ]
    lines += [f"Risk: {st.risk_level.badge}", f"Mode: {'⚠️ LIVE' if st.mode == 'live' else '📝 PAPER'}", ""]
    if b is not None:
        lines += ["Decision:", "✅ TRADE APPROVED" if approved else "⚪ NO TRADE", "", "Reason:",
                  escape(_reason(b, d))]
    s = rt.portfolio.stats(st.mode, days=0)
    lines += ["", SEP, "", f"Today's Performance ({st.mode.upper()}):", f"Trades: {s.trades}",
              f"Win Rate: {f'{s.win_rate:.0f}%' if s.win_rate is not None else NA}",
              f"Net P&L: {_usd(s.realized)}", f"Fees: {_usd(s.fees, signed=False)}", "", SEP]
    if st.emergency_stop:
        lines.append("🚨 EMERGENCY STOP - all orders blocked")
    buttons = list(BTC_MENU)
    buttons += [("▶️ Resume", "resume") if st.paused else ("⏸ Pause", "pause"), ("🚨 E-Stop", "estop")]
    return Screen("\n".join(lines), grid(buttons), route="home", refreshable=True)


def _reason(b: Any, d: Any) -> str:
    if d is not None and d.approved:
        e = b.edge
        return (f"Model {_pct(e.p_low)}+ vs price {_c(e.ask)}: conservative net edge {e.ev_low_cents:+.1f}¢ "
                f"after fees and slippage" if e else "All checks passed")
    if d is not None and not d.approved:
        return d.reason
    return b.primary_reason or "waiting"


# ------------------------------------------------------------------ signal screen
def _label(v: float | None, pos: str, neg: str, flat: str, lim: float) -> str:
    if v is None:
        return NA
    if v >= lim:
        return f"🟢 {pos} ({v:+.2f})"
    if v <= -lim:
        return f"🔴 {neg} ({v:+.2f})"
    return f"⚪ {flat} ({v:+.2f})"


def signal(rt: BotRuntime) -> Screen:
    v = btc_view(rt)
    back = [[("🔄 Refresh", "btc"), ("🔙 Back", "home")]]
    if v is None or v.snapshot is None or v.signal is None or v.signal.btc is None:
        return Screen("₿ No BTC 15-minute market data yet.\nThe scanner keeps checking automatically.", back,
                      route="btc", refreshable=True)
    snap, b, d = v.snapshot, v.signal.btc, v.decision
    approved = bool(d and d.approved)
    f, e = b.features, b.edge
    direction = b.label if approved else (f"⚪ NO TRADE (leaning {b.leaning})" if b.leaning != "WAIT" else "⚪ NO TRADE")
    lines = [SEP, "₿ <b>BTC · 15 MIN</b>", SEP, "", f"<b>DIRECTION: {direction}</b>", "",
             f"BTC Price: {_usd(f.spot, signed=False)}",
             f"Contract: <code>{escape(v.info.ticker)}</code>",
             f"YES means: {escape(b.terms.yes_means)}",
             f"YES: {_c(snap.yes_ask)} · NO: {_c(snap.no_ask)} (ask)",
             f"Time Remaining: {fmt_countdown(snap.time_remaining())}", "", SEP, ""]
    mom = f"{f.ret_5m_bps:+.0f} bps / 5m ({f.mom_z_5m:+.1f}σ) · {f.momentum_state.replace('_', ' ').title()}" \
        if f.ret_5m_bps is not None and f.mom_z_5m is not None else NA
    vol = (f"{f.sigma_1m * 1e4:.1f} bps/min · ratio {f.vol_ratio:.2f}" + (" · SHOCK" if f.shock else "")
           if f.sigma_1m is not None and f.vol_ratio is not None else NA)
    volume = (f"{f.rel_volume:.1f}x normal" + (" · SPIKE" if f.volume_spike else "")
              if f.rel_volume is not None else NA)
    km = rt._kalshi_move(v.info.ticker, snap.ts)
    model = NA
    if b.p_yes is not None:
        cal = "" if b.calibration.calibrated else " ⚠️ uncalibrated"
        model = f"P(YES) {_pct(b.p_yes)} [{_pct(b.p_yes_low)}-{_pct(b.p_yes_high)}]{cal}"
    lines += [
        f"MOMENTUM: {mom}",
        f"TREND: {_label(f.trend_strength, 'Up', 'Down', 'Flat', 0.3)}",
        f"VOLATILITY: {vol}",
        f"VOLUME: {volume}",
        f"KALSHI PRICE: YES mid {_c(snap.yes_mid)}" + (f" ({km:+.0f}¢ / 5m)" if km is not None else ""),
        f"MODEL PROBABILITY: {model}",
        f"ESTIMATED NET EDGE: {f'{e.side.upper()} {e.ev_cents:+.1f}¢ point / {e.ev_low_cents:+.1f}¢ conservative' if e else NA}",
        f"SIGNAL STRENGTH: {b.strength}/100",
        f"REGIME: {b.regime.badge}",
        f"RISK: {rt.state.risk_level.badge}",
        "", "DECISION:", "✅ TRADE" if approved else "⚪ NO TRADE", "", "Reasons:",
    ]
    if approved:
        lines += [f"+ {escape(s)}" for s in b.supporting[:4]] or ["+ all checks passed"]
        lines += [f"⚠️ {escape(w)}" for w in (d.warnings if d else ())][:3]
    else:
        reasons = list(dict.fromkeys((d.failures if d else ()) or [r.text for r in b.rejections if r.critical]))
        lines += [f"• {escape(r)}" for r in reasons[:5]]
    lines += ["", SCORE_NOTE]
    buttons: list[Button] = []
    if approved and d is not None:
        buttons.append(("⚠️ Live Trade" if rt.state.mode == "live" else "💰 Paper Trade", f"buy:{v.info.ticker}"))
    buttons += [("🧠 Why?", f"bwhy:{v.info.ticker}"), ("📋 Checklist", f"chk:{v.info.ticker}"),
                ("❌ Pass", "home"), ("🔙 Back", "home")]
    return Screen("\n".join(lines), grid(buttons), route="btc", refreshable=True)


def why(rt: BotRuntime, ticker: str) -> Screen:
    v = rt.view(ticker)
    back = [[("🔙 Back", "btc")]]
    if v is None or v.signal is None or v.signal.btc is None:
        return Screen("No BTC analysis for this market yet.", back, route=f"bwhy:{ticker}")
    b, d = v.signal.btc, v.decision
    approved = bool(d and d.approved)
    lines = ["🧠 <b>WHY THIS TRADE?</b>" if approved else "🧠 <b>WHY NO TRADE?</b>", "",
             f"Contract: {escape(b.terms.yes_means)}",
             f"Settlement source: {escape(b.terms.settlement_source)}", ""]
    if b.edges:
        lines.append("<b>Executable prices vs model</b>")
        for e in b.edges:
            lines.append(f"{e.side.upper()}: ask {_c(e.ask)} + costs {e.cost_cents - e.ask:.1f}¢ · P(win) "
                         f"{_pct(e.p_point)} (low {_pct(e.p_low)}) → EV {e.ev_cents:+.1f}¢ / {e.ev_low_cents:+.1f}¢")
        lines.append("")
    if b.supporting:
        lines += ["<b>Supporting</b>", *[f"✓ {escape(s)}" for s in b.supporting], ""]
    if b.conflicting:
        lines += ["<b>Conflicting</b>", *[f"✗ {escape(s)}" for s in b.conflicting], ""]
    if b.rejections:
        lines.append("<b>No-trade checks</b>")
        for r in b.rejections:
            lines.append(f"{'⛔' if r.critical else '⚠️'} {escape(r.text)}")
        lines.append("")
    if d is not None and not d.approved:
        extra = [x for x in d.failures if x not in {r.text for r in b.rejections}]
        if extra:
            lines += ["<b>Account / risk limits</b>", *[f"⛔ {escape(x)}" for x in extra[:5]], ""]
    lines += [f"Calibration: {escape(b.calibration.label)}",
              f"Data age: Kalshi {_age(b.data_age_sec.get('kalshi'))} · BTC {_age(b.data_age_sec.get('btc'))}",
              "", SCORE_NOTE]
    return Screen("\n".join(lines)[:3900], back, route=f"bwhy:{ticker}", refreshable=True)


def _age(v: float | None) -> str:
    return NA if v is None else f"{v:.0f}s"


# ------------------------------------------------------------------ analytics
def perf_metrics(positions: list[Position]) -> dict[str, Any]:
    ps = sorted(positions, key=lambda p: p.closed_at or p.opened_at)
    pnls = [p.realized_pnl or 0.0 for p in ps]
    fees = sum(p.entry_fee + p.exit_fee for p in ps)
    eq = peak = dd = 0.0
    streak = worst = 0
    for x in pnls:
        eq += x
        peak, dd = max(peak, eq), max(dd, peak - eq)
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    gw, gl = sum(x for x in pnls if x > 0), -sum(x for x in pnls if x <= 0)
    n = len(pnls)
    wins = sum(1 for x in pnls if x > 0)
    return {"trades": n, "wins": wins, "losses": n - wins, "win_rate": wins / n if n else None,
            "net": sum(pnls), "fees": fees, "gross": sum(pnls) + fees, "avg": sum(pnls) / n if n else None,
            "pf": (gw / gl) if gl > 0 else (math.inf if gw > 0 else None), "max_dd": dd, "streak": worst}


def _perf_lines(title: str, m: dict[str, Any]) -> list[str]:
    if not m["trades"]:
        return [f"<b>{title}</b>", "No closed trades yet."]
    pf = "∞" if m["pf"] == math.inf else (NA if m["pf"] is None else f"{m['pf']:.2f}")
    out = [f"<b>{title}</b>",
           f"Trades {m['trades']} · W {m['wins']} / L {m['losses']} · WR {_pct(m['win_rate'])}",
           f"Gross {_usd(m['gross'])} · Fees {_usd(m['fees'], False)} · Net {_usd(m['net'])}",
           f"Avg/trade {_usd(m['avg'])} · PF {pf} · Max DD {_usd(m['max_dd'], False)} · Worst streak {m['streak']}"]
    if m["trades"] < 30:
        out.append(f"⚠️ Only {m['trades']} trades - too few to judge.")
    return out


def analytics(rt: BotRuntime) -> Screen:
    paper = perf_metrics(rt.repo.closed_positions(mode="paper", limit=100_000))
    live = perf_metrics(rt.repo.closed_positions(mode="live", limit=100_000))
    cal = rt.calibration
    lines = ["📈 <b>BTC ANALYTICS</b>", "", "<i>Backtest, paper and live are never combined.</i>", "",
             *_perf_lines("📝 PAPER (simulated)", paper), "", *_perf_lines("⚠️ LIVE (real money)", live), ""]
    lines += ["<b>🎯 Model calibration</b>", escape(cal.state.label) if cal else "⚠️ MODEL NOT YET CALIBRATED"]
    if cal and cal.state.brier_model is not None:
        mk = f"{cal.state.brier_market:.4f}" if cal.state.brier_market is not None else NA
        lines.append(f"Brier model {cal.state.brier_model:.4f} vs Kalshi {mk} (lower = better)")
    rep = load_report(Path(rt.settings.btc_backtest_report))
    lines += ["", "<b>🧪 BACKTEST (last saved)</b>"]
    if rep:
        fs = rep["variants"]["full strategy"]["oos"]
        lines += [f"{rep['data']['first_day']} .. {rep['data']['last_day']} · OOS trades {fs['trades']} · "
                  f"net {_usd(fs['net_pnl'])} (1 contract)", escape(rep["verdict"])]
    else:
        lines.append("No backtest yet (run scripts/btc_backtest.py).")
    buttons = [("🎯 Calibration", "bcal"), ("🧪 Backtest", "bt"), ("📜 History", "hist:all"),
               ("🔬 Research", "anl"), BACK]
    return Screen("\n".join(lines), grid(buttons), route="banl")


def calibration(rt: BotRuntime) -> Screen:
    rt.refresh_model()
    cal = rt.calibration
    lines = ["🎯 <b>MODEL CALIBRATION</b>", "", "Settled BTC markets, each market counted once.", ""]
    if cal is None:
        lines.append("⚠️ MODEL NOT YET CALIBRATED")
    else:
        lines += [escape(cal.state.label), f"Markets: {cal.markets} · predictions: {cal.minutes}", "",
                  "<code>Estimated  Markets  Actual</code>"]
        for bk in cal.buckets:
            act = _pct(bk.actual) if bk.actual is not None else "—"
            pred = _pct(bk.predicted) if bk.predicted is not None else "—"
            lines.append(f"<code>{bk.label:>8}  {bk.markets:>7}  {act:>6}</code>  (avg est {pred})")
        if cal.state.ece is not None:
            lines.append(f"\nCalibration error (ECE): {cal.state.ece * 100:.1f} pts "
                         f"(limit {rt.settings.calibration_ece_tol * 100:.0f})")
        if cal.state.status != "CALIBRATED":
            lines.append(f"\n⚠️ Not trusted yet: needs {rt.settings.hist_min_samples}+ markets, small error "
                         "and accuracy at least equal to Kalshi's own prices.")
    return Screen("\n".join(lines), [[("🔙 Back", "banl")]], route="bcal")


def backtest(rt: BotRuntime) -> Screen:
    from app.research.btc_backtest import format_report

    rep = load_report(Path(rt.settings.btc_backtest_report))
    if not rep:
        text = ("🧪 <b>BACKTEST</b>\n\nNo backtest report yet. On the bot's computer run:\n"
                "<code>python scripts/replay.py --days 30</code>\n<code>python scripts/btc_backtest.py</code>\n\n"
                "It replays settled Kalshi BTC markets through this exact engine using only data available "
                "at each minute, chooses settings on earlier days and tests them on later days.")
        return Screen(text, [[("🔙 Back", "banl")]], route="bt")
    body = escape(format_report(rep, full=False))
    text = f"🧪 <b>BACKTEST</b> · {escape(rep.get('generated', ''))}\n\n<code>{body}</code>"
    return Screen(text[:3900], [[("🔙 Back", "banl")]], route="bt")


def strategy(rt: BotRuntime) -> Screen:
    p = rt.profile()
    s = rt.settings
    lines = [
        "🧠 <b>BTC STRATEGY</b>", "",
        "Markets: Kalshi Bitcoin 15-minute contracts only (verified rules)",
        f"BTC data: Coinbase BTC-USD (proxy for BRTI, ±{s.btc_basis_bps:g} bps basis)", "",
        "<b>Probability model</b>",
        "P(YES) = Φ(ln(BTC / threshold) / (σ·√T))",
        f"σ = last {s.btc_sigma_window_min} min realised volatility; T adjusted for the settlement average.",
        "Range from volatility error + basis; trades use the conservative end.", "",
        "<b>Trade only if</b>",
        f"• Net edge after fees + slippage ≥ {p.min_ev_cents:+g}¢ (conservative)",
        f"• Signal strength ≥ {p.min_btc_strength} · uncertainty ≤ {p.max_prob_width * 100:.0f} pts",
        f"• Spread ≤ {p.max_spread_cents:g}¢ · ≥ {p.min_time_remaining_sec}s left · entry "
        f"{p.min_entry_price_cents:g}-{p.max_entry_price_cents:g}¢",
        f"• Regime not in: {escape(', '.join(sorted(p.regimes_blocked)) or 'none')}",
        "• No BTC/Kalshi conflict, move not already priced in, data fresh",
        "• Model calibrated" + (" (required)" if p.require_known_ev else " (paper may trade while collecting)"), "",
        f"<b>Sizing</b>: ≤ ${p.max_position_usd:g}/trade, Kelly × {p.kelly_fraction:g} on the conservative P, "
        f"daily loss ${p.max_daily_loss_usd:g}, pause after {p.max_consecutive_losses} losses",
        "", "Live orders always need manual confirmation.",
    ]
    return Screen("\n".join(lines), [[("📈 Analytics", "banl"), ("🧪 Backtest", "bt")], [("⚙️ Settings", "cfg"), BACK]],
                  route="strat")


def status_lines(rt: BotRuntime) -> list[str]:
    feed = rt.health().get("btc_feed")
    if feed is None:
        return []
    age = f" · {feed.age_sec:.0f}s old" if feed.age_sec is not None else ""
    err = f" · {escape(feed.error)}" if feed.error and not feed.ok else ""
    return [f"BTC Feed: {feed.badge} ({escape(feed.source)}{age}){err}"]


__all__ = ["home", "signal", "why", "analytics", "calibration", "backtest", "strategy", "status_lines", "APP_NAME"]
