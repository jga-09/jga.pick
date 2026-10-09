"""Pure text/keyboard renderers for every Telegram screen and alert (HTML mode)."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any

from app import APP_NAME
from app.kalshi.market_data import ASSET_ICONS, MarketSnapshot
from app.risk.profiles import RiskLevel, RiskProfile
from app.strategy.signals import Direction, SignalResult, Validity
from app.telegram.keyboards import BACK, MAIN_MENU, Button, Screen, grid
from app.trading.positions import Position
from app.utils.time import fmt_countdown, utcnow

if TYPE_CHECKING:
    from app.risk.manager import RiskDecision
    from app.runtime import BotRuntime, MarketView
    from app.state import BotState
    from app.strategy.engine import SignalFlip
    from app.trading.tickets import TradeTicket

SEP = "━━━━━━━━━━━━━━━━━━"
CONFIDENCE_NOTE = "<i>Confidence = signal strength, not a win probability.</i>"


# ------------------------------------------------------------------ format
def money(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "n/a"
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else ("-" if v < 0 else "")
    return f"{sign}${abs(v):,.2f}"


def cents(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.0f}¢" if abs(v - round(v)) < 0.05 else f"{v:.1f}¢"


def onoff(v: bool) -> str:
    return "🟢 ON" if v else "🔴 OFF"


def mode_badge(mode: str) -> str:
    return "⚠️ LIVE" if mode == "live" else "📝 PAPER"


def dir_badge(sig: SignalResult | None) -> str:
    if sig is None:
        return "⚪ —"
    return f"{sig.direction.emoji} {sig.direction.value}"


def market_line(view: MarketView) -> str:
    return f"{view.info.icon} {escape(view.info.label)}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f}%"


# -------------------------------------------------------------------- home
def home(rt: BotRuntime) -> Screen:
    st = rt.state
    h = rt.health()
    run = "🔴 Stopped" if not st.running else ("⏸ Paused" if st.paused else "🟢 Running")
    data = "🧪 Fixture Data" if rt.fixture_mode else ("🟢 Live Data" if h["live_data"] else "🔴 No Data")
    view = rt.focus_view()
    stats = rt.portfolio.stats(st.mode, days=0)
    opens = rt.portfolio.open_positions(st.mode)

    if st.emergency_stop:
        status_line = "🚨 EMERGENCY STOP — orders blocked"
    elif view is None:
        status_line = "🔍 Searching for active 15M markets"
    elif view.decision and view.decision.approved:
        status_line = "🟢 Online — trade setup active"
    else:
        status_line = "🟢 Online — waiting for signal"

    lines = [
        f"📡 <b>{APP_NAME}</b>",
        f"{run} · {data}",
        status_line,
        f"🎯 Market: {escape(view.info.label) if view else '—'}",
        SEP,
        f"📊 Today: {money(stats.realized)}",
        f"📈 WR: {_pct(stats.win_rate)} · Trades: {stats.trades}",
        f"💰 Open: {len(opens)} · Bal: {money(rt.portfolio.balance(st.mode), signed=False)}",
        SEP,
    ]
    if view and view.signal and view.snapshot:
        sig, snap = view.signal, view.snapshot
        a = sig.analysis
        approved = bool(view.decision and view.decision.approved)
        label = f"{sig.leaning.emoji} {sig.leaning.value}" if approved else "⚪ NO TRADE"
        lines += [
            f"🎯 Signal: {label}",
            f"Quality: {a.quality}/100 · {a.grade} · Conf {sig.confidence}%" if a else f"Confidence: {sig.confidence}%",
            f"YES {cents(snap.yes_ask)} · NO {cents(snap.no_ask)}",
            f"⏱ {fmt_countdown(snap.time_remaining())}",
        ]
        if a:
            est = view.decision.estimate if view.decision else None
            ev = f"{est.ev_cents / 100:+.2f}$" if est is not None and est.sufficient else "unknown"
            lines += [f"📈 {a.regime.badge}", f"💰 EV: {ev} · 📚 n={est.n_markets if est else 0}"]
    else:
        lines += ["🎯 Signal: ⚪ —", "No market data yet" if st.running else "Press 🟢 Start to begin"]
    best_t = rt.model.best("time")
    lines += [f"🕐 Best window: {escape(best_t.key.split('/')[1]) + ' min' if best_t else 'insufficient data'}"]
    if rt.adaptive.mode != "NORMAL":
        lines += [rt.adaptive.badge]
    lines += [
        SEP,
        f"🛡 {st.risk_level.badge} RISK",
        f"{mode_badge(st.mode)} MODE",
        f"🤖 Auto Trade: {'ON' if st.auto_trade else 'OFF'}",
    ]
    buttons = list(MAIN_MENU)
    buttons += [("▶️ Resume", "resume") if st.paused else ("⏸ Pause", "pause"), ("🚨 E-Stop", "estop")]
    return Screen("\n".join(lines), grid(buttons), route="home", refreshable=True)


# ------------------------------------------------------------- signal card
def _hist_lines(rt: BotRuntime, view: MarketView) -> list[str]:
    d = view.decision
    est = d.estimate if d else None
    if est is not None and est.sufficient:
        return [f"📚 Similar setups: {est.n_markets:,}",
                f"📊 Historical WR: {est.win_rate:.0%}",
                f"💹 Est. edge: {est.edge * 100:+.1f}% · EV {est.ev_cents / 100:+.2f}$/contract"]
    n = est.n_markets if est else 0
    return [f"📚 Similar setups: ⚠️ INSUFFICIENT DATA (n={n})", "💹 Est. edge / EV: unknown"]


def _time_window(rt: BotRuntime, bucket: str) -> str:
    best = rt.model.best("time")
    star = " ⭐" if best is not None and best.key.endswith("/" + bucket) else ""
    return f"{escape(bucket)} min{star}"


def signal_card(rt: BotRuntime, ticker: str) -> Screen:
    view = rt.view(ticker)
    if view is None or view.snapshot is None:
        return Screen("⚪ No data for this market yet.", [[("🔄 Refresh", f"mkt:{ticker}"), ("🔙 Back", "signals")]],
                      route=f"mkt:{ticker}")
    sig, snap, decision = view.signal, view.snapshot, view.decision
    prof = rt.profile()
    head = [SEP, f"🎯 <b>{escape(view.info.asset)} · {view.info.window_minutes}M</b>", SEP, ""]
    if sig is None or sig.analysis is None:
        return Screen("\n".join([*head, "⚪ Collecting data…"]), [[("🔄 Refresh", f"mkt:{ticker}"), ("🔙 Back", "signals")]],
                      route=f"mkt:{ticker}", refreshable=True)
    a = sig.analysis
    approved = bool(decision and decision.approved)
    remaining = f"⏱ {fmt_countdown(snap.time_remaining())} remaining · {_time_window(rt, a.time_bucket)}"
    if not approved:
        # ⚪ NO TRADE card: show the lean, both scores and exactly why it is rejected.
        quality_flags = [] if rt.settings.strategy_mode == "confirm" else a.reasons
        reasons = list(dict.fromkeys([*quality_flags, *(decision.failures if decision else ())]))
        lean = f"{sig.leaning.emoji} {sig.leaning.value}" if sig.leaning is not Direction.WAIT else "⚪ none"
        lines = [*head, "<b>⚪ NO TRADE</b>", "", f"Signal: {lean}", f"Raw Confidence: {sig.confidence}%",
                 f"Signal Quality: {a.quality}/100 · {a.grade}", "",
                 f"YES: {cents(snap.yes_ask)} · NO: {cents(snap.no_ask)}", remaining,
                 f"📈 Regime: {a.regime.badge}", "", "Decision:", "❌ REJECTED", "", "Reasons:",
                 *[f"• {escape(r)}" for r in reasons[:6]]]
        buttons: list[Button] = [("🧠 Why?", f"why:{ticker}"), ("📋 Checklist", f"chk:{ticker}"),
                                 ("🔄 Refresh", f"mkt:{ticker}"), ("🔙 Back", "signals")]
        return Screen("\n".join(lines), grid(buttons), route=f"mkt:{ticker}", refreshable=True)

    lines = [
        *head, f"<b>{sig.leaning.emoji} {sig.leaning.value}</b>", "",
        f"Signal Quality: {a.quality}/100 · {a.grade}",
        f"Confidence: {sig.confidence}%", "",
        f"YES: {cents(snap.yes_ask)} · NO: {cents(snap.no_ask)}", remaining, "",
        f"📈 Regime: {a.regime.badge}",
        f"⚡ Momentum: {sig.momentum_label} · {a.accel_state.title()}",
        f"🔗 Underlying: {a.underlying_state.title()}" + (" ⚠️ DIVERGENCE" if a.divergence else ""),
        f"Book: {sig.book_label} · Spread: {cents(snap.spread)}",
        *_hist_lines(rt, view), "",
        f"🛡 Risk: {prof.level.badge}", "", "Decision:", "✅ TRADE APPROVED",
        *[f"⚠️ {escape(w)}" for w in decision.warnings],  # type: ignore[union-attr]
    ]
    buttons = []
    if decision and decision.side:
        buttons.append((f"💰 BUY {decision.side.upper()}", f"buy:{ticker}"))
    buttons += [("🧠 Why?", f"why:{ticker}"), ("📋 Checklist", f"chk:{ticker}"), ("📊 Details", f"det:{ticker}"),
                ("❌ Pass", "signals"), ("🔙 Back", "home")]
    return Screen("\n".join(lines), grid(buttons), route=f"mkt:{ticker}", refreshable=True)


def signal_details(rt: BotRuntime, ticker: str) -> Screen:
    view = rt.view(ticker)
    if view is None or view.signal is None or view.snapshot is None:
        return Screen("No signal yet.", [[("🔙 Back", f"mkt:{ticker}")]])
    sig, snap = view.signal, view.snapshot
    f = sig.features

    def fv(key: str, fmt: str = "{:.2f}", unit: str = "") -> str:
        v = f.get(key)
        return "n/a" if v is None else fmt.format(v) + unit

    lines = [
        f"📊 <b>DETAILS · {escape(view.info.label)}</b>",
        f"<code>{escape(ticker)}</code>",
        SEP,
        f"{dir_badge(sig)} · {sig.confidence}% · score {sig.score:+.2f}",
        f"Validity: {sig.validity.value}",
        "",
        "<b>Reasons</b>",
        *[escape(str(r)) for r in sig.reasons],
        "",
        "<b>Features</b>",
        f"Mom 60s: {fv('mom_60s', '{:+.1f}', '¢')} · 180s: {fv('mom_180s', '{:+.1f}', '¢')}",
        f"Slope: {fv('slope_cpm', '{:+.2f}', '¢/min')} · EMA Δ: {fv('ema_diff', '{:+.2f}', '¢')}",
        f"Accel: {fv('accel', '{:+.1f}', '¢')} · Vol: {fv('volatility', '{:.2f}', '¢')}",
        f"Book imb: {fv('book_imbalance', '{:+.2f}')} · Flow: {fv('trade_flow', '{:+.2f}')}",
        f"Trades 2m: {fv('trade_count_120s', '{:.0f}')} · Vol accel: {fv('volume_accel', '{:.2f}', 'x')}",
        f"Spread: {cents(snap.spread)} · YES bid/ask {cents(snap.yes_bid)}/{cents(snap.yes_ask)}",
        f"Spot vs strike: {fv('strike_dist_pct', '{:+.3f}', '%')}",
        f"History: {fv('history_sec', '{:.0f}', 's')} · Data age {snap.age_sec():.0f}s ({snap.source})",
        "",
        CONFIDENCE_NOTE,
    ]
    return Screen("\n".join(lines), [[("🔄 Refresh", f"det:{ticker}"), ("🔙 Back", f"mkt:{ticker}")]],
                  route=f"det:{ticker}")


def markets_list(rt: BotRuntime) -> Screen:
    views = rt.markets()
    lines = ["📊 <b>ACTIVE MARKETS</b>", ""]
    buttons: list[Button] = []
    if not views:
        lines.append("No active 15-minute markets found right now.\nThe scanner keeps checking automatically.")
    for v in views:
        sig = v.signal
        conf = f" · {sig.confidence}%" if sig else ""
        remaining = fmt_countdown(v.info.time_remaining())
        ok = bool(v.decision and v.decision.approved and sig)
        badge = f"{sig.leaning.emoji} {sig.leaning.value}" if ok and sig else "⚪ NO TRADE"
        q = f" · Q{sig.analysis.quality}" if sig and sig.analysis else ""
        lines += [market_line(v), f"{badge}{conf}{q}", f"⏱ {remaining}", ""]
        buttons.append((f"{v.info.icon} {v.info.asset} · {badge}", f"mkt:{v.info.ticker}"))
    buttons += [("🔄 Refresh", "signals"), BACK]
    return Screen("\n".join(lines).rstrip(), grid(buttons), route="signals", refreshable=True)


# ------------------------------------------------------------------ status
def _dot(ok: bool, yes: str, no: str) -> str:
    return f"🟢 {yes}" if ok else f"🔴 {no}"


def status(rt: BotRuntime) -> Screen:
    st, h = rt.state, rt.health()
    view = rt.focus_view()
    stats = rt.portfolio.stats(st.mode, days=0)
    if h["fixture"]:
        md = "🧪 FIXTURE"
    elif h["ws"]:
        md = "🟢 STREAMING"
    elif h["live_data"]:
        md = "🟢 POLLING"
    else:
        md = "🔴 NO DATA"
    if h["auth_failed"]:
        kalshi = "🔴 AUTH FAILED"
    else:
        kalshi = _dot(h["kalshi"], "CONNECTED", "DISCONNECTED") if st.running else "⚪ IDLE"
    bot = "🚨 E-STOP" if st.emergency_stop else ("⏸ PAUSED" if st.paused and st.running else
                                                  _dot(st.running, "RUNNING", "STOPPED"))
    lines = [
        "📡 <b>BOT STATUS</b>", "",
        f"Bot: {bot}",
        "Telegram: 🟢 CONNECTED",
        f"Kalshi: {kalshi}",
        f"Market Data: {md}",
        f"Signal Engine: {_dot(st.running, 'ACTIVE', 'IDLE')}",
        "Risk Manager: 🟢 ACTIVE",
        f"Trade Engine: {mode_badge(st.mode)}{' · 🔴 BLOCKED' if not st.trading_allowed else ''}",
        SEP,
        f"Market: {escape(view.info.label) if view else '—'}",
        f"Signal: {dir_badge(view.signal) if view else '⚪ —'}"
        + (f" · {view.signal.confidence}%" if view and view.signal else ""),
        f"Risk: {st.risk_level.badge}",
        f"Auto Trade: {onoff(st.auto_trade)}",
        f"Open Positions: {len(rt.portfolio.open_positions(st.mode))}",
        f"Today Trades: {stats.trades}",
        f"Today P&L: {money(stats.realized)}",
    ]
    if h["last_error"]:
        lines += ["", f"⚠️ <i>{escape(str(h['last_error'])[:150])}</i>"]
    return Screen("\n".join(lines), [[("🔄 Refresh", "status"), BACK]], route="status", refreshable=True)


# -------------------------------------------------------------------- risk
def profile_params(p: RiskProfile) -> list[str]:
    return [
        f"Confidence: ≥ {p.min_confidence}",
        f"Max Position: ${p.max_position_usd:g}",
        f"Max Open: {p.max_open_positions}",
        f"Daily Loss Limit: ${p.max_daily_loss_usd:g}",
        f"Min Time: {p.min_time_remaining_sec} sec",
        f"Max Spread: {p.max_spread_cents:g}¢",
        f"Signal Quality: ≥ {p.min_signal_quality}",
        f"Setups: {p.allowed_grades}",
    ]


def risk_menu(rt: BotRuntime) -> Screen:
    st = rt.state
    lines = ["🛡 <b>TRADING RISK</b>", "", "Current:", st.risk_level.badge, "", "Select:", "", SEP, "",
             "CURRENT PARAMETERS", "", *profile_params(rt.profile())]
    buttons = [[(lvl.badge, f"rset:{lvl.value}")] for lvl in RiskLevel]
    buttons += [[("⚙️ Customize", "rcust"), BACK]]
    return Screen("\n".join(lines), buttons, route="risk")


RISK_EFFECTS = {
    RiskLevel.HIGH: ["Larger positions", "More open trades", "Lower minimum confidence", "Higher exposure",
                     "Less time remaining"],
    RiskLevel.MEDIUM: ["Balanced position sizes", "Up to 2 open trades", "Moderate confidence bar"],
    RiskLevel.LOW: ["Small positions", "One open trade", "High confidence bar", "Longer cooldowns"],
}


def risk_confirm(rt: BotRuntime, new: RiskLevel) -> Screen:
    cur = rt.state.risk_level
    lines = ["⚠️ <b>CHANGE RISK LEVEL</b>", "", "Current:", cur.badge, "", "New:", new.badge, "",
             f"{new.title} allows:", *[f"• {e}" for e in RISK_EFFECTS[new]], "",
             *profile_params(rt.profile(new)), "", "<i>Risk level does not change paper/live mode.</i>"]
    return Screen("\n".join(lines), [[("✅ Confirm", f"rok:{new.value}"), ("❌ Cancel", "risk")]], route="risk")


RISK_FIELDS: dict[str, tuple[str, str, float]] = {
    # code: (field, label, step)
    "conf": ("min_confidence", "Confidence", 5),
    "pos": ("max_position_usd", "Max Position $", 5),
    "open": ("max_open_positions", "Max Open", 1),
    "loss": ("max_daily_loss_usd", "Daily Loss $", 5),
    "time": ("min_time_remaining_sec", "Min Time s", 30),
    "spr": ("max_spread_cents", "Max Spread ¢", 1),
    "qual": ("min_signal_quality", "Quality", 5),
}


def risk_customize(rt: BotRuntime, note: str = "") -> Screen:
    p = rt.profile()
    lines = [f"⚙️ <b>CUSTOMIZE {p.level.badge}</b>", "", *profile_params(p),
             f"Max Exposure: ${p.max_total_exposure_usd:g}", "",
             "<i>Hard safety limits apply to every profile.</i>"]
    if note:
        lines += ["", escape(note)]
    rows: list[list[Button]] = []
    for code, (_, label, _) in RISK_FIELDS.items():
        rows.append([(f"➖ {label}", f"radj:{code}:-"), (f"➕ {label}", f"radj:{code}:+")])
    rows.append([("♻️ Reset", "rrst"), ("🔙 Back", "risk")])
    return Screen("\n".join(lines), rows, route="rcust")


# ------------------------------------------------------------------ trades
def trades(rt: BotRuntime, page: int = 0, per_page: int = 5) -> Screen:
    mode = rt.state.mode
    rows = rt.repo.closed_positions(mode=mode, limit=per_page, offset=page * per_page)
    total = rt.repo.count_closed(mode)
    lines = [f"💰 <b>RECENT TRADES</b> · {mode_badge(mode)}", ""]
    if not rows:
        lines.append("No closed trades yet.")
    for p in rows:
        lines += [
            f"{'✅' if p.won else '❌'} {escape(p.label)}",
            f"{p.side.upper()} @ {cents(p.entry_price)} → {cents(p.exit_price)}",
            f"P&L: {money(p.realized_pnl)}",
            f"Risk: {p.risk_level.upper()}",
            "",
        ]
    nav: list[Button] = []
    if page > 0:
        nav.append(("◀️ Newer", f"trades:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(("Older ▶️", f"trades:{page + 1}"))
    buttons = [nav] if nav else []
    buttons += [[("📂 Positions", "pos"), BACK]]
    return Screen("\n".join(lines).rstrip(), buttons, route=f"trades:{page}")


def positions(rt: BotRuntime) -> Screen:
    mode = rt.state.mode
    opens = rt.portfolio.open_positions()
    lines = ["📂 <b>OPEN POSITIONS</b>", ""]
    buttons: list[list[Button]] = []
    if not opens:
        lines.append("No open positions.")
    for p in opens:
        snap = rt.market_data.latest.get(p.ticker)
        upnl = p.unrealized_pnl(snap.exit_price(p.side) if snap else None)
        remaining = fmt_countdown((p.close_time - utcnow()).total_seconds()) if p.close_time else "—"
        lines += [
            f"{mode_badge(p.mode)} · {escape(p.label)}",
            f"{p.side.upper()} x{p.contracts} @ {cents(p.entry_price)} · cost {money(p.cost, False)}",
            f"Mark: {cents(snap.exit_price(p.side) if snap else None)} · uP&L {money(upnl)}",
            f"⏱ {remaining}", "",
        ]
        buttons.append([(f"❌ Close {p.label}", f"pcl:{p.id}")])
    total_u = rt.portfolio.unrealized(mode, rt.market_data.latest)
    lines.append(f"Unrealized ({mode.upper()}): {money(total_u)}")
    buttons.append([("🔄 Refresh", "pos"), BACK])
    return Screen("\n".join(lines), buttons, route="pos", refreshable=True)


def close_confirm(rt: BotRuntime, pos: Position) -> Screen:
    snap = rt.market_data.latest.get(pos.ticker)
    bid = snap.exit_price(pos.side) if snap else None
    title = "⚠️ <b>LIVE EXIT CONFIRMATION</b>" if pos.mode == "live" else "📝 <b>PAPER EXIT</b>"
    lines = [title, "", escape(pos.label), f"SELL {pos.side.upper()} x{pos.contracts}",
             f"Bid: {cents(bid)} (entry {cents(pos.entry_price)})",
             f"Est. P&L: {money(pos.unrealized_pnl(bid))}"]
    return Screen("\n".join(lines), [[("✅ Confirm Exit", f"pclok:{pos.id}"), ("❌ Cancel", "pos")]], route="pos")


# ----------------------------------------------------------------- history
PERIODS = {"today": ("Today", 0), "7d": ("7 Days", 7), "30d": ("30 Days", 30), "all": ("All Time", None)}


def history(rt: BotRuntime, period: str) -> Screen:
    name, days = PERIODS.get(period, PERIODS["today"])
    mode = rt.state.mode
    s = rt.portfolio.stats(mode, days=days)
    lines = [f"📜 <b>HISTORY · {name}</b> · {mode_badge(mode)}", ""]
    if s.trades == 0:
        lines.append("No closed trades in this period.")
    else:
        lines += [
            f"P&L: {money(s.realized)}",
            f"Trades: {s.trades} · W {s.wins} / L {s.losses}",
            f"Win Rate: {_pct(s.win_rate)}",
            f"Avg Trade: {money(s.avg_result)} · Avg Entry: {cents(s.avg_entry)}",
            f"Best: {money(s.best)} · Worst: {money(s.worst)}",
            f"Fees: {money(s.fees, False)}",
            "", "<b>By asset</b>", *[f"{ASSET_ICONS.get(a, '•')} {a}: {money(v)}" for a, v in s.by_asset.items()],
            "", "<b>By risk</b>", *[f"{RiskLevel(r).emoji} {r.upper()}: {money(v)}" for r, v in s.by_risk.items()],
            "", "<b>By confidence</b>",
            *[f"{escape(b)}: {n} trades · {w}W · {money(p)}" for b, (n, w, p) in s.by_confidence.items()],
        ]
    if mode == "paper":
        lines += ["", f"Paper balance: {money(rt.portfolio.paper_balance(), False)} "
                      f"(start {money(rt.settings.paper_starting_balance, False)})"]
    lines += ["", "<i>Past results are recorded data, not a profitability claim.</i>"]
    buttons = grid([(("• " if k == period else "") + v[0], f"hist:{k}") for k, v in PERIODS.items()])
    buttons.append([BACK])
    return Screen("\n".join(lines), buttons, route=f"hist:{period}")


# ---------------------------------------------------------------- strategy
def strategy(rt: BotRuntime) -> Screen:
    st, p = rt.state, rt.profile()
    und = rt.underlying.name
    lines = [
        "🧠 <b>STRATEGY</b>", "",
        "Type:", "Directional", "",
        "Mode:", ("🧪 CONFIRM (experimental V4, paper only)" if rt.settings.strategy_mode == "confirm"
                  else "Quality filter"), "",
        "Market:", f"{rt.settings.market_duration_minutes} MIN", "",
        "Signal Model:", "Multi-factor confirmation (10 components)" + (" + Spot" if und != "none" else ""), "",
        "Minimum Confidence:", str(p.min_confidence), "",
        "Minimum Signal Quality:", str(p.min_signal_quality), "",
        "Allowed Setups:", p.allowed_grades, "",
        "Risk:", st.risk_level.title, "",
        "Auto Trade:", "ON" if st.auto_trade else "OFF",
    ]
    return Screen("\n".join(lines), [[("📈 Analytics", "anl"), ("📊 Indicators", "ind")],
                                      [("⚙️ Settings", "cfg"), BACK]], route="strat")


def indicators(rt: BotRuntime) -> Screen:
    from app.strategy.scoring import DEFAULT_WEIGHTS

    und = rt.underlying.name
    desc = {
        "momentum": "Δ YES mid over 60s",
        "trend": "EMA(5/20) + regression slope",
        "orderbook": "YES vs NO bid depth, near-touch depth, imbalance change",
        "volume": "Aggressive taker flow × volume intensity",
        "prob_move": "Market probability move over 5 min",
        "underlying": f"Spot vs strike + spot momentum ({und})" if und != "none" else "Spot feed: not configured",
        "acceleration": "60s slope vs prior 2 min slope",
    }
    lines = ["📊 <b>INDICATORS</b>", ""]
    for k, w in DEFAULT_WEIGHTS.items():
        avail = not (k == "underlying" and und == "none")
        lines.append(f"{'🟢' if avail else '⚪'} {k.title()} ({w:.0%}): {desc[k]}")
    lines += ["", "Penalties: wide spread, high volatility, short history, near expiry.",
              f"Engine floor: {rt.settings.signal_min_confidence}", "", CONFIDENCE_NOTE]
    return Screen("\n".join(lines), [[("🔙 Back", "strat")]], route="ind")


# ------------------------------------------------------------------ config
def config(rt: BotRuntime) -> Screen:
    st, p = rt.state, rt.profile()
    lines = [
        "⚙️ <b>CONFIGURATION</b>", "",
        "Assets:", ", ".join(rt.settings.asset_list) or "—", "",
        "Duration:", f"{rt.settings.market_duration_minutes} MIN", "",
        "Risk:", st.risk_level.badge, "",
        "Execution:", mode_badge(st.mode), "",
        "Auto Trade:", onoff(st.auto_trade), "",
        "Min Confidence:", str(p.min_confidence), "",
        "Position Limit:", f"${p.max_position_usd:g}",
    ]
    buttons = [("🛡 Risk", "risk"), ("🎯 Assets", "assets"), ("💰 Position", "possz"),
               ("🤖 Auto Trade", "auto"), ("📝 Paper/Live", "mode"), BACK]
    return Screen("\n".join(lines), grid(buttons), route="cfg")


def assets(rt: BotRuntime) -> Screen:
    active = {v.info.asset: v for v in rt.markets()}
    lines = ["🎯 <b>ASSETS</b>", "", "Tap an asset to make it the dashboard focus.", ""]
    for a in rt.settings.asset_list:
        v = active.get(a)
        mark = "🟢" if v else "⚪"
        focus = " ⭐" if rt.state.focus_asset == a else ""
        lines.append(f"{mark} {ASSET_ICONS.get(a, '•')} {a}{focus} — {escape(v.info.ticker) if v else 'no active market'}")
    lines += ["", "<i>Edit ASSETS / SERIES_TICKERS in .env to change the universe.</i>"]
    buttons = grid([(f"{ASSET_ICONS.get(a, '•')} {a}", f"focus:{a}") for a in rt.settings.asset_list])
    buttons.append([("🔙 Back", "cfg")])
    return Screen("\n".join(lines), buttons, route="assets")


def position_settings(rt: BotRuntime) -> Screen:
    p, s = rt.profile(), rt.settings
    lines = [
        "💰 <b>POSITION SIZING</b>", "",
        f"Profile: {p.level.badge}",
        f"Max per trade: ${p.max_position_usd:g}",
        f"Max total exposure: ${p.max_total_exposure_usd:g}",
        f"Max balance fraction: {p.max_balance_fraction:.0%}",
        f"Entry price band: {p.min_entry_price_cents:g}-{p.max_entry_price_cents:g}¢",
        f"Min book liquidity: {p.min_liquidity_contracts:g} contracts",
        "",
        f"Paper start balance: {money(s.paper_starting_balance, False)}",
        f"Paper slippage: {s.paper_slippage_cents}¢ · Fee rate: {s.fee_rate:g}",
        "", "<i>Size scales 50-100% of max with confidence above the threshold.</i>",
    ]
    return Screen("\n".join(lines), [[("⚙️ Customize", "rcust"), ("🔙 Back", "cfg")]], route="possz")


def auto_trade(rt: BotRuntime, note: str = "") -> Screen:
    st = rt.state
    lines = ["🤖 <b>AUTO TRADE</b>", "", f"Status: {onoff(st.auto_trade)}", f"Mode: {mode_badge(st.mode)}", "",
             "When ON, risk-approved signals are executed automatically.",
             "Live auto-trading additionally requires ALLOW_LIVE_AUTOTRADE=true "
             f"({'✅' if rt.settings.live_autotrade_allowed else '❌'})."]
    if note:
        lines += ["", escape(note)]
    return Screen("\n".join(lines), [[("🟢 Turn ON", "auto:on"), ("🔴 Turn OFF", "auto:off")], [("🔙 Back", "cfg")]],
                  route="auto")


def mode_screen(rt: BotRuntime, note: str = "") -> Screen:
    s = rt.settings

    def ck(ok: bool) -> str:
        return "✅" if ok else "❌"

    lines = [
        "📝 <b>EXECUTION MODE</b>", "", f"Current: {mode_badge(rt.state.mode)}", "",
        "Live trading requires ALL of:",
        f"{ck(s.live_trading)} LIVE_TRADING=true",
        f"{ck(not s.paper_trading)} PAPER_TRADING=false",
        f"{ck(s.has_kalshi_credentials)} Kalshi credentials",
        f"{ck(s.data_source == 'kalshi')} Real Kalshi data",
        "✅ Admin + per-trade confirmation",
    ]
    if note:
        lines += ["", escape(note)]
    buttons = [[("📝 Paper", "mode:paper"), ("⚠️ Live", "mode:live")], [("🔙 Back", "cfg")]]
    return Screen("\n".join(lines), buttons, route="mode")


def live_mode_confirm() -> Screen:
    text = ("⚠️ <b>SWITCH TO LIVE TRADING?</b>\n\nOrders will use REAL money on Kalshi.\n"
            "Every live trade still needs its own confirmation.")
    return Screen(text, [[("✅ Enable LIVE", "modeok:live"), ("❌ Cancel", "mode")]], route="mode")


# ----------------------------------------------------------- trade tickets
def ticket_screen(t: TradeTicket) -> Screen:
    d = Direction(t.direction)
    action = f"BUY {t.side.upper()}"
    if t.mode == "live":
        lines = ["⚠️ <b>LIVE TRADE CONFIRMATION</b>", "", escape(t.label), "", "Signal:", f"{d.emoji} {d.value}", "",
                 "Action:", action, "", "Price:", f"{cents(t.quoted_price_cents)} (limit {cents(t.limit_price_cents)})", "", "Contracts:", str(t.contracts), "",
                 "Maximum Cost:", money(t.max_cost, False), "", "Risk:", RiskLevel(t.risk_level).badge, "",
                 "<i>Expires in 60s. Risk is re-checked before submission.</i>"]
        buttons = [[("✅ CONFIRM LIVE TRADE", f"tok:{t.id}")], [("❌ CANCEL", f"tno:{t.id}")]]
    else:
        lines = ["📝 <b>PAPER TRADE</b>", "", escape(t.label), "", "Direction:", f"{d.emoji} {d.value}", "",
                 "Action:", action, "", "Entry:", f"{cents(t.quoted_price_cents)} (max {cents(t.limit_price_cents)})", "", "Contracts:", str(t.contracts), "",
                 "Cost:", f"≤ {money(t.max_cost, False)} incl. est. fee", "",
                 "Risk:", RiskLevel(t.risk_level).badge]
        buttons = [[("✅ PAPER BUY", f"tok:{t.id}"), ("❌ CANCEL", f"tno:{t.id}")]]
    return Screen("\n".join(lines), buttons, route="home")


def trade_result(pos: Position) -> Screen:
    head = "⚠️ <b>LIVE ORDER FILLED</b>" if pos.mode == "live" else "✅ <b>PAPER ORDER FILLED</b>"
    text = (f"{head}\n\n{escape(pos.label)}\nBUY {pos.side.upper()} x{pos.contracts} @ {cents(pos.entry_price)}\n"
            f"Cost: {money(pos.cost, False)} · Fee: {money(pos.entry_fee, False)}")
    return Screen(text, [[("📂 Positions", "pos"), ("🏠 Home", "home")]], route="pos")


def notice(text: str, back: str = "home") -> Screen:
    return Screen(text, [[("🔙 Back", back), ("🏠 Home", "home")]] if back != "home" else [[("🏠 Home", "home")]],
                  route=back)


# --------------------------------------------------------------- emergency
def emergency(rt: BotRuntime) -> Screen:
    st = rt.state
    if not st.emergency_stop:
        text = ("🚨 <b>EMERGENCY STOP?</b>\n\nImmediately blocks ALL new orders and turns auto trade off.\n"
                "Market data and signals keep running.")
        return Screen(text, [[("🚨 ACTIVATE", "estopok"), ("❌ Cancel", "home")]], route="estop")
    run = rt.health()
    text = "\n".join([
        "🚨 <b>EMERGENCY STOP ACTIVE</b>", "", "Trading:", "🔴 DISABLED", "",
        "Market Data:", "🟢 RUNNING" if run["running"] else "⚪ IDLE", "",
        "Signals:", "🟢 RUNNING" if run["running"] else "⚪ IDLE", "", "Orders:", "🔴 BLOCKED",
    ])
    return Screen(text, [[("🔓 Reset Stop", "ereset")], [("🏠 Home", "home")]], route="estop")


def emergency_reset_confirm() -> Screen:
    return Screen("🔓 <b>RESET EMERGENCY STOP?</b>\n\nOrders will be allowed again (auto trade stays OFF).",
                  [[("✅ Confirm Reset", "eresetok"), ("❌ Cancel", "estop")]], route="estop")


def unauthorized() -> Screen:
    return Screen("⛔ Unauthorized. This bot is restricted to configured admins.", [], route="none")


def help_text() -> str:
    return "\n".join([
        f"📡 <b>{APP_NAME} — commands</b>", "",
        "/menu — dashboard", "/status — bot status", "/markets — active markets",
        "/signals — current signals", "/risk — risk profile", "/config — configuration",
        "/positions — open positions", "/trades — recent trades", "/pnl — today's P&L",
        "/history — performance", "/strategy — strategy info", "/pause · /resume — trading",
        "/stop — stop the bot", "/estop — emergency stop", "/health — health check", "/help — this help",
        "", CONFIDENCE_NOTE,
    ])


def pnl(rt: BotRuntime) -> Screen:
    mode = rt.state.mode
    s = rt.portfolio.stats(mode, days=0)
    u = rt.portfolio.unrealized(mode, rt.market_data.latest)
    lines = [f"💵 <b>P&L</b> · {mode_badge(mode)}", "",
             f"Today realized: {money(s.realized)}", f"Unrealized: {money(u)}",
             f"Balance: {money(rt.portfolio.balance(mode), False)}",
             f"Exposure: {money(rt.portfolio.exposure(mode), False)}",
             f"Trades today: {s.trades} · WR {_pct(s.win_rate)}"]
    return Screen("\n".join(lines), [[("📜 History", "hist:all"), BACK]], route="pnl", refreshable=True)


# ------------------------------------------------------------------ alerts
def strong_signal_alert(sig: SignalResult, snap: MarketSnapshot, decision: RiskDecision,
                        st: BotState) -> tuple[str, list[list[Button]]]:
    text = "\n".join([
        "🚨 <b>STRONG SETUP</b>", "", escape(snap.info.label), "", f"{sig.leaning.emoji} {sig.leaning.value}",
        f"Signal Quality: {sig.quality}/100 · {sig.grade}", f"Confidence: {sig.confidence}%",
        "", f"YES: {cents(snap.yes_ask)}", f"NO: {cents(snap.no_ask)}", "",
        "Time:", fmt_countdown(snap.time_remaining()), "", "Risk:", st.risk_level.badge, "",
        "Status:", "✅ Trade allowed" if decision.approved else f"❌ {escape(decision.reason)}",
    ])
    buttons: list[Button] = []
    if decision.approved:
        buttons.append(("💰 Trade", f"buy:{snap.ticker}"))
    buttons.append(("📊 Details", f"mkt:{snap.ticker}"))
    return text, [buttons]


def flip_alert(flip: SignalFlip) -> str:
    p, c = flip.previous, flip.current
    neg = [r.text for r in c.reasons if r.positive]
    reason = "Momentum reversal" if c.components.get("momentum", 0) * p.score < 0 else (neg[0] if neg else "Signal change")
    return "\n".join(["⚠️ <b>SIGNAL FLIP</b>", "", escape(flip.label), "",
                      f"{p.leaning.emoji} {p.leaning.value}", "→", f"{c.leaning.emoji} {c.leaning.value}", "",
                      "Previous:", f"{p.confidence}%", "", "New:", f"{c.confidence}%", "", "Reason:", escape(reason)])


def trade_opened_alert(pos: Position) -> str:
    return (f"{'⚠️ LIVE' if pos.mode == 'live' else '📝 PAPER'} <b>TRADE OPENED</b>\n{escape(pos.label)} · "
            f"BUY {pos.side.upper()} x{pos.contracts} @ {cents(pos.entry_price)}")


def trade_closed_alert(pos: Position) -> str:
    return (f"{'✅' if pos.won else '❌'} <b>TRADE CLOSED</b> · {pos.mode.upper()}\n{escape(pos.label)} "
            f"{pos.side.upper()} @ {cents(pos.entry_price)} → {cents(pos.exit_price)}\n"
            f"P&L: {money(pos.realized_pnl)} ({escape(pos.close_reason or '')})")


def large_loss_alert(pos: Position) -> str:
    return f"🔻 <b>LARGE LOSS</b>\n{escape(pos.label)}: {money(pos.realized_pnl)}"


def daily_limit_alert(p: RiskProfile) -> str:
    return (f"🛑 <b>DAILY LOSS LIMIT REACHED</b>\n{p.level.badge} limit ${p.max_daily_loss_usd:g}.\n"
            "New trades are blocked until tomorrow (UTC).")


def describe_validity(v: Validity) -> str:
    return v.value.replace("_", " ").title()


def fmt_any(v: Any) -> str:
    return escape(str(v))
