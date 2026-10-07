import base64
import json
from datetime import timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.errors import KalshiAuthError, KalshiHTTPError, KalshiTransientError
from app.kalshi.auth import KalshiSigner
from app.kalshi.client import KalshiClient
from app.kalshi.market_data import build_snapshot, parse_market_info, parse_orderbook
from app.kalshi.markets import MarketDiscovery, detect_asset
from app.utils.time import utcnow
from tests.conftest import make_settings


def test_signer_produces_verifiable_rsa_pss_signature():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    signer = KalshiSigner("key-id", pem)
    h = signer.headers("GET", "https://api.elections.kalshi.com/trade-api/v2/portfolio/balance?x=1",
                       timestamp_ms=1700000000000)
    assert h["KALSHI-ACCESS-KEY"] == "key-id" and h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"1700000000000GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256(),
    )
    assert "BEGIN" not in repr(signer)


def test_invalid_key_raises_without_leaking():
    with pytest.raises(KalshiAuthError) as ei:
        KalshiSigner("id", b"-----BEGIN PRIVATE KEY-----\nSECRETSTUFF\n-----END PRIVATE KEY-----")
    assert "SECRETSTUFF" not in str(ei.value)


def raw_market(**kw):
    now = utcnow()
    d = {"ticker": "KXBTC15M-26OCT071915-15", "event_ticker": "KXBTC15M-26OCT071915",
         "title": "BTC price up in next 15 mins?", "open_time": (now - timedelta(minutes=5)).isoformat(),
         "close_time": (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"), "status": "active",
         "yes_bid_dollars": "0.6300", "yes_ask_dollars": "0.6500", "no_bid_dollars": "0.3500",
         "no_ask_dollars": "0.3700", "last_price_dollars": "0.6400", "volume_fp": "1200.00",
         "open_interest_fp": "800.00", "floor_strike": 64321.5, "result": ""}
    d.update(kw)
    return d


def test_parse_market_and_quotes():
    info = parse_market_info(raw_market(), asset="BTC", series_ticker="KXBTC15M")
    assert info.label == "BTC 15M" and info.floor_strike == 64321.5 and info.close_time.tzinfo
    book = parse_orderbook({"yes_dollars": [["0.6200", "50.00"], ["0.6300", "20.00"]],
                            "no_dollars": [["0.3500", "30.00"]]})
    snap = build_snapshot(info, raw_market(), book, ())
    assert (snap.yes_bid, snap.yes_ask, snap.no_bid, snap.no_ask) == (63, 65, 35, 37)
    assert snap.spread == 2 and snap.ask_liquidity("yes") == 30 and snap.ask_liquidity("no") == 20


def test_missing_quotes_stay_none():
    info = parse_market_info(raw_market(), asset="BTC", series_ticker="KXBTC15M")
    snap = build_snapshot(info, raw_market(yes_bid_dollars="0.0000", yes_ask_dollars="1.0000",
                                           no_bid_dollars="0.0000", no_ask_dollars="1.0000",
                                           last_price_dollars="0.0000"), None, ())
    assert snap.yes_bid is None and snap.yes_ask is None and snap.yes_mid is None


def test_detect_asset():
    assert detect_asset("KXBTC15M", ["BTC", "ETH"]) == "BTC"
    assert detect_asset("Ethereum price 15 min", ["BTC", "ETH"]) == "ETH"
    assert detect_asset("KXSOLANA", ["BTC"]) is None


def _transport(handler):
    return httpx.MockTransport(handler)


async def test_client_error_mapping_and_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path.endswith("/exchange/status"):
            return httpx.Response(500) if calls["n"] == 1 else httpx.Response(200, json={"trading_active": True})
        if "balance" in request.url.path:
            return httpx.Response(401, json={"error": {"message": "bad sig"}})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    c = KalshiClient(make_settings(data_source="kalshi"), transport=_transport(handler))
    assert (await c.get_exchange_status())["trading_active"] is True  # 500 retried
    with pytest.raises(KalshiAuthError):
        await c.get_balance()  # no credentials
    with pytest.raises(KalshiHTTPError):
        await c.get_market("X")
    await c.close()


async def test_client_timeout_is_transient(monkeypatch):
    def handler(request):
        raise httpx.ConnectTimeout("t")

    async def once(fn, **_):
        return await fn()

    monkeypatch.setattr("app.kalshi.client.retry_async", once)  # skip retry delays
    c = KalshiClient(make_settings(data_source="kalshi"), transport=_transport(handler))
    with pytest.raises(KalshiTransientError):
        await c.get_market("X")
    await c.close()


async def test_discovery_filters_and_tracks_15m_markets():
    now = utcnow()
    good = raw_market()
    hourly = raw_market(ticker="KXBTCH-1", open_time=(now - timedelta(minutes=30)).isoformat(),
                        close_time=(now + timedelta(minutes=30)).isoformat())
    expired = raw_market(ticker="KXBTC15M-OLD", close_time=(now - timedelta(minutes=1)).isoformat())
    seen = []

    def handler(request):
        path = request.url.path
        if path.endswith("/series"):
            return httpx.Response(200, json={"series": [
                {"ticker": "KXBTC15M", "title": "Bitcoin 15 min", "frequency": "fifteen_min"},
                {"ticker": "KXBTCD", "title": "Bitcoin daily", "frequency": "daily"}]})
        if path.endswith("/markets"):
            seen.append(request.url.params.get("series_ticker"))
            if request.url.params.get("series_ticker") == "KXBTC15M":
                return httpx.Response(200, json={"markets": [good, hourly, expired], "cursor": ""})
            return httpx.Response(200, json={"markets": [], "cursor": ""})
        return httpx.Response(404)

    settings = make_settings(data_source="kalshi", assets="BTC,ETH")
    c = KalshiClient(settings, transport=_transport(handler))
    d = MarketDiscovery(c, settings)
    st = await d.discover()
    assert "KXBTCD" not in seen and "KXBTC15M" in seen and "KXETH15M" in seen
    assert list(st.by_ticker) == [good["ticker"]]
    assert st.current["BTC"].ticker == good["ticker"] and "ETH" not in st.current
    assert st.event_map[good["ticker"]] == good["event_ticker"]
    later = st.current["BTC"].close_time + timedelta(seconds=1)
    assert d.prune_expired(later) == [good["ticker"]] and "BTC" not in st.current
    await c.close()


def test_ws_ticker_merge(runtime):
    info = parse_market_info(raw_market(), asset="BTC", series_ticker="KXBTC15M")
    runtime.market_data.latest[info.ticker] = build_snapshot(info, raw_market(), None, ())
    snap = runtime.market_data.apply_ws_ticker({"market_ticker": info.ticker, "yes_bid_dollars": "0.7000",
                                                "yes_ask_dollars": "0.7200", "price_dollars": "0.7100"})
    assert snap.yes_bid == 70 and snap.yes_ask == 72 and snap.last_price == 71 and snap.source == "ws"
    assert runtime.market_data.apply_ws_ticker({"market_ticker": "UNKNOWN", "yes_bid_dollars": "0.1"}) is None
    json.dumps({"ok": True})
