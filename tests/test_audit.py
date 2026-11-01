"""Profitability audit: holdout separation, no-edge detection, planted-edge detection, paper records."""

from __future__ import annotations

import shutil
from datetime import timedelta

import pytest

from app.research.btc_audit import format_audit, paper_records, run_audit
from app.research.btc_backtest import load_dataset
from app.research.btc_synthetic import generate


@pytest.fixture(scope="module")
def efficient(tmp_path_factory):
    p = tmp_path_factory.mktemp("eff")
    generate(p, days=10, mode="efficient")
    return load_dataset(p)


@pytest.fixture(scope="module")
def lagged(tmp_path_factory):
    p = tmp_path_factory.mktemp("lag")
    generate(p, days=10, mode="lagged")
    return load_dataset(p)


def test_holdout_is_the_final_untouched_days(efficient):
    r = run_audit(efficient, holdout_days=3)
    days = efficient.days
    assert r["data"]["holdout_days"] == [days[-3], days[-1]]
    assert r["data"]["dev_days"][1] < r["data"]["holdout_days"][0]


def test_efficient_prices_show_no_edge_and_retain_baseline(efficient):
    r = run_audit(efficient, holdout_days=3)
    assert not any(d["decision"] == "ACCEPT" for d in r["decisions"])
    assert r["verdict"].startswith("NO DEMONSTRATED EDGE")
    b1 = r["results"]["dev"]["B1"]
    assert b1["trades"] >= 30 and b1["ev_cents"] < 0  # paying the ask on fair prices loses the costs
    assert "VERDICT" in format_audit(r)


def test_planted_edge_is_found(lagged):
    r = run_audit(lagged, holdout_days=3)
    ref = r["results"]["dev"]["REF"]
    assert ref["trades"] >= 30 and ref["ev_95"][0] > 0


def test_paper_records_read_only_and_deduplicated(tmp_path, runtime):
    from app.config import Settings
    from app.database.db import Database
    from app.database.repository import Repository
    from app.trading.positions import Position
    from app.utils.time import utcnow

    db = tmp_path / "bot.db"
    repo = Repository(Database(Settings(_env_file=None, database_url=f"sqlite:///{db}").database_url))
    repo.init()
    for i, (price, pnl) in enumerate(((72, -0.72), (60, 0.40), (55, 0.45))):
        p = Position(mode="paper", ticker=f"T{i}", event_ticker="E", asset="BTC", label="BTC 15M", side="yes",
                     contracts=1, entry_price=price, entry_fee=0.0, risk_level="low", client_order_id=f"c{i}",
                     close_time=utcnow() + timedelta(minutes=8))
        p.status, p.realized_pnl, p.close_reason, p.closed_at = "closed", pnl, "settled YES", utcnow()
        repo.save_position(p)
    backup = tmp_path / "bot.db.backup-1"
    shutil.copy(db, backup)
    out = paper_records([db, backup])
    assert out["trades"] == 3  # the backup's copies are not double counted
    assert out["overall"]["net_pnl"] == 0.13 and out["overall"]["sufficient"] is False
    assert {k: v["trades"] for k, v in out["segments"]["entry_price"].items()} == {"55-65c": 2, "65-75c": 1}
