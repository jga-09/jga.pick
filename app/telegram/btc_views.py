"""Telegram screens for the BTC 15-minute engine (STRATEGY_MODE=btc).

Design rules:
* Plain language first; technical numbers live behind "🧠 Full details".
* Short screens, one idea per block, the decision in one line.
* Buttons change with the bot's state (Start only when stopped, etc.).
* Never show a fabricated price or statistic (``—`` when unknown), never offer a trade
  button when the setup was rejected, and keep backtest / paper / live separate.
"""

from __future__ import annotations

import math
from datetime import datetime
from html import escape as _html_escape
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from app import DISPLAY_NAME
from app.research.btc_backtest import load_report
from app.strategy.btc_decision import NoTrade
from app.telegram.keyboards import Button, Screen
from app.trading.positions import Position
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.runtime import BotRuntime, MarketView

LINE = "━━━━━━━━━━━━━━"
NA = "—"
HOME: Button = ("🏠 Home", "home")
NY = ZoneInfo("America/New_York")

FRIENDLY: dict[NoTrade, str] = {
    NoTrade.TRADING_PAUSED: "Trading is paused",
    NoTrade.STALE_DATA: "Waiting for fresh price data",
    NoTrade.SETTLEMENT_UNVERIFIED: "Couldn't confirm this market's rules",
    NoTrade.INSUFFICIENT_TIME: "Too close to the end of the 15 minutes",
    NoTrade.DAILY_LOSS: "Daily loss limit reached",
    NoTrade.POSITION_LIMIT: "Already holding the maximum number of trades",
    NoTrade.EXCESSIVE_SPREAD: "Buy and sell prices are too far apart",
    NoTrade.INSUFFICIENT_LIQUIDITY: "Not enough contracts for sale at this price",
    NoTrade.UNFAVORABLE_REGIME: "BTC is too choppy right now",
    NoTrade.EXCESSIVE_UNCERTAINTY: "The bot isn't sure enough",
    NoTrade.CONFLICT: "BTC and the Kalshi price disagree",
    NoTrade.PRICE_REFLECTS_MOVE: "The price already moved - too late to get in",
    NoTrade.WEAK_DIRECTION: "BTC isn't moving clearly in one direction",
    NoTrade.LONGSHOT: "Cheap long-shot contract - on Kalshi these lose money on average",
    NoTrade.INSUFFICIENT_EV: "Price is too high for the odds (not enough profit after fees)",
    NoTrade.INSUFFICIENT_EVIDENCE: "Still learning - needs more finished markets first",
    NoTrade.NOT_CALIBRATED: "The bot's odds haven't proven accurate yet",
}
# RiskManager account checks are plain strings; map them by keyword.
_ACCOUNT = (
    ("bot stopped", "Bot is stopped - tap ▶️ Start"),
    ("EMERGENCY", "Emergency stop is on"),
    ("trading paused", "Trading is paused"),
    ("Daily loss limit", "Daily loss limit reached - resting until tomorrow"),
    ("Max open positions", "Already holding the maximum number of trades"),
    ("Max exposure", "Money-at-risk limit reached"),
    ("Already holding", "Already in this market"),
    ("in flight", "An order for this market is already being processed"),
    ("losses in a row", "Taking a break after a losing streak"),
    ("cooldown", "Short pause after a recent trade"),
    ("Position size is zero", "Trade would be too small for your limits"),
    ("after exact fees", "Price is too high for the odds (not enough profit after fees)"),
    ("not permitted", "This mode isn't allowed by the bot's settings"),
)


# ------------------------------------------------------------------ helpers
SEP = "━━━━━━━━━━━━━━━━━━"


def escape(text: str) -> str:
    return _html_escape(text, quote=False)  # keep apostrophes readable


def _n(k: int, word: str) -> str:
    return f"{k} {word}{'' if k == 1 else 's'}"


def _big(v: float | None) -> str:
    """BTC-sized dollar amounts without cents ($60,300)."""
    return NA if v is None else f"${v:,.0f}"


def _usd(v: float | None, signed: bool = True) -> str:
    if v is None:
        return NA
    if not signed or abs(v) < 0.005:
        return f"${abs(v):,.2f}"
    return f"{'+' if v > 0 else '-'}${abs(v):,.2f}"


def _c(v: float | None) -> str:
    return NA if v is None else f"{v:.0f}¢"


def _pct(v: float | None) -> str:
    return NA if v is None else f"{v * 100:.0f}%"


def _left(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}m {s % 60:02d}s"


def _clock(dt: datetime) -> str:
    return dt.astimezone(NY).strftime("%-I:%M %p ET")


def _bar(frac: float, width: int = 10) -> str:
    n = max(0, min(width, round(frac * width)))
    return "▰" * n + "▱" * (width - n)


def grid2(buttons: list[Button]) -> list[list[Button]]:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def friendly(text: str, btc: Any = None) -> str:
    if btc is not None:
        for r in btc.rejections:
            if r.text == text:
                return FRIENDLY.get(r.code, r.code.value)
    for key, nice in _ACCOUNT:
        if key.lower() in text.lower():
            return nice
    return text.split(":")[0]


def reasons(btc: Any, decision: Any, limit: int = 3) -> list[str]:
    raw = list(decision.failures) if decision is not None else [r.text for r in btc.rejections if r.critical]
    out: list[str] = []
    for t in raw:
        nice = friendly(t, btc)
        if nice not in out:
            out.append(nice)
    return out[:limit]


def btc_view(rt: BotRuntime) -> MarketView | None:
    return next((v for v in rt.markets() if v.info.asset == "BTC"), None)


def _question(b: Any, v: MarketView) -> str:
    t = b.terms if b else None
    when = _clock(v.info.close_time)
    if t is None or t.threshold is None or t.kind is None:
        return f"BTC market closing {when}"
    word = "above" if t.kind == "above" else "below"
    return f"Will BTC be {word} <b>{_big(t.threshold)}</b> at {when}?"


def _btc_vs_target(b: Any) -> str:
    f = b.features
    if f.spot is None or b.terms.threshold is None:
        return f"BTC {_big(f.spot)}"
    diff = f.spot - b.terms.threshold
    if abs(diff) < 0.5:
        return f"BTC {_big(f.spot)} · right at the target"
    return f"BTC {_big(f.spot)} · {_big(abs(diff))} {'above' if diff > 0 else 'below'} target"


def _dot(ok: bool | None) -> str:
    return "🟢" if ok else "🔴"


def _feeds(rt: BotRuntime) -> tuple[str, str]:
    st, h = rt.state, rt.health()
    if h["fixture"]:
        kalshi = "🧪"
    elif not st.running:
        kalshi = "⚪"
    else:
        kalshi = _dot(h["kalshi"] and h["live_data"] and not h["auth_failed"])
    feed = h.get("btc_feed")
    if feed is None:
        btc = "⚪"
    elif feed.state == "FIXTURE":
        btc = "🧪"
    elif not st.running and feed.last_ts is None:
        btc = "⚪"
    else:
        btc = {"CONNECTED": "🟢", "STALE": "🟡"}.get(feed.state, "🔴")
    return kalshi, btc


def _state_line(rt: BotRuntime) -> str:
    st = rt.state
    if st.emergency_stop:
        return "🚨 Emergency stop"
    if not st.running:
        return "⚪ Stopped"
    return "⏸ Paused" if st.paused else "🟢 Running"


def _mode(rt: BotRuntime) -> str:
    return "💵 Real money" if rt.state.mode == "live" else "📝 Paper"


def _control_buttons(rt: BotRuntime) -> list[list[Button]]:
    st = rt.state
    if st.emergency_stop:
        return [[("🔓 Turn off emergency stop", "estop")]]
    if not st.running:
        row = [("▶️ Start", "start")]
    else:
        row = [("▶️ Resume", "resume") if st.paused else ("⏸ Pause", "pause"), ("🛑 Stop", "stop")]
    return [row, [("🚨 Emergency stop", "estop")]]


# ------------------------------------------------------------------ home
def _arrow(direction: str) -> str:
    return {"UP": "🟢 UP", "DOWN": "🔴 DOWN"}.get(direction, "⚪ —")


HOME_MENU: list[Button] = [
    ("🟢 Start", "start"), ("⚙️ Config", "cfg"),
    ("🛑 Stop", "stop"), ("📊 Status", "status"),
    ("🎯 Signals", "btc"), ("💰 Trades", "trades:0"),
    ("🛡 Risk", "risk"), ("📜 History", "banl"),
    ("🧠 Strategy", "strat"), ("🧹 Clear", "clear"),
]


def home(rt: BotRuntime) -> Screen:
    st = rt.state
    v = btc_view(rt)
    b = v.signal.btc if v and v.signal else None
    d = v.decision if v else None
    kalshi, btc_feed = _feeds(rt)
    if kalshi == "🧪" or btc_feed == "🧪":
        data = "🧪 Fixture Data"
    elif kalshi == "🟢" and btc_feed == "🟢":
        data = "🟢 Live Data"
    elif not st.running:
        data = "⚪ Data off"
    else:
        data = f"🟡 Data: Kalshi {kalshi} BTC {btc_feed}"
    approved = bool(b is not None and d is not None and d.approved and b.edge)
    if st.emergency_stop:
        status_line = "🚨 EMERGENCY STOP — orders blocked"
    elif not st.running:
        status_line = "⚪ Offline — press 🟢 Start"
    elif v is None:
        status_line = "🔍 Searching for the next BTC 15M market"
    elif approved:
        status_line = "🟢 Online — trade setup active"
    else:
        status_line = "🟢 Online — waiting for a good setup"
    s = rt.portfolio.stats(st.mode, days=0)
    opens = rt.portfolio.open_positions(st.mode)
    lines = [
        f"📡 <b>{DISPLAY_NAME}</b>",
        f"{_state_line(rt)} · {data}",
        status_line,
        f"🎯 Market: {escape(v.info.label) if v else '—'}",
        SEP,
        f"📊 Today: {_usd(s.realized)}",
        f"📈 WR: {_pct(None if s.win_rate is None else s.win_rate / 100)}"
        f" · Trades: {s.trades}",
        f"💰 Open: {len(opens)} · Bal: {_usd(rt.portfolio.balance(st.mode), signed=False)}",
        SEP,
    ]
    if v is None or b is None:
        lines += ["🎯 Signal: ⚪ —", "Collecting market data…" if st.running else "Press 🟢 Start to begin"]
    else:
        snap = v.snapshot
        if approved:
            e = b.edge
            lines += [f"🎯 Signal: <b>{_arrow(b.terms.direction_for_side(e.side))}</b> · BUY {e.side.upper()}",
                      f"Bot's odds: {_pct(e.p_point)} · Edge {e.ev_low_cents:+.0f}¢"]
        else:
            why = reasons(b, d, 1)
            lines += ["🎯 Signal: ⚪ NO TRADE", f"💬 {escape(why[0]) if why else 'Waiting for a good setup'}"]
        if b.terms.threshold is not None:
            lines.append(f"₿ {_big(b.features.spot)} vs target {_big(b.terms.threshold)}")
        if snap:
            lines.append(f"YES {_c(snap.yes_ask)} · NO {_c(snap.no_ask)}")
        lines.append(f"⏱ {_left(v.info.time_remaining())}")
    lines += [
        SEP,
        f"🛡 {st.risk_level.badge} RISK",
        f"{'💵 LIVE' if st.mode == 'live' else '📝 PAPER'} MODE",
        f"🤖 Auto Trade: {'ON' if st.auto_trade else 'OFF'}",
    ]
    buttons = list(HOME_MENU)
    buttons += [("▶️ Resume", "resume") if st.paused else ("⏸ Pause", "pause"),
                ("🔓 E-Stop off", "estop") if st.emergency_stop else ("🚨 E-Stop", "estop")]
    connected = rt.kalshi_key_source() != "none"
    kalshi_row = [("🔑 Kalshi API ✅" if connected else "🔑 Connect Kalshi API", "kacct")]
    return Screen("\n".join(lines), grid2(buttons) + [kalshi_row], route="home", refreshable=True)


# ------------------------------------------------------------------ signal
def _moving(f: Any) -> str:
    if f.ret_5m_bps is None:
        return NA
    pct = f.ret_5m_bps / 100
    direction = "up" if pct > 0.005 else "down" if pct < -0.005 else "flat"
    state = {"ACCELERATING_UP": "speeding up", "ACCELERATING_DOWN": "speeding up", "WEAKENING": "slowing down",
             "REVERSING": "turning around", "STEADY": "steady"}.get(f.momentum_state, "")
    move = f"{direction} {abs(pct):.2f}%" if direction != "flat" else "flat"
    return f"{move}{f' · {state}' if state else ''}"


def _level(v: float | None, lo: float, hi: float, words: tuple[str, str, str]) -> str:
    if v is None:
        return NA
    return words[0] if v < lo else words[2] if v > hi else words[1]


def signal(rt: BotRuntime) -> Screen:
    v = btc_view(rt)
    nav = [[("🔄 Refresh", "btc"), HOME]]
    if v is None or v.snapshot is None or v.signal is None or v.signal.btc is None:
        return Screen("₿ <b>BTC SIGNAL</b>\n\nNo BTC 15-minute market data yet.\nThe bot keeps checking automatically.",
                      nav, route="btc", refreshable=True)
    snap, b, d = v.snapshot, v.signal.btc, v.decision
    approved = bool(d and d.approved and b.edge)
    f, e = b.features, b.edge
    trend = _level(f.trend_strength, -0.3, 0.3, ("down", "flat", "up"))
    vol = _level(f.vol_ratio, 0.7, 1.4, ("calm", "normal", "wild"))
    volume = _level(f.rel_volume, 0.6, 1.6, ("quiet", "normal", "busy"))
    lines = [
        "₿ <b>BTC 15-MIN SIGNAL</b>",
        _question(b, v),
        f"⏱ {_left(snap.time_remaining())} left",
        "",
        "<b>💵 Prices now</b>",
        f"YES {_c(snap.yes_ask)} · NO {_c(snap.no_ask)}",
        _btc_vs_target(b),
        "",
        "<b>🧠 Bot's view</b>",
    ]
    if b.p_yes is None:
        lines.append("Not enough data to estimate the odds yet.")
    else:
        lines.append(f"Chance YES wins: <b>{_pct(b.p_yes)}</b> (range {_pct(b.p_yes_low)}–{_pct(b.p_yes_high)})")
        if e:
            lines.append(f"Best buy: {e.side.upper()} at {_c(e.ask)} · profit after fees ≈ {e.ev_low_cents:+.0f}¢/contract")
        lines.append(f"Signal strength: {_bar(b.strength / 100)} {b.strength}")
    lines += [
        "",
        "<b>📈 What BTC is doing</b>",
        f"Last 5 min: {_moving(f)}",
        f"Trend {trend} · moves {vol} · trading {volume}",
        f"Mood: {b.regime.badge.split(' ', 1)[1].title()}",
        "",
    ]
    if approved:
        lines.append(f"✅ <b>TRADE FOUND - BUY {e.side.upper()}</b>")
        lines.append(f"• Bot's odds {_pct(e.p_point)} vs price {_c(e.ask)} - a bargain after fees")
        if f.spot is not None and b.terms.threshold is not None:
            lines.append(f"• {_btc_vs_target(b).split(' · ', 1)[-1].capitalize()} with "
                         f"{int(snap.time_remaining() // 60)} min left")
        lines += [f"⚠️ {escape(w)}" for w in (d.warnings if d else ())][:2]
    else:
        lines.append("⚪ <b>NO TRADE</b>")
        lines += [f"• {escape(r)}" for r in reasons(b, d)]
    if not b.calibration.calibrated:
        lines += ["", "<i>The bot's odds are estimates until proven on enough finished markets.</i>"]
    buttons: list[list[Button]] = []
    if approved and e is not None:
        label = "💵 Buy with real money" if rt.state.mode == "live" else "📝 Paper trade"
        buttons.append([(f"{label}: {e.side.upper()} {_c(e.ask)}", f"buy:{v.info.ticker}")])
    buttons += [[("🧠 Full details", f"bwhy:{v.info.ticker}"), ("📋 Checklist", f"chk:{v.info.ticker}")], *nav]
    return Screen("\n".join(lines), buttons, route="btc", refreshable=True)


def why(rt: BotRuntime, ticker: str) -> Screen:
    v = rt.view(ticker)
    back = [[("🔙 Signal", "btc"), HOME]]
    if v is None or v.signal is None or v.signal.btc is None:
        return Screen("No BTC analysis for this market yet.", back, route=f"bwhy:{ticker}")
    b, d = v.signal.btc, v.decision
    f = b.features
    lines = ["🧠 <b>FULL DETAILS</b>", "",
             "<b>The contract</b>", f"YES = {escape(b.terms.yes_means)}", f"NO = {escape(b.terms.no_means)}",
             f"<code>{escape(ticker)}</code>", ""]
    if b.edges:
        lines.append("<b>Price vs the bot's odds</b> (per contract)")
        for e in b.edges:
            lines.append(f"{e.side.upper()}: pay {_c(e.ask)} + {e.cost_cents - e.ask:.1f}¢ fees/slippage · "
                         f"wins {_pct(e.p_point)} (cautious {_pct(e.p_low)}) → {e.ev_low_cents:+.1f}¢")
        lines.append("")
    lines += ["<b>BTC numbers</b>",
              f"Distance to target: {f.dist_bps:+.0f} bps ({f.dist_sigma:+.2f}σ)" if f.dist_bps is not None
              and f.dist_sigma is not None else "Distance to target: —",
              f"Momentum 5m: {f.mom_z_5m:+.1f}σ · 15m: {f.mom_z_15m:+.1f}σ" if f.mom_z_5m is not None
              and f.mom_z_15m is not None else "Momentum: —",
              f"Volatility: {f.sigma_1m * 1e4:.1f} bps/min" if f.sigma_1m else "Volatility: —", ""]
    if b.supporting:
        lines += ["<b>For</b>", *[f"✓ {escape(s)}" for s in b.supporting], ""]
    if b.conflicting:
        lines += ["<b>Against</b>", *[f"✗ {escape(s)}" for s in b.conflicting], ""]
    checks = list(dict.fromkeys(list(d.failures) if d else [r.text for r in b.rejections if r.critical]))
    if checks:
        lines += ["<b>Why not</b>", *[f"⛔ {escape(friendly(x, b))} <i>({escape(x)})</i>" for x in checks[:6]], ""]
    lines += [f"Odds accuracy: {escape(_accuracy_short(b.calibration))}",
              f"Data age: Kalshi {_age(b.data_age_sec.get('kalshi'))} · BTC {_age(b.data_age_sec.get('btc'))}",
              "<i>Signal strength is an evidence score, not a win chance.</i>"]
    return Screen("\n".join(lines)[:3900], back, route=f"bwhy:{ticker}", refreshable=True)


def _age(v: float | None) -> str:
    return NA if v is None else f"{v:.0f}s"


def _accuracy_short(cal: Any) -> str:
    if cal.status == "CALIBRATED":
        return f"✅ proven on {cal.n_markets} markets"
    if cal.status == "NOT_CALIBRATED":
        return f"⚠️ not accurate enough yet ({cal.n_markets} markets)"
    return f"⏳ still learning ({cal.n_markets} markets so far)"


# ------------------------------------------------------------------ results
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


def _perf_block(title: str, all_time: dict[str, Any], today: dict[str, Any]) -> list[str]:
    out = [f"<b>{title}</b>"]
    if not all_time["trades"]:
        return out + ["No finished trades yet."]
    out.append(f"Today: {_n(today['trades'], 'trade')} · {_usd(today['net'])}" if today["trades"]
               else "Today: no trades")
    out.append(f"All time: {_n(all_time['trades'], 'trade')} · {_pct(all_time['win_rate'])} won · "
               f"<b>{_usd(all_time['net'])}</b>")
    out.append(f"Per trade {_usd(all_time['avg'])} · fees {_usd(all_time['fees'], False)} · "
               f"worst dip {_usd(all_time['max_dd'], False)}")
    if all_time["trades"] < 30:
        out.append(f"{_bar(all_time['trades'] / 30)} {all_time['trades']}/30 trades before results mean much")
    return out


def analytics(rt: BotRuntime) -> Screen:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    blocks = []
    for mode, title in (("paper", "📝 Paper trading"), ("live", "💵 Real money")):
        blocks += _perf_block(title, perf_metrics(rt.repo.closed_positions(mode=mode, limit=100_000)),
                              perf_metrics(rt.repo.closed_positions(mode=mode, since=start, limit=100_000)))
        blocks.append("")
    cal = rt.calibration
    lines = ["📈 <b>RESULTS</b>", "", *blocks, "<b>🎯 Are the bot's odds accurate?</b>"]
    lines += _accuracy_lines(rt, cal.state if cal else None)
    rep = load_report(Path(rt.settings.btc_backtest_report))
    lines += ["", "<b>🧪 Backtest</b>", _backtest_one_liner(rep)]
    buttons = grid2([("🎯 Odds accuracy", "bcal"), ("🧪 Backtest", "bt"), ("📜 History", "hist:all"),
                     ("🔬 Research", "anl")]) + [[HOME]]
    return Screen("\n".join(lines), buttons, route="banl")


def _accuracy_lines(rt: BotRuntime, state: Any) -> list[str]:
    need = rt.settings.hist_min_samples
    if state is None or state.status == "INSUFFICIENT":
        n = state.n_markets if state else 0
        return [f"⏳ Still learning {_bar(n / need)} {n}/{need} markets"]
    if state.status == "CALIBRATED":
        return [f"✅ Yes - checked on {state.n_markets} finished markets"]
    return [f"⚠️ Not yet - off by {(state.ece or 0) * 100:.0f} pts on {state.n_markets} markets",
            "Trading stays blocked on Low/Medium until this improves."]


def _backtest_one_liner(rep: dict[str, Any] | None) -> str:
    if not rep:
        return "Not run yet - tap 🧪 Backtest to see how."
    fs = rep["variants"]["full strategy"]["oos"]
    if not fs["trades"]:
        return f"{rep['data']['first_day']}–{rep['data']['last_day']}: found no edge (0 test trades)"
    return (f"{rep['data']['first_day']}–{rep['data']['last_day']}: {fs['trades']} test trades · "
            f"{_pct(fs['win_rate'])} won · {(fs['avg_profit_per_trade'] or 0) * 100:+.1f}¢ per contract")


def calibration(rt: BotRuntime) -> Screen:
    rt.refresh_model()
    cal = rt.calibration
    lines = ["🎯 <b>ARE THE BOT'S ODDS ACCURATE?</b>", "",
             "When the bot says something has a 70% chance, it should happen about 7 times in 10.", ""]
    lines += _accuracy_lines(rt, cal.state if cal else None)
    if cal and cal.markets:
        lines += ["", "<code>Bot said   Happened  Markets</code>"]
        for bk in cal.buckets:
            if bk.markets:
                lines.append(f"<code>{bk.label:>8}   {_pct(bk.actual):>8}  {bk.markets:>7}</code>")
        if cal.state.brier_model is not None and cal.state.brier_market is not None:
            better = cal.state.brier_model <= cal.state.brier_market
            lines.append(f"\nCompared with Kalshi's own prices: {'✅ more accurate' if better else '⚠️ less accurate'}")
    return Screen("\n".join(lines), [[("🔙 Results", "banl"), HOME]], route="bcal")


def backtest(rt: BotRuntime) -> Screen:
    rep = load_report(Path(rt.settings.btc_backtest_report))
    nav = [[("🔙 Results", "banl"), HOME]]
    if not rep:
        text = ("🧪 <b>BACKTEST</b>\n\nA backtest replays past BTC markets to check whether the strategy "
                "would have made money after fees, using only what was known at the time.\n\n"
                "Not run yet. In the Linux terminal:\n"
                "<code>cd ~/jga.pick</code>\n"
                "<code>sudo docker compose run --rm bot python scripts/replay.py --days 30</code>\n"
                "<code>sudo docker compose run --rm bot python scripts/btc_backtest.py</code>")
        return Screen(text, nav, route="bt")
    d = rep["data"]
    fs = rep["variants"]["full strategy"]["oos"]
    lines = ["🧪 <b>BACKTEST</b>", f"<i>Past data, not live results · {escape(rep.get('generated', '')[:10])}</i>", "",
             f"Tested on {len(d['test_days'])} unseen days ({d['first_day']} – {d['last_day']}), "
             f"{d['markets']} markets", ""]
    if fs["trades"]:
        pf = fs["profit_factor"]
        lines += ["<b>Full strategy</b>", f"Trades: {fs['trades']} · won {_pct(fs['win_rate'])}",
                  f"Profit: {_usd(fs['net_pnl'])} ({(fs['avg_profit_per_trade'] or 0) * 100:+.1f}¢ per contract)",
                  f"Worst dip: {_usd(fs['max_drawdown'], False)} · win/loss ratio "
                  f"{'∞' if pf == math.inf else f'{pf:.2f}' if pf else NA}", ""]
    else:
        lines += ["<b>Full strategy</b>", "Made no test trades - no edge found on earlier days.", ""]
    lines.append("<b>Which parts helped</b> (✅ helped · ❌ hurt · ➖ no clear difference)")
    for c in rep.get("component_tests", []):
        icon = {"improves": "✅", "hurts": "❌", "insufficient": "❔"}.get(c["verdict"].split()[0], "➖")
        lines.append(f"{icon} {escape(COMPONENTS.get(c['component'], c['component']))}")
    lines += ["", "<b>Verdict</b>", escape(plain_verdict(rep["verdict"]))]
    return Screen("\n".join(lines)[:3900], nav, route="bt")


VERDICTS = (
    ("POSITIVE out of sample", "✅ Made money on days it had never seen. Next step: paper-trade it for a few weeks."),
    ("POSITIVE ON AVERAGE", "🟡 Slightly profitable, but it could be luck. Keep paper trading - no real money yet."),
    ("NO EVIDENCE", "❌ It never found a profitable setting. Don't use real money."),
    ("NO EDGE", "❌ Not profitable after fees. Don't use real money."),
    ("INSUFFICIENT", "⚠️ Not enough history to judge. Download more days and run it again."),
)
COMPONENTS = {
    "momentum vs baseline": "Following BTC's recent move",
    "trend filter": "Trend filter",
    "volatility filter": "Skipping choppy markets",
    "Kalshi price filter": "Price limits",
    "probability model on momentum": "Odds check on momentum trades",
    "probability model + net edge vs momentum": "Odds model + profit-after-fees check",
    "no-trade filters on the model": "All the no-trade safety checks",
    "late-entry filter": "Too-late-to-enter check",
    "regime filter": "Market-mood filter",
    "strength filter": "Signal-strength filter",
    "longshot guard": "Skip cheap long-shots",
}


def plain_verdict(text: str) -> str:
    return next((nice for key, nice in VERDICTS if text.startswith(key)), text)


# ------------------------------------------------------------------ settings / info
def settings(rt: BotRuntime, note: str = "") -> Screen:
    st, p = rt.state, rt.profile()
    lines = ["⚙️ <b>SETTINGS</b>", "",
             f"Risk level: {st.risk_level.emoji} {st.risk_level.value.title()}",
             f"Mode: {_mode(rt)}" + (" (practice money)" if st.mode == "paper" else ""),
             f"Auto trade: {'🟢 On' if st.auto_trade else '⚪ Off'}", "",
             f"Max per trade: ${p.max_position_usd:g}",
             f"Daily loss limit: ${p.max_daily_loss_usd:g}",
             f"Min profit per contract: {p.min_ev_cents:g}¢ after fees", "",
             {"env": "🔑 Kalshi key: ✅ connected (from .env)",
              "telegram": "🔑 Kalshi key: ✅ connected · /removekey to remove",
              "none": "🔑 Kalshi key: ⚪ not connected · send /setkey"}[rt.kalshi_key_source()]]
    if note:
        lines += ["", escape(note)]
    buttons = grid2([("🔑 Kalshi account", "kacct"), ("🛡 Risk level", "risk"),
                     ("🤖 Auto trade", "auto"), ("📝 Paper / Real", "mode"),
                     ("🧠 How it works", "strat"), ("📊 Connections", "status")]) + [[("🧹 Tidy chat", "clear"), HOME]]
    return Screen("\n".join(lines), buttons, route="cfg")


def account(rt: BotRuntime, info: dict[str, Any]) -> Screen:
    """Kalshi account: connection, real balance, and what automatic trading needs."""
    st, s = rt.state, rt.settings
    src = info["source"]
    lines = ["🔑 <b>KALSHI ACCOUNT</b>", ""]
    if src == "none":
        lines += ["Status: ⚪ Not connected", "",
                  "Connect your Kalshi API key so the bot can read your real balance and positions,",
                  "and later place orders for you. Your messages are deleted the moment you send them."]
    else:
        lines.append(f"Status: ✅ Connected ({'from .env' if src == 'env' else 'via Telegram'})")
        if info["error"]:
            lines.append(f"⚠️ {escape(info['error'])}")
        else:
            lines += [f"💵 Real balance: <b>{_usd(info['balance'], signed=False)}</b>",
                      f"📂 Open Kalshi positions: {info['positions'] if info['positions'] is not None else NA}"]
    live_ready = s.live_allowed and src != "none"
    lines += [SEP, "🤖 <b>Automatic trading</b>",
              f"📝 Paper: Auto trade {'🟢 ON' if st.auto_trade and st.mode == 'paper' else '⚪ OFF'} "
              "(no key needed)",
              "💵 Real money: " + ("🟢 allowed - every order asks you to confirm" if live_ready else "⚪ OFF")]
    if not live_ready:
        steps = []
        if src == "none":
            steps.append("1. Connect your key (button below)")
        steps.append(f"{len(steps) + 1}. Build a paper record first (30+ trades, odds rated accurate)")
        steps.append(f"{len(steps) + 1}. In .env set LIVE_TRADING=true and PAPER_TRADING=false, restart")
        lines += ["", "<b>To use real money later:</b>", *steps]
    lines += ["", "<i>The key is stored only on the bot's server and is never shown in chat. "
                  "Real-money orders always need your confirmation.</i>"]
    rows: list[list[Button]] = [[("🔁 Replace key" if src != "none" else "🔑 Connect key", "keyset")]]
    if src == "telegram":
        rows[0].append(("🗑 Remove key", "keydel"))
    rows.append([("🔄 Refresh", "kacct"), ("🔙 Back", "cfg")])
    return Screen("\n".join(lines), rows, route="kacct")


def strategy(rt: BotRuntime) -> Screen:
    p = rt.profile()
    lvl = rt.state.risk_level
    lines = [
        "🧠 <b>HOW THE BOT DECIDES</b>", "",
        "1. Reads the market's rules (what YES means)",
        "2. Watches BTC's live price",
        "3. Estimates the chance YES wins",
        "4. Compares that chance with the Kalshi price",
        "5. Trades only if the price is a bargain after fees and nothing looks risky", "",
        f"<b>At {lvl.emoji} {lvl.value.title()} risk it needs</b>",
        f"• At least {p.min_ev_cents:g}¢ profit per contract after fees",
        f"• Signal strength {p.min_btc_strength}+ and a confident estimate",
        f"• {p.min_time_remaining_sec // 60}+ min left, prices {p.min_entry_price_cents:g}–{p.max_entry_price_cents:g}¢",
        "• Odds proven accurate" if p.require_known_ev else "• Paper trades allowed while the odds are still being checked",
        "", "<b>Safety</b>",
        f"• Max ${p.max_position_usd:g} per trade, ${p.max_daily_loss_usd:g} loss per day",
        f"• Pauses after {p.max_consecutive_losses} losses in a row",
        "• Real-money orders always need your tap to confirm",
    ]
    return Screen("\n".join(lines), [[("📈 Results", "banl"), ("🧪 Backtest", "bt")], [("🔙 Settings", "cfg"), HOME]],
                  route="strat")


def status(rt: BotRuntime) -> Screen:
    st, h = rt.state, rt.health()
    kalshi, btc_feed = _feeds(rt)
    feed = h.get("btc_feed")
    feed_txt = {"🟢": "connected", "🟡": "delayed", "🔴": "disconnected", "🧪": "test data", "⚪": "idle"}
    lines = ["📊 <b>CONNECTIONS</b>", "",
             f"Bot: {_state_line(rt)}",
             "Telegram: 🟢 connected",
             f"Kalshi: {kalshi} {feed_txt[kalshi]}" + (" - check your API key" if h["auth_failed"] else ""),
             f"BTC price: {btc_feed} {feed_txt[btc_feed]}"
             + (f" ({feed.age_sec:.0f}s ago)" if feed is not None and feed.age_sec is not None else ""),
             "", f"{_mode(rt)} · {st.risk_level.emoji} {st.risk_level.value.title()} risk · "
                 f"Auto {'on' if st.auto_trade else 'off'}",
             f"Open trades: {len(rt.portfolio.open_positions(st.mode))}"]
    if h["last_error"]:
        lines += ["", f"⚠️ <i>{escape(str(h['last_error'])[:150])}</i>"]
    return Screen("\n".join(lines), [[("🔄 Refresh", "status"), ("🔙 Settings", "cfg")], [HOME]], route="status",
                  refreshable=True)


def status_lines(rt: BotRuntime) -> list[str]:
    feed = rt.health().get("btc_feed")
    if feed is None:
        return []
    age = f" · {feed.age_sec:.0f}s old" if feed.age_sec is not None else ""
    return [f"BTC Feed: {feed.badge} ({escape(feed.source)}{age})"]


# ------------------------------------------------------------------ trades
def trades(rt: BotRuntime, page: int = 0, per_page: int = 8) -> Screen:
    mode = rt.state.mode
    rows = rt.repo.closed_positions(mode=mode, limit=per_page, offset=page * per_page)
    total = rt.repo.count_closed(mode)
    opens = rt.portfolio.open_positions(mode)
    lines = [f"💰 <b>TRADES</b> · {_mode(rt)}", ""]
    if opens:
        lines.append(f"<b>Open now ({len(opens)})</b>")
        for p in opens:
            lines.append(f"⏳ {p.side.upper()} x{p.contracts} @ {_c(p.entry_price)} · closes {_clock(p.close_time)}"
                         if p.close_time else f"⏳ {p.side.upper()} x{p.contracts} @ {_c(p.entry_price)}")
        lines.append("")
    lines.append("<b>Finished</b>" if rows else "No finished trades yet.")
    for p in rows:
        when = _clock(p.closed_at) if p.closed_at else ""
        lines.append(f"{'✅' if p.won else '❌'} {p.side.upper()} x{p.contracts} @ {_c(p.entry_price)} → "
                     f"{_c(p.exit_price)} · <b>{_usd(p.realized_pnl)}</b> · {when}")
    nav: list[Button] = []
    if page > 0:
        nav.append(("◀️ Newer", f"trades:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(("Older ▶️", f"trades:{page + 1}"))
    buttons = [nav] if nav else []
    buttons += [[("📂 Manage open", "pos"), ("📜 History", "hist:all")], [HOME]]
    return Screen("\n".join(lines), buttons, route=f"trades:{page}")


__all__ = ["home", "signal", "why", "analytics", "calibration", "backtest", "settings", "strategy", "status",
           "status_lines", "trades", "perf_metrics"]
