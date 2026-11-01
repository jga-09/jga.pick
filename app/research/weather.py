"""Kalshi daily-high-temperature markets (KXHIGH*): honest, pre-registered forecast-edge test.

Idea: Kalshi settles each city's daily high on the NWS climate report (CLI) for ONE station. Weather
models forecast a grid box, not that thermometer, and their errors at a given station are partly
systematic. Correct the multi-model forecast by its recent error AT THAT STATION and compare the
resulting probability for each temperature bracket with the Kalshi price.

Rules (fixed before any result is seen; not tuned):
* forecasts: Open-Meteo "previous runs" archive - the forecast exactly as it was issued 1 or 2 days
  earlier (no re-analysis, no hindsight). Daily max = max of the hourly 2 m temperature over the
  local-STANDARD-time day (the NWS climate day). Models: GFS, ECMWF IFS, ICON, GEM; >= 2 required.
* decision times (America/New_York):
    V1  day D 10:00 ET using the 1-day-old forecasts         (primary)
    V2  same as V1 but WITHOUT station bias correction         (one change vs V1)
    V3  day D-1 18:00 ET using the 2-day-old forecasts         (one change vs V1)
  Using older runs than were actually available is deliberately conservative (no look-ahead).
* station correction: residual = observed high - forecast, over the last 30 days with data that
  ended at least 2 days before D (so the observation was published before the decision).
  >= 10 days required. mu = forecast + mean residual; sigma = max(1.5 F, std of residuals).
  V2: mu = forecast; sigma = max(1.5 F, RMS of residuals).
* bracket probability: P(lo - 0.5 <= T < hi + 0.5) under Normal(mu, sigma) (CLI highs are whole degrees).
* trade: at the decision time, take the latest hourly Kalshi quote (<= 3 h old). Buy YES at ask, or NO
  at 100 - bid, + 1c slippage + Kalshi taker fee, when expected value >= 5c per contract and the
  price is 3..97c. Every qualifying bracket counts (one contract each); held to settlement.
* evaluation: development dates, then the LAST 21 DATES as an untouched holdout (looked at once).
  Brackets of one event are correlated, so the 95% interval resamples whole EVENTS. A variant is
  SUPPORTED only with >= 30 development trades, an interval above 0, and a profitable holdout.
Settlement uses Kalshi's own recorded result for every market.
"""

from __future__ import annotations

import gzip
import json
import math
import random
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.research.btc_backtest import SimCosts, fee_per_contract

ET = ZoneInfo("America/New_York")
MIN_N = 30
MIN_EV = 5.0
MIN_HISTORY = 10
HISTORY_DAYS = 30
MIN_SIGMA = 1.5
MAX_QUOTE_AGE = timedelta(hours=3)
HOLDOUT_DATES = 21
MODELS = ("gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_seamless")


@dataclass(frozen=True)
class City:
    key: str
    name: str
    series: tuple[str, ...]  # current name first, legacy names after
    station: str  # NWS climate-report station Kalshi settles on
    lat: float
    lon: float
    std_tz: str  # local STANDARD time (the NWS climate day ignores daylight saving)


CITIES: tuple[City, ...] = (
    City("ny", "New York (Central Park)", ("KXHIGHNY", "HIGHNY"), "KNYC", 40.7794, -73.9692, "Etc/GMT+5"),
    City("chi", "Chicago (Midway)", ("KXHIGHCHI", "HIGHCHI"), "KMDW", 41.7868, -87.7522, "Etc/GMT+6"),
    City("mia", "Miami", ("KXHIGHMIA", "HIGHMIA"), "KMIA", 25.7881, -80.3169, "Etc/GMT+5"),
    City("aus", "Austin", ("KXHIGHAUS", "HIGHAUS"), "KAUS", 30.1831, -97.6799, "Etc/GMT+6"),
    City("den", "Denver", ("KXHIGHDEN", "HIGHDEN"), "KDEN", 39.8466, -104.6562, "Etc/GMT+7"),
    City("lax", "Los Angeles (LAX)", ("KXHIGHLAX", "HIGHLAX"), "KLAX", 33.9382, -118.3866, "Etc/GMT+8"),
    City("phl", "Philadelphia", ("KXHIGHPHIL", "HIGHPHIL"), "KPHL", 39.8733, -75.2268, "Etc/GMT+5"),
)
CITY_BY_KEY = {c.key: c for c in CITIES}

VARIANTS = (("V1", "day D 10:00 ET, 1-day-old forecasts, station-corrected (primary)", 1, True),
            ("V2", "V1 without station bias correction", 1, False),
            ("V3", "evening before (D-1 18:00 ET), 2-day-old forecasts, station-corrected", 2, True))

_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
                                       "OCT", "NOV", "DEC"), 1)}


# ------------------------------------------------------------------ contract parsing
def event_date(ticker: str) -> date | None:
    """'KXHIGHNY-26OCT20' or 'KXHIGHNY-26OCT20-B67.5' -> 2026-10-20."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)", ticker or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def _num(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def bracket(market: dict[str, Any]) -> tuple[int | None, int | None] | None:
    """Whole-degree range [lo, hi] (inclusive; None = open) that makes this market resolve YES.

    The YES subtitle ('68° to 69°', '70° or above', '61° or below') is used first; the strike fields
    are the fallback ('between' floor..cap, 'greater' > floor, 'less' < cap)."""
    for key in ("yes_sub_title", "subtitle"):
        s = (market.get(key) or "").replace("°", "").replace("º", "")
        if m := re.search(r"(-?\d+)\s*(?:to|-)\s*(-?\d+)", s):
            return int(m.group(1)), int(m.group(2))
        if m := re.search(r"(-?\d+)\s*(?:or above|or higher|or more|\+)", s, re.I):
            return int(m.group(1)), None
        if m := re.search(r"(-?\d+)\s*(?:or below|or lower|or less)", s, re.I):
            return None, int(m.group(1))
    kind = (market.get("strike_type") or "").lower()
    lo, hi = _num(market.get("floor_strike")), _num(market.get("cap_strike"))
    if kind == "between" and lo is not None and hi is not None:
        return math.ceil(lo), math.floor(hi)
    if kind == "greater" and lo is not None:
        return math.floor(lo) + 1, None
    if kind == "less" and hi is not None:
        return None, math.ceil(hi) - 1
    return None


def in_bracket(t: int, b: tuple[int | None, int | None]) -> bool:
    return (b[0] is None or t >= b[0]) and (b[1] is None or t <= b[1])


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bracket_prob(b: tuple[int | None, int | None], mu: float, sigma: float) -> float:
    lo = 0.0 if b[0] is None else _phi((b[0] - 0.5 - mu) / sigma)
    hi = 1.0 if b[1] is None else _phi((b[1] + 0.5 - mu) / sigma)
    return min(1.0, max(0.0, hi - lo))


def derive_high(markets: Iterable[tuple[tuple[int | None, int | None], str]]) -> int | None:
    """The only whole-degree high consistent with every settled bracket of an event, if unique."""
    cand = set(range(-40, 131))
    for b, result in markets:
        cand = {t for t in cand if in_bracket(t, b) == (result == "yes")}
    return next(iter(cand)) if len(cand) == 1 else None


# ------------------------------------------------------------------ external data parsing
def daily_max(times: Sequence[str], temps: Sequence[float | None], min_hours: int = 20) -> dict[str, float]:
    """Hourly local-time series -> {date: max} for days with >= min_hours valid values."""
    by_day: dict[str, list[float]] = defaultdict(list)
    for t, v in zip(times, temps, strict=False):
        if v is not None and isinstance(t, str) and len(t) >= 10:
            by_day[t[:10]].append(float(v))
    return {d: max(v) for d, v in by_day.items() if len(v) >= min_hours}


def parse_open_meteo(data: dict[str, Any]) -> dict[int, dict[str, float]]:
    """previous-runs response (one model) -> {lead: {date: daily max}}."""
    h = data.get("hourly") or {}
    times = h.get("time") or []
    out: dict[int, dict[str, float]] = {}
    for lead in (1, 2):
        key = next((k for k in h if k.startswith(f"temperature_2m_previous_day{lead}")), None)
        if key:
            out[lead] = daily_max(times, h[key])
    return out


def parse_iem_cli(data: dict[str, Any]) -> dict[str, int]:
    """Iowa Environmental Mesonet CLI JSON -> {date: observed high (F)}."""
    out = {}
    for r in data.get("results") or []:
        try:
            out[str(r["valid"])[:10]] = int(round(float(r["high"])))
        except (KeyError, TypeError, ValueError):
            continue  # 'M' = missing
    return out


# ------------------------------------------------------------------ dataset
@dataclass
class WMarket:
    ticker: str
    bracket: tuple[int | None, int | None]
    result: str
    quotes: list[tuple[float, float | None, float | None]]  # (unix end of hour, yes bid, yes ask)

    def quote(self, at: datetime) -> tuple[float | None, float | None]:
        t = at.timestamp()
        best = None
        for q in self.quotes:
            if q[0] <= t:
                best = q
            else:
                break
        if best is None or t - best[0] > MAX_QUOTE_AGE.total_seconds():
            return None, None
        return best[1], best[2]


@dataclass
class WEvent:
    city: str
    day: date
    markets: list[WMarket] = field(default_factory=list)


@dataclass
class WeatherData:
    events: dict[tuple[str, date], WEvent]
    forecasts: dict[str, dict[str, dict[str, dict[str, float]]]]  # city -> date -> lead -> model -> max
    obs: dict[str, dict[str, int]]  # city -> date -> high (IEM)
    derived: dict[str, dict[str, int]]  # city -> date -> high implied by Kalshi results

    def high(self, city: str, d: str) -> int | None:
        v = self.obs.get(city, {}).get(d)
        return v if v is not None else self.derived.get(city, {}).get(d)


def compact_candles(raw: list[dict[str, Any]]) -> list[list[float | None]]:
    from app.research.replay import parse_candles
    return [[c.ts.timestamp(), c.yes_bid, c.yes_ask] for c in parse_candles(raw)]


def load_weather(cache: Path) -> WeatherData:
    series_city = {s: c.key for c in CITIES for s in c.series}
    events: dict[tuple[str, date], WEvent] = {}
    for f in sorted((cache / "markets").glob("*.json.gz")):
        try:
            rec = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        m = rec.get("market") or {}
        city = rec.get("city") or series_city.get((m.get("ticker") or "").split("-")[0])
        d = event_date(m.get("event_ticker") or m.get("ticker") or "")
        b = bracket(m)
        if city is None or d is None or b is None or m.get("result") not in ("yes", "no"):
            continue
        quotes = sorted((float(q[0]), q[1], q[2]) for q in rec.get("candles") or [])
        events.setdefault((city, d), WEvent(city, d)).markets.append(WMarket(m["ticker"], b, m["result"], quotes))
    forecasts, obs = {}, {}
    for c in CITIES:
        fp, op = cache / f"forecast_{c.key}.json", cache / f"obs_{c.key}.json"
        forecasts[c.key] = json.loads(fp.read_text()) if fp.exists() else {}
        obs[c.key] = json.loads(op.read_text()) if op.exists() else {}
    derived: dict[str, dict[str, int]] = defaultdict(dict)
    for (city, d), ev in events.items():
        h = derive_high((m.bracket, m.result) for m in ev.markets)
        if h is not None:
            derived[city][d.isoformat()] = h
    return WeatherData(events, forecasts, obs, dict(derived))


# ------------------------------------------------------------------ model
def forecast_mean(wd: WeatherData, city: str, d: str, lead: int) -> float | None:
    by_model = (wd.forecasts.get(city, {}).get(d) or {}).get(str(lead)) or {}
    vals = [v for k, v in by_model.items() if k in MODELS and v is not None]
    return sum(vals) / len(vals) if len(vals) >= 2 else None


def distribution(wd: WeatherData, city: str, d: date, lead: int, corrected: bool) -> tuple[float, float] | None:
    """(mu, sigma) for the high on day d, using only observations published before the decision."""
    fc = forecast_mean(wd, city, d.isoformat(), lead)
    if fc is None:
        return None
    resid: list[float] = []
    k = 2
    while len(resid) < HISTORY_DAYS and k <= 2 * HISTORY_DAYS + 2:
        dd = (d - timedelta(days=k)).isoformat()
        k += 1
        f, o = forecast_mean(wd, city, dd, lead), wd.high(city, dd)
        if f is not None and o is not None:
            resid.append(o - f)
    if len(resid) < MIN_HISTORY:
        return None
    if corrected:
        return fc + statistics.fmean(resid), max(MIN_SIGMA, statistics.stdev(resid))
    return fc, max(MIN_SIGMA, math.sqrt(statistics.fmean(r * r for r in resid)))


def decision_time(d: date, lead: int) -> datetime:
    return datetime.combine(d, time(10), ET) if lead == 1 else datetime.combine(d - timedelta(days=1), time(18), ET)


# ------------------------------------------------------------------ simulation
@dataclass(frozen=True)
class WTrade:
    city: str
    day: date
    ticker: str
    side: str
    price: float  # cents paid before slippage/fee
    cost: float  # cents incl. slippage + fee
    p: float  # model probability for our side
    won: bool

    @property
    def net(self) -> float:  # dollars per contract
        return ((100.0 if self.won else 0.0) - self.cost) / 100


def event_trades(wd: WeatherData, ev: WEvent, lead: int, corrected: bool, costs: SimCosts) -> tuple[list[WTrade], list[tuple[float, float, int]]]:
    """Trades for one event + (model p, market mid, outcome) calibration points."""
    dist = distribution(wd, ev.city, ev.day, lead, corrected)
    if dist is None:
        return [], []
    at = decision_time(ev.day, lead)
    trades, cal = [], []
    for m in ev.markets:
        bid, ask = m.quote(at)
        if bid is None and ask is None:
            continue
        p = bracket_prob(m.bracket, *dist)
        yes_won = m.result == "yes"
        if bid is not None and ask is not None:
            cal.append((p, (bid + ask) / 200, int(yes_won)))
        best = None
        for side, price, ps in (("yes", ask, p), ("no", None if bid is None else 100 - bid, 1 - p)):
            if price is None or not 3 <= price <= 97:
                continue
            fill = min(99.0, price + costs.slippage_cents)
            cost = fill + fee_per_contract(fill, costs)
            ev_c = ps * 100 - cost
            if ev_c >= MIN_EV and (best is None or ev_c > best[0]):
                best = (ev_c, side, price, cost, ps)
        if best:
            _, side, price, cost, ps = best
            trades.append(WTrade(ev.city, ev.day, m.ticker, side, price, cost, ps, yes_won == (side == "yes")))
    return trades, cal


def _cluster_boot(trades: Sequence[WTrade], n: int = 2000, seed: int = 11) -> tuple[float, float] | None:
    groups: dict[tuple[str, date], list[float]] = defaultdict(list)
    for t in trades:
        groups[(t.city, t.day)].append(t.net)
    g = list(groups.values())
    if len(g) < 10:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        pick = rng.choices(g, k=len(g))
        tot, cnt = sum(sum(x) for x in pick), sum(len(x) for x in pick)
        means.append(100 * tot / cnt)
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def summarize(trades: Sequence[WTrade]) -> dict[str, Any]:
    n = len(trades)
    if not n:
        return {"trades": 0, "events": 0, "net_pnl": 0.0, "ev_cents": None, "ev_95": None}
    ts = sorted(trades, key=lambda t: t.day)
    eq = peak = dd = 0.0
    for t in ts:
        eq += t.net
        peak, dd = max(peak, eq), max(dd, peak - eq)
    return {"trades": n, "events": len({(t.city, t.day) for t in trades}),
            "win_rate": sum(t.won for t in trades) / n,
            "avg_cost": sum(t.cost for t in trades) / n,
            "avg_model_p": sum(t.p for t in trades) / n,
            "ev_cents": 100 * sum(t.net for t in trades) / n, "ev_95": _cluster_boot(trades),
            "net_pnl": round(sum(t.net for t in trades), 2), "max_drawdown": round(dd, 2),
            "yes_share": sum(t.side == "yes" for t in trades) / n}


def _brier(points: Sequence[tuple[float, float, int]]) -> dict[str, Any]:
    if not points:
        return {"n": 0}
    return {"n": len(points), "model": sum((p - o) ** 2 for p, _, o in points) / len(points),
            "market": sum((q - o) ** 2 for _, q, o in points) / len(points)}


def contract_check(wd: WeatherData) -> dict[str, Any]:
    """Does our reading of each bracket agree with Kalshi's result, given the official observed high?"""
    agree = total = 0
    for (city, d), ev in wd.events.items():
        o = wd.obs.get(city, {}).get(d.isoformat())
        if o is None:
            continue
        for m in ev.markets:
            total += 1
            agree += in_bracket(o, m.bracket) == (m.result == "yes")
    return {"markets": total, "agree": agree, "rate": agree / total if total else None}


def coverage(wd: WeatherData) -> dict[str, dict[str, int]]:
    out = {}
    for c in CITIES:
        evs = [ev for (city, _), ev in wd.events.items() if city == c.key]
        out[c.key] = {"events": len(evs), "markets": sum(len(e.markets) for e in evs),
                      "forecast_days": sum(1 for d in wd.forecasts.get(c.key, {}) if forecast_mean(wd, c.key, d, 1) is not None),
                      "obs_days": len(wd.obs.get(c.key, {})), "derived_days": len(wd.derived.get(c.key, {}))}
    return out


def run_weather(wd: WeatherData, holdout_dates: int = HOLDOUT_DATES, costs: SimCosts | None = None) -> dict[str, Any]:
    costs = costs or SimCosts()
    dates = sorted({d for _, d in wd.events})
    if len(dates) <= holdout_dates + 14:
        raise ValueError(f"need more than {holdout_dates + 14} days of weather markets (have {len(dates)})")
    hold_from = dates[-holdout_dates]
    rows = []
    cal_dev: list[tuple[float, float, int]] = []
    primary_dev: list[WTrade] = []
    for key, label, lead, corrected in VARIANTS:
        dev: list[WTrade] = []
        hold: list[WTrade] = []
        for ev in wd.events.values():
            tr, cal = event_trades(wd, ev, lead, corrected, costs)
            (hold if ev.day >= hold_from else dev).extend(tr)
            if key == "V1" and ev.day < hold_from:
                cal_dev.extend(cal)
        sd, sh = summarize(dev), summarize(hold)
        ok = sd["trades"] >= MIN_N and sd["ev_95"] is not None and sd["ev_95"][0] > 0 and sh["net_pnl"] > 0
        rows.append({"variant": key, "label": label, "dev": sd, "holdout": sh,
                     "decision": "SUPPORTED (paper-test next)" if ok else "NOT SUPPORTED"})
        if key == "V1":
            primary_dev = dev
    by_city = {}
    for c in CITIES:
        s = summarize([t for t in primary_dev if t.city == c.key])
        if s["trades"]:
            by_city[c.key] = s
    sup = [r["variant"] for r in rows if r["decision"].startswith("SUPPORTED")]
    verdict = (f"SUPPORTED: {', '.join(sup)} showed positive net EV on development AND the untouched holdout. "
               "Next step: paper trading only; real money stays off." if sup else
               "NOT SUPPORTED: no variant shows positive net EV after Kalshi costs with 30+ trades, an interval "
               "above zero and a profitable holdout. Retain the baseline (no trading).")
    return {"label": "KALSHI DAILY-HIGH WEATHER TEST (pre-registered; forecasts as issued; real Kalshi quotes)",
            "data": {"events": len(wd.events), "dev": [dates[0].isoformat(), (hold_from - timedelta(days=1)).isoformat()],
                     "holdout": [hold_from.isoformat(), dates[-1].isoformat()], "coverage": coverage(wd),
                     "contract_check": contract_check(wd)},
            "calibration": _brier(cal_dev), "rows": rows, "by_city": by_city, "verdict": verdict}


def format_weather(r: dict[str, Any]) -> str:
    d = r["data"]
    L = [r["label"], f"{d['events']} city-days. Development {d['dev'][0]}..{d['dev'][1]}; untouched holdout "
         f"{d['holdout'][0]}..{d['holdout'][1]}. 1 contract per bracket.", "", "Data coverage:"]
    for k, c in d["coverage"].items():
        L.append(f"  {k:<4} {c['events']:>4} days / {c['markets']:>5} brackets · forecasts {c['forecast_days']} days · "
                 f"official highs {c['obs_days']} (+{c['derived_days']} implied by Kalshi results)")
    cc = d["contract_check"]
    if cc["rate"] is not None:
        warn = "" if cc["rate"] >= 0.97 else "  <-- WARNING: bracket reading disagrees with Kalshi; results unreliable"
        L.append(f"Bracket reading vs Kalshi results: {cc['agree']}/{cc['markets']} agree ({cc['rate'] * 100:.1f}%){warn}")
    cal = r["calibration"]
    if cal.get("n"):
        better = "model better" if cal["model"] < cal["market"] else "Kalshi price better"
        L.append(f"Accuracy on {cal['n']} development brackets (Brier, lower = better): model {cal['model']:.4f} "
                 f"vs Kalshi price {cal['market']:.4f} -> {better}")
    L.append("")
    for x in r["rows"]:
        s, h = x["dev"], x["holdout"]
        L.append(f"{x['variant']} {x['label']}")
        if not s["trades"]:
            L.append(f"   dev 0 trades | holdout {h['trades']} trades net {h['net_pnl']:+.2f}$  -> {x['decision']}")
            continue
        ci = f" (95% {s['ev_95'][0]:+.1f}..{s['ev_95'][1]:+.1f})" if s["ev_95"] else ""
        L.append(f"   dev {s['trades']} trades on {s['events']} days · win {s['win_rate'] * 100:.1f}% vs model "
                 f"{s['avg_model_p'] * 100:.1f}% · avg cost {s['avg_cost']:.1f}c · EV {s['ev_cents']:+.2f}c{ci} · "
                 f"net {s['net_pnl']:+.2f}$ · maxDD {s['max_drawdown']:.2f}$")
        L.append(f"   holdout {h['trades']} trades net {h['net_pnl']:+.2f}$"
                 + (f" (EV {h['ev_cents']:+.2f}c)" if h["trades"] else "") + f"  -> {x['decision']}")
    if r["by_city"]:
        L += ["", "V1 by city (development; context only, 30+ trades needed to mean anything):"]
        for k, s in r["by_city"].items():
            flag = "" if s["trades"] >= MIN_N else " [insufficient sample]"
            L.append(f"  {k:<4} {s['trades']:>4} trades EV {s['ev_cents']:+.2f}c net {s['net_pnl']:+.2f}${flag}")
    L += ["", "VERDICT: " + r["verdict"], "Probabilities are model ESTIMATES, not guarantees."]
    return "\n".join(L)
