"""Research report on the bot's REAL recorded observations (data/bot.db by default).

    python scripts/research.py
    python scripts/research.py --days 30 --unit days --size 3

Every number comes from markets the bot actually observed and that have settled.
Anything below HIST_MIN_SAMPLES distinct markets is reported as insufficient.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.research.backtest import BacktestConfig  # noqa: E402
from app.research.reporting import research_report  # noqa: E402
from app.utils.time import utcnow  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="only use the last N days")
    ap.add_argument("--unit", choices=["markets", "days"], default="markets", help="walk-forward chunk unit")
    ap.add_argument("--size", type=int, default=None, help="chunk size (markets or days)")
    args = ap.parse_args()
    s = Settings()
    repo = Repository(Database(s.database_url))
    repo.init()
    since = utcnow() - timedelta(days=args.days) if args.days else None
    obs = repo.observations(source="live", since=since)
    cfg = BacktestConfig(fee_rate=s.fee_rate, slippage=s.paper_slippage_cents, min_samples=s.hist_min_samples,
                         prior_strength=s.calibration_prior_strength)
    print("REAL DATA RESEARCH REPORT (paper-mode observations recorded by the bot)\n")
    print(research_report(obs, cfg, args.unit, args.size))


if __name__ == "__main__":
    main()
