from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from app.config import Settings
from app.database.db import Database
from app.database.repository import Repository
from app.kalshi.fixtures import (
    FIXTURE_STRIKES,
    FixtureKalshiClient,
    FixtureUnderlyingProvider,
    fixture_spot_history,
    make_snapshot,
)
from app.kalshi.market_data import MarketInfo, MarketSnapshot, OrderBook
from app.risk.profiles import load_profiles
from app.runtime import BotRuntime
from app.utils.time import utcnow

ADMIN_ID = 4242


def make_settings(**kw: Any) -> Settings:
    base: dict[str, Any] = dict(_env_file=None, data_source="fixture", database_url="sqlite://",
                                telegram_admin_ids=str(ADMIN_ID), telegram_bot_token="1234567890:FAKEfakeFAKEfakeFAKEfakeFAKEfake123")
    base.update(kw)
    return Settings(**base)


def make_info(asset: str = "BTC", now: datetime | None = None, remaining: float = 522,
              elapsed: float = 378) -> MarketInfo:
    now = now or utcnow()
    return MarketInfo(
        ticker=f"KX{asset}15M-TEST", event_ticker=f"KX{asset}15M-TEST", series_ticker=f"KX{asset}15M",
        asset=asset, title=f"{asset} test", subtitle="", open_time=now - timedelta(seconds=elapsed),
        close_time=now + timedelta(seconds=remaining), expiration_time=None, status="active",
        floor_strike=FIXTURE_STRIKES.get(asset, 100.0),
    )


def quote_snapshot(info: MarketInfo, yes_bid: float = 60, yes_ask: float = 62, ts: datetime | None = None,
                   depth: float = 100) -> MarketSnapshot:
    book = OrderBook(yes_bids=((yes_bid, depth),), no_bids=((100 - yes_ask, depth),))
    return MarketSnapshot(info=info, ts=ts or utcnow(), yes_bid=yes_bid, yes_ask=yes_ask,
                          no_bid=100 - yes_ask, no_ask=100 - yes_bid, last_price=(yes_bid + yes_ask) / 2,
                          orderbook=book)


def feed_history(rt: BotRuntime, info: MarketInfo, trend: str, steps: int = 37, threshold: int | None = None,
                 spot: bool = True):
    """Feed ~3 minutes of fixture history (+ synthetic spot) ending now; returns the last SignalResult."""
    now = utcnow()
    result = None
    rt.discovery.state.current[info.asset] = info
    rt.discovery.state.by_ticker[info.ticker] = info
    if isinstance(rt.client, FixtureKalshiClient):
        rt.client.register(info, trend)
    for i in range(steps):
        ts = now - timedelta(seconds=(steps - 1 - i) * 5)
        snap = make_snapshot(info.asset, trend, ts, info=info, ts=ts)
        rt.market_data.latest[snap.ticker] = snap
        hist = fixture_spot_history(info.asset, trend, info, ts) if spot else []
        result = rt.engine.evaluate(snap, threshold=threshold or rt.profile().min_confidence,
                                    min_quality=rt.profile().min_signal_quality, now=ts,
                                    spot=hist[-1] if hist else None, spot_history=hist)
        rt._last_signal[snap.ticker] = result
    return result


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def repo() -> Repository:
    r = Repository(Database("sqlite://"))
    r.init()
    return r


@pytest.fixture
def runtime(settings: Settings, repo: Repository) -> BotRuntime:
    client = FixtureKalshiClient()
    rt = BotRuntime(settings, repo, client, underlying=FixtureUnderlyingProvider(client=client),
                    profiles=load_profiles(read_env=False))
    return rt
