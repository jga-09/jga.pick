"""Walk-forward backtesting over recorded observations.

Every strategy trades at most once per market (the first observation it
accepts, in time order) at the recorded ask + slippage, pays estimated fees and
is settled with the real recorded outcome. Strategies that learn anything (V6)
only ever learn from data strictly earlier than the period they are tested on.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.research.calibration import HistoricalModel, cost_cents
from app.research.dataset import Observation
from app.research.stats import TradeMetrics, diff_z, mean, stdev, trade_metrics
from app.strategy.config import ALL_FILTERS
from app.strategy.quality import FILTER_TEXT

Selector = Callable[[Observation, dict[str, Any]], str | None]


@dataclass
class Strategy:
    key: str
    name: str
    select: Selector
    fit: Callable[[list[Observation], list[Observation], "BacktestConfig"], dict[str, Any]] | None = None


@dataclass(frozen=True)
class BacktestConfig:
    fee_rate: float = 0.07
    slippage: float = 1.0
    min_samples: int = 30
    prior_strength: float = 20.0
    min_ev_cents: float = 1.0
    min_val_trades: int = 10
    ev_gate: str = "lower"


@dataclass
class SimTrade:
    ticker: str
    ts: datetime
    side: str
    price: float
    won: bool
    pnl: float  # dollars per contract
    hold_sec: float
    obs: Observation


def _sign(side: str | None) -> int:
    return 1 if side == "yes" else -1 if side == "no" else 0


def _comp(o: Observation, name: str) -> float | None:
    return o.components.get(name)


def _agree(o: Observation, names: tuple[str, ...], level: float) -> str | None:
    vals = [_comp(o, n) for n in names]
    if any(v is None for v in vals):
        return None
    if all(v >= level for v in vals):  # type: ignore[operator]
        return "yes"
    if all(v <= -level for v in vals):  # type: ignore[operator]
        return "no"
    return None


def _v0(o: Observation, ctx: dict[str, Any]) -> str | None:
    return o.side


def _v1(o: Observation, ctx: dict[str, Any]) -> str | None:
    return _agree(o, ("momentum",), 0.5)


def _v2(o: Observation, ctx: dict[str, Any]) -> str | None:
    return _agree(o, ("momentum", "trend"), 0.3)


def _v3(o: Observation, ctx: dict[str, Any]) -> str | None:
    side = _v2(o, ctx)
    ob = _comp(o, "orderbook")
    return side if side and ob is not None and ob * _sign(side) >= 0.1 else None


def _v4(o: Observation, ctx: dict[str, Any]) -> str | None:
    side = _v3(o, ctx)
    u = _comp(o, "underlying")
    return side if side and u is not None and u * _sign(side) >= 0.2 else None


def _v5(o: Observation, ctx: dict[str, Any]) -> str | None:
    return o.side if o.grade in ("A+", "A", "B") else None


def _v6(o: Observation, ctx: dict[str, Any]) -> str | None:
    if o.grade not in ("A+", "A", "B") or o.quality < ctx.get("min_quality", 70):
        return None
    model: HistoricalModel | None = ctx.get("model")
    if model is None or o.side is None or o.entry_price is None:
        return None
    est = model.estimate(o.grade, o.regime, o.time_bucket, o.side, o.entry_price)
    gate = est.ev_low_cents if ctx.get("ev_gate", "lower") == "lower" else est.ev_cents
    if est.poor or not est.sufficient or (gate or 0) < ctx.get("min_ev", 1.0):
        return None
    return o.side


def _fit_v6(train: list[Observation], val: list[Observation], cfg: BacktestConfig) -> dict[str, Any]:
    model = HistoricalModel(train, cfg.min_samples, cfg.prior_strength, cfg.fee_rate, cfg.slippage)
    best_q, best_ev = 90, None
    for q in (70, 80, 90):  # deliberately tiny grid -> little room to overfit
        trades = simulate(val, _v6, {"model": model, "min_quality": q, "min_ev": cfg.min_ev_cents,
                                     "ev_gate": cfg.ev_gate}, cfg)
        if len(trades) >= cfg.min_val_trades:
            ev = mean([t.pnl for t in trades])
            if best_ev is None or ev > best_ev:
                best_q, best_ev = q, ev
    # Refit on train + validation before testing (still strictly earlier than the test period).
    model = HistoricalModel(train + val, cfg.min_samples, cfg.prior_strength, cfg.fee_rate, cfg.slippage)
    return {"model": model, "min_quality": best_q, "min_ev": cfg.min_ev_cents, "ev_gate": cfg.ev_gate}


STRATEGIES: list[Strategy] = [
    Strategy("V0", "Baseline: every engine lean", _v0),
    Strategy("V1", "Momentum only", _v1),
    Strategy("V2", "Momentum + Trend", _v2),
    Strategy("V3", "Momentum + Trend + Order book", _v3),
    Strategy("V4", "V3 + Underlying", _v4),
    Strategy("V5", "Full confirmation (grade ≥ B)", _v5),
    Strategy("V6", "V5 + regime/time/price filter + EV gate (fit on past only)", _v6, _fit_v6),
]


def simulate(obs: list[Observation], select: Selector, ctx: dict[str, Any], cfg: BacktestConfig) -> list[SimTrade]:
    traded: set[str] = set()
    out: list[SimTrade] = []
    for o in sorted(obs, key=lambda x: x.ts):
        if o.ticker in traded or o.outcome not in ("yes", "no"):
            continue
        side = select(o, ctx)
        if side is None:
            continue
        price = o.yes_ask if side == "yes" else o.no_ask
        if price is None or not 1 <= price <= 99:
            continue
        paid = price + cost_cents(price, cfg.fee_rate, cfg.slippage)
        won = o.outcome == side
        traded.add(o.ticker)
        out.append(SimTrade(o.ticker, o.ts, side, price, won, ((100 if won else 0) - paid) / 100,
                            (o.close_time - o.ts).total_seconds(), o))
    return out


def metrics(trades: list[SimTrade]) -> TradeMetrics:
    return trade_metrics([t.pnl for t in trades], [t.price for t in trades], [t.hold_sec for t in trades])


# ------------------------------------------------------------- walk-forward
def chunk_markets(obs: list[Observation], unit: str = "markets", size: int = 200) -> list[list[Observation]]:
    """Split chronologically into chunks of whole markets (by close time)."""
    by_market: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        by_market[o.ticker].append(o)
    markets = sorted(by_market.values(), key=lambda rows: rows[0].close_time)
    chunks: list[list[Observation]] = []
    if unit == "days":
        by_day: dict[str, list[Observation]] = defaultdict(list)
        for rows in markets:
            by_day[rows[0].close_time.strftime("%Y-%m-%d")].extend(rows)
        days = sorted(by_day)
        for i in range(0, len(days), size):
            chunks.append([o for d in days[i:i + size] for o in by_day[d]])
    else:
        for i in range(0, len(markets), size):
            chunks.append([o for rows in markets[i:i + size] for o in rows])
    return [c for c in chunks if c]


@dataclass
class StrategyResult:
    strategy: Strategy
    oos: TradeMetrics
    fold_evs: list[float] = field(default_factory=list)
    fold_wrs: list[float] = field(default_factory=list)
    trades: list[SimTrade] = field(default_factory=list)

    @property
    def ev_stability(self) -> float | None:
        return stdev(self.fold_evs) if len(self.fold_evs) >= 2 else None


@dataclass
class WalkForwardResult:
    folds: int
    markets: int
    results: list[StrategyResult]
    note: str = ""


def walk_forward(obs: list[Observation], cfg: BacktestConfig | None = None, unit: str = "markets",
                 size: int = 200, strategies: list[Strategy] | None = None) -> WalkForwardResult:
    """Rolling train(i) -> validate(i+1) -> test(i+2) over chronological chunks."""
    cfg = cfg or BacktestConfig()
    settled = [o for o in obs if o.outcome in ("yes", "no")]
    chunks = chunk_markets(settled, unit, size)
    strategies = strategies or STRATEGIES
    n_markets = len({o.ticker for o in settled})
    if len(chunks) < 3:
        return WalkForwardResult(0, n_markets, [], note=f"Need ≥3 chunks of {size} {unit}; have {len(chunks)}.")
    results = {s.key: StrategyResult(s, trade_metrics([])) for s in strategies}
    for i in range(len(chunks) - 2):
        train, val, test = chunks[i], chunks[i + 1], chunks[i + 2]
        for s in strategies:
            ctx = s.fit(train, val, cfg) if s.fit else {}
            trades = simulate(test, s.select, ctx, cfg)
            r = results[s.key]
            r.trades.extend(trades)
            if trades:
                r.fold_evs.append(mean([t.pnl for t in trades]))
                r.fold_wrs.append(sum(t.won for t in trades) / len(trades))
    for r in results.values():
        r.oos = metrics(r.trades)
    return WalkForwardResult(len(chunks) - 2, n_markets, list(results.values()))


# ---------------------------------------------------------- filter ablation
@dataclass(frozen=True)
class FilterVerdict:
    name: str
    text: str
    n_flagged: int
    n_clean: int
    ev_flagged: float | None
    ev_clean: float | None
    wr_flagged: float | None
    wr_clean: float | None
    z: float | None
    verdict: str  # helps | hurts | no_evidence | insufficient


def filter_ablation(obs: list[Observation], cfg: BacktestConfig | None = None) -> list[FilterVerdict]:
    """Does each no-trade filter actually flag worse setups?

    Filters are fixed a-priori rules (nothing is fitted), so evaluating them on
    the whole recorded history does not leak. One trade per market per side of
    the comparison (first flagged / first clean observation).
    """
    cfg = cfg or BacktestConfig()
    settled = [o for o in obs if o.outcome in ("yes", "no") and o.side]
    out = []
    for name in ALL_FILTERS:
        flagged = simulate(settled, lambda o, c, n=name: o.side if n in o.flags else None, {}, cfg)
        clean = simulate(settled, lambda o, c, n=name: o.side if n not in o.flags else None, {}, cfg)
        fp, cp = [t.pnl for t in flagged], [t.pnl for t in clean]
        z = diff_z(fp, cp)
        if len(flagged) < cfg.min_samples or len(clean) < cfg.min_samples:
            verdict = "insufficient"
        elif z is not None and z <= -2:
            verdict = "helps"
        elif z is not None and z >= 2:
            verdict = "hurts"
        else:
            verdict = "no_evidence"
        out.append(FilterVerdict(
            name, FILTER_TEXT.get(name, name), len(flagged), len(clean),
            mean(fp) if fp else None, mean(cp) if cp else None,
            (sum(t.won for t in flagged) / len(flagged)) if flagged else None,
            (sum(t.won for t in clean) / len(clean)) if clean else None, z, verdict,
        ))
    return out


# ---------------------------------------------------------------- reporting
def fmt_metrics(m: TradeMetrics) -> str:
    if m.trades == 0:
        return "0 trades"
    pf = "∞" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}" if m.profit_factor else "—"
    sig = "" if m.trades >= 30 else "  ⚠️ <30 trades"
    return (f"{m.trades:>5} trades  WR {m.win_rate:6.1%} [{m.wr_low:.0%}-{m.wr_high:.0%}]  "
            f"EV {m.ev * 100:+6.2f}¢  P&L {m.pnl:+8.2f}  PF {pf:>5}  maxDD {m.max_drawdown:6.2f}  "
            f"streak {m.max_losing_streak}{sig}")


def report(wf: WalkForwardResult, ablation: list[FilterVerdict] | None = None) -> str:
    lines = [f"WALK-FORWARD: {wf.folds} folds over {wf.markets} markets (out-of-sample test periods only)"]
    if wf.note:
        lines.append(f"⚠️ {wf.note}")
    for r in wf.results:
        stab = f"  fold-EV sd {r.ev_stability * 100:.2f}¢" if r.ev_stability is not None else ""
        lines.append(f"  {r.strategy.key} {r.strategy.name}")
        lines.append(f"      {fmt_metrics(r.oos)}{stab}")
    if ablation:
        lines.append("\nNO-TRADE FILTER ABLATION (flagged vs clean setups, EV per contract):")
        for v in ablation:
            if v.ev_flagged is None or v.ev_clean is None:
                lines.append(f"  {v.name:<22} n/a (flagged {v.n_flagged}, clean {v.n_clean})")
                continue
            z = f"{v.z:+.1f}" if v.z is not None else "n/a"
            lines.append(f"  {v.name:<22} {v.verdict:<12} flagged {v.n_flagged:>5} EV {v.ev_flagged * 100:+6.2f}¢ "
                         f"WR {v.wr_flagged:.0%} | clean {v.n_clean:>5} EV {v.ev_clean * 100:+6.2f}¢ "
                         f"WR {v.wr_clean:.0%} | z {z}")
    return "\n".join(lines)
