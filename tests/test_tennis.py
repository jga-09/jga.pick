import gzip
import io
import json
import random
import zipfile
from datetime import date, timedelta

from app.research.tennis_download import is_match_series as is_match_series_ok
from app.research.tennis import (
    OddsRow, decision_time, join, load_kalshi, load_odds, read_xlsx, run_tennis, same_player,
)


def test_same_player():
    assert same_player("Jannik Sinner", "Sinner J.")
    assert same_player("Alex de Minaur", "De Minaur A.")
    assert same_player("Felix Auger-Aliassime", "Auger-Aliassime F.")
    assert same_player("Roberto Bautista Agut", "Bautista Agut R.")
    assert same_player("Iga Świątek", "Swiatek I.")
    assert same_player("Wu Yibing", "Wu Y.")
    assert not same_player("Jannik Sinner", "Alcaraz C.")
    assert not same_player("Jack Draper", "Sinner J.")
    assert not same_player("Sinner", "Sinner")


def _xlsx(rows):
    def cell(ref, v):
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    body = "".join(f'<row r="{i + 1}">' + "".join(cell(f"{chr(65 + j)}{i + 1}", v) for j, v in enumerate(r)) + "</row>"
                   for i, r in enumerate(rows))
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             f"{body}</sheetData></worksheet>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_read_xlsx_and_odds(tmp_path):
    data = _xlsx([["Date", "Winner", "Loser", "PSW", "PSL", "Comment"],
                  [46315, "Sinner J.", "Alcaraz C.", 1.8, 2.1, "Completed"]])
    rows = read_xlsx(data)
    assert rows[0]["Winner"] == "Sinner J." and rows[0]["PSW"] == 1.8
    (tmp_path / "2026_atp.xlsx").write_bytes(data)
    (o,) = load_odds(tmp_path)
    assert o.day == date(2026, 10, 20) and abs(o.p_winner - (1 / 1.8) / (1 / 1.8 + 1 / 2.1)) < 1e-12


def test_series_filter():
    assert is_match_series_ok({"ticker": "KXATPMATCH", "title": "ATP Tennis Match"})
    assert not is_match_series_ok({"ticker": "KXATPDOUBLES", "title": "ATP doubles match"})
    assert not is_match_series_ok({"ticker": "KXNFLGAME", "title": "NFL game"})
    assert not is_match_series_ok({"ticker": "KXATPCHALLENGERMATCH", "title": "ATP Challenger match"})


def _word(i):
    out = ""
    i += 26 * 27
    while i:
        i, r = divmod(i, 26)
        out = chr(97 + r) + out
    return out.capitalize()


def _write_market(mdir, ev, name, result, quotes):
    m = {"ticker": f"{ev}-{name.split()[-1].upper()}", "event_ticker": ev, "result": result, "yes_sub_title": name,
         "close_time": "2026-10-21T00:00:00Z"}
    (mdir / f"{m['ticker']}.json.gz").write_bytes(gzip.compress(json.dumps({"market": m, "candles": quotes}).encode()))


def _synthetic(tmp_path, lag: bool, n=400, seed=1):
    rng = random.Random(seed)
    mdir = tmp_path / "markets"
    mdir.mkdir()
    odds = []
    start = date(2026, 5, 1)
    for i in range(n):
        d = start + timedelta(days=i // 3)
        p_true = rng.uniform(0.2, 0.8)
        a_wins = rng.random() < p_true
        # Kalshi mid: efficient = the truth; lagging = shrunk most of the way to 50c (stale)
        mid = 100 * (0.5 + (p_true - 0.5) * (0.4 if lag else 1.0)) + rng.gauss(0, 1)
        t = decision_time(d).timestamp() - 3600
        sa, sb = _word(2 * i), _word(2 * i + 1)
        ev = f"KXATPMATCH-{d:%y}{d.strftime('%b').upper()}{d:%d}{sa.upper()}"
        _write_market(mdir, ev, f"Ann {sa}", "yes" if a_wins else "no", [[t, round(mid - 1), round(mid + 1)]])
        _write_market(mdir, ev, f"Bob {sb}", "no" if a_wins else "yes", [[t, round(99 - mid), round(101 - mid)]])
        ta, tb = f"{sa} A.", f"{sb} B."
        win, lose, pw = (ta, tb, p_true) if a_wins else (tb, ta, 1 - p_true)
        odds.append(OddsRow(d, "atp", win, lose, 1 / pw, 1 / (1 - pw), "Completed"))
    return load_kalshi(mdir), odds


def test_join_pairs_names(tmp_path):
    ms, odds = _synthetic(tmp_path, lag=False, n=40)
    js = join(ms, odds)
    assert len(js) == 40 and all(j.winner_agrees for j in js)


def test_efficient_kalshi_is_dead(tmp_path):
    ms, odds = _synthetic(tmp_path, lag=False)
    r = run_tennis(ms, odds)
    assert r["data"]["winner_agreement"] == [400, 400]
    assert r["rows"][0]["decision"].startswith("DEAD")


def test_lagging_kalshi_is_flagged(tmp_path):
    ms, odds = _synthetic(tmp_path, lag=True)
    r = run_tennis(ms, odds)
    assert r["rows"][0]["decision"].startswith("WORTH A FORWARD TEST")
    assert "SUPPORTED" not in r["rows"][0]["decision"]
