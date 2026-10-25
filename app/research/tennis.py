"""Kalshi tennis match-winner markets vs the sharp bookmaker line (Pinnacle).

Question: are Kalshi's pre-match tennis prices far enough from the sharp line to profit after fees?

Data (all free, all real):
* Kalshi (public API): settled ATP/WTA match markets (one per player, YES = that player wins), hourly quotes.
* tennis-data.co.uk: every ATP/WTA tour match with Pinnacle CLOSING odds (PSW/PSL = winner/loser).

IMPORTANT LIMITATION, stated up front: Pinnacle closing odds are the price at match start. Our Kalshi
quote is taken EARLIER (D-1 20:00 UTC, before any match on day D anywhere in the world). News between
the two (injury, withdrawal) is in the closing line but was not knowable at our decision time. So:

* P1 "closing-line gap" (UPPER BOUND, uses information from the future): buy on Kalshi when the
  Pinnacle no-vig probability gives expected value >= 5c after 1c slippage and the taker fee. If this
  loses, the strategy is DEAD - even knowing the later sharp price was not enough. If it wins, it is
  NOT proof: it only justifies a forward paper test with LIVE Pinnacle odds.
* P0 "Kalshi favourite" (honest, uses only the Kalshi quote): buy the side priced >= 50c. Baseline.

Rules (fixed before results): one trade per match, held to settlement on Kalshi's own result; price
3..97c; quote <= 6 h old; walkovers skipped (no pre-match odds). Development matches, then the LAST 21
DAYS untouched; bootstrap 95% intervals; 30-trade minimum.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import random
import re
import unicodedata
import zipfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from app.research.btc_backtest import SimCosts, fee_per_contract
from app.utils.time import parse_ts

MIN_N = 30
MIN_EV = 5.0
MAX_QUOTE_AGE = timedelta(hours=6)
HOLDOUT_DAYS = 21
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
                                       "OCT", "NOV", "DEC"), 1)}


# ------------------------------------------------------------------ names
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", s.replace("-", " ")).strip()


def same_player(kalshi_name: str, td_name: str) -> bool:
    """'Alex de Minaur' vs 'De Minaur A.'; 'Felix Auger-Aliassime' vs 'Auger-Aliassime F.'."""
    toks = _norm(kalshi_name).split()
    parts = (td_name or "").strip().rsplit(" ", 1)  # surname(s) + initials token ('J.', 'J.L.')
    if not toks or len(parts) != 2 or "." not in parts[1]:
        return False
    surname = _norm(parts[0]).replace(" ", "")
    initial = _norm(parts[1])[:1]
    full = "".join(toks)
    if surname and full.endswith(surname) and toks[0][:1] == initial:
        return True
    # surname-first names ('Wu Yibing' on Kalshi, 'Wu Y.' in the odds file)
    return bool(surname) and full.startswith(surname) and len(toks) > 1 and toks[-1][:1] == initial


# ------------------------------------------------------------------ odds files
def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):  # type: ignore[union-attr]
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(data: bytes) -> list[dict[str, Any]]:
    """Minimal .xlsx reader (first sheet, header row) - avoids a new dependency."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    z = zipfile.ZipFile(io.BytesIO(data))
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
    sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
    rows: list[list[Any]] = []
    for row in ElementTree.fromstring(z.read(sheet)).iter(f"{{{ns['m']}}}row"):
        vals: dict[int, Any] = {}
        for c in row.findall("m:c", ns):
            t, v = c.get("t"), c.find("m:v", ns)
            if t == "s" and v is not None:
                val: Any = shared[int(v.text or 0)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
            elif v is not None and v.text is not None:
                try:
                    val = float(v.text)
                except ValueError:
                    val = v.text
            else:
                continue
            vals[_col(c.get("r", "A"))] = val
        if vals:
            rows.append([vals.get(i) for i in range(max(vals) + 1)])
    if not rows:
        return []
    head = [str(h).strip() if h is not None else "" for h in rows[0]]
    return [{h: (r[i] if i < len(r) else None) for i, h in enumerate(head) if h} for r in rows[1:]]


def _date(v: Any) -> date | None:
    if isinstance(v, (int, float)):
        return date(1899, 12, 30) + timedelta(days=int(v))
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _odds(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 1.0 else None


@dataclass(frozen=True)
class OddsRow:
    day: date
    tour: str
    winner: str
    loser: str
    psw: float | None
    psl: float | None
    comment: str

    @property
    def p_winner(self) -> float | None:
        """Pinnacle closing probability of the eventual winner, margin removed."""
        if self.psw is None or self.psl is None:
            return None
        a, b = 1 / self.psw, 1 / self.psl
        return a / (a + b)


def parse_odds_rows(rows: Sequence[dict[str, Any]], tour: str) -> list[OddsRow]:
    out = []
    for r in rows:
        d = _date(r.get("Date"))
        if d is None or not r.get("Winner") or not r.get("Loser"):
            continue
        out.append(OddsRow(d, tour, str(r["Winner"]).strip(), str(r["Loser"]).strip(), _odds(r.get("PSW")),
                           _odds(r.get("PSL")), str(r.get("Comment") or "").strip()))
    return out


def load_odds(td_dir: Path) -> list[OddsRow]:
    out: list[OddsRow] = []
    for f in sorted(td_dir.glob("*")):
        tour = "wta" if "wta" in f.name.lower() else "atp"
        try:
            if f.suffix == ".xlsx":
                rows = read_xlsx(f.read_bytes())
            elif f.suffix == ".csv":
                rows = list(csv.DictReader(io.StringIO(f.read_text(encoding="utf-8", errors="replace"))))
            else:
                continue
        except (OSError, zipfile.BadZipFile, ElementTree.ParseError, IndexError):
            continue
        out.extend(parse_odds_rows(rows, tour))
    return out


# ------------------------------------------------------------------ Kalshi matches
@dataclass
class KMarket:
    ticker: str
    player: str
    result: str
    quotes: list[tuple[float, float | None, float | None]]

    def quote(self, at: datetime) -> tuple[float | None, float | None]:
        t, best = at.timestamp(), None
        for q in self.quotes:
            if q[0] <= t:
                best = q
            else:
                break
        if best is None or t - best[0] > MAX_QUOTE_AGE.total_seconds():
            return None, None
        return best[1], best[2]


@dataclass
class KMatch:
    event: str
    approx_day: date
    markets: list[KMarket]


def player_name(m: dict[str, Any]) -> str | None:
    for k in ("yes_sub_title", "subtitle"):
        if m.get(k):
            return str(m[k]).strip()
    mt = re.search(r"Will (.+?) win", m.get("title") or "")
    return mt.group(1).strip() if mt else None


def ticker_day(ticker: str) -> date | None:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def load_kalshi(mdir: Path) -> list[KMatch]:
    by_event: dict[str, list[tuple[dict[str, Any], list]]] = defaultdict(list)
    for f in sorted(mdir.glob("*.json.gz")):
        try:
            rec = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        m = rec.get("market") or {}
        if m.get("result") in ("yes", "no") and m.get("event_ticker"):
            by_event[m["event_ticker"]].append((m, rec.get("candles") or []))
    out = []
    for ev, items in by_event.items():
        mk = []
        for m, candles in items:
            name = player_name(m)
            if name:
                mk.append(KMarket(m["ticker"], name, m["result"], sorted((float(q[0]), q[1], q[2]) for q in candles)))
        if len(mk) != 2 or sum(x.result == "yes" for x in mk) != 1:
            continue  # not a clean two-player match market
        close = parse_ts(items[0][0].get("close_time"))
        day = ticker_day(ev) or (close.date() if close else None)
        if day:
            out.append(KMatch(ev, day, mk))
    return out


@dataclass
class Joined:
    match: KMatch
    odds: OddsRow
    winner_agrees: bool


def join(matches: Sequence[KMatch], odds: Sequence[OddsRow]) -> list[Joined]:
    by_day: dict[date, list[OddsRow]] = defaultdict(list)
    for o in odds:
        by_day[o.day].append(o)
    out = []
    for km in matches:
        a, b = km.markets
        hit = None
        for dd in (0, -1, 1, -2, 2):
            for o in by_day.get(km.approx_day + timedelta(days=dd), []):
                if ((same_player(a.player, o.winner) and same_player(b.player, o.loser))
                        or (same_player(b.player, o.winner) and same_player(a.player, o.loser))):
                    hit = o
                    break
            if hit:
                break
        if hit:
            k_winner = a if a.result == "yes" else b
            out.append(Joined(km, hit, same_player(k_winner.player, hit.winner)))
    return out


def decision_time(d: date) -> datetime:
    return datetime.combine(d - timedelta(days=1), time(20), UTC)


# ------------------------------------------------------------------ simulation
@dataclass(frozen=True)
class TTrade:
    day: date
    event: str
    side_desc: str
    price: float
    cost: float
    won: bool

    @property
    def net(self) -> float:
        return ((100.0 if self.won else 0.0) - self.cost) / 100


def _options(j: Joined, costs: SimCosts) -> list[tuple[str, float, float, bool, KMarket, bool]]:
    """(desc, price, cost, wins, market, is_yes) for every buyable side at the decision time."""
    at = decision_time(j.odds.day)
    out = []
    for m in j.match.markets:
        bid, ask = m.quote(at)
        won_yes = m.result == "yes"
        for is_yes, price in ((True, ask), (False, None if bid is None else 100 - bid)):
            if price is None or not 3 <= price <= 97:
                continue
            fill = min(99.0, price + costs.slippage_cents)
            out.append((f"{'YES' if is_yes else 'NO'} {m.player}", price, fill + fee_per_contract(fill, costs),
                        won_yes == is_yes, m, is_yes))
    return out


def p_pin(j: Joined, player: str) -> float | None:
    pw = j.odds.p_winner
    if pw is None:
        return None
    return pw if same_player(player, j.odds.winner) else 1 - pw


def trade_p1(j: Joined, costs: SimCosts) -> TTrade | None:
    best = None
    for desc, price, cost, won, m, is_yes in _options(j, costs):
        p = p_pin(j, m.player)
        if p is None:
            return None
        ev = (p if is_yes else 1 - p) * 100 - cost
        if ev >= MIN_EV and (best is None or ev > best[0]):
            best = (ev, TTrade(j.odds.day, j.match.event, desc, price, cost, won))
    return best[1] if best else None


def trade_p0(j: Joined, costs: SimCosts) -> TTrade | None:
    yes = [o for o in _options(j, costs) if o[5] and o[1] >= 50]
    if not yes:
        return None
    desc, price, cost, won, _, _ = max(yes, key=lambda o: o[1])
    return TTrade(j.odds.day, j.match.event, desc, price, cost, won)


def _boot(xs: Sequence[float], n: int = 3000, seed: int = 5) -> tuple[float, float] | None:
    if len(xs) < 10:
        return None
    rng = random.Random(seed)
    means = sorted(100 * sum(rng.choices(xs, k=len(xs))) / len(xs) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def summarize(trades: Sequence[TTrade]) -> dict[str, Any]:
    n = len(trades)
    if not n:
        return {"trades": 0, "net_pnl": 0.0, "ev_cents": None, "ev_95": None}
    nets = [t.net for t in sorted(trades, key=lambda t: t.day)]
    eq = peak = dd = 0.0
    for x in nets:
        eq += x
        peak, dd = max(peak, eq), max(dd, peak - eq)
    return {"trades": n, "win_rate": sum(t.won for t in trades) / n, "avg_cost": sum(t.cost for t in trades) / n,
            "ev_cents": 100 * sum(nets) / n, "ev_95": _boot(nets), "net_pnl": round(sum(nets), 2),
            "max_drawdown": round(dd, 2)}


def run_tennis(matches: Sequence[KMatch], odds: Sequence[OddsRow], holdout_days: int = HOLDOUT_DAYS,
               costs: SimCosts | None = None) -> dict[str, Any]:
    costs = costs or SimCosts()
    joined = [j for j in join(matches, odds) if "walkover" not in j.odds.comment.lower()]
    priced = [j for j in joined if j.odds.p_winner is not None and _options(j, costs)]
    if len(priced) < MIN_N:
        raise ValueError(f"only {len(priced)} Kalshi matches could be paired with Pinnacle odds and a pre-match quote")
    days = sorted({j.odds.day for j in priced})
    hold_from = days[-1] - timedelta(days=holdout_days - 1)
    dev = [j for j in priced if j.odds.day < hold_from]
    hold = [j for j in priced if j.odds.day >= hold_from]
    # accuracy on development matches: Kalshi mid (first player) vs Pinnacle closing (no-vig)
    bk = bp = 0.0
    gaps: list[float] = []
    for j in dev:
        m = j.match.markets[0]
        bid, ask = m.quote(decision_time(j.odds.day))
        p = p_pin(j, m.player)
        if bid is None or ask is None or p is None:
            continue
        o = 1.0 if m.result == "yes" else 0.0
        mid = (bid + ask) / 200
        bk += (mid - o) ** 2
        bp += (p - o) ** 2
        gaps.append(abs(mid - p) * 100)
    acc = ({"n": len(gaps), "kalshi": bk / len(gaps), "pinnacle_close": bp / len(gaps),
            "mean_gap_c": sum(gaps) / len(gaps), "share_gap_over_5c": sum(g > 5 for g in gaps) / len(gaps)}
           if gaps else {"n": 0})
    rows = []
    for key, label, fn in (("P1", "closing-line gap (UPPER BOUND - uses the later closing price)", trade_p1),
                           ("P0", "buy the Kalshi favourite (honest baseline, no outside data)", trade_p0)):
        td = [t for j in dev if (t := fn(j, costs))]
        th = [t for j in hold if (t := fn(j, costs))]
        sd, sh = summarize(td), summarize(th)
        passed = sd["trades"] >= MIN_N and sd["ev_95"] is not None and sd["ev_95"][0] > 0 and sh["net_pnl"] > 0
        if key == "P1":
            dec = ("WORTH A FORWARD TEST ONLY (not proof: closing odds were not known at entry)" if passed else
                   "DEAD (even the later sharp price did not beat Kalshi's costs)")
        else:
            dec = "SUPPORTED (paper-test next)" if passed else "NOT SUPPORTED"
        rows.append({"variant": key, "label": label, "dev": sd, "holdout": sh, "decision": dec})
    p1 = rows[0]["decision"]
    if p1.startswith("DEAD"):
        verdict = ("NOT SUPPORTED: Kalshi's pre-match tennis prices were not far enough from Pinnacle's closing line "
                   "to profit after fees, even using that later price. A live-odds strategy cannot do better. "
                   "Retain the baseline (no trading).")
    else:
        verdict = ("INCONCLUSIVE BUT ALIVE: against Pinnacle's CLOSING line Kalshi was mispriced by more than its costs. "
                   "Part of that may be news that arrived after our entry time, so this is not proof. Next step: a "
                   "forward PAPER test comparing Kalshi with LIVE Pinnacle odds. Real money stays off.")
    agree = sum(j.winner_agrees for j in joined)
    return {"label": "KALSHI TENNIS vs PINNACLE (pre-match; real Kalshi quotes; free closing odds)",
            "data": {"kalshi_matches": len(matches), "paired_with_odds": len(joined),
                     "with_pinnacle_and_quote": len(priced), "winner_agreement": [agree, len(joined)],
                     "dev": [days[0].isoformat(), (hold_from - timedelta(days=1)).isoformat()],
                     "holdout": [hold_from.isoformat(), days[-1].isoformat()],
                     "by_tour": {t: sum(j.odds.tour == t for j in priced) for t in ("atp", "wta")}},
            "accuracy": acc, "rows": rows, "verdict": verdict}


def format_tennis(r: dict[str, Any]) -> str:
    d = r["data"]
    ag, n = d["winner_agreement"]
    L = [r["label"],
         f"Kalshi matches {d['kalshi_matches']} · paired with tennis-data {d['paired_with_odds']} · with Pinnacle odds "
         f"AND a Kalshi quote at D-1 20:00 UTC {d['with_pinnacle_and_quote']} (ATP {d['by_tour']['atp']}, "
         f"WTA {d['by_tour']['wta']})",
         f"Development {d['dev'][0]}..{d['dev'][1]}; untouched holdout {d['holdout'][0]}..{d['holdout'][1]}."]
    if n:
        warn = "" if ag / n >= 0.97 else "  <-- WARNING: pairing looks wrong; results unreliable"
        L.append(f"Winner check (Kalshi result vs tennis-data): {ag}/{n} agree ({ag / n * 100:.1f}%){warn}")
    a = r["accuracy"]
    if a.get("n"):
        L.append(f"Accuracy on {a['n']} development matches (Brier, lower = better): Kalshi at entry {a['kalshi']:.4f} "
                 f"vs Pinnacle closing {a['pinnacle_close']:.4f} · average gap {a['mean_gap_c']:.1f}c · gap > 5c in "
                 f"{a['share_gap_over_5c'] * 100:.0f}% of matches")
    L.append("")
    for x in r["rows"]:
        s, h = x["dev"], x["holdout"]
        L.append(f"{x['variant']} {x['label']}")
        if not s["trades"]:
            L.append(f"   dev 0 trades | holdout {h['trades']} trades net {h['net_pnl']:+.2f}$  -> {x['decision']}")
            continue
        ci = f" (95% {s['ev_95'][0]:+.1f}..{s['ev_95'][1]:+.1f})" if s["ev_95"] else ""
        L.append(f"   dev {s['trades']} trades · win {s['win_rate'] * 100:.1f}% · avg cost {s['avg_cost']:.1f}c · "
                 f"EV {s['ev_cents']:+.2f}c{ci} · net {s['net_pnl']:+.2f}$ · maxDD {s['max_drawdown']:.2f}$")
        L.append(f"   holdout {h['trades']} trades net {h['net_pnl']:+.2f}$"
                 + (f" (EV {h['ev_cents']:+.2f}c)" if h["trades"] else "") + f"  -> {x['decision']}")
    L += ["", "VERDICT: " + r["verdict"], "Probabilities are ESTIMATES, not guarantees."]
    return "\n".join(L)
