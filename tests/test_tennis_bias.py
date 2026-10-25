"""Kalshi-only tennis bias test on synthetic records (no network)."""

import random
from datetime import UTC, date, datetime, time, timedelta

from app.research.tennis_bias import (
    Rule,
    apply,
    build_matches,
    decision_time,
    is_winner_series,
    quote_at,
    run,
    ticker_day,
)

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _rec(series, event, ticker, result, candles):
    return {"series": series, "market": {"ticker": ticker, "event_ticker": event, "result": result},
            "candles": candles}


def make(n_days=60, per_day=20, bias=0.0, seed=1):
    """Fair world (bias=0): fav wins with prob = its price. bias>0: favourites win more than priced."""
    rng = random.Random(seed)
    recs = []
    d0 = date(2026, 6, 1)
    for d in range(n_days):
        day = d0 + timedelta(days=d)
        ts = datetime.combine(day - timedelta(days=1), time(19), UTC).timestamp()
        for k in range(per_day):
            p = rng.uniform(0.5, 0.95)
            fav_wins = rng.random() < min(0.99, p + bias)
            c = round(p * 100)
            ev = f"KXATPMATCH-{day:%y}{MONTHS[day.month - 1]}{day:%d}P{k}"
            recs.append(_rec("KXATPMATCH", ev, ev + "-A", "yes" if fav_wins else "no", [[ts, c - 1, c + 1]]))
            recs.append(_rec("KXATPMATCH", ev, ev + "-B", "no" if fav_wins else "yes",
                             [[ts, 100 - c - 1, 100 - c + 1]]))
    return recs


def test_helpers():
    assert ticker_day("KXATPMATCH-26AUG30SAMMAC") == date(2026, 8, 30)
    assert is_winner_series("KXWTAMATCH") and is_winner_series("KXATPMATCH")
    assert not is_winner_series("KXATPEXACTMATCH") and not is_winner_series("KXWTABLETENNISMATCH")
    t = decision_time(date(2026, 8, 30))
    assert quote_at([[t.timestamp() - 3600, 40, 42]], t) == (40, 42)
    assert quote_at([[t.timestamp() - 7 * 3600, 40, 42]], t) == (None, None)  # too old
    assert quote_at([[t.timestamp() + 60, 40, 42]], t) == (None, None)  # after decision time: never used


def test_exact_score_and_unclean_markets_skipped():
    recs = make(n_days=1, per_day=2)
    recs.append(_rec("KXATPEXACTMATCH", "KXATPEXACTMATCH-26JUN01X", "KXATPEXACTMATCH-26JUN01X-A30", "yes", []))
    recs.append(_rec("KXATPMATCH", "KXATPMATCH-26JUN01Y", "KXATPMATCH-26JUN01Y-A", "yes", []))
    matches, skipped = build_matches(recs)
    assert len(matches) == 2 and skipped.get("not a clean two-player market") == 1


def test_fair_world_shows_no_edge():
    matches, _ = build_matches(make(bias=0.0))
    r = run(matches)
    assert not r["verdict"].startswith("POSSIBLE EDGE")


def test_biased_world_is_found_out_of_sample():
    matches, _ = build_matches(make(bias=0.08, seed=2))
    r = run(matches)
    assert r["pick"] is not None and r["pick"][0].buy_fav
    assert r["verdict"].startswith("POSSIBLE EDGE")


def test_one_trade_per_match_cheapest_entry():
    matches, _ = build_matches(make(n_days=2, per_day=5))
    trades = apply(Rule(True, 50, 97), matches, 0.07, 1.0)
    assert len({t.match.event for t in trades}) == len(trades)
    m = matches[0]
    assert m.entry(True) == min(m.fav.ask, 100 - m.dog.bid)
