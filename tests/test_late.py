"""Late-entry test: fresh/seen split by date, window limits respected, verdict logic."""

from __future__ import annotations

from datetime import datetime

from app.research.btc_backtest import load_dataset
from app.research.btc_late import format_late, run_late
from app.research.btc_synthetic import generate


def test_split_window_and_verdict(tmp_path):
    from datetime import UTC
    generate(tmp_path, days=6, mode="lagged", start=datetime(2026, 10, 9, tzinfo=UTC))
    ds = load_dataset(tmp_path)
    rep = run_late(ds, fresh_after="2026-10-11")
    assert rep["data"]["markets_fresh"] > 0 and rep["data"]["markets_seen"] > 0
    l3 = next(r for r in rep["rows"] if r["key"] == "L3")
    ctrl = next(r for r in rep["rows"] if r["key"] == "ALL")
    assert ctrl["fresh"]["trades"] >= l3["fresh"]["trades"]
    assert "VERDICT" in format_late(rep)


def test_no_fresh_data_is_reported(tmp_path):
    from datetime import UTC
    generate(tmp_path, days=3, mode="efficient", start=datetime(2026, 9, 1, tzinfo=UTC))
    rep = run_late(load_dataset(tmp_path), fresh_after="2026-10-12")
    assert rep["verdict"].startswith("NO FRESH DATA")
