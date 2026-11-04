"""Forward-test report: is fair value making money on markets it has never seen (paper trades)?

    docker exec kalshi-directional-bot python scripts/forward_report.py
    docker exec kalshi-directional-bot python scripts/forward_report.py --assets HYPE,DOGE --since 2026-11-05

Reads the bot's own CLOSED PAPER trades (after the real fees the bot paid on paper) and judges each coin with
rules fixed BEFORE the test started (never change them after seeing results):

  PASS            >= 14 days AND >= 150 markets AND the average after fees is > 0 with the whole 95% range > 0
  FAIL            the whole 95% range < 0, OR >= 28 days with an average <= 0
  KEEP TESTING    anything else (not enough evidence yet either way)

Only PASS is a reason to consider real money - and then start with the smallest size. Read-only.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.database.db import Database
from app.database.repository import Repository

MIN_DAYS, MIN_MARKETS, MAX_DAYS = 14, 150, 28


def boot(xs: list[float], n: int = 4000) -> tuple[float, float] | None:
    if len(xs) < 10:
        return None
    rng = random.Random(11)
    ms = sorted(sum(rng.choices(xs, k=len(xs))) / len(xs) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n) - 1]


def verdict(days: float, per_market: list[float]) -> str:
    n = len(per_market)
    avg = sum(per_market) / n if n else 0.0
    ci = boot(per_market)
    if ci and ci[1] < 0:
        return "❌ FAIL - losing money (the whole range is below zero)"
    if days >= MAX_DAYS and avg <= 0:
        return f"❌ FAIL - {MAX_DAYS}+ days without a positive average"
    if days >= MIN_DAYS and n >= MIN_MARKETS and ci and ci[0] > 0:
        return "✅ PASS - positive after fees and the range stays above zero (consider the smallest live size)"
    need = []
    if days < MIN_DAYS:
        need.append(f"{MIN_DAYS - days:.0f} more days")
    if n < MIN_MARKETS:
        need.append(f"{MIN_MARKETS - n} more markets")
    if ci and ci[0] <= 0 <= ci[1]:
        need.append("a range that clears zero")
    return "⏳ KEEP TESTING - needs " + ", ".join(need or ["more evidence"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", default="HYPE,DOGE")
    ap.add_argument("--since", help="YYYY-MM-DD (default: first paper trade in the database)")
    a = ap.parse_args()
    assets = [x.strip().upper() for x in a.assets.split(",") if x.strip()]
    since = datetime.fromisoformat(a.since).replace(tzinfo=UTC) if a.since else None
    repo = Repository(Database(Settings().database_url))
    rows = [p for p in repo.closed_positions(mode="paper", since=since, limit=1_000_000)
            if p.asset.upper() in assets and p.realized_pnl is not None]
    if not rows:
        print("No closed paper trades yet for " + ",".join(assets) + ". Let the bot run; check again in a few days.")
        return 0
    start = since or min(p.opened_at for p in rows)
    days = (datetime.now(UTC) - start).total_seconds() / 86400
    print(f"FORWARD TEST (paper) since {start:%Y-%m-%d} · {days:.1f} days · rules: PASS needs {MIN_DAYS}+ days, "
          f"{MIN_MARKETS}+ markets, 95% range above 0\n")
    for asset in assets:
        by_market: dict[str, list] = defaultdict(list)
        for p in rows:
            if p.asset.upper() == asset:
                by_market[p.ticker].append(p)
        if not by_market:
            print(f"{asset}: no closed trades yet\n")
            continue
        # cents per contract per MARKET (partial exits are combined), after every fee paid
        per_market, pnl_usd, wins, by_day = [], 0.0, 0, defaultdict(float)
        for ps in by_market.values():
            pnl = sum(p.realized_pnl or 0.0 for p in ps)
            contracts = sum(p.contracts for p in ps) or 1
            per_market.append(100 * pnl / contracts)
            pnl_usd += pnl
            wins += pnl > 0
            by_day[max(p.closed_at for p in ps if p.closed_at).strftime("%m-%d")] += pnl
        n = len(per_market)
        ci = boot(per_market)
        rng = f" (95% range {ci[0]:+.1f}..{ci[1]:+.1f}¢)" if ci else " (range needs 10+ markets)"
        print(f"{asset}: {n} markets · win {wins / n:.0%} · avg {sum(per_market) / n:+.2f}¢/contract after fees{rng}"
              f" · P&L ${pnl_usd:+.2f}")
        worst = min(by_day.items(), key=lambda kv: kv[1])
        best = max(by_day.items(), key=lambda kv: kv[1])
        print(f"   days traded {len(by_day)} · best day {best[0]} ${best[1]:+.2f} · worst day {worst[0]} "
              f"${worst[1]:+.2f}")
        print(f"   {verdict(days, per_market)}\n")
    print("Backtest expectation for comparison (mid-window, 6c edge): HYPE about +1..+3c, DOGE about +2..+3c per\n"
          "contract, ~40% win rate. Long losing streaks are normal at that win rate. Research only - nothing traded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
