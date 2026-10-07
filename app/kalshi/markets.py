"""Automatic discovery of short-duration (e.g. 15-minute) Kalshi markets.

Strategy:
1. Resolve candidate *series*: explicit SERIES_TICKERS, plus (optionally)
   series from ``GET /series?category=Crypto`` whose ticker/title matches a
   configured asset and the configured duration (e.g. ticker ending ``15M`` or
   frequency/title mentioning 15 minutes), plus the conventional
   ``KX{ASSET}{N}M`` ticker as a last-resort guess. Nothing is assumed to exist:
   series that return no markets are simply ignored.
2. For each series fetch ``GET /markets?series_ticker=..&status=open``.
3. Keep markets whose open->close window matches the configured duration and
   that have not closed yet; the earliest-closing one per asset is "current".
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config import Settings
from app.errors import KalshiError, MalformedResponseError
from app.kalshi.market_data import ASSET_ALIASES, MarketInfo, parse_market_info
from app.utils.logging import log_event
from app.utils.time import utcnow

log = logging.getLogger(__name__)


def detect_asset(text: str, assets: list[str]) -> str | None:
    upper = text.upper()
    for asset in assets:
        for alias in ASSET_ALIASES.get(asset, (asset,)):
            if re.search(rf"(?:^|[^A-Z]|KX){re.escape(alias)}", upper):
                return asset
    return None


def series_matches_duration(series: dict[str, Any], minutes: int) -> bool:
    ticker = str(series.get("ticker", "")).upper()
    text = f"{series.get('frequency', '')} {series.get('title', '')}".lower()
    return (
        ticker.endswith(f"{minutes}M")
        or f"{minutes} min" in text
        or f"{minutes}-min" in text
        or (minutes == 15 and "fifteen" in text)
    )


@dataclass
class DiscoveryState:
    current: dict[str, MarketInfo] = field(default_factory=dict)  # asset -> current market
    upcoming: dict[str, MarketInfo] = field(default_factory=dict)  # asset -> next market
    by_ticker: dict[str, MarketInfo] = field(default_factory=dict)
    event_map: dict[str, str] = field(default_factory=dict)  # market ticker -> event ticker
    last_run: datetime | None = None
    last_error: str | None = None


class MarketDiscovery:
    SERIES_CACHE_SEC = 3600

    def __init__(self, client: Any, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.state = DiscoveryState()
        self._series: dict[str, str] = {}  # series ticker -> asset
        self._series_loaded_at = 0.0

    async def resolve_series(self) -> dict[str, str]:
        if self._series and time.time() - self._series_loaded_at < self.SERIES_CACHE_SEC:
            return self._series
        assets = self.settings.asset_list
        minutes = self.settings.market_duration_minutes
        found: dict[str, str] = {}
        for s in self.settings.series_list:
            asset = detect_asset(s, assets) or (assets[0] if len(assets) == 1 else s)
            found[s] = asset
        if self.settings.auto_discover_series:
            try:
                for series in await self.client.get_series_list(category="Crypto"):
                    ticker = str(series.get("ticker", "")).upper()
                    asset = detect_asset(f"{ticker} {series.get('title', '')}", assets)
                    if asset and series_matches_duration(series, minutes):
                        found.setdefault(ticker, asset)
            except KalshiError as exc:
                log_event(log, "SERIES_LOOKUP_FAILED", logging.WARNING, error=exc)
            for asset in assets:  # conventional naming as a last-resort candidate
                found.setdefault(f"KX{asset}{minutes}M", asset)
        self._series = found
        self._series_loaded_at = time.time()
        return found

    def _is_target_window(self, info: MarketInfo) -> bool:
        dur = info.duration_sec
        if dur is None:
            return True  # cannot verify; series filter already applied
        target = self.settings.market_duration_minutes * 60
        return abs(dur - target) <= self.settings.duration_tolerance_sec

    async def discover(self, now: datetime | None = None) -> DiscoveryState:
        now = now or utcnow()
        series = await self.resolve_series()
        candidates: dict[str, list[MarketInfo]] = {}
        errors: list[str] = []
        for series_ticker, asset in series.items():
            try:
                raw_markets = await self.client.get_markets(series_ticker=series_ticker, status="open")
            except KalshiError as exc:
                errors.append(f"{series_ticker}: {exc}")
                continue
            for raw in raw_markets:
                try:
                    info = parse_market_info(raw, asset=asset, series_ticker=series_ticker)
                except MalformedResponseError as exc:
                    log_event(log, "MARKET_PARSE_ERROR", logging.WARNING, error=exc)
                    continue
                if info.close_time <= now or not self._is_target_window(info):
                    continue
                if info.status not in ("", "active", "open", "initialized"):
                    continue
                candidates.setdefault(asset, []).append(info)
        self._apply(candidates, now)
        self.state.last_error = "; ".join(errors) if errors else None
        return self.state

    def _apply(self, candidates: dict[str, list[MarketInfo]], now: datetime) -> None:
        st = self.state
        new_by_ticker: dict[str, MarketInfo] = {}
        st.current, st.upcoming = {}, {}
        for asset, infos in candidates.items():
            infos.sort(key=lambda m: m.close_time)
            live = [m for m in infos if m.open_time is None or m.open_time <= now]
            future = [m for m in infos if m.open_time is not None and m.open_time > now]
            if live:
                st.current[asset] = live[0]
            if future:
                st.upcoming[asset] = future[0]
            for m in infos:
                new_by_ticker[m.ticker] = m
        for ticker, info in new_by_ticker.items():
            if ticker not in st.by_ticker:
                log_event(
                    log, "MARKET_DISCOVERED", ticker=ticker, event_ticker=info.event_ticker,
                    asset=info.asset, close=info.close_time.isoformat(),
                )
        for ticker in set(st.by_ticker) - set(new_by_ticker):
            log_event(log, "MARKET_REMOVED", ticker=ticker)
        st.by_ticker = new_by_ticker
        st.event_map = {t: m.event_ticker for t, m in new_by_ticker.items()}
        st.last_run = now

    def prune_expired(self, now: datetime | None = None) -> list[str]:
        """Remove markets whose close time has passed; returns removed tickers."""
        now = now or utcnow()
        st = self.state
        expired = [t for t, m in st.by_ticker.items() if m.close_time <= now]
        for t in expired:
            st.by_ticker.pop(t, None)
            st.event_map.pop(t, None)
            log_event(log, "MARKET_EXPIRED", ticker=t)
        for asset in list(st.current):
            if st.current[asset].ticker in expired:
                nxt = st.upcoming.pop(asset, None)
                if nxt and nxt.close_time > now:
                    st.current[asset] = nxt
                else:
                    del st.current[asset]
        return expired
