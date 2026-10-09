"""Plain-text research report shared by scripts/research.py and scripts/simulate.py."""

from __future__ import annotations

import random
from collections import defaultdict

from app.research.backtest import (
    STRATEGIES,
    BacktestConfig,
    filter_ablation,
    fmt_metrics,
    metrics,
    report,
    simulate,
    walk_forward,
)
from app.research.calibration import HistoricalModel
from app.research.dataset import Observation
from app.research.importance import feature_importance
from app.strategy.quality import TIME_BUCKETS


def research_report(obs: list[Observation], cfg: BacktestConfig, unit: str = "markets", size: int | None = None) -> str:
    settled = [o for o in obs if o.outcome in ("yes", "no")]
    n_markets = len({o.ticker for o in settled})
    out = [f"Observations: {len(settled):,} settled across {n_markets:,} markets"]
    if n_markets < cfg.min_samples:
        out.append(f"⚠️ INSUFFICIENT DATA: need at least {cfg.min_samples} settled markets for any statistic.")
        return "\n".join(out)
    model = HistoricalModel(settled, cfg.min_samples, cfg.prior_strength, cfg.fee_rate, cfg.slippage)

    def row(st):  # type: ignore[no-untyped-def]
        if st is None or st.n_markets < cfg.min_samples:
            return f"INSUFFICIENT (n={st.n_markets if st else 0})"
        return (f"n={st.n_markets:>5}  WR {st.win_rate:5.1%} [{st.wr_low:.0%}-{st.wr_high:.0%}]  "
                f"avg price {st.avg_price:4.1f}¢  edge {st.edge * 100:+5.1f}pp  EV {st.ev_cents:+5.1f}¢")

    out.append("\nCALIBRATION - signal quality (engine direction, per market)")
    for st in model.table("quality"):
        out.append(f"  Q {st.key.split('/')[1]:>7}: {row(st)}")
    out.append("CALIBRATION - confidence")
    for st in model.table("conf"):
        out.append(f"  C {st.key.split('/')[1]:>7}: {row(st)}")
    out.append("BY GRADE")
    for g in ("A+", "A", "B", "C", "NO_TRADE"):
        out.append(f"  {g:>8}: {row(model.stat('grade', g))}")
    out.append("BY REGIME")
    for st in model.table("regime"):
        out.append(f"  {st.key.split('/')[1]:>16}: {row(st)}")
    out.append("BY TIME REMAINING")
    for b in TIME_BUCKETS:
        out.append(f"  {b:>6} min: {row(model.stat('time', b))}")
    out.append("BY ENTRY PRICE")
    for st in model.table("price"):
        side, bucket = st.key.split("/")[1:]
        out.append(f"  {side.upper():>3} {bucket:>6}¢: {row(st)}")

    out.append("\nFEATURE IMPORTANCE - predicts the outcome BEYOND the price? (r vs excess outcome; stars need |z|>=2)")
    for f in feature_importance(settled, cfg.min_samples, cfg.fee_rate, cfg.slippage):
        eff = "—" if f.effect is None else (f"r {f.effect:+.3f}" if f.kind == "directional" else f"lift {f.effect:+.2f}¢")
        z = "—" if f.z is None else f"{f.z:+.1f}"
        out.append(f"  {f.name:<13} {f.star_text}  {eff:<14} z {z:>5}  n={f.n_markets:<5} {f.status}")

    size = size or max(cfg.min_samples * 2, n_markets // 8)
    wf = walk_forward(settled, cfg, unit, size)
    out.append("")
    out.append(report(wf, filter_ablation(settled, cfg)))
    return "\n".join(out)


def bootstrap_ev(pnls: list[float], n: int = 5000, seed: int = 1) -> tuple[float, float, float]:
    """95% bootstrap interval for mean P&L per trade, and P(mean <= 0)."""
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(pnls, k=len(pnls))) / len(pnls) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)], sum(1 for m in means if m <= 0) / n


def stress_report(obs: list[Observation], key: str, cfg: BacktestConfig, picked_on_this_data: bool = True) -> str:
    """Robustness check of one fixed (unfitted) strategy rule."""
    strat = next(x for x in STRATEGIES if x.key == key)
    base = next(x for x in STRATEGIES if x.key == "V0")
    trades = simulate(obs, strat.select, {}, cfg)
    n_markets = len({o.ticker for o in obs if o.outcome in ("yes", "no")})
    out = [f"{strat.key} {strat.name} on {n_markets} settled markets"]
    if len(trades) < 20:
        out.append(f"⚠️ INSUFFICIENT DATA: only {len(trades)} trades.")
        return "\n".join(out)
    out.append(f"ALL            {fmt_metrics(metrics(trades))}")
    out.append(f"baseline V0    {fmt_metrics(metrics(simulate(obs, base.select, {}, cfg)))}")
    lo, hi, p_neg = bootstrap_ev([t.pnl for t in trades])
    out.append(f"EV per contract 95% bootstrap range: {lo * 100:+.1f}¢ to {hi * 100:+.1f}¢  "
               f"(chance the true EV is <= 0: {p_neg:.1%})")

    def split(title, keyfn):  # type: ignore[no-untyped-def]
        groups: dict[str, list] = defaultdict(list)
        for t in trades:
            groups[keyfn(t)].append(t)
        out.append(f"\n{title}")
        for k in sorted(groups):
            out.append(f"  {k:<12} {fmt_metrics(metrics(groups[k]))}")

    ordered = sorted(trades, key=lambda t: t.ts)
    half = ordered[len(ordered) // 2].ts
    split("STABILITY OVER TIME (first vs second half)", lambda t: "1st half" if t.ts < half else "2nd half")
    split("BY ASSET", lambda t: t.obs.asset)
    split("BY SIDE", lambda t: "YES (UP)" if t.side == "yes" else "NO (DOWN)")
    split("BY TIME LEFT AT ENTRY", lambda t: t.obs.time_bucket + " min")
    split("BY ENTRY PRICE", lambda t: "<50¢" if t.price < 50 else "50-70¢" if t.price < 70 else "70-85¢"
          if t.price < 85 else "85¢+")
    halves: dict[bool, list[float]] = defaultdict(list)
    for t in trades:
        halves[t.ts < half].append(t.pnl)
    both_positive = all(sum(v) > 0 for v in halves.values())
    out.append("\nVERDICT")
    if lo > 0 and both_positive:
        out.append("✅ Positive across the whole bootstrap range and in both halves.")
    elif p_neg < 0.2 and both_positive:
        out.append("🟡 Leaning positive but not conclusive.")
    else:
        out.append("❌ Not robust: the result could easily be luck (or one half carries it).")
    if picked_on_this_data:
        out.append("Note: this rule was picked after seeing this data - a pass is encouraging, not proof.")
    return "\n".join(out)
