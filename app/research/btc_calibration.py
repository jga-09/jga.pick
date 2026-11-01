"""Calibration of the BTC probability model, measured on settled markets.

Every BTC evaluation is stored once per market-minute (``observations`` with
``source="btc"``) together with the model probability and, after settlement,
the real result. Because the model has no fitted parameters, each stored
prediction is out-of-sample.

Rules that keep this honest:
* Minutes of the same market share one outcome, so each prediction is scored on
  its own but weighted 1/k within its market (each MARKET counts once in total),
  and sample sizes are reported in markets.
* "Calibrated" requires enough markets, an expected calibration error (ECE) below
  tolerance AND a Brier score no worse than Kalshi's own mid price. If the market
  is more accurate than the model, apparent "edges" are mostly model error.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from app.strategy.btc_decision import CalibrationState

SIDE_BUCKETS = ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))


@dataclass(frozen=True)
class Bucket:
    label: str
    markets: int
    predicted: float | None  # mean predicted probability of the favoured side
    actual: float | None  # share of those markets the favoured side won


@dataclass(frozen=True)
class CalibrationReport:
    state: CalibrationState
    buckets: tuple[Bucket, ...]
    markets: int
    minutes: int


def _weighted_predictions(observations: Sequence[object]) -> list[tuple[str, float, float | None, int, float]]:
    """(ticker, model P(YES), Kalshi mid or None, outcome 1/0, weight) per prediction.

    Each prediction is scored against its market's real outcome, and a market with k
    stored minutes gives each of them weight 1/k, so every market counts once in total."""
    groups: dict[str, list[object]] = defaultdict(list)
    for o in observations:
        if getattr(o, "features", {}).get("p_yes") is not None and getattr(o, "outcome", None) in ("yes", "no"):
            groups[o.ticker].append(o)  # type: ignore[attr-defined]
    out = []
    for ticker, rows in groups.items():
        w = 1.0 / len(rows)
        for r in rows:
            f = r.features  # type: ignore[attr-defined]
            mid = f["yes_mid"] / 100 if f.get("yes_mid") is not None else None
            out.append((ticker, f["p_yes"], mid, 1 if r.outcome == "yes" else 0, w))  # type: ignore[attr-defined]
    return out


def calibration_report(observations: Sequence[object], min_markets: int = 30,
                       ece_tol: float = 0.05) -> CalibrationReport:
    preds = _weighted_predictions(observations)
    n = len({t for t, *_ in preds})
    total_w = sum(w for *_, w in preds)
    buckets = []
    ece = 0.0
    for lo, hi in SIDE_BUCKETS:
        sel = []
        for ticker, p, _, y, w in preds:
            side_p, won = (p, y) if p >= 0.5 else (1 - p, 1 - y)
            if lo <= side_p < hi:
                sel.append((ticker, side_p, won, w))
        label = f"{int(lo * 100)}-{min(100, int(hi * 100))}%"
        if sel:
            sw = sum(w for *_, w in sel)
            pred = sum(sp * w for _, sp, _, w in sel) / sw
            act = sum(won * w for _, _, won, w in sel) / sw
            ece += sw / total_w * abs(pred - act)
            buckets.append(Bucket(label, len({t for t, *_ in sel}), pred, act))
        else:
            buckets.append(Bucket(label, 0, None, None))
    brier_model = sum(w * (p - y) ** 2 for _, p, _, y, w in preds) / total_w if total_w else None
    with_mid = [(m, y, w) for _, _, m, y, w in preds if m is not None]
    wm = sum(w for *_, w in with_mid)
    brier_mkt = sum(w * (m - y) ** 2 for m, y, w in with_mid) / wm if wm else None
    if n < min_markets:
        status = "INSUFFICIENT"
    elif ece > ece_tol or (brier_mkt is not None and brier_model is not None and brier_model > brier_mkt):
        status = "NOT_CALIBRATED"
    else:
        status = "CALIBRATED"
    state = CalibrationState(status, n, round(ece, 4) if n else None, brier_model, brier_mkt)
    return CalibrationReport(state, tuple(buckets), n, len(preds))
