"""Stress-test one strategy on the bot's REAL recorded data - no waiting.

    python scripts/test_strategy.py            # V4 (confirmation rule)
    python scripts/test_strategy.py --strategy V4L

The rule is fixed (nothing is fitted), so every recorded market is a fair test
case - with one caveat: V4 was *chosen* after seeing these same markets, so a
pass here is encouraging, not proof. Forward data is still the final judge.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.research.backtest import STRATEGIES, BacktestConfig  # noqa: E402
from app.research.reporting import stress_report  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="V4", choices=[s.key for s in STRATEGIES if s.fit is None])
    args = ap.parse_args()
    s = Settings()
    repo = Repository(Database(s.database_url))
    obs = repo.observations(source="live")
    cfg = BacktestConfig(fee_rate=s.fee_rate, slippage=s.paper_slippage_cents)
    print("REAL RECORDED DATA\n")
    print(stress_report(obs, args.strategy, cfg))


if __name__ == "__main__":
    main()
