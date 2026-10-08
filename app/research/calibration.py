"""Historical model: calibration, similar-setup statistics, edge and EV.

Design choices that keep this honest:
* Each *market* counts once. Observations from the same 15-minute market share
  one outcome, so they are weighted 1/k within a group and sample sizes are
  reported in distinct markets.
* Edge is measured against the price actually paid (win - price), and the
  estimate is shrunk towards zero edge (the market is assumed efficient) with a
  prior of ``prior_strength`` markets. With little data, EV ~= -costs, so the
  model cannot trade on noise.
* A condition is only declared "historically poor" when the *upper* bound of
  its win-rate interval is still below its break-even price.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from app.research.dataset import Observation
from app.research.stats import stdev, wilson
from app.strategy.quality import price_bucket

CONF_BUCKETS = ((50, 60), (60, 70), (70, 80), (80, 90), (90, 101))
QUALITY_BUCKETS = ((0, 60), (60, 70), (70, 80), (80, 90), (90, 101))


def cost_cents(price: float, fee_rate: float, slippage: float) -> float:
    p = price / 100
    return fee_rate * p * (1 - p) * 100 + slippage


@dataclass(frozen=True)
class GroupStat:
    key: str
    n_obs: int
    n_markets: int
    win_rate: float
    avg_price: float  # cents
    edge: float  # mean(win - price/100), market-weighted
    edge_se: float
    wr_low: float
    wr_high: float
    avg_cost: float  # cents per contract (fees + slippage)

    @property
    def breakeven(self) -> float:
        return (self.avg_price + self.avg_cost) / 100

    @property
    def ev_cents(self) -> float:
        """Raw (unshrunk) expected value per contract after costs."""
        return self.edge * 100 - self.avg_cost


def group_stat(key: str, obs: list[Observation], fee_rate: float, slippage: float) -> GroupStat | None:
    by_market: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        if o.won is not None and o.entry_price is not None:
            by_market[o.ticker].append(o)
    if not by_market:
        return None
    m_win, m_price, m_edge, m_cost = [], [], [], []
    for rows in by_market.values():
        k = len(rows)
        m_win.append(sum(1.0 for r in rows if r.won) / k)
        m_price.append(sum(r.entry_price for r in rows) / k)  # type: ignore[misc]
        m_edge.append(sum((1.0 if r.won else 0.0) - r.entry_price / 100 for r in rows) / k)  # type: ignore[operator]
        m_cost.append(sum(cost_cents(r.entry_price, fee_rate, slippage) for r in rows) / k)  # type: ignore[arg-type]
    n = len(by_market)
    wins = sum(m_win)
    lo, hi = wilson(wins, n, z=1.28)  # 80% interval
    return GroupStat(
        key=key, n_obs=sum(len(v) for v in by_market.values()), n_markets=n, win_rate=wins / n,
        avg_price=sum(m_price) / n, edge=sum(m_edge) / n, edge_se=(stdev(m_edge) / n ** 0.5) if n > 1 else 1.0,
        wr_low=lo, wr_high=hi, avg_cost=sum(m_cost) / n,
    )


@dataclass(frozen=True)
class HistEstimate:
    level: str  # which similar-setup group was used, or "none"
    n_markets: int
    n_obs: int
    win_rate: float | None
    raw_edge: float | None  # fraction
    edge: float | None  # shrunk, fraction
    p_model: float | None
    ev_cents: float | None  # after costs, per contract
    ev_low_cents: float | None
    sufficient: bool
    poor: tuple[str, ...] = ()

    @property
    def ev_dollars(self) -> float | None:
        return None if self.ev_cents is None else self.ev_cents / 100


@dataclass
class HistoricalModel:
    observations: list[Observation]
    min_samples: int = 30
    prior_strength: float = 20.0
    fee_rate: float = 0.07
    slippage: float = 1.0
    groups: dict[tuple, GroupStat] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.obs = [o for o in self.observations if o.won is not None and o.entry_price is not None]
        self._build()

    # ------------------------------------------------------------- building
    def _add(self, key: tuple, members: list[Observation]) -> None:
        st = group_stat("/".join(map(str, key)), members, self.fee_rate, self.slippage)
        if st:
            self.groups[key] = st

    def _group_by(self, name: str, fn: Callable[[Observation], tuple]) -> None:
        buckets: dict[tuple, list[Observation]] = defaultdict(list)
        for o in self.obs:
            k = fn(o)
            if k is not None:
                buckets[(name, *k)].append(o)
        for k, members in buckets.items():
            self._add(k, members)

    def _build(self) -> None:
        self._add(("all",), self.obs)
        self._group_by("grade", lambda o: (o.grade,))
        self._group_by("grade_regime", lambda o: (o.grade, o.regime))
        self._group_by("grade_regime_time", lambda o: (o.grade, o.regime, o.time_bucket))
        self._group_by("regime", lambda o: (o.regime,))
        self._group_by("regime_dir", lambda o: (o.regime, o.direction))
        self._group_by("time", lambda o: (o.time_bucket,))
        self._group_by("price", lambda o: (o.side, o.price_bucket))
        self._group_by("conf", lambda o: (_bucket(o.confidence, CONF_BUCKETS),))
        self._group_by("quality", lambda o: (_bucket(o.quality, QUALITY_BUCKETS),))
        self._group_by("accel", lambda o: (o.accel_state,))
        self._group_by("underlying", lambda o: (o.underlying_state,))

    # -------------------------------------------------------------- queries
    def stat(self, *key: str) -> GroupStat | None:
        return self.groups.get(tuple(key))

    def sufficient(self, st: GroupStat | None) -> bool:
        return st is not None and st.n_markets >= self.min_samples

    @property
    def n_markets(self) -> int:
        st = self.stat("all")
        return st.n_markets if st else 0

    def estimate(self, grade: str, regime: str, tbucket: str, side: str, price: float,
                 time_bucket_label: str | None = None) -> HistEstimate:
        poor = self.poor_conditions(regime, tbucket, side, price, grade)
        for level, key in (("grade+regime+time", ("grade_regime_time", grade, regime, tbucket)),
                           ("grade+regime", ("grade_regime", grade, regime)),
                           ("grade", ("grade", grade))):
            st = self.groups.get(key)
            if self.sufficient(st):
                assert st is not None
                shrink = st.n_markets / (st.n_markets + self.prior_strength)
                edge = st.edge * shrink
                costs = cost_cents(price, self.fee_rate, self.slippage)
                ev = edge * 100 - costs
                ev_low = (edge - 1.28 * st.edge_se * shrink) * 100 - costs
                return HistEstimate(level, st.n_markets, st.n_obs, st.win_rate, st.edge, edge,
                                    min(0.99, max(0.01, price / 100 + edge)), ev, ev_low, True, poor)
        any_st = self.groups.get(("grade", grade))
        return HistEstimate("none", any_st.n_markets if any_st else 0, any_st.n_obs if any_st else 0,
                            any_st.win_rate if any_st else None, None, None, None, None, None, False, poor)

    def poor_conditions(self, regime: str, tbucket: str, side: str, price: float, grade: str) -> tuple[str, ...]:
        out = []
        for label, key in ((f"regime {regime}", ("regime", regime)), (f"time {tbucket} min", ("time", tbucket)),
                           (f"{side.upper()} {price_bucket(price)}¢", ("price", side, price_bucket(price))),
                           (f"grade {grade}", ("grade", grade))):
            st = self.groups.get(key)
            if self.sufficient(st) and st.wr_high < st.breakeven:  # type: ignore[union-attr]
                out.append(f"Historically poor: {label} (WR {st.win_rate:.0%}, n={st.n_markets})")  # type: ignore[union-attr]
        return tuple(out)

    def table(self, name: str) -> list[GroupStat]:
        rows = [st for k, st in self.groups.items() if k[0] == name]
        return sorted(rows, key=lambda s: s.key)

    def best(self, name: str, worst: bool = False) -> GroupStat | None:
        rows = [st for st in self.table(name) if self.sufficient(st)]
        if not rows:
            return None
        return (min if worst else max)(rows, key=lambda s: s.ev_cents)


def _bucket(v: float, edges: Iterable[tuple[int, int]]) -> str:
    for lo, hi in edges:
        if lo <= v < hi:
            return f"{lo}-{min(hi, 100)}"
    return "other"
