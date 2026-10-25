"""Tennis live-odds recorder: pairing, no-vig maths, analysis, quota, key safety (no network)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.research import tennis_live as tl

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


def _k(ev, ticker, player, bid, ask):
    return {"event_ticker": ev, "ticker": ticker, "yes_sub_title": player,
            "yes_bid_dollars": f"{bid / 100:.4f}", "yes_ask_dollars": f"{ask / 100:.4f}"}


def _o(home, away, ph, pa, start, books=("pinnacle", "bet365")):
    return {"home_team": home, "away_team": away, "commence_time": start.isoformat().replace("+00:00", "Z"),
            "bookmakers": [{"key": b, "markets": [{"key": "h2h", "outcomes": [
                {"name": home, "price": ph}, {"name": away, "price": pa}]}]} for b in books]}


def test_names_and_novig():
    assert tl.same_player("Felix Auger-Aliassime", "Félix Auger Aliassime")
    assert tl.same_player("Alex de Minaur", "Alex De Minaur")
    assert not tl.same_player("Alexander Zverev", "Mischa Zverev")
    assert abs(tl.novig(1.5, 2.6) - (1 / 1.5) / (1 / 1.5 + 1 / 2.6)) < 1e-12
    assert tl.novig(1.0, 2.0) is None


def test_pair_skips_started_matches_and_reads_both_sides():
    km = [_k("E1", "E1-SIN", "Jannik Sinner", 70, 72), _k("E1", "E1-RUN", "Holger Rune", 28, 30),
          _k("E2", "E2-A", "Iga Swiatek", 60, 62), _k("E2", "E2-B", "Coco Gauff", 38, 40)]
    ev = [_o("Holger Rune", "Jannik Sinner", 3.2, 1.38, NOW + timedelta(hours=5)),
          _o("Iga Swiatek", "Coco Gauff", 1.6, 2.4, NOW - timedelta(minutes=1))]  # already started
    rows = tl.pair(km, ev, NOW)
    assert {r.ticker for r in rows} == {"E1-SIN", "E1-RUN"}
    sin = next(r for r in rows if r.ticker == "E1-SIN")
    assert sin.sharp_p and sin.sharp_p > 0.65 and sin.n_books == 2 and sin.opp_bid == 28


def test_trades_and_report_from_db(tmp_path):
    db = tl.connect(tmp_path / "t.db")
    rows = []
    names = [f"{a} {b}" for a in ("Anna", "Bea", "Cleo", "Dana") for b in ("Ober", "Pils", "Quinn", "Rossi",
                                                                            "Stahl", "Tuck", "Ulm", "Voss", "Weber", "Yost")]
    for i in range(40):  # Kalshi underprices the favourite at 60c vs sharp ~70%: a known edge
        a, b = names[i], "Zed Zorn"
        km = [_k(f"E{i}", f"E{i}-A", a, 59, 60), _k(f"E{i}", f"E{i}-B", b, 40, 41)]
        rows += tl.pair(km, [_o(a, b, 1.40, 3.30, NOW + timedelta(hours=3))], NOW)
    tl.save(db, rows)
    assert tl.unsettled(db, NOW) == []  # not finished yet
    later = NOW + timedelta(hours=12)
    for i in range(40):
        a_won = i % 10 < 7
        tl.save_result(db, f"E{i}-A", "yes" if a_won else "no", later)
        tl.save_result(db, f"E{i}-B", "no" if a_won else "yes", later)
    tr = tl.trades(db, "sharp")
    assert len(tr) == 40 and all(t.side != "back opponent" for t in tr)
    assert sum(t.pnl for t in tr) > 0
    b = tl.brier(db)
    assert b["n"] == 80 and b["n_sharp"] == 80
    assert tl.status(db)["matches"] == 40


def test_quota_spreading():
    import scripts.tennis_odds_recorder as rec

    d = datetime(2026, 10, 2, tzinfo=UTC)  # 30 days left
    assert rec.round_gap_hours(4, 3, None, d) == 4
    assert rec.round_gap_hours(4, 3, 500, d) == pytest.approx(24 * 3 / ((500 - 25) / 30))
    assert rec.round_gap_hours(4, 3, 20, d) == 24.0


async def test_api_key_never_appears_in_errors():
    import scripts.tennis_odds_recorder as rec

    def boom(req):
        raise httpx.ConnectError("failed for " + str(req.url))

    api = rec.OddsAPI("SECRETKEY123")
    api.http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    with pytest.raises(RuntimeError) as e:
        await api.tennis_sports()
    assert "SECRETKEY123" not in str(e.value) and e.value.__cause__ is None
    api.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(RuntimeError) as e:
        await api.odds("tennis_atp_paris")
    assert "SECRETKEY123" not in str(e.value)
    await api.close()


