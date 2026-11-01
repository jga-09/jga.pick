"""Download Kalshi tennis match markets (hourly quotes) and tennis-data.co.uk odds files (resumable).

Kalshi series are discovered from the official /series list (Sports category): any series whose
ticker or title mentions ATP or WTA and a match. Nothing is invented; missing data is reported.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.errors import KalshiError
from app.research.replay import HistoryDownloader
from app.research.weather import compact_candles
from app.research.weather_download import _hourly_candles
from app.utils.time import parse_ts

TD_URL = "http://www.tennis-data.co.uk/{year}{suffix}/{year}.xlsx"
FALLBACK_SERIES = ("KXATPMATCH", "KXWTAMATCH")


def is_match_series(s: dict[str, Any]) -> bool:
    text = f"{s.get('ticker', '')} {s.get('title', '')}".upper()
    return ("ATP" in text or "WTA" in text) and "MATCH" in text and not any(w in text for w in ("DOUBLES", "CHALLENGER", "ITF"))


async def find_series(client: Any, say: Callable[[str], None]) -> list[str]:
    found: list[str] = []
    try:
        for cat in ("Sports", None):
            for s in await client.get_series_list(cat):
                if is_match_series(s) and s.get("ticker") not in found:
                    found.append(s["ticker"])
            if found:
                break
    except KalshiError as exc:
        say(f"  series list failed ({exc}); trying the usual names")
    for t in FALLBACK_SERIES:
        if t not in found:
            found.append(t)
    return found


async def kalshi_tennis(client: Any, cache: Path, days: int, say: Callable[[str], None]) -> None:
    mdir = cache / "markets"
    mdir.mkdir(parents=True, exist_ok=True)
    until = datetime.now(UTC)
    since = until - timedelta(days=days)
    hd = HistoryDownloader(client, cache / "_unused")
    for series in await find_series(client, say):
        markets = await hd.settled_markets(series, since, until)
        if not markets:
            say(f"  {series}: no settled markets in the last {days} days")
            continue
        new = failed = 0
        for i, m in enumerate(markets, 1):
            path = mdir / f"{m['ticker']}.json.gz"
            if path.exists():
                continue
            o, c = parse_ts(m.get("open_time")), parse_ts(m.get("close_time"))
            if o is None or c is None:
                continue
            o = max(o, c - timedelta(days=10))  # long-listed matches: the pre-match days are enough
            try:
                raw = await _hourly_candles(client, series, m["ticker"], int(o.timestamp()), int(c.timestamp()))
            except KalshiError as exc:
                failed += 1
                if failed <= 3:
                    say(f"  {m['ticker']}: {exc}")
                continue
            rec = {"series": series, "market": m, "candles": compact_candles(raw)}
            path.write_bytes(gzip.compress(json.dumps(rec, separators=(",", ":")).encode()))
            new += 1
            if i % 200 == 0:
                say(f"  {series}: {i}/{len(markets)} markets")
        say(f"  {series}: {len(markets)} settled markets, {new} downloaded now, {failed} failed")


async def odds_files(cache: Path, days: int, say: Callable[[str], None]) -> None:
    tdir = cache / "td"
    tdir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    years = sorted({(now - timedelta(days=days)).year, now.year})
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
        for year in years:
            for tour, suffix in (("atp", ""), ("wta", "w")):
                path = tdir / f"{year}_{tour}.xlsx"
                if path.exists() and year < now.year:
                    continue  # past years do not change; the current year is always refreshed
                try:
                    r = await http.get(TD_URL.format(year=year, suffix=suffix))
                    r.raise_for_status()
                    if not r.content.startswith(b"PK"):
                        raise ValueError("not an .xlsx file")
                    path.write_bytes(r.content)
                    say(f"  {tour.upper()} {year}: {len(r.content) // 1024} KB")
                except (httpx.HTTPError, ValueError) as exc:
                    say(f"  {tour.upper()} {year}: download failed ({type(exc).__name__} {str(exc)[:100]})")
