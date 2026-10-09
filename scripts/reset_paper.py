"""Reset the PAPER trading balance and history.

    python scripts/reset_paper.py                 # reset to PAPER_STARTING_BALANCE
    python scripts/reset_paper.py --balance 500   # also set a new starting balance

Stop the bot first. A backup of the database is saved next to it. Deletes only
paper positions/trades/orders/daily P&L/trade notes. Research data
(observations, signals) and settings are kept, and live data is never touched.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.repository import Repository  # noqa: E402


def bot_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-f", "app.main"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def set_env(key: str, value: str) -> None:
    env = ROOT / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{key}=")] + [f"{key}={value}"]
    env.write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=None, help="new PAPER_STARTING_BALANCE")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()
    if bot_running():
        print("⚠️  The bot is running. Stop it first:\n   tmux kill-session -t bot; pkill -f app.main")
        sys.exit(1)
    s = Settings()
    url = s.database_url
    if url.startswith("sqlite:///"):
        db_file = Path(url.split("sqlite:///", 1)[1])
        db_file = db_file if db_file.is_absolute() else ROOT / db_file
        if db_file.exists():
            backup = db_file.with_name(f"{db_file.name}.backup-{datetime.now():%Y%m%d-%H%M%S}")
            shutil.copy2(db_file, backup)
            print(f"Backup saved: {backup}")
    repo = Repository(Database(url))
    repo.init()
    n_closed = repo.count_closed("paper")
    n_open = len(repo.open_positions("paper"))
    new_bal = args.balance if args.balance is not None else s.paper_starting_balance
    print(f"This deletes {n_closed} closed and {n_open} open PAPER positions and resets the balance to "
          f"${new_bal:,.2f}.\nRecorded setups (research data) are kept.")
    if not args.yes and input("Type RESET to continue: ").strip() != "RESET":
        print("Cancelled - nothing changed.")
        return
    counts = repo.reset_paper()
    if args.balance is not None:
        set_env("PAPER_STARTING_BALANCE", f"{args.balance:g}")
    print("✅ Paper trading reset:", ", ".join(f"{k} {v}" for k, v in counts.items()))
    print(f"Balance is now ${new_bal:,.2f}. Start the bot again: ~/jga.pick/scripts/start.sh")


if __name__ == "__main__":
    main()
