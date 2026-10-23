"""Hammer/Drift/Surge/Fade test: rules fire as specified, conflict rule, no-edge and planted-edge checks."""

from __future__ import annotations

import pytest

from app.research.btc_backtest import load_dataset
from app.research.btc_four import format_four, run_four, signals
from app.research.btc_synthetic import generate


@pytest.fixture(scope="module")
def lagged(tmp_path_factory):
    p = tmp_path_factory.mktemp("lag4")
    generate(p, days=9, mode="lagged")
    return p, load_dataset(p)


def test_signals_follow_the_rules(lagged):
    _, ds = lagged
    fired = [(st, s) for steps in ds.markets.values() for st in steps for s in [signals(st)] if s]
    assert fired
    for st, s in fired:
        for name, (side, price) in s.items():
            p = (st.estimate.p_yes if side == "yes" else 1 - st.estimate.p_yes) * 100
            if name in ("hammer", "fade", "surge"):
                assert p - price >= 5.0  # min edge at the ask
            if name == "fade":
                assert price <= 35.0
            if name == "surge":
                assert "drift" in s and s["drift"][0] == side


def test_planted_edge_found_and_report_formats(lagged):
    cache, ds = lagged
    rep = run_four(cache, ds, holdout_days=3)
    hammer = next(r for r in rep["rows"] if r["strategy"] == "hammer")
    assert hammer["dev"]["trades"] >= 30 and hammer["dev"]["ev_cents"] > 0
    assert "VERDICT" in format_four(rep)


def test_efficient_prices_not_supported(tmp_path):
    generate(tmp_path, days=9, mode="efficient")
    rep = run_four(tmp_path, load_dataset(tmp_path), holdout_days=3)
    assert rep["verdict"].startswith("NOT SUPPORTED")
