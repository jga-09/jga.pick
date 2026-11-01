"""Model comparison on real settled Kalshi markets, per asset, with honest walk-forward evaluation.

Rows are minutes of a market (decision window 2-12 minutes before close). Every feature at a minute uses only
data known at that minute (Kalshi 1-minute candle close; underlying bars that ENDED by then). Each market keeps
all its minutes together in one split, and every market counts once (rows weighted 1/k) in training and in
the probability metrics.

Models, all predicting P(YES), all trained per asset (one asset never informs another):
  MARKET  Kalshi's own mid price (the benchmark every model must beat)
  FAIR    log-normal fair value  Phi(ln(S/K) / (sigma*sqrt(T)))  - no fitting (the BTC engine's idea)
  HIST    historical-model style: market price + shrunk empirical edge by (time bucket, price bucket)
  LR      logistic regression (standardised features)
  RF      random forest
  HGB     scikit-learn histogram gradient boosting
  LGBM    LightGBM (if installed)
Optional calibration (Platt <1000 markets, isotonic >=1000) uses only the LAST 20% of each training window, in
time order, and only when that part holds >= ``min_cal_markets`` markets.

Evaluation: chronological walk-forward over the development days (train on every market that CLOSED before the
test day, refit every ``refit_days``), then ONE look at an untouched holdout (the last ``holdout_days`` days),
with the model trained on all development days. Trades: one per market, the first minute whose expected value
after the exact per-order Kalshi fee (rounded up to the cent) and slippage reaches ``min_ev_cents``, paying the
ask, spread <= ``max_spread``. Nothing is tuned on the holdout.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import NormalDist
from typing import Any

from app.research.stats import trade_metrics
from app.research.ta import Indicators
from app.trading.fees import estimate_fee

_ND = NormalDist()
FEATURES = ("k_mid", "k_spread", "k_mom1", "k_mom3", "k_mom5", "t_left", "k_vol", "dist_sigma", "fair_p",
            "ret1", "ret5", "ret15", "ema_gap", "rsi14", "macd", "bb_pctb", "atr", "vwap_z", "vol_accel", "rv60")
MODEL_NAMES = ("MARKET", "FAIR", "HIST", "LR", "RF", "HGB", "LGBM")


# ------------------------------------------------------------------ dataset
@dataclass(frozen=True)
class Row:
    ticker: str
    asset: str
    close: datetime
    ts: datetime
    t_left: float  # minutes
    yes_bid: float  # cents
    yes_ask: float
    y: int  # 1 = YES won
    x: dict[str, float | None]


def build_rows(asset: str, markets: Sequence[dict[str, Any]], ind: Indicators, *, t_min: float = 2.0,
               t_max: float = 12.0, max_age_sec: float = 120.0) -> tuple[list[Row], dict[str, int]]:
    """markets: dicts with ticker, close (datetime), strike, result, candles [(ts, bid, ask, volume)]."""
    rows: list[Row] = []
    skipped: dict[str, int] = defaultdict(int)
    for m in markets:
        if m.get("result") not in ("yes", "no") or not isinstance(m.get("strike"), (int, float)):
            skipped["no result or strike"] += 1
            continue
        candles = sorted(m["candles"], key=lambda c: c[0])
        mids: list[tuple[datetime, float]] = []
        for ts, bid, ask, vol in candles:
            if bid is None or ask is None or not 0 < bid < ask < 100:
                skipped["no two-sided quote"] += 1
                continue
            mid = (bid + ask) / 2
            mids.append((ts, mid))
            t_left = (m["close"] - ts).total_seconds() / 60
            if not t_min <= t_left <= t_max:
                continue
            i = bisect_right(ind.ends, ts.timestamp()) - 1
            if i < 60 or ts.timestamp() - ind.ends[i] > max_age_sec:
                skipped["no fresh underlying price"] += 1
                continue
            x = features(ind, i, mids, float(m["strike"]), t_left, bid, ask, vol)
            if x is None:
                skipped["indicator warm-up"] += 1
                continue
            rows.append(Row(m["ticker"], asset, m["close"], ts, t_left, bid, ask, int(m["result"] == "yes"), x))
    return rows, dict(skipped)


def _mom(mids: list[tuple[datetime, float]], k: int) -> float | None:
    return mids[-1][1] - mids[-1 - k][1] if len(mids) > k else None


def features(ind: Indicators, i: int, mids: list[tuple[datetime, float]], strike: float, t_left: float,
             bid: float, ask: float, vol: float | None) -> dict[str, float | None] | None:
    c = ind.bars[i].close
    rv = ind.rv60[i]
    if rv is None or rv <= 0 or c <= 0 or strike <= 0:
        return None
    dist = math.log(c / strike) / (rv * math.sqrt(max(t_left, 0.25)))
    e9, e21 = ind.ema9[i], ind.ema21[i]

    def ret(k: int) -> float:
        return math.log(c / ind.bars[i - k].close) * 1e4 if ind.bars[i - k].close > 0 else 0.0

    return {
        "k_mid": (bid + ask) / 200, "k_spread": ask - bid, "k_mom1": _mom(mids, 1), "k_mom3": _mom(mids, 3),
        "k_mom5": _mom(mids, 5), "t_left": t_left, "k_vol": math.log1p(vol) if vol is not None else None,
        "dist_sigma": max(-8.0, min(8.0, dist)), "fair_p": _ND.cdf(max(-8.0, min(8.0, dist))),
        "ret1": ret(1), "ret5": ret(5), "ret15": ret(15),
        "ema_gap": (e9 - e21) / c * 1e4 if e9 is not None and e21 is not None else None,
        "rsi14": ind.rsi14[i], "macd": ind.macd[i] / c * 1e4 if ind.macd[i] is not None else None,
        "bb_pctb": ind.bb[i], "atr": ind.atr14[i] / c * 1e4 if ind.atr14[i] is not None else None,
        "vwap_z": ind.vwap_z[i], "vol_accel": ind.vol_accel[i], "rv60": rv * 1e4,
    }


def usable_features(rows: Sequence[Row]) -> list[str]:
    """Features present in every row of this asset (e.g. gold has no volume -> no VWAP/volume features)."""
    return [f for f in FEATURES if rows and all(r.x.get(f) is not None for r in rows)]


def weights(rows: Sequence[Row]) -> list[float]:
    count: dict[str, int] = defaultdict(int)
    for r in rows:
        count[r.ticker] += 1
    return [1.0 / count[r.ticker] for r in rows]


# ------------------------------------------------------------------ models
class Model:
    name = "?"

    def fit(self, rows: Sequence[Row], feats: list[str]) -> Model:
        return self

    def predict(self, rows: Sequence[Row]) -> list[float]:
        raise NotImplementedError


class MarketModel(Model):
    name = "MARKET"

    def predict(self, rows):
        return [r.x["k_mid"] for r in rows]


class FairModel(Model):
    name = "FAIR"

    def predict(self, rows):
        return [r.x["fair_p"] for r in rows]


def _tb(t: float) -> str:
    return "2-5" if t < 5 else "5-8" if t < 8 else "8-12"


def _pb(p: float) -> int:
    return min(9, int(p * 10))


class HistModel(Model):
    """The existing historical-model idea: market price + empirical excess win rate of similar setups,
    shrunk towards zero with a prior of ``prior`` markets (each market counted once)."""

    name = "HIST"

    def __init__(self, prior: float = 20.0) -> None:
        self.prior = prior
        self.edge: dict[tuple, float] = {}

    def fit(self, rows, feats):
        acc: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for r in rows:
            acc[(_tb(r.t_left), _pb(r.x["k_mid"]))][r.ticker].append(r.y - r.x["k_mid"])
        for k, by_m in acc.items():
            per = [sum(v) / len(v) for v in by_m.values()]
            n = len(per)
            self.edge[k] = sum(per) / n * n / (n + self.prior)
        return self

    def predict(self, rows):
        return [min(0.999, max(0.001, r.x["k_mid"] + self.edge.get((_tb(r.t_left), _pb(r.x["k_mid"])), 0.0)))
                for r in rows]


class SklearnModel(Model):
    def __init__(self, name: str, factory: Callable[[], Any], scale: bool = False) -> None:
        self.name = name
        self.factory = factory
        self.scale = scale
        self.est: Any = None
        self.feats: list[str] = []

    def _x(self, rows):
        import numpy as np

        return np.array([[r.x[f] for f in self.feats] for r in rows], dtype=float)

    def fit(self, rows, feats):
        import numpy as np

        self.feats = feats
        est = self.factory()
        if self.scale:
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler

            est = make_pipeline(StandardScaler(), est)
        y = np.array([r.y for r in rows])
        w = np.array(weights(rows))
        if len(set(y.tolist())) < 2:
            self.est = float(y.mean()) if len(y) else 0.5
            return self
        kw = {f"{est.steps[-1][0]}__sample_weight": w} if self.scale else {"sample_weight": w}
        est.fit(self._x(rows), y, **kw)
        self.est = est
        return self

    def predict(self, rows):
        if not rows:
            return []
        if isinstance(self.est, float):
            return [self.est] * len(rows)
        return [float(p) for p in self.est.predict_proba(self._x(rows))[:, 1]]


def available_models(names: Sequence[str] = MODEL_NAMES, seed: int = 7) -> list[Callable[[], Model]]:
    out: list[Callable[[], Model]] = []
    have_sklearn = _has("sklearn")
    for n in names:
        if n == "MARKET":
            out.append(MarketModel)
        elif n == "FAIR":
            out.append(FairModel)
        elif n == "HIST":
            out.append(HistModel)
        elif n == "LR" and have_sklearn:
            from sklearn.linear_model import LogisticRegression

            out.append(lambda: SklearnModel("LR", lambda: LogisticRegression(C=0.5, max_iter=500), scale=True))
        elif n == "RF" and have_sklearn:
            from sklearn.ensemble import RandomForestClassifier

            out.append(lambda: SklearnModel("RF", lambda: RandomForestClassifier(
                n_estimators=150, max_depth=8, min_samples_leaf=60, n_jobs=1, random_state=seed)))
        elif n == "HGB" and have_sklearn:
            from sklearn.ensemble import HistGradientBoostingClassifier

            out.append(lambda: SklearnModel("HGB", lambda: HistGradientBoostingClassifier(
                max_depth=4, learning_rate=0.05, max_iter=200, min_samples_leaf=80, random_state=seed)))
        elif n == "LGBM" and _has("lightgbm"):
            from lightgbm import LGBMClassifier

            out.append(lambda: SklearnModel("LGBM", lambda: LGBMClassifier(
                n_estimators=250, learning_rate=0.03, num_leaves=15, min_child_samples=80, subsample=0.8,
                subsample_freq=1, colsample_bytree=0.8, random_state=seed, verbose=-1)))
    return out


def _has(mod: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(mod) is not None


# ------------------------------------------------------------- calibration
class Calibrated(Model):
    """Fit on the first 80% of the training markets (time order); calibrate on the last 20%."""

    def __init__(self, base: Model, min_cal_markets: int = 150) -> None:
        self.base = base
        self.name = base.name + "+cal"
        self.min_cal_markets = min_cal_markets
        self.cal: Callable[[float], float] | None = None
        self.method = "none"

    def fit(self, rows, feats):
        markets = sorted({r.ticker: r.close for r in rows}.items(), key=lambda kv: kv[1])
        cut = int(len(markets) * 0.8)
        late = {t for t, _ in markets[cut:]}
        if len(late) < self.min_cal_markets:
            self.base.fit(rows, feats)  # not enough data to calibrate: plain model, labelled as such
            return self
        fit_rows = [r for r in rows if r.ticker not in late]
        cal_rows = [r for r in rows if r.ticker in late]
        self.base.fit(fit_rows, feats)
        p = self.base.predict(cal_rows)
        y = [r.y for r in cal_rows]
        w = weights(cal_rows)
        if len(markets) >= 1000:
            self.cal, self.method = isotonic(p, y, w), "isotonic"
        else:
            self.cal, self.method = platt(p, y, w), "platt"
        return self

    def predict(self, rows):
        p = self.base.predict(rows)
        return [self.cal(x) for x in p] if self.cal else p


def platt(p: Sequence[float], y: Sequence[int], w: Sequence[float], iters: int = 200) -> Callable[[float], float]:
    """1-d logistic recalibration on logit(p), fitted by Newton's method."""
    def lg(x: float) -> float:
        x = min(1 - 1e-6, max(1e-6, x))
        return math.log(x / (1 - x))
    zs = [lg(x) for x in p]
    a, b = 1.0, 0.0
    for _ in range(iters):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for z, yi, wi in zip(zs, y, w, strict=True):
            q = 1 / (1 + math.exp(-(a * z + b)))
            d = (q - yi) * wi
            g0 += d * z
            g1 += d
            v = q * (1 - q) * wi
            h00 += v * z * z
            h01 += v * z
            h11 += v
        det = h00 * h11 - h01 * h01 + 1e-12
        da, db = (h11 * g0 - h01 * g1) / det, (h00 * g1 - h01 * g0) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-8:
            break
    return lambda x: 1 / (1 + math.exp(-(a * lg(x) + b)))


def isotonic(p: Sequence[float], y: Sequence[int], w: Sequence[float]) -> Callable[[float], float]:
    """Weighted pool-adjacent-violators; prediction = step function, clipped to [0.001, 0.999]."""
    pts = sorted(zip(p, y, w, strict=True))
    blocks: list[list[float]] = []  # [sum_wy, sum_w, x_max]
    for x, yi, wi in pts:
        blocks.append([yi * wi, wi, x])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            b = blocks.pop()
            blocks[-1][0] += b[0]
            blocks[-1][1] += b[1]
            blocks[-1][2] = b[2]
    xs = [b[2] for b in blocks]
    vs = [min(0.999, max(0.001, b[0] / b[1])) for b in blocks]

    def f(x: float) -> float:
        i = bisect_right(xs, x)
        return vs[min(i, len(vs) - 1)]
    return f


# --------------------------------------------------------------- evaluation
@dataclass
class Trade:
    ticker: str
    close: datetime
    side: str
    price: float
    fee_c: float
    p: float
    won: bool
    pnl: float  # dollars per contract
    t_left: float
    vol_regime: str


def fee_cents(price: float, n: int, rate: float) -> float:
    """Exact Kalshi fee per contract (cents) for an order of ``n`` contracts (rounded up per order)."""
    return estimate_fee(n, price, rate) * 100 / n


def pick_trades(rows: Sequence[Row], preds: Sequence[float], *, min_ev: float, fee_rate: float, slippage: float,
                order_size: int, max_spread: float, regime: Callable[[Row], str]) -> list[Trade]:
    by_m: dict[str, list[tuple[Row, float]]] = defaultdict(list)
    for r, p in zip(rows, preds, strict=True):
        by_m[r.ticker].append((r, p))
    out = []
    for items in by_m.values():
        for r, p in sorted(items, key=lambda it: it[0].ts):
            if r.yes_ask - r.yes_bid > max_spread:
                continue
            best = None
            for side, price, prob in (("yes", r.yes_ask, p), ("no", 100 - r.yes_bid, 1 - p)):
                if not 3 <= price <= 97:
                    continue
                fc = fee_cents(price, order_size, fee_rate)
                ev = prob * 100 - price - fc - slippage
                if best is None or ev > best[0]:
                    best = (ev, side, price, fc, prob)
            if best and best[0] >= min_ev:
                ev, side, price, fc, prob = best
                won = (r.y == 1) == (side == "yes")
                pnl = ((100 if won else 0) - price - fc - slippage) / 100
                out.append(Trade(r.ticker, r.close, side, price, fc, prob, won, pnl, r.t_left, regime(r)))
                break
    return sorted(out, key=lambda t: t.close)


def prob_metrics(rows: Sequence[Row], preds: Sequence[float]) -> dict[str, Any]:
    if not rows:
        return {"n_markets": 0}
    w = weights(rows)
    sw = sum(w)
    brier = sum(wi * (p - r.y) ** 2 for r, p, wi in zip(rows, preds, w, strict=True)) / sw
    ll = -sum(wi * (r.y * math.log(min(1 - 1e-4, max(1e-4, p))) + (1 - r.y) * math.log(min(1 - 1e-4, max(1e-4, 1 - p))))
              for r, p, wi in zip(rows, preds, w, strict=True)) / sw
    mkt = sum(wi * (r.x["k_mid"] - r.y) ** 2 for r, wi in zip(rows, w, strict=True)) / sw
    buckets: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for r, p, wi in zip(rows, preds, w, strict=True):
        b = buckets[min(9, int(p * 10))]
        b[0] += wi * p
        b[1] += wi * r.y
        b[2] += wi
    cal = [(f"{k * 10}-{k * 10 + 10}%", v[2], v[0] / v[2], v[1] / v[2]) for k, v in sorted(buckets.items())]
    ece = sum(v[2] / sw * abs(v[0] / v[2] - v[1] / v[2]) for v in buckets.values())
    return {"n_markets": len({r.ticker for r in rows}), "brier": brier, "logloss": ll, "brier_market": mkt,
            "ece": ece, "calibration": cal}


def bootstrap(pnls: Sequence[float], n: int = 2000, seed: int = 11) -> tuple[float, float] | None:
    if len(pnls) < 10:
        return None
    rng = random.Random(seed)
    ms = sorted(sum(rng.choices(pnls, k=len(pnls))) / len(pnls) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n) - 1]


def trade_report(trades: Sequence[Trade]) -> dict[str, Any]:
    pnls = [t.pnl for t in trades]
    m = trade_metrics(pnls, [t.price for t in trades], [t.t_left * 60 for t in trades])
    cost = sum((t.price + t.fee_c) / 100 for t in trades)
    out = {"trades": m.trades, "win_rate": m.win_rate, "wr_ci": (m.wr_low, m.wr_high), "pnl": m.pnl,
           "roi": m.pnl / cost if cost else None, "profit_factor": m.profit_factor, "max_dd": m.max_drawdown,
           "avg": m.ev, "ev_ci": bootstrap(pnls), "avg_price": m.avg_entry}
    for key, fn in (("by_time", lambda t: _tb(t.t_left)), ("by_price", lambda t: _price_band(t.price)),
                    ("by_regime", lambda t: t.vol_regime)):
        groups: dict[str, list[float]] = defaultdict(list)
        for t in trades:
            groups[fn(t)].append(t.pnl)
        out[key] = {k: (len(v), sum(v) / len(v)) for k, v in sorted(groups.items())}
    return out


def _price_band(p: float) -> str:
    return "<40" if p < 40 else "40-55" if p < 55 else "55-70" if p < 70 else "70+"


@dataclass
class AssetResult:
    asset: str
    n_markets: int
    days: tuple[str, str]
    holdout_from: str
    feats: list[str]
    dev: dict[str, dict[str, Any]] = field(default_factory=dict)  # model -> {"prob":..., "trades":...}
    holdout: dict[str, dict[str, Any]] = field(default_factory=dict)
    calibration_method: dict[str, str] = field(default_factory=dict)
    note: str = ""


def evaluate_asset(asset: str, rows: list[Row], *, holdout_days: int = 7, min_train_days: int = 5,
                   refit_days: int = 3, min_ev: float = 2.0, fee_rate: float = 0.07, slippage: float = 1.0,
                   order_size: int = 1, max_spread: float = 5.0, calibrate: bool = True,
                   model_names: Sequence[str] = MODEL_NAMES, min_cal_markets: int = 150) -> AssetResult:
    rows = sorted(rows, key=lambda r: (r.close, r.ts))
    days = sorted({r.close.strftime("%Y-%m-%d") for r in rows})
    feats = usable_features(rows)
    if len(days) < min_train_days + holdout_days + 1:
        return AssetResult(asset, len({r.ticker for r in rows}), (days[0] if days else "", days[-1] if days else ""),
                           "", feats, note=f"INSUFFICIENT DATA: {len(days)} days (need "
                                           f"{min_train_days + holdout_days + 1}+)")
    hold_from = days[-holdout_days]
    dev_days = days[:-holdout_days]
    hold_start = datetime.fromisoformat(hold_from).replace(tzinfo=UTC)
    dev_rows = [r for r in rows if r.close < hold_start]
    hold_rows = [r for r in rows if r.close >= hold_start]
    rv_sorted = sorted(r.x["rv60"] for r in dev_rows)
    lo_t, hi_t = rv_sorted[len(rv_sorted) // 3], rv_sorted[2 * len(rv_sorted) // 3]

    def regime(r: Row) -> str:
        v = r.x["rv60"]
        return "low vol" if v < lo_t else "high vol" if v > hi_t else "normal vol"

    factories = available_models(model_names)
    res = AssetResult(asset, len({r.ticker for r in rows}), (days[0], days[-1]), hold_from, feats)

    def make(f: Callable[[], Model]) -> Model:
        m = f()
        return Calibrated(m, min_cal_markets) if calibrate and m.name not in ("MARKET", "FAIR", "HIST") else m

    # walk-forward over development days
    dev_pred: dict[str, list[tuple[Row, float]]] = defaultdict(list)
    for start in range(min_train_days, len(dev_days), refit_days):
        test_days = dev_days[start:start + refit_days]
        t0 = datetime.fromisoformat(test_days[0]).replace(tzinfo=UTC)
        t1 = datetime.fromisoformat(test_days[-1]).replace(tzinfo=UTC) + timedelta(days=1)
        train = [r for r in dev_rows if r.close <= t0]  # every training label was known before the test day
        test = [r for r in dev_rows if t0 <= r.close < t1 and r.ts >= t0]
        if not train or not test:
            continue
        for f in factories:
            m = make(f).fit(train, feats)
            dev_pred[m.name].extend(zip(test, m.predict(test), strict=True))
    for name, pairs in dev_pred.items():
        rs, ps = [p[0] for p in pairs], [p[1] for p in pairs]
        res.dev[name] = {"prob": prob_metrics(rs, ps),
                         "trades": trade_report(pick_trades(rs, ps, min_ev=min_ev, fee_rate=fee_rate,
                                                            slippage=slippage, order_size=order_size,
                                                            max_spread=max_spread, regime=regime))}
    # one look at the untouched holdout
    for f in factories:
        m = make(f).fit(dev_rows, feats)
        ps = m.predict(hold_rows)
        res.holdout[m.name] = {"prob": prob_metrics(hold_rows, ps),
                               "trades": trade_report(pick_trades(hold_rows, ps, min_ev=min_ev, fee_rate=fee_rate,
                                                                  slippage=slippage, order_size=order_size,
                                                                  max_spread=max_spread, regime=regime))}
        if isinstance(m, Calibrated):
            res.calibration_method[m.name] = m.method
    return res


def verdict(dev: dict[str, Any] | None, hold: dict[str, Any] | None, min_trades: int = 30) -> str:
    if not dev or not hold:
        return "INSUFFICIENT DATA"
    d, h = dev["trades"], hold["trades"]
    if h["trades"] < min_trades or d["trades"] < min_trades:
        return f"INSUFFICIENT DATA ({d['trades']} dev / {h['trades']} holdout trades; need {min_trades}+ each)"
    ci = h["ev_ci"]
    if (d["avg"] or 0) > 0 and (h["avg"] or 0) > 0 and ci and ci[0] > 0:
        return "PASSES (positive on walk-forward AND holdout, holdout 95% range above zero) - paper-test only"
    if (d["avg"] or 0) > 0 and (h["avg"] or 0) > 0:
        return "INCONCLUSIVE (positive but the holdout range includes zero)"
    return "NO EDGE after fees"
