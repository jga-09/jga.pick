"""Kalshi-only tennis test: are Kalshi's pre-match prices systematically off?

No outside odds needed. For every settled two-player match (two markets, one per
player, exactly one settles YES) take the pre-match quote and ask: when Kalshi
priced the favourite at X cents, did the favourite win X% of the time? A known
pattern in betting markets ("favourite-longshot bias") is that longshots are
overpriced; if that gap is bigger than Kalshi's fee + slippage, buying one side
in some price range could be profitable.

Rules fixed before looking at results:
* quote = latest hourly candle at or before D-1 20:00 UTC (D = match day in the
  ticker), at most 6 h old - before any match on day D starts anywhere;
* entry 3..97c; one trade per match, held to Kalshi's own result;
* cheaper of: YES on the chosen player at the ask, or NO on the opponent at 100 - bid;
* the LAST 21 DAYS are a holdout: a rule is picked on the earlier days only
  (>= 30 trades, positive EV after costs), then judged once on the holdout.
"""

from __future__ import annotations

import gzip
import json
import random
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from app.research.calibration import cost_cents
from app.research.stats import mean, wilson

MIN_N = 30
HOLDOUT_DAYS = 21
MAX_QUOTE_AGE = timedelta(hours=6)
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
                                       "OCT", "NOV", "DEC"), 1)}
# favourite-mid ranges tested (fixed grid, not tuned)
RANGES = ((50, 60), (60, 70), (70, 80), (80, 90), (90, 97), (50, 70), (70, 97), (50, 97))


def ticker_day(ticker: str) -> date | None:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def is_winner_series(series: str) -> bool:
    """Match-winner markets only (exact-score, tiebreak, set markets are different questions)."""
    s = series.upper()
    return ("ATP" in s or "WTA" in s) and s.endswith("MATCH") and not any(
        w in s for w in ("EXACT", "TIEBREAK", "TABLETENNIS", "SET", "DOUBLES", "CHALLENGER", "ITF"))


def decision_time(d: date) -> datetime:
    return datetime.combine(d - timedelta(days=1), time(20), UTC)


@dataclass(frozen=True)
class Side:
    ticker: str
    won: bool
    bid: float | None  # cents, at decision time
    ask: float | None


@dataclass(frozen=True)
class Match:
    event: str
    tour: str
    day: date
    fav: Side
    dog: Side

    @property
    def fav_mid(self) -> float:
        return (self.fav.bid + self.fav.ask) / 2  # type: ignore[operator]

    def entry(self, buy_fav: bool) -> float | None:
        """Cheapest way to back one player: YES on them at ask, or NO on the opponent at 100 - bid."""
        me, other = (self.fav, self.dog) if buy_fav else (self.dog, self.fav)
        prices = [p for p in (me.ask, None if other.bid is None else 100 - other.bid) if p is not None]
        prices = [p for p in prices if 3 <= p <= 97]
        return min(prices) if prices else None


def quote_at(candles: Sequence[Sequence[Any]], at: datetime) -> tuple[float | None, float | None]:
    t, best = at.timestamp(), None
    for q in sorted(candles, key=lambda q: float(q[0])):
        if float(q[0]) <= t:
            best = q
        else:
            break
    if best is None or t - float(best[0]) > MAX_QUOTE_AGE.total_seconds():
        return None, None
    return best[1], best[2]


def build_matches(records: Iterable[dict[str, Any]]) -> tuple[list[Match], dict[str, int]]:
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped: dict[str, int] = defaultdict(int)
    for rec in records:
        m = rec.get("market") or {}
        if not is_winner_series(str(rec.get("series", ""))):
            continue
        if m.get("result") in ("yes", "no") and m.get("event_ticker"):
            by_event[m["event_ticker"]].append(rec)
    out = []
    for ev, recs in by_event.items():
        if len(recs) != 2 or sum(r["market"]["result"] == "yes" for r in recs) != 1:
            skipped["not a clean two-player market"] += 1
            continue
        day = ticker_day(ev)
        if day is None:
            skipped["no match date in ticker"] += 1
            continue
        sides = []
        for r in recs:
            bid, ask = quote_at(r.get("candles") or [], decision_time(day))
            sides.append(Side(r["market"]["ticker"], r["market"]["result"] == "yes", bid, ask))
        a, b = sides
        if None in (a.bid, a.ask, b.bid, b.ask) or not (a.bid or b.bid):
            skipped["no two-sided pre-match quote"] += 1
            continue
        fav, dog = (a, b) if (a.bid + a.ask) >= (b.bid + b.ask) else (b, a)  # type: ignore[operator]
        tour = "wta" if "WTA" in str(recs[0].get("series", "")).upper() else "atp"
        out.append(Match(ev, tour, day, fav, dog))
    return sorted(out, key=lambda x: x.day), dict(skipped)


def load_records(mdir: Path) -> list[dict[str, Any]]:
    out = []
    for f in sorted(mdir.glob("*.json.gz")):
        try:
            out.append(json.loads(gzip.decompress(f.read_bytes())))
        except (OSError, ValueError):
            continue
    return out


# ------------------------------------------------------------------ trading
@dataclass(frozen=True)
class Trade:
    match: Match
    buy_fav: bool
    price: float
    pnl: float  # dollars per contract after costs

    @property
    def won(self) -> bool:
        return (self.match.fav if self.buy_fav else self.match.dog).won


@dataclass(frozen=True)
class Rule:
    buy_fav: bool
    lo: float
    hi: float

    @property
    def name(self) -> str:
        return f"buy {'favourite' if self.buy_fav else 'underdog'} when favourite is {self.lo:.0f}-{self.hi:.0f}c"


RULES = [Rule(f, lo, hi) for f in (True, False) for lo, hi in RANGES]


def trade(m: Match, buy_fav: bool, fee_rate: float, slippage: float) -> Trade | None:
    price = m.entry(buy_fav)
    if price is None:
        return None
    won = (m.fav if buy_fav else m.dog).won
    return Trade(m, buy_fav, price, ((100 if won else 0) - price - cost_cents(price, fee_rate, slippage)) / 100)


def apply(rule: Rule, matches: Sequence[Match], fee_rate: float, slippage: float) -> list[Trade]:
    out = []
    for m in matches:
        if rule.lo <= m.fav_mid < rule.hi:
            t = trade(m, rule.buy_fav, fee_rate, slippage)
            if t:
                out.append(t)
    return out


def bootstrap(pnls: Sequence[float], n: int = 3000, seed: int = 5) -> tuple[float, float] | None:
    if len(pnls) < 10:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(pnls, k=len(pnls))) / len(pnls) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def summary(trades: Sequence[Trade]) -> dict[str, Any]:
    pnls = [t.pnl for t in trades]
    wins = sum(t.won for t in trades)
    return {"trades": len(trades), "win_rate": wins / len(trades) if trades else None,
            "avg_price": mean([t.price for t in trades]) if trades else None,
            "ev_c": 100 * mean(pnls) if trades else None, "total": sum(pnls), "ev_95": bootstrap(pnls)}


def calibration(matches: Sequence[Match], fee_rate: float, slippage: float) -> list[dict[str, Any]]:
    rows = []
    for lo, hi in RANGES[:5]:
        ms = [m for m in matches if lo <= m.fav_mid < hi]
        if not ms:
            continue
        wins = sum(m.fav.won for m in ms)
        mid = mean([m.fav_mid for m in ms])
        wr = wins / len(ms)
        cost = cost_cents(mid, fee_rate, slippage)
        rows.append({"range": f"{lo}-{hi}c", "n": len(ms), "avg_mid": mid, "fav_win": wr,
                     "ci": wilson(wins, len(ms)), "gap_c": 100 * wr - mid, "cost_c": cost})
    return rows


def run(matches: Sequence[Match], fee_rate: float = 0.07, slippage: float = 1.0,
        holdout_days: int = HOLDOUT_DAYS) -> dict[str, Any]:
    if len(matches) < MIN_N:
        raise ValueError(f"only {len(matches)} matches with a pre-match quote")
    last = matches[-1].day
    hold_from = last - timedelta(days=holdout_days - 1)
    dev = [m for m in matches if m.day < hold_from]
    hold = [m for m in matches if m.day >= hold_from]
    table = []
    for r in RULES:
        table.append((r, summary(apply(r, dev, fee_rate, slippage)), summary(apply(r, hold, fee_rate, slippage))))
    eligible = [(r, d, h) for r, d, h in table if d["trades"] >= MIN_N and (d["ev_c"] or 0) > 0]
    pick = max(eligible, key=lambda x: x[1]["ev_c"]) if eligible else None
    if pick is None:
        verdict = ("NO EDGE: no rule was profitable after costs on the development days, so nothing is traded. "
                   "Kalshi's tennis prices look fair enough that fees eat any bias.")
    else:
        _, d, h = pick
        lo95 = d["ev_95"][0] if d["ev_95"] else None
        if h["trades"] >= MIN_N and (h["ev_c"] or 0) > 0 and lo95 is not None and lo95 > 0:
            verdict = "POSSIBLE EDGE: profitable on development AND untouched holdout days. Next: paper-test it live."
        elif (h["ev_c"] or 0) > 0:
            verdict = ("WEAK: the chosen rule was positive on the holdout but the evidence is thin "
                       f"({h['trades']} holdout trades). Collect more days before trusting it.")
        else:
            verdict = "NO EDGE: the rule picked on development days lost on the untouched holdout days (it was luck)."
    return {"matches": len(matches), "dev": (dev[0].day if dev else None, hold_from - timedelta(days=1)),
            "holdout": (hold_from, last), "n_dev": len(dev), "n_hold": len(hold),
            "by_tour": {t: sum(m.tour == t for m in matches) for t in ("atp", "wta")},
            "calibration_dev": calibration(dev, fee_rate, slippage), "table": table,
            "pick": pick, "verdict": verdict}
