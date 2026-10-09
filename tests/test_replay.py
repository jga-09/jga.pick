"""Historical replay against mocked Kalshi/Coinbase APIs that follow the official schemas."""

import json
import math
from datetime import UTC, datetime, timedelta

import httpx

from app.kalshi.client import KalshiClient
from app.kalshi.fixtures import fixture_mid
from app.research.backtest import BacktestConfig
from app.research.replay import (
    CoinbaseHistory,
    HistoryDownloader,
    parse_candles,
    parse_coinbase,
    replay_engine,
    replay_market,
)
from app.research.reporting import stress_report
from tests.conftest import make_settings

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _market(i: int, trend: str) -> dict:
    open_t = T0 + timedelta(minutes=15 * i)
    close = open_t + timedelta(minutes=15)
    final = fixture_mid(trend, 900)
    return {"ticker": f"KXBTC15M-T{i:03d}", "event_ticker": f"KXBTC15M-E{i:03d}", "title": "BTC up?",
            "open_time": open_t.isoformat(), "close_time": close.isoformat(), "status": "finalized",
            "result": "yes" if final > 50 else "no", "floor_strike": 60000.0}


def _candles(m: dict, trend: str, historical: bool) -> list[dict]:
    open_t = datetime.fromisoformat(m["open_time"])
    out = []
    for k in range(1, 16):
        mid = fixture_mid(trend, k * 60)
        bid, ask = (mid - 1) / 100, (mid + 1) / 100
        if historical:
            dist = lambda v: {"open": f"{v:.4f}", "low": f"{v:.4f}", "high": f"{v:.4f}", "close": f"{v:.4f}"}  # noqa: E731
            out.append({"end_period_ts": int((open_t + timedelta(minutes=k)).timestamp()), "yes_bid": dist(bid),
                        "yes_ask": dist(ask), "price": {"close": f"{mid / 100:.4f}"}, "volume": "10.00",
                        "open_interest": "5.00"})
        else:
            dist = lambda v: {k2 + "_dollars": f"{v:.4f}" for k2 in ("open", "low", "high", "close")}  # noqa: E731
            out.append({"end_period_ts": int((open_t + timedelta(minutes=k)).timestamp()), "yes_bid": dist(bid),
                        "yes_ask": dist(ask), "price": {"close_dollars": f"{mid / 100:.4f}"},
                        "volume_fp": "10.00", "open_interest_fp": "5.00"})
    return out


def _trades(m: dict, trend: str) -> list[dict]:
    open_t = datetime.fromisoformat(m["open_time"])
    side_up = trend == "up"
    return [{"trade_id": f"{m['ticker']}-{k}", "ticker": m["ticker"], "count_fp": "5.00",
             "yes_price_dollars": "0.5000", "no_price_dollars": "0.5000",
             "taker_outcome_side": ("yes" if side_up else "no") if k % 4 else ("no" if side_up else "yes"),
             "created_time": (open_t + timedelta(seconds=20 * k)).isoformat()} for k in range(1, 44)]


def _kalshi_transport(n: int):
    trends = ["up" if i % 2 else "down" for i in range(n)]
    markets = [_market(i, trends[i]) for i in range(n)]
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        p, q = req.url.path, req.url.params
        calls.append(p)
        if p.endswith("/series"):
            return httpx.Response(200, json={"series": [{"ticker": "KXBTC15M", "title": "Bitcoin 15 min",
                                                         "frequency": "fifteen_min"}]})
        if p.endswith("/historical/markets"):
            return httpx.Response(200, json={"markets": [], "cursor": ""})
        if p.endswith("/markets") and q.get("status") == "settled":
            return httpx.Response(200, json={"markets": markets if q.get("series_ticker") == "KXBTC15M" else [],
                                             "cursor": ""})
        for i, m in enumerate(markets):
            t = m["ticker"]
            if p.endswith(f"/series/KXBTC15M/markets/{t}/candlesticks"):
                if i % 3 == 0:  # pretend older markets moved behind the historical cutoff
                    return httpx.Response(404, json={"error": {"message": "not found"}})
                return httpx.Response(200, json={"ticker": t, "candlesticks": _candles(m, trends[i], False)})
            if p.endswith(f"/historical/markets/{t}/candlesticks"):
                return httpx.Response(200, json={"ticker": t, "candlesticks": _candles(m, trends[i], True)})
        if p.endswith("/markets/trades"):
            m = next(x for x in markets if x["ticker"] == q.get("ticker"))
            trades = _trades(m, trends[markets.index(m)])
            if q.get("cursor") is None:  # two pages to exercise pagination
                return httpx.Response(200, json={"trades": trades[:20], "cursor": "p2"})
            return httpx.Response(200, json={"trades": trades[20:], "cursor": ""})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    return httpx.MockTransport(handler), markets, calls


def _coinbase_transport():
    def handler(req: httpx.Request) -> httpx.Response:
        start = datetime.fromisoformat(req.url.params["start"])
        end = datetime.fromisoformat(req.url.params["end"])
        rows = []
        for k in range(301):  # inclusive end, like the real API
            ts = start + timedelta(minutes=k)
            if ts > end:
                break
            minute = (ts - T0).total_seconds() / 60
            window, pos = divmod(minute, 15)
            up = int(window) % 2 == 1
            price = 60000 * (1 + (0.004 if up else -0.004) * (pos / 15) + 0.0001 * math.sin(minute))
            rows.append([int(ts.timestamp()), price, price, price, price, 1.0])
        return httpx.Response(200, json=rows[::-1])  # Coinbase returns newest first

    return httpx.MockTransport(handler)


def test_parse_live_and_historical_candles():
    m = _market(0, "up")
    live, hist = parse_candles(_candles(m, "up", False)), parse_candles(_candles(m, "up", True))
    assert len(live) == len(hist) == 15
    assert live[5].yes_bid == hist[5].yes_bid and live[5].yes_ask == hist[5].yes_ask
    assert parse_candles([{"end_period_ts": 1, "yes_bid": {"close_dollars": "0.0000"},
                           "yes_ask": {"close_dollars": "1.0000"}, "price": {}}])[0].yes_bid is None


def test_parse_coinbase_sorted_and_shifted_to_minute_end():
    rows = [[1700000060, 1, 2, 1.5, 1.7, 3], [1700000000, 1, 2, 1.5, 1.6, 3], [1700000060, 1, 2, 1.5, 1.7, 3]]
    prices = parse_coinbase(rows, "BTC")
    assert [p.price for p in prices] == [1.6, 1.7]  # duplicate boundary minute removed
    assert prices[0].ts.timestamp() == 1700000060


async def test_download_replay_and_report_end_to_end(tmp_path):
    transport, markets, calls = _kalshi_transport(60)
    client = KalshiClient(make_settings(data_source="kalshi", kalshi_read_rps=50), transport=transport)
    dl = HistoryDownloader(client, tmp_path / "cache")
    spot = CoinbaseHistory(tmp_path / "cache", http=httpx.AsyncClient(transport=_coinbase_transport()))
    found = await dl.settled_markets("KXBTC15M", T0 - timedelta(days=1), T0 + timedelta(days=2))
    assert len(found) == 60
    prices = await spot.day("BTC", T0)
    assert 1440 <= len(prices) <= 1441  # inclusive end may include the next 00:00 candle
    obs = []
    for m in found:
        rec = await dl.market_data("KXBTC15M", m)
        assert rec is not None and rec["candles"] and len(rec["trades"]) == 43  # paginated
        obs += replay_market(rec, "BTC", prices, replay_engine)
    assert any("/historical/markets/" in c for c in calls)  # 404 fallback was used
    assert obs and all(o.source == "replay" and o.outcome in ("yes", "no") for o in obs)
    assert all("orderbook" not in o.components for o in obs)  # never invented
    assert any("underlying" in o.components for o in obs)
    # cached: a second pass makes no new HTTP calls
    n_calls = len(calls)
    await dl.market_data("KXBTC15M", found[0])
    assert len(calls) == n_calls
    cached = json.loads((tmp_path / "cache" / f"{found[0]['ticker']}.json").read_text())
    assert cached["market"]["ticker"] == found[0]["ticker"]
    report = stress_report(obs, "V4L", BacktestConfig(), picked_on_this_data=False)
    assert "V4L" in report and "VERDICT" in report
    await spot.close()
    await client.close()


async def test_spot_unavailable_is_reported_not_faked(tmp_path):
    def down(req):
        return httpx.Response(503)

    spot = CoinbaseHistory(tmp_path, http=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    assert await spot.day("BTC", T0) == [] and spot.available is False
    await spot.close()
