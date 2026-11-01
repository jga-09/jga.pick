"""Load cached real history for the model comparison (no network, nothing synthesised).

Crypto: ``data/replay_cache`` from scripts/replay.py (Kalshi v2 records + Coinbase spot_{ASSET}_{day}.json.gz).
Gold:   ``data/gold_cache`` from scripts/gold_check.py --days N (Kalshi records + pyth_xau_*.json.gz closes).
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.research.ta import Bar
from app.utils.time import parse_ts


def load_crypto_bars(cache: Path, asset: str) -> list[Bar]:
    by_end: dict[float, Bar] = {}
    for f in sorted(cache.glob(f"spot_{asset}_*.json.gz")):
        try:
            rows = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        for r in rows:  # Coinbase [start, low, high, open, close, volume]
            if isinstance(r, list) and len(r) >= 6:
                end = float(r[0]) + 60
                by_end[end] = Bar(end, float(r[2]), float(r[1]), float(r[4]), float(r[5]))
    return [by_end[k] for k in sorted(by_end)]


def load_gold_bars(cache: Path) -> list[Bar]:
    by_end: dict[float, Bar] = {}
    for f in sorted(cache.glob("pyth_xau_*.json.gz")):
        try:
            rows = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        for t, c in rows:  # [bar END ts, close]; Pyth gives no volume
            by_end[float(t)] = Bar(float(t), float(c), float(c), float(c), None)
    return [by_end[k] for k in sorted(by_end)]


def load_markets(cache: Path, prefix: str) -> list[dict[str, Any]]:
    out = []
    for f in sorted(cache.glob(f"{prefix}*.json.gz")):
        try:
            rec = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        if rec.get("v") != 2:
            continue
        m = rec.get("market") or {}
        close = parse_ts(m.get("close_time"))
        if close is None or m.get("cap_strike") is not None:
            continue
        out.append({"ticker": m.get("ticker"), "close": close, "strike": m.get("floor_strike"),
                    "result": m.get("result"),
                    "candles": [(datetime.fromtimestamp(c[0], UTC), c[1], c[2], c[4] if len(c) > 4 else None)
                                for c in rec.get("candles") or []]})
    return out
