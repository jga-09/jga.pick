"""Telegram screens for the research layer: why/checklist, analytics, experiments.

Every statistic shown here comes from recorded data; anything under the
minimum sample size is shown as "⚠️ INSUFFICIENT DATA".
"""

from __future__ import annotations

import time
from html import escape
from typing import TYPE_CHECKING, Any

from app.research.backtest import BacktestConfig, filter_ablation, walk_forward
from app.research.importance import feature_importance
from app.research.stats import trade_metrics
from app.research.tradeanalysis import LOSS_TEXT, WIN_TEXT, pattern_counts
from app.strategy.quality import FILTER_TEXT, TIME_BUCKETS
from app.strategy.regime import Regime
from app.telegram.keyboards import Screen, grid

if TYPE_CHECKING:
    from app.research.calibration import GroupStat
    from app.runtime import BotRuntime

SEP = "━━━━━━━━━━━━━━━━━━"
INSUFFICIENT = "⚠️ INSUFFICIENT DATA"
COMPONENT_LABELS = {
    "momentum": "Momentum", "trend": "Trend", "orderbook": "Order Book", "volume": "Volume",
    "prob_move": "Prob. Move", "underlying": "Underlying", "acceleration": "Acceleration",
    "liquidity": "Liquidity", "volatility": "Volatility", "stability": "Stability", "time": "Time",
}


def pct(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.1f}%" if signed else f"{v * 100:.0f}%"


def stat_line(st: GroupStat | None, min_n: int) -> str:
    if st is None or st.n_markets < min_n:
        n = st.n_markets if st else 0
        return f"{INSUFFICIENT} (n={n})"
    return f"WR {st.win_rate:.0%} · need {st.breakeven:.0%} · EV {st.ev_cents:+.1f}¢ (n={st.n_markets})"


# -------------------------------------------------------------- why / checklist
def why(rt: BotRuntime, ticker: str) -> Screen:
    view = rt.view(ticker)
    back = [[("🔙 Back", f"mkt:{ticker}")]]
    if view is None or view.signal is None or view.signal.analysis is None:
        return Screen("No analysis for this market yet.", back, route=f"why:{ticker}")
    sig, a, d = view.signal, view.signal.analysis, view.decision
    lean = sig.leaning
    lines = ["🧠 <b>WHY THIS TRADE?</b>" if d and d.approved else "🧠 <b>WHY NOT?</b>", "",
             escape(view.info.label), f"{lean.emoji} {lean.value}", "",
             f"Signal Quality: {a.quality}/100 · {a.grade}", f"Confidence: {sig.confidence}%", "",
             "<b>Component points</b>"]
    for k, pts in sorted(a.points.items(), key=lambda kv: -kv[1]):
        mark = "✓" if pts > 0.5 else "✗" if pts < -0.5 else "·"
        lines.append(f"{mark} {COMPONENT_LABELS.get(k, k)} {pts:+.0f} / {a.max_points.get(k, 0):.0f}")
    missing = [COMPONENT_LABELS[k] for k in ("underlying",) if k not in a.points]
    if missing:
        lines.append(f"· {', '.join(missing)}: unavailable")
    est = d.estimate if d else None
    lines += ["", f"Regime: {a.regime.badge}", f"Momentum: {a.accel_state.title()}",
              f"Underlying: {a.underlying_state.title()}" + (" ⚠️ DIVERGENCE" if a.divergence else "")]
    if est is not None and est.sufficient:
        lines += ["", f"Historical setup win rate: {est.win_rate:.0%}",
                  f"Sample: {est.n_markets:,} similar setups ({est.level})",
                  f"Estimated edge: {pct(est.edge, True)}", f"Estimated EV: {est.ev_cents / 100:+.2f}$ / contract"]
    else:
        n = est.n_markets if est else 0
        lines += ["", f"Historical setup: {INSUFFICIENT} (n={n})", "Estimated edge: unknown"]
    if a.reasons:
        lines += ["", "<b>Flags</b>", *[f"• {escape(r)}" for r in a.reasons]]
    lines += ["", f"Risk: {rt.state.risk_level.badge}", "",
              "Decision:", "✅ TRADE" if d and d.approved else f"❌ {escape(d.reason if d else 'n/a')}"]
    return Screen("\n".join(lines), [[("✅ Checklist", f"chk:{ticker}"), ("🔙 Back", f"mkt:{ticker}")]],
                  route=f"why:{ticker}")


def checklist(rt: BotRuntime, ticker: str) -> Screen:
    view = rt.view(ticker)
    back = [[("🧠 Why?", f"why:{ticker}"), ("🔙 Back", f"mkt:{ticker}")]]
    if view is None or view.decision is None or not view.decision.checklist:
        return Screen("No checklist yet - waiting for a directional setup.", back, route=f"chk:{ticker}")
    d = view.decision
    lines = ["📋 <b>TRADE CHECKLIST</b>", "", f"Market: {escape(view.info.label)}", ""]
    for c in d.checklist:
        lines.append(f"{c.label}: {escape(c.value)} {c.mark}")
    lines += ["", f"Risk: {rt.state.risk_level.badge}", "", "Final:"]
    if d.approved:
        lines.append("🟢 TRADE APPROVED")
        lines += [f"⚠️ {escape(w)}" for w in d.warnings]
    else:
        lines += ["🔴 TRADE REJECTED", *[f"• {escape(f)}" for f in d.failures[:6]]]
    return Screen("\n".join(lines), back, route=f"chk:{ticker}", refreshable=True)


# -------------------------------------------------------------------- analytics
def analytics(rt: BotRuntime) -> Screen:
    m = rt.model
    n_min = rt.settings.hist_min_samples
    trades = rt.repo.closed_positions(mode=rt.state.mode, limit=100_000)
    tm = trade_metrics([p.realized_pnl or 0.0 for p in reversed(trades)])
    total, settled = rt.repo.count_observations()
    lines = ["🧠 <b>STRATEGY ANALYTICS</b>", "", f"<b>Trades ({rt.state.mode.upper()})</b>"]
    if tm.trades == 0:
        lines.append("No closed trades yet.")
    else:
        pf = "∞" if tm.profit_factor == float("inf") else f"{tm.profit_factor:.2f}" if tm.profit_factor else "—"
        lines += [f"Win Rate: {tm.win_rate:.1%} ({tm.wins}/{tm.trades})" + ("" if tm.trades >= n_min else " ⚠️ small"),
                  f"EV: {tm.ev:+.2f}$/trade · PF {pf}", f"Max DD: ${tm.max_drawdown:.2f} · Streak {tm.max_losing_streak}"]
    lines += ["", f"<b>Setups</b> (observations {settled:,}/{total:,} settled · {m.n_markets:,} markets)"]
    for g in ("A+", "A", "B", "C", "NO_TRADE"):
        lines.append(f"{g}: {stat_line(m.stat('grade', g), n_min)}")
    best_regime, best_time, best_price = m.best("regime"), m.best("time"), m.best("price")
    lines += [SEP,
              f"Best Regime: {escape(best_regime.key.split('/')[1]) if best_regime else INSUFFICIENT}",
              f"Best Time: {escape(best_time.key.split('/')[1]) + ' min' if best_time else INSUFFICIENT}",
              f"Best Entry: {escape(' '.join(best_price.key.split('/')[1:])) + '¢' if best_price else INSUFFICIENT}"]
    feats = rt.research("importance")
    ok = [f for f in feats if f.stars > 0]
    lines += [SEP, f"Best Feature: {COMPONENT_LABELS.get(ok[0].name, ok[0].name) if ok else INSUFFICIENT}",
              f"Weakest Feature: {COMPONENT_LABELS.get(ok[-1].name, ok[-1].name) if len(ok) > 1 else INSUFFICIENT}",
              "", f"Market mode: {rt.adaptive.badge}" + (f"\n{escape(rt.adaptive.reason)}" if rt.adaptive.reason else "")]
    buttons = [("📊 Detailed Stats", "anld"), ("📏 Calibration", "cal"), ("📉 Loss Analysis", "loss"),
               ("📈 Win Analysis", "wins"), ("🧪 Experiments", "exp"), ("🧠 Features", "feat"), ("🔙 Back", "strat")]
    return Screen("\n".join(lines), grid(buttons), route="anl")


def detailed(rt: BotRuntime) -> Screen:
    m, n_min = rt.model, rt.settings.hist_min_samples
    lines = ["📊 <b>DETAILED STATS</b>", "<i>WR vs break-even (price + costs), per distinct market.</i>", "",
             "<b>Time remaining</b>"]
    best_t = m.best("time")
    for b in TIME_BUCKETS:
        st = m.stat("time", b)
        star = " ⭐" if best_t and st and st.key == best_t.key else ""
        lines.append(f"{escape(b):>5} min: {stat_line(st, n_min)}{star}")
    lines += ["", "<b>Regime (UP / DOWN)</b>"]
    for r in Regime:
        up, dn = m.stat("regime_dir", r.value, "UP"), m.stat("regime_dir", r.value, "DOWN")
        if up or dn:
            lines.append(f"{r.badge}")
            lines.append(f"  UP {stat_line(up, n_min)}")
            lines.append(f"  DOWN {stat_line(dn, n_min)}")
    lines += ["", "<b>Entry price</b>"]
    for st in m.table("price"):
        side, bucket = st.key.split("/")[1:]
        lines.append(f"{side.upper()} {escape(bucket)}¢: {stat_line(st, n_min)}")
    lines += ["", "<b>Momentum state</b>"]
    for st in m.table("accel"):
        lines.append(f"{st.key.split('/')[1].title()}: {stat_line(st, n_min)}")
    lines += ["", "<b>Underlying</b>"]
    for st in m.table("underlying"):
        lines.append(f"{st.key.split('/')[1].title()}: {stat_line(st, n_min)}")
    return _long(Screen("\n".join(lines), [[("🔙 Back", "anl")]], route="anld"))


def calibration(rt: BotRuntime) -> Screen:
    m, n_min = rt.model, rt.settings.hist_min_samples
    lines = ["📏 <b>CALIBRATION</b>", "<i>Is a higher score actually winning more often?</i>", "",
             "<b>Confidence</b> | markets | WR | avg price | edge"]
    for st in m.table("conf"):
        lines.append(_calib_row(st, n_min))
    lines += ["", "<b>Signal Quality</b> | markets | WR | avg price | edge"]
    for st in m.table("quality"):
        lines.append(_calib_row(st, n_min))
    lines += ["", "A score is only meaningful if its EDGE (WR minus price paid) rises with the bucket."]
    return Screen("\n".join(lines), [[("🔙 Back", "anl")]], route="cal")


def _calib_row(st: GroupStat, n_min: int) -> str:
    b = escape(st.key.split("/")[1])
    if st.n_markets < n_min:
        return f"{b}: {INSUFFICIENT} (n={st.n_markets})"
    return f"{b}: {st.n_markets} | {st.win_rate:.0%} | {st.avg_price:.0f}¢ | edge {st.edge * 100:+.1f}pp"


def loss_analysis(rt: BotRuntime) -> Screen:
    ctxs = rt.repo.trade_contexts(limit=500)
    losses = [c for c in ctxs if c["won"] is False]
    lines = ["📉 <b>LOSS ANALYSIS</b>", ""]
    if not losses:
        lines.append("No analysed losses yet.")
    else:
        last = losses[0]
        e = last["entry"]
        lines += ["<b>Last loss</b>", escape(last["ticker"]), e.get("direction", "?"), "",
                  "Reason:", escape(last["summary"] or "—"), "",
                  f"Regime: {e.get('regime', '—')}", f"Quality: {e.get('quality', '—')} · {e.get('grade', '—')}",
                  f"Time remaining: {_mmss(e.get('time_remaining'))}", ""]
        lines.append(f"<b>Recurring patterns</b> ({len(losses)} losses)")
        for text, n in pattern_counts(ctxs, won=False)[:8]:
            lines.append(f"• {escape(text)}: {n}")
    lines += ["", "<i>Tags are heuristics for spotting patterns, not proof of cause.</i>"]
    return Screen("\n".join(lines), [[("📈 Wins", "wins"), ("🔙 Back", "anl")]], route="loss")


def win_analysis(rt: BotRuntime) -> Screen:
    ctxs = rt.repo.trade_contexts(limit=500)
    wins = [c for c in ctxs if c["won"] is True]
    lines = ["📈 <b>WIN ANALYSIS</b>", ""]
    if not wins:
        lines.append("No analysed wins yet.")
    else:
        last = wins[0]
        e = last["entry"]
        lines += ["<b>Last win</b>", escape(last["ticker"]), e.get("direction", "?"), "", "Successful conditions:"]
        lines += [f"✓ {WIN_TEXT.get(t, t)}" for t in last["tags"]] or ["—"]
        lines += [f"✓ {escape(str(e.get('time_bucket', '?')))} minutes remaining", "",
                  f"<b>Recurring patterns</b> ({len(wins)} wins)"]
        for text, n in pattern_counts(ctxs, won=True)[:8]:
            lines.append(f"• {escape(text)}: {n}")
    return Screen("\n".join(lines), [[("📉 Losses", "loss"), ("🔙 Back", "anl")]], route="wins")


def experiments(rt: BotRuntime) -> Screen:
    wf = rt.research("walkforward")
    lines = ["🧪 <b>STRATEGY COMPARISON</b>", "<i>Walk-forward, out-of-sample test periods only.</i>", ""]
    if not wf.results:
        lines += [INSUFFICIENT, escape(wf.note)]
    else:
        lines.append(f"{wf.folds} folds · {wf.markets:,} markets")
        for r in wf.results:
            o = r.oos
            if o.trades == 0:
                lines += ["", f"<b>{r.strategy.key}</b> {escape(r.strategy.name)}", "0 trades"]
                continue
            pf = "∞" if o.profit_factor == float("inf") else f"{o.profit_factor:.2f}" if o.profit_factor else "—"
            warn = " ⚠️ <30" if o.trades < 30 else ""
            lines += ["", f"<b>{r.strategy.key}</b> {escape(r.strategy.name)}",
                      f"WR {o.win_rate:.1%} [{o.wr_low:.0%}-{o.wr_high:.0%}] · {o.trades} trades{warn}",
                      f"EV {o.ev * 100:+.1f}¢ · P&L ${o.pnl:+.2f} · PF {pf} · DD ${o.max_drawdown:.2f}"]
        lines += ["", "<i>Nothing is deployed automatically. Prefer positive OOS EV with ≥30 trades.</i>"]
    return _long(Screen("\n".join(lines), [[("🧱 Filters", "filt"), ("🔙 Back", "anl")]], route="exp"))


def filters(rt: BotRuntime) -> Screen:
    verdicts = rt.research("ablation")
    icon = {"helps": "✅", "hurts": "❌", "no_evidence": "⚪", "insufficient": "⚠️"}
    lines = ["🧱 <b>NO-TRADE FILTERS</b>", "<i>Do flagged setups really do worse? (EV per contract)</i>", ""]
    for v in verdicts:
        if v.verdict == "insufficient" or v.ev_flagged is None or v.ev_clean is None:
            lines.append(f"⚠️ {escape(v.text)}: insufficient data (flagged {v.n_flagged})")
        else:
            lines.append(f"{icon[v.verdict]} {escape(v.text)}: flagged {v.ev_flagged * 100:+.1f}¢ vs clean "
                         f"{v.ev_clean * 100:+.1f}¢ (n={v.n_flagged}, z={v.z:+.1f})")
    lines += ["", "✅ helps · ❌ flagged setups did better (consider DISABLED_FILTERS) · ⚪ no evidence"]
    return _long(Screen("\n".join(lines), [[("🔙 Back", "exp")]], route="filt"))


def features(rt: BotRuntime) -> Screen:
    scores = rt.research("importance")
    lines = ["🧠 <b>FEATURE PERFORMANCE</b>", "<i>Predictive power BEYOND the market price (excess outcome); stars need |z| ≥ 2.</i>", ""]
    for f in scores:
        name = COMPONENT_LABELS.get(f.name, f.name)
        if f.status == "insufficient":
            lines.append(f"{name}: {INSUFFICIENT} (n={f.n_markets})")
            continue
        metric = f"r {f.effect:+.3f}" if f.kind == "directional" else f"lift {f.effect:+.1f}¢"
        note = {"not_significant": " (not significant)", "inverse": " (works in reverse!)"}.get(f.status, "")
        lines.append(f"{name}: {f.star_text}  {metric} n={f.n_markets}{note}")
    return Screen("\n".join(lines), [[("🔙 Back", "anl")]], route="feat")


# ----------------------------------------------------------------------- utils
def _mmss(sec: Any) -> str:
    if sec is None:
        return "—"
    s = int(sec)
    return f"{s // 60}:{s % 60:02d}"


def _long(screen: Screen, limit: int = 3900) -> Screen:
    if len(screen.text) > limit:
        screen.text = screen.text[:limit].rsplit("\n", 1)[0] + "\n…"
    return screen


class ResearchCache:
    """Expensive research computations, recomputed at most every ``ttl`` seconds."""

    def __init__(self, ttl: float = 300.0) -> None:
        self.ttl = ttl
        self._cache: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, compute: Any) -> Any:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        value = compute()
        self._cache[key] = (time.time(), value)
        return value

    def clear(self) -> None:
        self._cache.clear()


def compute_research(rt: BotRuntime, key: str) -> Any:
    obs = rt.model.obs
    s = rt.settings
    cfg = BacktestConfig(fee_rate=s.fee_rate, slippage=s.paper_slippage_cents, min_samples=s.hist_min_samples,
                         prior_strength=s.calibration_prior_strength)
    if key == "importance":
        return feature_importance(obs, s.hist_min_samples, s.fee_rate, s.paper_slippage_cents)
    if key == "ablation":
        return filter_ablation(obs, cfg)
    if key == "walkforward":
        n = len({o.ticker for o in obs})
        size = max(s.hist_min_samples * 2, n // 8)
        return walk_forward(obs, cfg, "markets", size)
    raise KeyError(key)


_ = (FILTER_TEXT, LOSS_TEXT)  # re-exported for screens that list them
