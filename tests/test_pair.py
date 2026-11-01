"""Pair completion: payoff per fill combination, no look-back fills, taker completion, report."""

from __future__ import annotations

import gzip
import json

import pytest

from app.research.btc_backtest import SimCosts, load_dataset
from app.research.btc_pair import format_pair, market_result, run_pair
from app.research.btc_synthetic import generate


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    p = tmp_path_factory.mktemp("pair")
    generate(p, days=9, mode="efficient", spread=4.0)
    return p, load_dataset(p)


def _first(steps):
    return next(s for s in steps if s.t_left >= 600 and s.yes_bid is not None)


def test_payoffs(data):
    _, ds = data
    steps = next(iter(ds.markets.values()))
    st = _first(steps)
    t = st.ts.timestamp()
    yb, nb = st.yes_bid, st.no_bid
    both = [(t + 10, yb - 1, "no"), (t + 20, 100 - nb + 1, "yes")]  # prints through both bids
    r = market_result(steps, both, "P1", "through", SimCosts())
    assert r["kind"] == "both" and abs(r["net"] - (100 - yb - nb) / 100) < 1e-9
    yes_only = [(t + 10, yb - 1, "no")]
    r = market_result(steps, yes_only, "P1", "through", SimCosts())
    assert r["kind"] == "yes only"
    assert abs(r["net"] - ((100 if st.outcome == "yes" else 0) - yb) / 100) < 1e-9
    before = [(t - 5, 1.0, "no"), (t - 5, 99.0, "yes")]  # trades before placement never fill
    assert market_result(steps, before, "P1", "through", SimCosts())["kind"] == "none"
    at_price = [(t + 10, yb, "no")]  # touch only
    assert market_result(steps, at_price, "P1", "through", SimCosts())["kind"] == "none"
    assert market_result(steps, at_price, "P1", "touch", SimCosts())["kind"] == "yes only"


def test_report_runs_with_tape(data):
    cache, ds = data
    for f in cache.glob("KXBTC*.json.gz"):  # give every market a tape that fills both legs
        rec = json.loads(gzip.decompress(f.read_bytes()))
        steps = ds.markets.get(rec["market"]["ticker"])
        if not steps:
            continue
        st = _first(steps)
        t = st.ts.timestamp()
        rec["trades"] = [[t + 30, 1.0, 1, "n"], [t + 31, 99.0, 1, "y"]]
        f.write_bytes(gzip.compress(json.dumps(rec).encode()))
    rep = run_pair(cache, ds, holdout_days=3)
    p1 = next(r for r in rep["rows"] if r["variant"] == "P1" and r["rule"] == "through")
    assert p1["dev"]["both_filled"] == p1["dev"]["trades"] > 0 and p1["dev"]["ev_cents"] > 0
    assert "VERDICT" in format_pair(rep)
