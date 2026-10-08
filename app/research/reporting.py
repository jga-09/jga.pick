"""Plain-text research report shared by scripts/research.py and scripts/simulate.py."""

from __future__ import annotations

from app.research.backtest import BacktestConfig, filter_ablation, report, walk_forward
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
