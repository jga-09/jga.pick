"""Measured feature importance (never hand-assigned).

* Directional components: correlation of the YES-signed score with the EXCESS
  outcome (YES result - market-implied YES probability). The price already
  predicts the outcome, so a feature only matters if it predicts beyond the
  price. Significance uses distinct-market counts (conservative, because
  observations from one market share an outcome). AUC vs the raw outcome is
  reported alongside for reference only.
* Quality components (liquidity, volatility, stability, time): EV difference
  between the top and bottom third of engine-direction setups.
Stars are only awarded when the effect is statistically significant (|z| >= 2).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from app.research.calibration import cost_cents
from app.research.dataset import Observation
from app.research.stats import diff_z, mean
from app.strategy.components import DIRECTIONAL, QUALITY


@dataclass(frozen=True)
class FeatureScore:
    name: str
    kind: str  # directional | quality
    n_markets: int
    effect: float | None  # correlation with excess outcome (directional) or EV lift in cents (quality)
    z: float | None
    stars: int
    status: str  # ok | not_significant | insufficient | inverse

    @property
    def star_text(self) -> str:
        return "★" * self.stars + "☆" * (5 - self.stars)


def _stars_corr(r: float) -> int:
    d = abs(r)
    return 5 if d >= 0.15 else 4 if d >= 0.10 else 3 if d >= 0.06 else 2 if d >= 0.03 else 1


def _implied_yes(o: Observation) -> float | None:
    mid = o.features.get("yes_mid")
    if mid is None and o.yes_ask is not None and o.no_ask is not None:
        mid = (o.yes_ask + (100 - o.no_ask)) / 2
    return None if mid is None else mid / 100


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)


def _stars_lift(c: float) -> int:
    return 5 if c >= 8 else 4 if c >= 5 else 3 if c >= 3 else 2 if c >= 1 else 1


def feature_importance(obs: list[Observation], min_samples: int = 30, fee_rate: float = 0.07,
                       slippage: float = 1.0) -> list[FeatureScore]:
    settled = [o for o in obs if o.outcome in ("yes", "no")]
    out: list[FeatureScore] = []
    for name in DIRECTIONAL:
        rows = [o for o in settled if name in o.components and _implied_yes(o) is not None]
        markets = {o.ticker for o in rows}
        if len(markets) < min_samples:
            out.append(FeatureScore(name, "directional", len(markets), None, None, 0, "insufficient"))
            continue
        xs = [o.components[name] for o in rows]
        resid = [(1.0 if o.outcome == "yes" else 0.0) - _implied_yes(o) for o in rows]  # type: ignore[operator]
        r = _pearson(xs, resid)
        if r is None:
            out.append(FeatureScore(name, "directional", len(markets), None, None, 0, "insufficient"))
            continue
        z = r * math.sqrt(len(markets))
        if abs(z) < 2:
            out.append(FeatureScore(name, "directional", len(markets), r, z, 0, "not_significant"))
        else:
            out.append(FeatureScore(name, "directional", len(markets), r, z, _stars_corr(r) if r > 0 else 0,
                                    "ok" if r > 0 else "inverse"))
    directional = [o for o in settled if o.side and o.entry_price is not None]
    for name in QUALITY:
        rows = [o for o in directional if name in o.components]
        if len({o.ticker for o in rows}) < min_samples * 2:
            out.append(FeatureScore(name, "quality", len({o.ticker for o in rows}), None, None, 0, "insufficient"))
            continue
        vals = sorted(o.components[name] for o in rows)
        lo_cut, hi_cut = vals[len(vals) // 3], vals[(2 * len(vals)) // 3]
        top = _per_market_pnl([o for o in rows if o.components[name] >= hi_cut], fee_rate, slippage)
        bottom = _per_market_pnl([o for o in rows if o.components[name] <= lo_cut], fee_rate, slippage)
        if len(top) < min_samples or len(bottom) < min_samples or hi_cut == lo_cut:
            out.append(FeatureScore(name, "quality", len({o.ticker for o in rows}), None, None, 0, "insufficient"))
            continue
        lift = (mean(top) - mean(bottom)) * 100
        z = diff_z(top, bottom)
        sig = z is not None and abs(z) >= 2
        out.append(FeatureScore(name, "quality", len({o.ticker for o in rows}), lift, z,
                                _stars_lift(lift) if sig and lift > 0 else 0,
                                ("ok" if lift > 0 else "inverse") if sig else "not_significant"))
    return sorted(out, key=lambda s: (-s.stars, s.name))


def _per_market_pnl(rows: list[Observation], fee_rate: float, slippage: float) -> list[float]:
    by_market: dict[str, list[float]] = defaultdict(list)
    for o in rows:
        p = o.entry_price
        assert p is not None
        paid = p + cost_cents(p, fee_rate, slippage)
        by_market[o.ticker].append(((100 if o.won else 0) - paid) / 100)
    return [mean(v) for v in by_market.values()]
