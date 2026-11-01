"""Technical indicators on 1-minute bars, computed causally (value at bar i uses bars <= i only).

All series are computed once per asset in O(n) and indexed by bar position. Bars carry their END time, so a
value at index i is known from ``bars[i].end`` onwards - nothing can leak from a later minute.
Volume-based indicators (VWAP, volume acceleration) are None when the feed has no volume (e.g. Pyth gold).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    end: float  # unix seconds at the END of the minute
    high: float
    low: float
    close: float
    volume: float | None = None


def ema(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    k = 2 / (n + 1)
    e: float | None = None
    for i, x in enumerate(xs):
        e = x if e is None else e + k * (x - e)
        out.append(e if i >= n - 1 else None)
    return out


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    """Wilder RSI (0-100)."""
    out: list[float | None] = [None] * len(closes)
    gain = loss = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, lo = max(d, 0.0), max(-d, 0.0)
        if i <= n:
            gain += g / n
            loss += lo / n
            if i < n:
                continue
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + lo) / n
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def macd_hist(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> list[float | None]:
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [None if a is None or b is None else a - b for a, b in zip(ef, es, strict=True)]
    out: list[float | None] = [None] * len(closes)
    k = 2 / (signal + 1)
    s: float | None = None
    seen = 0
    for i, v in enumerate(line):
        if v is None:
            continue
        s = v if s is None else s + k * (v - s)
        seen += 1
        if seen >= signal:
            out[i] = v - s
    return out


def rolling_mean_sd(xs: list[float], n: int) -> tuple[list[float | None], list[float | None]]:
    means: list[float | None] = [None] * len(xs)
    sds: list[float | None] = [None] * len(xs)
    s = s2 = 0.0
    for i, x in enumerate(xs):
        s += x
        s2 += x * x
        if i >= n:
            s -= xs[i - n]
            s2 -= xs[i - n] ** 2
        if i >= n - 1:
            m = s / n
            means[i] = m
            sds[i] = math.sqrt(max(0.0, s2 / n - m * m))
    return means, sds


def bollinger_pctb(closes: list[float], n: int = 20, k: float = 2.0) -> list[float | None]:
    m, sd = rolling_mean_sd(closes, n)
    out: list[float | None] = []
    for c, mi, si in zip(closes, m, sd, strict=True):
        out.append(None if mi is None or not si else (c - (mi - k * si)) / (2 * k * si))
    return out


def atr(bars: list[Bar], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    a: float | None = None
    for i, b in enumerate(bars):
        tr = b.high - b.low if i == 0 else max(b.high - b.low, abs(b.high - bars[i - 1].close),
                                                abs(b.low - bars[i - 1].close))
        if i < n:
            a = (a or 0.0) + tr / n
            if i == n - 1:
                out[i] = a
        else:
            a = (a * (n - 1) + tr) / n  # type: ignore[operator]
            out[i] = a
    return out


def log_returns(closes: list[float]) -> list[float]:
    return [0.0] + [math.log(closes[i] / closes[i - 1]) if closes[i - 1] > 0 and closes[i] > 0 else 0.0
                    for i in range(1, len(closes))]


def session_vwap_z(bars: list[Bar], session_sec: int = 86400) -> list[float | None]:
    """(close - VWAP) / volume-weighted sd, VWAP anchored at 00:00 UTC (TradingView 'Session' for crypto)."""
    out: list[float | None] = [None] * len(bars)
    day = None
    pv = pv2 = v = 0.0
    count = 0
    for i, b in enumerate(bars):
        if b.volume is None:
            return [None] * len(bars)
        d = int((b.end - 1) // session_sec)
        if d != day:
            day, pv, pv2, v, count = d, 0.0, 0.0, 0.0, 0
        tp = (b.high + b.low + b.close) / 3
        pv += tp * b.volume
        pv2 += tp * tp * b.volume
        v += b.volume
        count += 1
        if v > 0 and count >= 15:
            vw = pv / v
            sd = math.sqrt(max(0.0, pv2 / v - vw * vw))
            out[i] = (b.close - vw) / sd if sd > 0 else 0.0
    return out


def volume_accel(bars: list[Bar], short: int = 5, long: int = 60) -> list[float | None]:
    """Last-5-minute volume / average 5-minute volume over the last hour."""
    if any(b.volume is None for b in bars):
        return [None] * len(bars)
    vols = [float(b.volume or 0.0) for b in bars]
    out: list[float | None] = [None] * len(bars)
    s_short = s_long = 0.0
    for i, x in enumerate(vols):
        s_short += x
        s_long += x
        if i >= short:
            s_short -= vols[i - short]
        if i >= long:
            s_long -= vols[i - long]
        if i >= long - 1 and s_long > 0:
            out[i] = s_short / (s_long / long * short)
    return out


@dataclass
class Indicators:
    """Per-bar indicator columns for one asset."""

    bars: list[Bar]
    ends: list[float]
    ret: list[float]
    ema9: list[float | None]
    ema21: list[float | None]
    rsi14: list[float | None]
    macd: list[float | None]
    bb: list[float | None]
    atr14: list[float | None]
    vwap_z: list[float | None]
    vol_accel: list[float | None]
    rv60: list[float | None]  # per-minute realized sd of log returns over the last 60 minutes


def compute(bars: list[Bar]) -> Indicators:
    closes = [b.close for b in bars]
    rets = log_returns(closes)
    _, rv = rolling_mean_sd(rets, 60)
    return Indicators(bars, [b.end for b in bars], rets, ema(closes, 9), ema(closes, 21), rsi(closes),
                      macd_hist(closes), bollinger_pctb(closes), atr(bars), session_vwap_z(bars),
                      volume_accel(bars), rv)
