"""Phase 2: each asset's historical estimate comes only from that asset's own observations."""

from dataclasses import replace

from app.research.calibration import HistoricalModel
from tests.test_research import obs


def _set(asset, n, outcome, start=0):
    return [replace(obs(start + i, outcome=outcome), ticker=f"{asset}-{start + i:05d}", asset=asset) for i in range(n)]


def test_btc_history_cannot_approve_gold_or_eth():
    btc_winners = _set("BTC", 200, "yes")  # strongly positive BTC history
    model = HistoricalModel(btc_winners + _set("ETH", 5, "no", 1000), min_samples=30)
    btc = model.estimate("B", "STRONG_TREND", "6-9", "yes", 60, asset="BTC")
    assert btc.sufficient and btc.scope == "asset:BTC" and btc.ev_cents > 0
    gold = model.estimate("B", "STRONG_TREND", "6-9", "yes", 60, asset="GOLD")
    assert not gold.sufficient and gold.scope == "asset:GOLD" and gold.n_markets == 0 and gold.ev_cents is None
    eth = model.estimate("B", "STRONG_TREND", "6-9", "yes", 60, asset="ETH")
    assert not eth.sufficient and eth.n_markets == 5  # only ETH's own 5 markets count


def test_each_asset_gets_its_own_edge():
    model = HistoricalModel(_set("BTC", 100, "yes") + _set("SOL", 100, "no", 500), min_samples=30)
    btc = model.estimate("B", "STRONG_TREND", "6-9", "yes", 60, asset="BTC")
    sol = model.estimate("B", "STRONG_TREND", "6-9", "yes", 60, asset="SOL")
    assert btc.win_rate == 1.0 and sol.win_rate == 0.0
    assert btc.n_markets == sol.n_markets == 100
    assert model.for_asset("BTC").n_markets == 100 and model.n_markets == 200  # root keeps a pooled view


def test_adding_other_asset_data_never_changes_an_asset_estimate():
    base = _set("BTC", 60, "yes") + _set("BTC", 40, "no", 100)
    alone = HistoricalModel(base, min_samples=30).estimate("B", "STRONG_TREND", "6-9", "yes", 60, asset="BTC")
    mixed = HistoricalModel(base + _set("ETH", 500, "no", 1000) + _set("GOLD", 300, "yes", 5000),
                            min_samples=30).estimate("B", "STRONG_TREND", "6-9", "yes", 60, asset="BTC")
    assert alone == mixed


def test_pooled_estimate_is_labelled_and_never_sufficient():
    model = HistoricalModel(_set("BTC", 100, "yes") + _set("ETH", 100, "yes", 500), min_samples=30)
    pooled = model.estimate("B", "STRONG_TREND", "6-9", "yes", 60)  # no asset given
    assert pooled.scope == "pooled" and not pooled.sufficient


def test_single_asset_root_is_that_asset():
    model = HistoricalModel(_set("BTC", 100, "yes"), min_samples=30)
    est = model.estimate("B", "STRONG_TREND", "6-9", "yes", 60)
    assert est.sufficient and est.scope == "asset:BTC"


def test_asset_names_are_case_insensitive():
    model = HistoricalModel(_set("btc", 50, "yes"), min_samples=30)
    assert model.estimate("B", "STRONG_TREND", "6-9", "yes", 60, asset="BTC").n_markets == 50
