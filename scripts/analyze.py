"""Break down closed trades to see where the strategy wins and loses.

    python scripts/analyze.py            # paper trades (default)
    python scripts/analyze.py --mode live
    python scripts/analyze.py --days 7

Key idea: a contract bought at P cents needs a win rate above ~P% (plus fees)
to break even. "Edge" below = actual win rate - break-even win rate.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.trading.positions import Position  # noqa: E402
from app.utils.time import utcnow  # noqa: E402


def breakeven(p: Position) -> float:
    """Win rate (%) needed to break even on this trade, including fees."""
    cost = p.contracts * p.entry_price / 100 + p.entry_fee + p.exit_fee
    return cost / p.contracts * 100 if p.contracts else 0.0


def summarize(rows: list[Position]) -> str:
    n = len(rows)
    wins = sum(1 for p in rows if (p.realized_pnl or 0) > 0)
    wr = wins / n * 100
    be = sum(breakeven(p) for p in rows) / n
    pnl = sum(p.realized_pnl or 0 for p in rows)
    entry = sum(p.entry_price for p in rows) / n
    flag = "✅" if wr > be else "❌"
    return (f"{n:>4} trades  WR {wr:5.1f}%  avg entry {entry:4.1f}¢  need {be:5.1f}%  "
            f"edge {wr - be:+6.1f}  P&L {pnl:+8.2f} {flag}")


def table(title: str, rows: list[Position], key: Callable[[Position], str], order: list[str] | None = None) -> None:
    groups: dict[str, list[Position]] = {}
    for p in rows:
        groups.setdefault(key(p), []).append(p)
    print(f"\n── {title}")
    for k in order or sorted(groups):
        if k in groups:
            print(f"  {k:<12} {summarize(groups[k])}")


def price_bucket(p: Position) -> str:
    e = p.entry_price
    return "<40¢" if e < 40 else "40-54¢" if e < 55 else "55-69¢" if e < 70 else "70¢+"


def minutes_left(p: Position) -> str:
    if p.close_time is None:
        return "unknown"
    m = (p.close_time - p.opened_at).total_seconds() / 60
    return "<4 min" if m < 4 else "4-8 min" if m < 8 else "8+ min"


def conf_bucket(p: Position) -> str:
    c = p.confidence
    return "90+" if c >= 90 else f"{(c // 5) * 5}-{(c // 5) * 5 + 4}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="paper", choices=["paper", "live"])
    ap.add_argument("--days", type=int, default=None)
    args = ap.parse_args()

    settings = Settings()
    repo = Repository(Database(settings.database_url))
    since = utcnow() - timedelta(days=args.days) if args.days else None
    rows = repo.closed_positions(mode=args.mode, since=since, limit=1_000_000)
    if not rows:
        print("No closed trades yet.")
        return

    print(f"{args.mode.upper()} trades{f' (last {args.days}d)' if args.days else ''}")
    print(f"  ALL          {summarize(rows)}")
    table("By entry price", rows, price_bucket, ["<40¢", "40-54¢", "55-69¢", "70¢+"])
    table("By confidence", rows, conf_bucket)
    table("By time left at entry", rows, minutes_left, ["<4 min", "4-8 min", "8+ min", "unknown"])
    table("By side", rows, lambda p: f"{p.side.upper()} ({'UP' if p.side == 'yes' else 'DOWN'})")
    table("By asset", rows, lambda p: p.asset)
    table("By risk level", rows, lambda p: p.risk_level.upper())
    print("\nedge = win rate - break-even win rate. Positive edge is what makes money, not a high win rate.")
    if len(rows) < 100:
        print(f"⚠️  Only {len(rows)} trades - groups this small are mostly noise. Aim for 100+ before tuning.")


if __name__ == "__main__":
    main()
