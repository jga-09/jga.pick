"""Profitability audit of the BTC 15-minute strategy.

    python scripts/btc_audit.py                       # uses data/replay_cache + the bot database
    python scripts/btc_audit.py --holdout-days 7 --risk low --slippage 1

Reads REAL cached markets (scripts/replay.py) and REAL closed paper positions (the bot database and
its backups, read-only). Writes data/audit_report.md and data/audit_report.json. Changes nothing in
the bot's configuration; live trading is not touched.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.research.btc_audit import format_audit, run_audit  # noqa: E402
from app.research.btc_backtest import SimCosts, load_dataset  # noqa: E402
from app.risk.profiles import RiskLevel  # noqa: E402
from app.strategy.btc_model import ModelConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def bot_databases() -> list[Path]:
    url = Settings().database_url
    if not url.startswith("sqlite:///"):
        return []
    db = Path(url.split("sqlite:///", 1)[1])
    db = db if db.is_absolute() else ROOT / db
    return [p for p in [db, *sorted(db.parent.glob(db.name + ".backup-*"))] if p.is_file()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--out", default=str(ROOT / "data" / "audit_report"))
    ap.add_argument("--holdout-days", type=int, default=7, help="final days kept untouched until the end")
    ap.add_argument("--risk", default="low", choices=[r.value for r in RiskLevel])
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--slippage", type=float, default=1.0)
    ap.add_argument("--no-paper", action="store_true", help="skip paper-trading records")
    args = ap.parse_args()
    cache = Path(args.cache)
    if not cache.exists() or not any(cache.glob("KXBTC*.json.gz")):
        print(f"No BTC market history in {cache}.\nRun first:  python scripts/replay.py --days 30")
        return 1
    print("Loading and replaying cached BTC markets (only data available at each minute is used)...")
    ds = load_dataset(cache, ModelConfig())
    dbs = [] if args.no_paper else bot_databases()
    try:
        report = run_audit(ds, SimCosts(args.fee_rate, args.slippage), args.holdout_days, RiskLevel(args.risk), dbs)
    except ValueError as exc:
        print(f"Not enough data: {exc}. Download more days with scripts/replay.py.")
        return 1
    text = format_audit(report)
    print()
    print(text)
    out = Path(args.out)
    out.with_suffix(".md").write_text(text)
    out.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\nSaved: {out.with_suffix('.md')} and {out.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
