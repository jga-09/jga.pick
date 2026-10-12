"""SYNTHETIC BTC 15-minute markets in the replay-cache format - for testing the machinery only.

This is NOT market evidence. It produces a random-walk BTC path, 15-minute
"above the opening price" contracts on it, and Kalshi-style quotes that are either

* ``efficient``: quotes = the true fair probability + noise (no edge after costs), or
* ``lagged``: quotes reflect BTC from ``lag_min`` minutes ago (a planted mispricing).

A correct backtest should find no out-of-sample edge on efficient data and should
find the planted edge on lagged data. Files are written with ``SYNTHETIC`` in the
market title and rules so they can never be mistaken for real history.
"""

from __future__ import annotations

import gzip
import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import NormalDist

_ND = NormalDist()


def generate(cache: Path, days: int = 8, start: datetime | None = None, seed: int = 7, vol_per_min: float = 0.0009,
             mode: str = "efficient", lag_min: int = 2, noise_cents: float | None = None, spread: float = 2.0) -> int:
    """Write ``days`` of synthetic data into ``cache``. Returns the number of markets.

    ``efficient`` quotes are the exact fair price (no noise) so no strategy should beat costs;
    ``lagged`` quotes add 1.5c of noise on top of the stale price."""
    if noise_cents is None:
        noise_cents = 0.0 if mode == "efficient" else 1.5
    rng = random.Random(seed)
    cache.mkdir(parents=True, exist_ok=True)
    start = start or datetime(2026, 9, 1, tzinfo=UTC)
    n = days * 1440 + 120
    t0 = start - timedelta(minutes=120)  # 2h warm-up so volatility exists at the first market
    closes = [60000.0]
    for _ in range(n):
        closes.append(closes[-1] * math.exp(rng.gauss(0, vol_per_min)))
    by_day: dict[str, list[list[float]]] = {}
    for i in range(n):
        ts = t0 + timedelta(minutes=i)  # period start
        o, c = closes[i], closes[i + 1]
        row = [int(ts.timestamp()), min(o, c) * 0.9999, max(o, c) * 1.0001, o, c, round(rng.uniform(5, 30), 3)]
        by_day.setdefault(ts.strftime("%Y%m%d"), []).append(row)
    for day, rows in by_day.items():
        (cache / f"spot_BTC_{day}.json.gz").write_bytes(gzip.compress(json.dumps(rows).encode()))

    def close_at(ts: datetime) -> float:  # close of the bar ending at ts
        return closes[int((ts - t0).total_seconds() // 60)]

    sigma_s = vol_per_min / math.sqrt(60)
    markets = 0
    for m in range(days * 96):
        open_t = start + timedelta(minutes=15 * m)
        close_t = open_t + timedelta(minutes=15)
        strike = round(close_at(open_t), 2)
        final = sum(close_at(close_t - timedelta(minutes=k)) for k in range(0, 2)) / 2  # ~last-minute average
        candles = []
        for k in range(1, 16):
            ts = open_t + timedelta(minutes=k)
            t_left = (close_t - ts).total_seconds()
            ref = close_at(ts - timedelta(minutes=lag_min)) if mode == "lagged" else close_at(ts)
            horizon = max(t_left - 60, 0) + min(t_left, 60) / 3 if t_left > 0 else 1
            p = _ND.cdf(math.log(ref / strike) / (sigma_s * math.sqrt(max(horizon, 1))))
            mid = min(97.0, max(3.0, 100 * p + rng.gauss(0, noise_cents)))
            bid, ask = round(mid - spread / 2), round(mid + spread / 2)
            candles.append([int(ts.timestamp()), float(max(1, bid)), float(min(99, ask)), mid, 10.0])
        ticker = f"KXBTC15M-SYN{close_t:%y%m%d%H%M}"
        rec = {"v": 2, "series": "KXBTC15M", "trades": [], "candles": candles, "market": {
            "ticker": ticker, "event_ticker": ticker, "title": "SYNTHETIC Bitcoin above open in 15 min?",
            "open_time": open_t.isoformat(), "close_time": close_t.isoformat(), "status": "finalized",
            "result": "yes" if final > strike else "no", "floor_strike": strike, "strike_type": "greater",
            "rules_primary": ("SYNTHETIC TEST DATA. If the simple average of the sixty seconds of CF Benchmarks' "
                              f"Bitcoin Real-Time Index (BRTI) before close is above {strike:.2f}, "
                              "the market resolves to Yes."),
        }}
        (cache / f"{ticker}.json.gz").write_bytes(gzip.compress(json.dumps(rec).encode()))
        markets += 1
    return markets
