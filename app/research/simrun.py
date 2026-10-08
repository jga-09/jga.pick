"""Run the REAL signal engine, filters, risk manager and research code on simulated markets."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.research.calibration import HistoricalModel, cost_cents
from app.research.dataset import Observation, from_signal
from app.research.simulator import generate
from app.research.stats import TradeMetrics, trade_metrics
from app.risk.manager import RiskContext, RiskManager
from app.risk.profiles import RiskLevel, RiskProfile, load_profiles
from app.risk.sizing import PositionSizer
from app.strategy.config import StrategyConfig
from app.strategy.engine import SignalEngine

EVAL_EVERY = 2  # evaluate every 2nd 5s step (10s) to keep runs fast


@dataclass
class PaperSimResult:
    level: RiskLevel
    pnls: list[float] = field(default_factory=list)  # dollars per trade (sized by RiskManager)
    per_contract: list[float] = field(default_factory=list)  # dollars per contract
    entries: list[float] = field(default_factory=list)
    holds: list[float] = field(default_factory=list)
    grades: dict[str, list[float]] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)

    @property
    def metrics(self) -> TradeMetrics:
        return trade_metrics(self.pnls, self.entries, self.holds)

    @property
    def contract_metrics(self) -> TradeMetrics:
        return trade_metrics(self.per_contract, self.entries, self.holds)


def collect_observations(world: str, n_markets: int, seed: int = 7,
                         config: StrategyConfig | None = None) -> list[Observation]:
    """Engine pass over simulated markets -> settled observations (one per market-minute)."""
    obs: list[Observation] = []
    for m in generate(world, n_markets, seed):
        engine = SignalEngine(config=config)
        seen: set[int] = set()
        for i, snap in enumerate(m.snapshots):
            if i % EVAL_EVERY:
                engine.record(snap)
                continue
            spot_hist = m.spot[max(0, i - 48): i + 1]
            sig = engine.evaluate(snap, now=snap.ts, spot=spot_hist[-1], spot_history=spot_hist)
            o = from_signal(sig, snap, snap.ts, source="sim")
            if o is not None and o.minute not in seen:
                seen.add(o.minute)
                o.outcome = m.outcome
                obs.append(o)
    return obs


def paper_simulation(world: str, n_markets: int, seed: int = 11, refit_every: int = 50,
                     levels: tuple[RiskLevel, ...] = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH),
                     fee_rate: float = 0.07, slippage: float = 1.0, min_samples: int = 30,
                     balance: float = 1000.0, ev_gate: str = "lower") -> dict[RiskLevel, PaperSimResult]:
    """Walk forward through fresh markets; the historical model only sees markets already settled."""
    profiles = load_profiles(read_env=False)
    risk = RiskManager(PositionSizer(fee_rate), stale_after_sec=30, price_buffer_cents=2, ev_gate=ev_gate)
    results = {lvl: PaperSimResult(lvl) for lvl in levels}
    history: list[Observation] = []
    model = HistoricalModel([], min_samples, 20.0, fee_rate, slippage)
    daily: dict[tuple[RiskLevel, str], float] = {}
    for n, m in enumerate(generate(world, n_markets, seed)):
        if n and n % refit_every == 0:
            model = HistoricalModel(history, min_samples, 20.0, fee_rate, slippage)
        engine = SignalEngine()
        seen: set[int] = set()
        traded: set[RiskLevel] = set()
        market_obs: list[Observation] = []
        for i, snap in enumerate(m.snapshots):
            if i % EVAL_EVERY:
                engine.record(snap)
                continue
            spot_hist = m.spot[max(0, i - 48): i + 1]
            sig = engine.evaluate(snap, now=snap.ts, spot=spot_hist[-1], spot_history=spot_hist)
            o = from_signal(sig, snap, snap.ts, source="sim")
            if o is not None and o.minute not in seen:
                seen.add(o.minute)
                market_obs.append(o)
            side = sig.leaning.side
            price = snap.entry_price(side) if side else None
            if sig.analysis is None or side is None or price is None:
                continue
            est = model.estimate(sig.analysis.grade, sig.analysis.regime.value, sig.analysis.time_bucket,
                                 side, price)
            day = snap.ts.strftime("%Y-%m-%d")
            for lvl in levels:
                if lvl in traded:
                    continue
                prof: RiskProfile = profiles[lvl]
                ctx = RiskContext(True, "", "paper", True, balance, 0, 0.0, frozenset(), frozenset(),
                                  daily.get((lvl, day), 0.0))
                d = risk.evaluate(sig, snap, prof, ctx, snap.ts, estimate=est)
                r = results[lvl]
                if not d.approved:
                    key = re.sub(r"[-+]?\d+(\.\d+)?", "#", d.reason.split("(")[0].split(":")[0]).strip()[:45]
                    r.rejections[key] = r.rejections.get(key, 0) + 1
                    continue
                traded.add(lvl)
                fill = min(99.0, price + slippage)
                won = m.outcome == side
                per_c = ((100 if won else 0) - fill - cost_cents(fill, fee_rate, 0)) / 100
                pnl = per_c * d.position_size
                r.pnls.append(pnl)
                r.per_contract.append(per_c)
                r.entries.append(fill)
                r.holds.append(snap.time_remaining(snap.ts))
                r.grades.setdefault(sig.analysis.grade, []).append(per_c)
                daily[(lvl, day)] = daily.get((lvl, day), 0.0) + pnl
        for o in market_obs:
            o.outcome = m.outcome  # settlement happens after the market closes
        history.extend(market_obs)
    return results
