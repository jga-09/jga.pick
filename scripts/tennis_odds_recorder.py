"""Record Kalshi tennis prices next to live bookmaker odds (forward test, research only).

    python scripts/tennis_odds_recorder.py            # run forever (use tmux), every ~4 h
    python scripts/tennis_odds_recorder.py --once     # one round, then exit
    python scripts/tennis_odds_recorder.py --report   # results so far

Needs a free API key from the-odds-api.com in .env as ODDS_API_KEY=... (never printed).
The free plan has a monthly request quota; the recorder spreads its requests over
the month automatically and stops calling the odds API when the quota runs low.
Kalshi is read through its public API. Nothing is traded.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from dotenv import dotenv_values

from app.config import Settings
from app.errors import KalshiError
from app.kalshi.client import KalshiClient
from app.research import tennis_live as tl

ROOT = Path(__file__).resolve().parents[1]
ODDS = "https://api.the-odds-api.com/v4"
SERIES = ("KXATPMATCH", "KXWTAMATCH")
RESERVE = 25  # never spend the last requests of the month


def odds_key() -> str:
    """From the environment or .env (read here so the bot's own settings need no change)."""
    key = os.environ.get("ODDS_API_KEY") or dotenv_values(ROOT / ".env").get("ODDS_API_KEY") or ""
    return key.strip()


def say(msg: str) -> None:
    print(f"{datetime.now(UTC):%Y-%m-%d %H:%M} {msg}", flush=True)


class OddsAPI:
    def __init__(self, key: str) -> None:
        self.key = key
        self.http = httpx.AsyncClient(timeout=30)
        self.remaining: float | None = None

    async def _get(self, path: str, **params: str) -> list:
        try:
            r = await self.http.get(f"{ODDS}{path}", params={"apiKey": self.key, **params})
        except httpx.HTTPError as exc:  # the URL holds the key: never print the exception text
            raise RuntimeError(f"odds API network error ({type(exc).__name__})") from None
        if r.headers.get("x-requests-remaining") is not None:
            try:
                self.remaining = float(r.headers["x-requests-remaining"])
            except ValueError:
                pass
        if r.status_code == 401:
            raise RuntimeError("odds API rejected the key (401) - check ODDS_API_KEY in .env")
        if r.status_code == 429:
            raise RuntimeError("odds API quota or rate limit reached (429)")
        if r.status_code >= 400:
            raise RuntimeError(f"odds API HTTP {r.status_code} on {path}")
        data = r.json()
        return data if isinstance(data, list) else []

    async def tennis_sports(self) -> list[str]:
        sports = await self._get("/sports")  # listing sports does not use quota
        return [s["key"] for s in sports if isinstance(s, dict) and s.get("active")
                and str(s.get("key", "")).startswith(("tennis_atp", "tennis_wta"))]

    async def odds(self, sport: str) -> list:
        return await self._get(f"/sports/{sport}/odds", regions="eu", markets="h2h", oddsFormat="decimal")

    async def close(self) -> None:
        await self.http.aclose()


def round_gap_hours(base: float, n_sports: int, remaining: float | None, now: datetime) -> float:
    """Stretch the interval so the monthly quota lasts until the month resets."""
    if remaining is None or n_sports == 0:
        return base
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    per_day = max(0.0, remaining - RESERVE) / days_left
    if per_day <= 0:
        return 24.0
    return max(base, 24 * n_sports / per_day)


async def one_round(s: Settings, odds: OddsAPI, client: KalshiClient, db) -> int:
    now = datetime.now(UTC)
    markets: list[dict] = []
    for ser in SERIES:
        try:
            markets += await client.get_markets(series_ticker=ser, status="open", limit=1000, max_pages=3)
        except KalshiError as exc:
            say(f"Kalshi {ser}: {type(exc).__name__}")
    sports = await odds.tennis_sports()
    if odds.remaining is not None and odds.remaining <= RESERVE + len(sports):
        say(f"odds quota low ({odds.remaining:.0f} left) - skipping odds this round")
        return len(sports)
    events: list = []
    for sp in sports:
        try:
            events += await odds.odds(sp)
        except RuntimeError as exc:
            say(str(exc))
            break
    rows = tl.pair(markets, events, now)
    tl.save(db, rows)
    sharp = sum(r.sharp_p is not None for r in rows)
    say(f"{len(markets)} Kalshi markets · {len(events)} odds events in {len(sports)} tournaments · "
        f"{len(rows) // 2} matches paired ({sharp // 2} with Pinnacle) · quota left "
        f"{'?' if odds.remaining is None else f'{odds.remaining:.0f}'}")
    settled = 0
    for t in tl.unsettled(db, now):
        try:
            m = await client.get_market(t)
        except KalshiError:
            continue
        if m.get("result") in ("yes", "no"):
            tl.save_result(db, t, m["result"], now)
            settled += 1
    if settled:
        say(f"recorded {settled} settled results")
    return len(sports)


def report(db) -> None:
    st = tl.status(db)
    print(f"Snapshots {st['snapshots']:,} · matches {st['matches']:,} · with Pinnacle {st['with_sharp']:,} · "
          f"settled markets {st['settled_markets']:,} · {st['first']} .. {st['last']}")
    b = tl.brier(db)
    if b["n"]:
        print(f"\nACCURACY at the last pre-match snapshot ({b['n']} markets, Brier, lower = better):")
        print(f"   Kalshi mid {b['kalshi']:.4f} · median bookmaker {b['median_book']:.4f}")
        if b["n_sharp"]:
            d = b["kalshi_minus_sharp"]
            print(f"   vs Pinnacle on {b['n_sharp']} markets: Kalshi is {'LESS' if d > 0 else 'MORE'} accurate "
                  f"by {abs(d):.4f}")
    for use, label in (("sharp", "Pinnacle no-vig"), ("median", "median of all bookmakers (weaker)")):
        tr = tl.trades(db, use)
        print(f"\nRULE (fixed in advance): back the side {label} says is worth >= {tl.MARGIN_C:.0f}c "
              "after fee + 1c slippage; first pre-match snapshot; one per match")
        if not tr:
            print("   0 trades yet")
            continue
        pnls = [t.pnl for t in tr]
        ci = tl.boot(pnls)
        wr = sum(t.won for t in tr) / len(tr)
        print(f"   {len(tr)} trades · WR {wr:.1%} · avg price {sum(t.price for t in tr) / len(tr):.0f}c · "
              f"EV {100 * sum(pnls) / len(pnls):+.2f}c" + (f" (95% {ci[0] * 100:+.1f} to {ci[1] * 100:+.1f}c)" if ci else ""))
        if len(tr) < tl.MIN_N:
            print(f"   ⚠️ INSUFFICIENT DATA: need {tl.MIN_N}+ trades before reading anything into this.")
        elif ci and ci[0] > 0:
            print("   ✅ Positive with the whole range above zero. Keep recording; still paper only.")
        elif sum(pnls) > 0:
            print("   🟡 Positive on average but could be luck. Keep recording.")
        else:
            print("   ❌ No edge after costs so far.")


async def main_async(a: argparse.Namespace) -> int:
    s = Settings()
    db = tl.connect(Path(a.db))
    if a.report:
        report(db)
        return 0
    key = odds_key()
    if not key:
        print("Missing ODDS_API_KEY. Get a free key at the-odds-api.com and add to .env:\n   ODDS_API_KEY=your_key")
        return 1
    odds = OddsAPI(key)
    client = KalshiClient(s)
    try:
        while True:
            try:
                n = await one_round(s, odds, client, db)
            except RuntimeError as exc:
                say(str(exc))
                n = 1
            if a.once:
                return 0
            gap = round_gap_hours(a.hours, n, odds.remaining, datetime.now(UTC))
            say(f"next round in {gap:.1f} h")
            await asyncio.sleep(gap * 3600)
    finally:
        await odds.close()
        await client.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / "data" / "tennis_live.db"))
    ap.add_argument("--hours", type=float, default=4.0, help="minimum hours between rounds (default 4)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx INFO lines would include the key in URLs
    try:
        return asyncio.run(main_async(a))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
