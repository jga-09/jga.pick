"""Synthetic 15-minute market simulator for testing the research machinery.

THIS IS NOT MARKET DATA. Two worlds are provided:

* ``efficient`` - the underlying is a driftless random walk and Kalshi quotes
  the exact fair probability (plus quote noise). There is NO exploitable edge;
  a correct system must find none and refuse to trade.
* ``edge`` - ~35% of windows carry a persistent drift the market does not price
  in, and Kalshi quotes lag the spot by ``lag_sec``. A real (known) edge exists;
  a correct system should detect it out-of-sample.

Results from either world say nothing about real Kalshi markets. They only
check that the pipeline (signals -> filters -> calibration -> walk-forward ->
risk) is honest: no look-ahead, no edge invented from noise.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.data.underlying import SpotPrice
from app.kalshi.market_data import MarketInfo, MarketSnapshot, OrderBook, TradePrint

STEP_SEC = 5
WINDOW_SEC = 900


@dataclass(frozen=True)
class World:
    name: str
    drift_prob: float  # share of windows with a persistent (unpriced) drift
    drift_strength: float  # expected drift over the window, in units of window sigma
    lag_sec: int  # Kalshi quote lag vs spot
    quote_noise: float = 0.7  # cents
    wide_spread_prob: float = 0.10


WORLDS = {
    "efficient": World("efficient", 0.0, 0.0, 0),
    "edge": World("edge", 0.35, 0.8, 15),
}


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass
class SimMarket:
    info: MarketInfo
    snapshots: list[MarketSnapshot]
    spot: list[SpotPrice]
    outcome: str  # yes | no


def simulate_market(idx: int, start: datetime, world: World, rng: random.Random, asset: str = "BTC") -> SimMarket:
    steps = WINDOW_SEC // STEP_SEC
    vol_kind = rng.choices(["calm", "normal", "volatile"], [0.3, 0.5, 0.2])[0]
    sigma = {"calm": 0.00012, "normal": 0.00019, "volatile": 0.0004}[vol_kind]  # per 5s step (log)
    drift = 0.0
    if rng.random() < world.drift_prob:
        drift = rng.choice([-1, 1]) * world.drift_strength * sigma * math.sqrt(steps) / steps
    s0 = 60000.0 * math.exp(rng.gauss(0, 0.01))
    path = [s0]
    for _ in range(steps):
        path.append(path[-1] * math.exp(drift + rng.gauss(0, sigma)))
    k = s0
    wide = rng.random() < world.wide_spread_prob
    info = MarketInfo(
        ticker=f"SIM{asset}15M-{idx:06d}", event_ticker=f"SIM{asset}15M-{idx:06d}", series_ticker=f"SIM{asset}15M",
        asset=asset, title="Synthetic market", subtitle="", open_time=start,
        close_time=start + timedelta(seconds=WINDOW_SEC), expiration_time=None, status="active", floor_strike=k,
    )
    lag_steps = world.lag_sec // STEP_SEC
    snaps, spots, trades_buf = [], [], []

    def fair(i: int) -> float:
        rem = max(1, steps - i)
        return _phi(math.log(path[i] / k) / (sigma * math.sqrt(rem)))

    for i in range(steps):
        ts = start + timedelta(seconds=i * STEP_SEC)
        spots.append(SpotPrice(asset, path[i], ts, "sim"))
        p = fair(max(0, i - lag_steps))
        mid = min(98.0, max(2.0, round(100 * p + rng.gauss(0, world.quote_noise))))
        spread = rng.choice([6, 8, 10, 12]) if wide else rng.choice([1, 2, 2, 3])
        yes_bid, yes_ask = max(1.0, mid - spread // 2), min(99.0, mid + (spread - spread // 2))
        # Order flow reacts to recent *public* fair-value change (momentum of priced info only).
        flow = (fair(i) - fair(max(0, i - 6))) * 8
        yd = 120 * math.exp(0.8 * math.tanh(flow) + rng.gauss(0, 0.5))
        nd = 120 * math.exp(-0.8 * math.tanh(flow) + rng.gauss(0, 0.5))
        book = OrderBook(yes_bids=((yes_bid, yd * 0.4), (yes_bid - 1, yd * 0.6)),
                         no_bids=((100 - yes_ask, nd * 0.4), (99 - yes_ask, nd * 0.6)))
        for _ in range(_poisson(rng, 0.5)):
            side = "yes" if rng.random() < 0.5 + 0.3 * math.tanh(flow) else "no"
            trades_buf.append(TradePrint(f"{info.ticker}-{i}-{rng.random():.6f}", mid, float(rng.randint(1, 30)),
                                         side, ts))
        trades_buf = [t for t in trades_buf if (ts - t.ts).total_seconds() <= 300]
        snaps.append(MarketSnapshot(
            info=info, ts=ts, yes_bid=yes_bid, yes_ask=yes_ask, no_bid=100 - yes_ask, no_ask=100 - yes_bid,
            last_price=mid, orderbook=book, recent_trades=tuple(trades_buf), source="sim",
        ))
    return SimMarket(info, snaps, spots, "yes" if path[-1] > k else "no")


def _poisson(rng: random.Random, lam: float) -> int:
    n, p, limit = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return n
        n += 1


def generate(world: str, n_markets: int, seed: int = 7,
             start: datetime | None = None) -> Iterator[SimMarket]:
    w = WORLDS[world]
    rng = random.Random(seed)
    t0 = start or datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(n_markets):
        yield simulate_market(i, t0 + timedelta(seconds=i * WINDOW_SEC), w, rng)
