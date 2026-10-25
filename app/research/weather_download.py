"""Download everything the weather test needs into data/weather_cache (resumable).

* Kalshi (official public API, no key needed): settled KXHIGH* markets + HOURLY candlesticks
  (YES bid/ask) from market open until the end of the event day.
* Open-Meteo "previous runs" archive: hourly 2 m temperature exactly as forecast 1 and 2 days
  earlier, per model, in the city's local standard time.
* Iowa Environmental Mesonet: the official NWS climate-report daily highs (the settlement source).
Nothing is invented: missing data stays missing and is reported.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.errors import KalshiError, KalshiHTTPError
from app.research.replay import HistoryDownloader
from app.research.weather import CITIES, ET, MODELS, City, compact_candles, event_date, parse_iem_cli, parse_open_meteo
from app.utils.time import parse_ts

log = logging.getLogger(__name__)

OPEN_METEO = "https://previous-runs-api.open-meteo.com/v1/forecast"
IEM_CLI = "https://mesonet.agron.iastate.edu/json/cli.py"
KEEP = ("ticker", "event_ticker", "result", "strike_type", "floor_strike", "cap_strike", "yes_sub_title",
        "subtitle", "title", "open_time", "close_time")


async def kalshi_markets(client: Any, cache: Path, cities: list[City], since: datetime, until: datetime,
                         say: Callable[[str], None]) -> dict[str, int]:
    mdir = cache / "markets"
    mdir.mkdir(parents=True, exist_ok=True)
    hd = HistoryDownloader(client, cache / "_unused")
    counts: dict[str, int] = {}
    for c in cities:
        markets, series = [], None
        for s in c.series:
            markets = await hd.settled_markets(s, since, until)
            if markets:
                series = s
                break
        if not series:
            say(f"  {c.key}: no settled markets found (tried {', '.join(c.series)})")
            counts[c.key] = 0
            continue
        new = failed = 0
        for i, m in enumerate(markets, 1):
            path = mdir / f"{m['ticker']}.json.gz"
            if path.exists():
                continue
            d = event_date(m.get("event_ticker") or m["ticker"])
            open_t, close_t = parse_ts(m.get("open_time")), parse_ts(m.get("close_time"))
            if d is None or open_t is None or close_t is None:
                continue
            end = min(close_t, datetime.combine(d + timedelta(days=1), time(0), ET))
            try:
                raw = await _hourly_candles(client, series, m["ticker"], int(open_t.timestamp()), int(end.timestamp()))
            except KalshiError as exc:
                failed += 1
                if failed <= 3:
                    say(f"  {m['ticker']}: {exc}")
                continue
            rec = {"city": c.key, "series": series, "market": {k: m.get(k) for k in KEEP},
                   "candles": compact_candles(raw)}
            path.write_bytes(gzip.compress(json.dumps(rec, separators=(",", ":")).encode()))
            new += 1
            if i % 100 == 0:
                say(f"  {c.key}: {i}/{len(markets)} markets")
        counts[c.key] = len(markets)
        say(f"  {c.key} ({series}): {len(markets)} settled markets, {new} downloaded now, {failed} failed")
    return counts


async def _hourly_candles(client: Any, series: str, ticker: str, start: int, end: int) -> list[dict[str, Any]]:
    params = {"start_ts": start, "end_ts": end, "period_interval": 60}
    try:
        data = await client.get(f"/series/{series}/markets/{ticker}/candlesticks", params)
        if data.get("candlesticks"):
            return data["candlesticks"]
    except KalshiHTTPError as exc:
        if exc.status_code not in (400, 404):
            raise
    data = await client.get(f"/historical/markets/{ticker}/candlesticks", params)
    return data.get("candlesticks") or []


async def forecasts(http: httpx.AsyncClient, cache: Path, cities: list[City], start: date, end: date,
                    say: Callable[[str], None]) -> None:
    for c in cities:
        path = cache / f"forecast_{c.key}.json"
        store: dict[str, dict[str, dict[str, float]]] = json.loads(path.read_text()) if path.exists() else {}
        for model in MODELS:
            got = 0
            a = start
            while a <= end:
                b = min(end, a + timedelta(days=30))
                params = {"latitude": c.lat, "longitude": c.lon, "models": model, "timezone": c.std_tz,
                          "hourly": "temperature_2m_previous_day1,temperature_2m_previous_day2",
                          "temperature_unit": "fahrenheit", "start_date": a.isoformat(), "end_date": b.isoformat()}
                try:
                    r = await http.get(OPEN_METEO, params=params)
                    r.raise_for_status()
                    for lead, days in parse_open_meteo(r.json()).items():
                        for d, v in days.items():
                            store.setdefault(d, {}).setdefault(str(lead), {})[model] = round(v, 2)
                            got += lead == 1
                except (httpx.HTTPError, ValueError) as exc:
                    say(f"  forecast {c.key}/{model} {a}..{b}: {type(exc).__name__} {str(exc)[:120]}")
                a = b + timedelta(days=1)
            say(f"  {c.key} {model}: {got} days of 1-day-old forecasts")
        path.write_text(json.dumps(store, separators=(",", ":"), sort_keys=True))


async def observations(http: httpx.AsyncClient, cache: Path, cities: list[City], start: date, end: date,
                       say: Callable[[str], None]) -> None:
    for c in cities:
        path = cache / f"obs_{c.key}.json"
        store: dict[str, int] = json.loads(path.read_text()) if path.exists() else {}
        for year in range(start.year, end.year + 1):
            try:
                r = await http.get(IEM_CLI, params={"station": c.station, "year": year})
                r.raise_for_status()
                store.update(parse_iem_cli(r.json()))
            except (httpx.HTTPError, ValueError) as exc:
                say(f"  observations {c.key} {year}: {type(exc).__name__} {str(exc)[:120]}")
        path.write_text(json.dumps(store, separators=(",", ":"), sort_keys=True))
        say(f"  {c.key} ({c.station}): {len(store)} official daily highs")


async def download_all(client: Any, cache: Path, days: int, city_keys: list[str] | None = None,
                       say: Callable[[str], None] = print) -> None:
    cities = [c for c in CITIES if not city_keys or c.key in city_keys]
    until = datetime.now(UTC)
    since = until - timedelta(days=days)
    cache.mkdir(parents=True, exist_ok=True)
    say("Kalshi markets + hourly quotes...")
    await kalshi_markets(client, cache, cities, since, until, say)
    # forecasts/observations start earlier so the station correction has history on day one
    start, end = (since - timedelta(days=45)).date(), until.date()
    async with httpx.AsyncClient(timeout=60) as http:
        say("Archived forecasts (Open-Meteo previous runs)...")
        await forecasts(http, cache, cities, start, end, say)
        say("Official daily highs (NWS climate reports via Iowa Environmental Mesonet)...")
        await observations(http, cache, cities, start, end, say)
