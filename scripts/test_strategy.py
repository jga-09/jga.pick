"""Stress-test one strategy on the bot's REAL recorded data - no waiting.

    python scripts/test_strategy.py            # V4 (confirmation rule)
    python scripts/test_strategy.py --strategy V3

The rule is fixed (nothing is fitted), so every recorded market is a fair test
case - with one caveat: V4 was *chosen* after seeing these same markets, so a
pass here is encouraging, not proof. Forward data is still the final judge.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.research.backtest import STRATEGIES, BacktestConfig, fmt_metrics, metrics, simulate  # noqa: E402


def bootstrap_ev(pnls: list[float], n: int = 5000, seed: int = 1) -> tuple[float, float, float]:
    """95% bootstrap interval for mean P&L per trade, and P(mean <= 0)."""
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(pnls, k=len(pnls))) / len(pnls) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)], sum(1 for m in means if m <= 0) / n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="V4", choices=[s.key for s in STRATEGIES if s.fit is None])
    args = ap.parse_args()
    s = Settings()
    repo = Repository(Database(s.database_url))
    obs = repo.observations(source="live")
    cfg = BacktestConfig(fee_rate=s.fee_rate, slippage=s.paper_slippage_cents)
    strat = next(x for x in STRATEGIES if x.key == args.strategy)
    base = next(x for x in STRATEGIES if x.key == "V0")
    trades = simulate(obs, strat.select, {}, cfg)
    n_markets = len({o.ticker for o in obs})
    print(f"{strat.key} {strat.name} on {n_markets} settled markets (real recorded data)\n")
    if len(trades) < 20:
        print(f"⚠️ INSUFFICIENT DATA: only {len(trades)} trades. Let the bot record more markets.")
        return
    print(f"ALL            {fmt_metrics(metrics(trades))}")
    print(f"baseline V0    {fmt_metrics(metrics(simulate(obs, base.select, {}, cfg)))}")
    lo, hi, p_neg = bootstrap_ev([t.pnl for t in trades])
    print(f"\nEV per contract 95% bootstrap range: {lo * 100:+.1f}¢ to {hi * 100:+.1f}¢  "
          f"(chance the true EV is ≤ 0: {p_neg:.1%})")

    def split(title: str, key) -> None:  # type: ignore[no-untyped-def]
        groups: dict[str, list] = defaultdict(list)
        for t in trades:
            groups[key(t)].append(t)
        print(f"\n{title}")
        for k in sorted(groups):
            print(f"  {k:<12} {fmt_metrics(metrics(groups[k]))}")

    ordered = sorted(trades, key=lambda t: t.ts)
    half = ordered[len(ordered) // 2].ts
    split("STABILITY OVER TIME (first vs second half)", lambda t: "1st half" if t.ts < half else "2nd half")
    split("BY ASSET", lambda t: t.obs.asset)
    split("BY SIDE", lambda t: "YES (UP)" if t.side == "yes" else "NO (DOWN)")
    split("BY TIME LEFT AT ENTRY", lambda t: t.obs.time_bucket + " min")
    split("BY ENTRY PRICE", lambda t: "<50¢" if t.price < 50 else "50-70¢" if t.price < 70 else "70-85¢"
          if t.price < 85 else "85¢+")

    halves = defaultdict(list)
    for t in trades:
        halves[t.ts < half].append(t.pnl)
    both_positive = all(sum(v) > 0 for v in halves.values())
    print("\nVERDICT")
    if lo > 0 and both_positive:
        print("✅ Positive across the whole bootstrap range and in both halves. Promising - confirm on new data.")
    elif p_neg < 0.2 and both_positive:
        print("🟡 Leaning positive but not conclusive. Keep paper-testing forward before trusting it.")
    else:
        print("❌ Not robust: the result could easily be luck (or one half carries it). Do not rely on it.")
    print("Note: this rule was picked after seeing this data, so treat a pass as encouraging, not proof.")


if __name__ == "__main__":
    main()
