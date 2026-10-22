"""Favourite-maker test: placement on the favourite, conservative vs optimistic fills, no look-back."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.kalshi.market_data import TradePrint
from app.research.btc_backtest import load_dataset
from app.research.btc_favmaker import Order, filled, format_favmaker, place, run_favmaker
from app.research.btc_synthetic import generate


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    p = tmp_path_factory.mktemp("fav")
    generate(p, days=9, mode="efficient")
    return load_dataset(p)


def _t(st, secs, yes_price, taker):
    return TradePrint("x", yes_price, 1, taker, st.ts + timedelta(seconds=secs))


def test_places_on_the_favourite_at_its_bid(ds):
    steps = next(v for v in ds.markets.values() if any(s.yes_mid and s.yes_mid >= 65 for s in v))
    o = place(steps, 60, 95)
    assert o is not None and o.side == ("yes" if o.step.yes_mid >= 50 else "no")
    assert o.price == (o.step.yes_bid if o.side == "yes" else o.step.no_bid)


def test_fill_rules(ds):
    st = next(iter(ds.markets.values()))[0]
    o = Order(st, "yes", 70.0)
    before = [_t(st, -30, 60, "no")]  # trades before placement never count
    assert not filled(o, before, "through") and not filled(o, before, "touch")
    at_price = [_t(st, 30, 70, "no")]  # a seller at our price: only the optimistic rule fills
    assert not filled(o, at_price, "through") and filled(o, at_price, "touch")
    through = [_t(st, 30, 69, "yes")]  # printed below our bid: every order at 70 was taken
    assert filled(o, through, "through")
    o_no = Order(st, "no", 70.0)  # NO bid 70 == YES price 30
    assert filled(o_no, [_t(st, 30, 31, "yes")], "through")
    assert not filled(o_no, [_t(st, 30, 30, "yes")], "through") and filled(o_no, [_t(st, 30, 30, "yes")], "touch")


def test_report_runs_and_needs_samples(ds):
    rep = run_favmaker(ds, {}, holdout_days=3)  # no trade tape -> nothing fills -> not supported
    assert rep["verdict"].startswith("NOT SUPPORTED")
    assert all(r["dev"]["trades"] == 0 for r in rep["rows"])
    assert "VERDICT" in format_favmaker(rep)
