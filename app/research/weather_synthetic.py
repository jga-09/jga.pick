"""Synthetic weather cache for sanity-checking the test harness (never used as evidence).

mode "efficient": Kalshi prices already know each station's bias -> no edge should be found.
mode "naive":     Kalshi prices ignore a +3 F station bias -> the corrected model should profit.
"""

from __future__ import annotations

import gzip
import json
import math
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.research.weather import CITIES, ET, MODELS, bracket_prob, in_bracket


def generate(cache: Path, days: int = 120, mode: str = "efficient", seed: int = 3, cities: int = 3) -> None:
    rng = random.Random(seed)
    (cache / "markets").mkdir(parents=True, exist_ok=True)
    start = date(2026, 5, 1)
    for c in CITIES[:cities]:
        bias = 3.0
        fc_store: dict[str, dict[str, dict[str, float]]] = {}
        obs: dict[str, int] = {}
        for i in range(days + 40):
            d = start + timedelta(days=i)
            base = 70 + 10 * math.sin(i / 20)
            truth = round(base + bias + rng.gauss(0, 2.0))
            obs[d.isoformat()] = truth
            fc_store[d.isoformat()] = {str(lead): {m: round(base + rng.gauss(0, 0.7), 2) for m in MODELS[:3]}
                                       for lead in (1, 2)}
            if i < 40:
                continue
            fair_mu = base + (bias if mode == "efficient" else 0.0)
            centre = round(fair_mu)
            brackets = [(None, centre - 5)] + [(lo, lo + 1) for lo in range(centre - 4, centre + 5, 2)] + [(centre + 6, None)]
            tag = f"{d:%y}{d.strftime('%b').upper()}{d:%d}"
            ev = f"{c.series[0]}-{tag}"
            for j, b in enumerate(brackets):
                p = bracket_prob(b, fair_mu, 2.1)
                ticks = []
                t0 = datetime.combine(d - timedelta(days=1), time(10), ET)
                for h in range(30):
                    mid = 100 * p + rng.gauss(0, 1.0)
                    bid, ask = round(mid - 1), round(mid + 1)
                    ticks.append([(t0 + timedelta(hours=h)).timestamp(),
                                  bid if bid >= 1 else None, ask if ask <= 99 else None])
                sub = (f"{b[0]}° to {b[1]}°" if b[0] is not None and b[1] is not None
                       else f"{b[0]}° or above" if b[0] is not None else f"{b[1]}° or below")
                m = {"ticker": f"{ev}-X{j}", "event_ticker": ev, "result": "yes" if in_bracket(truth, b) else "no",
                     "yes_sub_title": sub}
                rec = {"city": c.key, "series": c.series[0], "market": m, "candles": ticks}
                (cache / "markets" / f"{m['ticker']}.json.gz").write_bytes(gzip.compress(json.dumps(rec).encode()))
        (cache / f"forecast_{c.key}.json").write_text(json.dumps(fc_store))
        (cache / f"obs_{c.key}.json").write_text(json.dumps(obs))
