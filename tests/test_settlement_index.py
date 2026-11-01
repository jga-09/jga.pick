"""Settlement-index feed (Kalshi /live_data), its use in backtests, and the reddit_time_delay test."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.data.settlement_index import SettlementIndexFeed, compact, parse_index_bars
from app.research.btc_backtest import SimCosts, load_dataset
from app.research.btc_reddit import EXIT, TRIGGER, format_reddit, run_reddit, trade_market
from app.research.btc_synthetic import generate

SAMPLE = {"live_data": {"type": "crypto", "is_historical": False, "details": {
    "coin": "BTC", "event_ticker": "KXBTC15M-26OCT180000", "candlesticks": {"1M": [
        {"open_ts_ms": 1792295400000, "open": 81889.07, "high": 81890.07, "low": 81883.79, "close": 81890.0655},
        {"open_ts_ms": 1792295460000, "open": 81890.29, "high": 81892.74, "low": 81890.29, "close": 81891.7243},
    ]}}}}


def test_parse_index_bars():
    bars = parse_index_bars(SAMPLE)
    assert [b.close for b in bars] == [81890.0655, 81891.7243]
    assert bars[0].ts == datetime.fromtimestamp(1792295460, UTC)  # END of the minute
    assert parse_index_bars({"live_data": {}}) == [] and parse_index_bars(None) == []
    assert compact(bars)[0] == [1792295460.0, 81890.0655]


class FakeClient:
    def __init__(self, resp=None, fail=False):
        self.resp, self.fail, self.calls = resp, fail, 0

    async def get_event_live_data(self, ev):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return self.resp


async def test_feed_latest_stale_and_failure():
    feed = SettlementIndexFeed()
    now = datetime.fromtimestamp(1792295470, UTC)
    spot = await feed.latest(FakeClient(SAMPLE), "E", now)
    assert spot.price == 81891.7243 and spot.source == "cf_index"
    assert await SettlementIndexFeed().latest(FakeClient(SAMPLE), "E", now + timedelta(minutes=10)) is None
    assert await SettlementIndexFeed().latest(FakeClient(fail=True), "E", now) is None
    c = FakeClient(SAMPLE)
    f2 = SettlementIndexFeed()
    await f2.latest(c, "E", now)
    await f2.latest(c, "E", now)
    assert c.calls == 1  # cached briefly


def test_default_spot_source_is_coinbase():
    from tests.conftest import make_settings

    assert make_settings().btc_spot_source == "coinbase"


@pytest.fixture(scope="module")
def cache(tmp_path_factory):
    p = tmp_path_factory.mktemp("idx")
    generate(p, days=9, mode="efficient")
    return p


def test_backtest_uses_index_when_present(cache):
    ds_cb = load_dataset(cache)
    tickers = list(ds_cb.markets)[:5]
    for f in cache.glob("KXBTC*.json.gz"):
        rec = json.loads(gzip.decompress(f.read_bytes()))
        steps = ds_cb.markets.get(rec["market"]["ticker"])
        if rec["market"]["ticker"] not in tickers or not steps:
            continue
        rec["index"] = [[s.ts.timestamp(), 12345.0] for s in steps]  # a recognisable constant
        f.write_bytes(gzip.compress(json.dumps(rec).encode()))
    ix = load_dataset(cache, spot_source="index")
    assert set(ix.markets) == set(tickers)  # markets without index data are skipped
    assert all(s.features.spot == 12345.0 for v in ix.markets.values() for s in v)
    assert ix.skipped.get("no settlement-index data")


def test_reddit_rules(cache):
    ds = load_dataset(cache)
    for steps in ds.markets.values():
        t = trade_market(steps, SimCosts(), stop=True)
        if t is None:
            continue
        assert t.entry >= TRIGGER and t.t_left <= 14 * 60
        if t.exited:
            assert t.payout <= EXIT
    rep = run_reddit(ds, holdout_days=3)
    assert "VERDICT" in format_reddit(rep)


async def test_runtime_uses_index_spot_with_fallback(runtime):
    from tests.conftest import make_info, quote_snapshot
    from app.utils.time import utcnow

    now = utcnow()
    t0 = int((now - timedelta(seconds=30)).timestamp() * 1000)
    resp = {"live_data": {"details": {"candlesticks": {"1M": [
        {"open_ts_ms": t0, "open": 1.0, "high": 1.0, "low": 1.0, "close": 77777.0}]}}}}
    runtime.settings = runtime.settings.model_copy(update={"btc_spot_source": "index"})
    runtime.fixture_mode = False
    runtime.client.get_event_live_data = FakeClient(resp).get_event_live_data
    sig = await runtime.btc_decision(quote_snapshot(make_info()), now)
    assert sig.features.spot == 77777.0
    runtime.client.get_event_live_data = FakeClient(fail=True).get_event_live_data
    runtime.settlement_index = SettlementIndexFeed()
    sig2 = await runtime.btc_decision(quote_snapshot(make_info()), now)
    assert sig2.features.spot != 77777.0  # fell back to the normal feed
