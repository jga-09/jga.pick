"""Phase 3: Gold discovery, settlement validation, Pyth parsing, stale/missing data, never traded."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.data.gold import PythGoldProvider, parse_benchmarks, parse_hermes, session_open
from app.errors import KalshiTransientError
from app.gold.markets import (
    discover_gold_series,
    frequency_minutes,
    is_gold_series,
    validate_gold_market,
)
from app.gold.status import gold_status
from app.kalshi.market_data import parse_market_info

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)  # a Wednesday, gold session open
FEED = "0xabc123"
RULES = ("If the price of gold (XAU/USD) as reported by Pyth at 11:15 AM ET is above 2650.00, then the market "
         "resolves to Yes.")


def _raw(ticker="KXGOLD15M-26OCT281115-15", rules=RULES, strike_type="greater", strike=2650.0, close=None,
         open_=None, bid="0.4500", ask="0.4800"):
    close = close or NOW + timedelta(minutes=10)
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "close_time": close.isoformat(),
            "open_time": (open_ or NOW - timedelta(minutes=5)).isoformat(), "status": "active",
            "strike_type": strike_type, "floor_strike": strike, "rules_primary": rules,
            "yes_bid_dollars": bid, "yes_ask_dollars": ask}


def _info(**kw):
    return parse_market_info(_raw(**kw), asset="GOLD", series_ticker="KXGOLD15M")


def test_series_detection_never_assumes_a_ticker():
    assert is_gold_series({"ticker": "KXGOLD15M", "title": "Gold price every 15 minutes"})
    assert is_gold_series({"ticker": "KXXAUD", "title": "XAU daily"})
    assert not is_gold_series({"ticker": "KXSILVERD", "title": "Silver daily"})
    assert not is_gold_series({"ticker": "KXBTC15M", "title": "Bitcoin"})
    assert frequency_minutes({"ticker": "KXGOLD15M"}) == 15
    assert frequency_minutes({"ticker": "KXGOLDD", "frequency": "daily"}) == 1440
    assert frequency_minutes({"ticker": "KXGOLDX"}) is None


async def test_discovery_scans_categories_and_full_list_and_reports_errors():
    client = MagicMock()

    async def series_list(category=None):
        if category == "Commodities":
            raise KalshiTransientError("down")
        return [{"ticker": "KXGOLD15M", "title": "Gold"}, {"ticker": "KXBTC15M", "title": "Bitcoin"}]

    client.get_series_list = series_list
    found, errors = await discover_gold_series(client, ["Commodities"])
    assert [s["ticker"] for s in found] == ["KXGOLD15M"] and errors


def test_settlement_validation():
    ok = validate_gold_market(_info(), "pyth", NOW)
    assert ok.verified and ok.kind == "above" and ok.strike == 2650 and ok.source == "pyth"
    assert not validate_gold_market(_info(), None, NOW).verified  # no matching provider
    wrong = validate_gold_market(_info(rules=RULES.replace("Pyth", "COMEX gold futures")), "pyth", NOW)
    assert any("settlement source is comex" in p for p in wrong.problems)
    assert not validate_gold_market(_info(rules=""), "pyth", NOW).verified
    assert not validate_gold_market(_info(strike_type="between"), "pyth", NOW).verified
    flipped = validate_gold_market(_info(rules=RULES.replace("above", "below")), "pyth", NOW)
    assert any("BELOW" in p for p in flipped.problems)
    assert not validate_gold_market(_info(rules="Bitcoin above 1 per Pyth"), "pyth", NOW).verified


def test_pyth_parsing_and_bar_timing():
    sp = parse_hermes({"parsed": [{"id": "abc123", "price": {"price": "265012345", "expo": -5,
                                                               "publish_time": int(NOW.timestamp())}}]}, FEED)
    assert sp.price == pytest.approx(2650.12345) and sp.ts == NOW
    assert parse_hermes({"parsed": [{"id": "zzz", "price": {}}]}, FEED) is None
    bars = parse_benchmarks({"s": "ok", "t": [100, 160], "o": [1, 2], "h": [1, 2], "l": [1, 2], "c": [1.5, 2.5]})
    assert [b.ts.timestamp() for b in bars] == [160, 220]  # stored by bar END (no look-ahead)
    assert parse_benchmarks({"s": "ok", "t": [1, 2], "o": [1], "h": [1], "l": [1], "c": [1]}) == []  # misaligned
    assert parse_benchmarks({"s": "no_data"}) == []


def test_gold_session_hours():
    assert session_open(NOW)
    assert not session_open(datetime(2026, 10, 31, 15, tzinfo=UTC))  # Saturday
    assert not session_open(datetime(2026, 10, 28, 21, 30, tzinfo=UTC))  # 17:30 New York: daily break


async def test_stale_or_missing_gold_price_is_never_used(monkeypatch):
    old = int((datetime.now(UTC) - timedelta(minutes=10)).timestamp())
    reply = {"parsed": [{"id": "abc123", "price": {"price": "2650", "expo": 0, "publish_time": old}}]}
    p = PythGoldProvider(FEED, stale_after_sec=120,
                         http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply))))
    assert await p.get_price("GOLD") is None and "stale" in p.last_error
    assert await p.get_price("BTC") is None
    p.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert await p.get_price("GOLD") is None and p.last_error
    with pytest.raises(ValueError):
        PythGoldProvider("")


async def test_gold_status_ready_only_when_everything_passes():
    client = MagicMock()
    client.get_series_list = AsyncMock(return_value=[{"ticker": "KXGOLD15M", "title": "Gold"}])
    client.get_markets = AsyncMock(return_value=[_raw()])
    provider = MagicMock()
    provider.name = "pyth_gold"
    provider.get_price = AsyncMock(return_value=MagicMock(price=2651.0, ts=NOW))
    st = await gold_status(client, provider, ["Commodities"], NOW)
    assert st.ready and st.market.ticker.startswith("KXGOLD15M") and st.spread == pytest.approx(3)
    assert "research-only" in st.trading_reason
    provider.get_price = AsyncMock(return_value=None)
    provider.last_error = "stale"
    st = await gold_status(client, provider, ["Commodities"], NOW)
    assert not st.ready and any("unavailable" in p for p in st.problems)
    st = await gold_status(client, None, ["Commodities"], NOW)
    assert not st.ready
    client.get_series_list = AsyncMock(return_value=[])
    assert (await gold_status(client, provider, [], NOW)).problems[-1] == "no Gold series found on Kalshi"


def test_risk_manager_never_approves_gold(runtime):
    from dataclasses import replace

    from tests.conftest import feed_history, make_info

    info = replace(make_info(), asset="GOLD")
    sig = feed_history(runtime, info, "up")
    snap = runtime.market_data.latest[info.ticker]
    dec = runtime.check_risk(sig, snap)
    assert not dec.approved and any("research-only" in f for f in dec.failures)


async def test_pyth_error_reports_status_and_retries_without_0x():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(404, text="Price ids not found")

    p = PythGoldProvider(FEED, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await p.get_price("GOLD") is None
    assert p.last_error.startswith("HTTP 404") and len(seen) == 2


async def test_kalshi_gold_feed_uses_settlement_candles():
    from app.data.gold import KalshiGoldFeed

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start_ms = int((now - timedelta(minutes=2)).timestamp() * 1000)
    payload = {"live_data": {"details": {"candlesticks": {"1M": [
        {"open_ts_ms": start_ms, "open": 3910, "high": 3912, "low": 3909, "close": 3911.5}]}}}}
    client = MagicMock()
    client.get_event_live_data = AsyncMock(return_value=payload)
    feed = KalshiGoldFeed()
    spot = await feed.latest(client, "KXGOLD15M-26NOV012130", now)
    assert spot.price == 3911.5 and spot.source == "kalshi_pyth_index"
    client.get_event_live_data = AsyncMock(return_value={})
    assert await KalshiGoldFeed().latest(client, "E2", now) is None


async def test_gold_status_with_kalshi_feed_is_ready():
    from app.data.gold import KalshiGoldFeed

    client = MagicMock()
    client.get_series_list = AsyncMock(return_value=[{"ticker": "KXGOLD15M", "title": "Gold 15-minute"}])
    client.get_markets = AsyncMock(return_value=[_raw()])
    feed = KalshiGoldFeed()
    feed.latest = AsyncMock(return_value=MagicMock(price=3911.0, ts=NOW))
    st = await gold_status(client, feed, ["Commodities"], NOW)
    assert st.ready and st.check.verified
