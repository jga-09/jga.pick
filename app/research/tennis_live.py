"""Forward test: Kalshi tennis prices vs live bookmaker odds, recorded at the SAME moment.

The historical tennis tests could only compare Kalshi with odds from a different
time (or none). This records both sides together going forward, so nothing is
looked up from the future:

* every few hours: Kalshi's open ATP/WTA match-winner markets (public API) and
  bookmaker match odds from The Odds API (free plan, key in ODDS_API_KEY);
* matches are paired by player names; only snapshots taken BEFORE the scheduled
  start are kept;
* after the matches finish, Kalshi's own result is fetched.

The test (rules fixed now, before any data exists): the first pre-match snapshot
where the sharp no-vig probability says buying on Kalshi is worth >= 3c after the
fee and 1c slippage is a paper trade, one per match, held to Kalshi's result.
Pinnacle is used when present, otherwise the median no-vig probability of all
bookmakers in the snapshot (weaker; reported separately).
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import statistics
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.research.calibration import cost_cents
from app.utils.time import parse_ts

MARGIN_C = 3.0
MIN_N = 30
SHARP = "pinnacle"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snap (
    ts TEXT NOT NULL, event TEXT NOT NULL, ticker TEXT NOT NULL, player TEXT NOT NULL,
    commence TEXT NOT NULL, yes_bid REAL, yes_ask REAL, opp_bid REAL, opp_ask REAL,
    sharp_p REAL, median_p REAL, n_books INTEGER, books TEXT,
    PRIMARY KEY (ts, ticker)
);
CREATE TABLE IF NOT EXISTS result (ticker TEXT PRIMARY KEY, result TEXT NOT NULL, checked TEXT NOT NULL);
"""


# ------------------------------------------------------------------ names
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", s.replace("-", " "))).strip()


def name_keys(name: str) -> set[str]:
    """'Felix Auger-Aliassime' -> {'auger aliassime|f', 'aliassime|f'}; last names may have several words."""
    parts = _norm(name).split()
    if len(parts) < 2:
        return {f"{parts[0]}|"} if parts else set()
    first = parts[0][0]
    return {f"{' '.join(parts[i:])}|{first}" for i in range(1, len(parts))}


def same_player(a: str, b: str) -> bool:
    return bool(name_keys(a) & name_keys(b))


# ------------------------------------------------------------------ odds
def novig(price_a: float, price_b: float) -> float | None:
    """Decimal odds of A and B -> no-vig probability that A wins."""
    if price_a <= 1 or price_b <= 1:
        return None
    ia, ib = 1 / price_a, 1 / price_b
    return ia / (ia + ib)


def player_prob(event: dict[str, Any], player: str) -> tuple[float | None, float | None, int, dict[str, float]]:
    """(sharp p, median p, n books, per-book p) that ``player`` wins this odds-API event."""
    per_book: dict[str, float] = {}
    for bk in event.get("bookmakers") or []:
        for mk in bk.get("markets") or []:
            if mk.get("key") != "h2h":
                continue
            outs = mk.get("outcomes") or []
            if len(outs) != 2:
                continue
            me = [o for o in outs if same_player(player, str(o.get("name", "")))]
            if len(me) != 1:
                continue
            other = outs[1] if outs[0] is me[0] else outs[0]
            try:
                p = novig(float(me[0]["price"]), float(other["price"]))
            except (KeyError, TypeError, ValueError):
                p = None
            if p is not None:
                per_book[str(bk.get("key"))] = p
    if not per_book:
        return None, None, 0, {}
    return per_book.get(SHARP), statistics.median(per_book.values()), len(per_book), per_book


# ------------------------------------------------------------------ Kalshi
def _cents(v: Any) -> float | None:
    try:
        c = round(float(v) * 100, 2)
    except (TypeError, ValueError):
        return None
    return c if 0 < c < 100 else None


def kalshi_player(m: dict[str, Any]) -> str | None:
    for k in ("yes_sub_title", "subtitle"):
        if m.get(k):
            return str(m[k]).strip()
    mt = re.search(r"Will (.+?) win", m.get("title") or "")
    return mt.group(1).strip() if mt else None


def kalshi_matches(markets: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """event_ticker -> its two player markets (anything else is skipped)."""
    by_ev: dict[str, list[dict[str, Any]]] = {}
    for m in markets:
        if m.get("event_ticker") and kalshi_player(m):
            by_ev.setdefault(m["event_ticker"], []).append(m)
    return {e: ms for e, ms in by_ev.items() if len(ms) == 2}


@dataclass(frozen=True)
class Row:
    ts: str
    event: str
    ticker: str
    player: str
    commence: str
    yes_bid: float | None
    yes_ask: float | None
    opp_bid: float | None
    opp_ask: float | None
    sharp_p: float | None
    median_p: float | None
    n_books: int
    books: str


def pair(kalshi_markets: Iterable[dict[str, Any]], odds_events: Iterable[dict[str, Any]],
         now: datetime) -> list[Row]:
    """One row per Kalshi player market whose match is found in the odds feed and has not started."""
    ks = kalshi_matches(kalshi_markets)
    rows: list[Row] = []
    for ev in odds_events:
        commence = parse_ts(ev.get("commence_time"))
        if commence is None or commence <= now:
            continue  # started: in-play prices are a different game
        home, away = str(ev.get("home_team", "")), str(ev.get("away_team", ""))
        for e, (a, b) in ks.items():
            pa, pb = kalshi_player(a) or "", kalshi_player(b) or ""
            if not ((same_player(pa, home) and same_player(pb, away)) or
                    (same_player(pa, away) and same_player(pb, home))):
                continue
            for me, opp in ((a, b), (b, a)):
                sharp, med, n, per = player_prob(ev, kalshi_player(me) or "")
                if med is None:
                    continue
                rows.append(Row(now.isoformat(), e, me["ticker"], kalshi_player(me) or "", commence.isoformat(),
                                _cents(me.get("yes_bid_dollars")), _cents(me.get("yes_ask_dollars")),
                                _cents(opp.get("yes_bid_dollars")), _cents(opp.get("yes_ask_dollars")),
                                sharp, med, n, json.dumps({k: round(v, 4) for k, v in per.items()})))
            break
    return rows


# ------------------------------------------------------------------ storage
def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def save(db: sqlite3.Connection, rows: Sequence[Row]) -> int:
    db.executemany("INSERT OR IGNORE INTO snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   [tuple(r.__dict__.values()) for r in rows])
    db.commit()
    return len(rows)


def unsettled(db: sqlite3.Connection, now: datetime, after_start: timedelta = timedelta(hours=6)) -> list[str]:
    cut = (now - after_start).isoformat()
    q = ("SELECT DISTINCT s.ticker FROM snap s LEFT JOIN result r ON r.ticker = s.ticker "
         "WHERE r.ticker IS NULL AND s.commence < ?")
    return [t for (t,) in db.execute(q, (cut,))]


def save_result(db: sqlite3.Connection, ticker: str, result: str, now: datetime) -> None:
    db.execute("INSERT OR REPLACE INTO result VALUES (?,?,?)", (ticker, result, now.isoformat()))
    db.commit()


# ------------------------------------------------------------------ analysis
@dataclass(frozen=True)
class LiveTrade:
    event: str
    ticker: str
    ts: str
    side: str  # "yes on <player>"
    price: float
    p: float
    won: bool
    pnl: float  # dollars per contract


def best_buy(r: dict[str, Any], p: float, fee: float, slip: float) -> tuple[str, float, float, bool] | None:
    """Back this player (YES at ask / NO on opponent at 100 - bid) or the opponent; best model EV."""
    opts = []
    back_me = [x for x in (r["yes_ask"], None if r["opp_bid"] is None else 100 - r["opp_bid"]) if x]
    back_opp = [x for x in (r["opp_ask"], None if r["yes_bid"] is None else 100 - r["yes_bid"]) if x]
    if back_me:
        c = min(back_me)
        opts.append((f"back {r['player']}", c, p * 100 - c - cost_cents(c, fee, slip), True))
    if back_opp:
        c = min(back_opp)
        opts.append(("back opponent", c, (1 - p) * 100 - c - cost_cents(c, fee, slip), False))
    opts = [o for o in opts if 3 <= o[1] <= 97]
    return max(opts, key=lambda o: o[2]) if opts else None


def trades(db: sqlite3.Connection, use: str = "sharp", margin: float = MARGIN_C, fee: float = 0.07,
           slip: float = 1.0) -> list[LiveTrade]:
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT s.*, r.result FROM snap s JOIN result r ON r.ticker = s.ticker "
                      "WHERE s.ts < s.commence ORDER BY s.ts").fetchall()
    db.row_factory = None
    done: set[str] = set()
    out = []
    for r in rows:
        if r["event"] in done or r["result"] not in ("yes", "no"):
            continue
        p = r["sharp_p"] if use == "sharp" else r["median_p"]
        if p is None:
            continue
        b = best_buy(dict(r), p, fee, slip)
        if b is None or b[2] < margin:
            continue
        side, price, _, back_me = b
        won = (r["result"] == "yes") == back_me
        cost = cost_cents(price, fee, slip)
        out.append(LiveTrade(r["event"], r["ticker"], r["ts"], side, price, p if back_me else 1 - p, won,
                             ((100 if won else 0) - price - cost) / 100))
        done.add(r["event"])
    return out


def boot(pnls: Sequence[float], n: int = 3000, seed: int = 3) -> tuple[float, float] | None:
    if len(pnls) < 10:
        return None
    rng = random.Random(seed)
    ms = sorted(sum(rng.choices(pnls, k=len(pnls))) / len(pnls) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n) - 1]


def brier(db: sqlite3.Connection) -> dict[str, Any]:
    """Accuracy at the LAST pre-match snapshot per market: Kalshi mid vs sharp vs median book."""
    q = ("SELECT s.ticker, s.yes_bid, s.yes_ask, s.sharp_p, s.median_p, r.result FROM snap s "
         "JOIN result r ON r.ticker = s.ticker WHERE s.ts < s.commence ORDER BY s.ts")
    last: dict[str, tuple] = {}
    for row in db.execute(q):
        last[row[0]] = row
    k, sh, md = [], [], []
    for _, bid, ask, sp, mp, res in last.values():
        if bid is None or ask is None or res not in ("yes", "no"):
            continue
        y = 1.0 if res == "yes" else 0.0
        k.append(((bid + ask) / 200 - y) ** 2)
        md.append((mp - y) ** 2)
        if sp is not None:
            sh.append(((bid + ask) / 200 - y) ** 2 - (sp - y) ** 2)
    return {"n": len(k), "kalshi": statistics.fmean(k) if k else None,
            "median_book": statistics.fmean(md) if md else None,
            "n_sharp": len(sh), "kalshi_minus_sharp": statistics.fmean(sh) if sh else None}


def status(db: sqlite3.Connection) -> dict[str, Any]:
    one = db.execute("SELECT COUNT(*), COUNT(DISTINCT event), MIN(ts), MAX(ts), "
                     "SUM(sharp_p IS NOT NULL) FROM snap").fetchone()
    settled = db.execute("SELECT COUNT(*) FROM result").fetchone()[0]
    return {"snapshots": one[0], "matches": one[1], "first": one[2], "last": one[3], "with_sharp": one[4] or 0,
            "settled_markets": settled}
