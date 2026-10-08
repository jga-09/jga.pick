"""SYNTHETIC simulation: checks the research machinery is honest. NOT market evidence.

    python scripts/simulate.py                      # both worlds, default sizes
    python scripts/simulate.py --world efficient --markets 1000

Two worlds (see app/research/simulator.py):
  efficient - no exploitable edge exists; a correct system finds none.
  edge      - a known, built-in edge exists; a correct system finds it out-of-sample.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.backtest import BacktestConfig, fmt_metrics  # noqa: E402
from app.research.reporting import research_report  # noqa: E402
from app.research.simrun import collect_observations, paper_simulation  # noqa: E402
from app.utils.logging import setup_logging  # noqa: E402

BANNER = "=" * 100
EV_GATE = "lower"


def run_world(world: str, n_obs: int, n_paper: int) -> None:
    print(f"\n{BANNER}\n🧪 SYNTHETIC WORLD: {world.upper()}  (not real market data)\n{BANNER}")
    t = time.time()
    obs = collect_observations(world, n_obs, seed=7)
    print(f"[research data: {n_obs} simulated markets, {time.time() - t:.0f}s]")
    print(research_report(obs, BacktestConfig(ev_gate=EV_GATE)))
    t = time.time()
    print(f"\nPAPER-TRADING SIMULATION on {n_paper} FRESH markets (model refit every 50 markets "
          f"on settled history only; one trade per market per profile) [{world}]")
    results = paper_simulation(world, n_paper, seed=11, ev_gate=EV_GATE)
    for lvl, r in results.items():
        print(f"  {lvl.title:<6} per-contract: {fmt_metrics(r.contract_metrics)}")
        m = r.metrics
        if m.trades:
            print(f"         sized $:      P&L {m.pnl:+.2f}  maxDD {m.max_drawdown:.2f}  EV/trade {m.ev:+.3f}")
        grades = ", ".join(f"{g}: {len(v)}" for g, v in sorted(r.grades.items()))
        print(f"         grades traded: {grades or '—'}")
        top = sorted(r.rejections.items(), key=lambda kv: -kv[1])[:4]
        print("         top rejections: " + "; ".join(f"{k} ({v})" for k, v in top))
    print(f"[paper simulation {time.time() - t:.0f}s]")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", choices=["efficient", "edge", "both"], default="both")
    ap.add_argument("--markets", type=int, default=2000, help="markets for the research dataset")
    ap.add_argument("--paper-markets", type=int, default=1500, help="fresh markets for the paper simulation")
    ap.add_argument("--ev-gate", choices=["lower", "point"], default="lower")
    args = ap.parse_args()
    global EV_GATE
    EV_GATE = args.ev_gate
    setup_logging("WARNING")
    print("⚠️  SYNTHETIC SIMULATION. Edges here exist only because the simulator puts them there.")
    print("    These results validate the machinery; they are NOT evidence about real Kalshi markets.")
    for w in (["efficient", "edge"] if args.world == "both" else [args.world]):
        run_world(w, args.markets, args.paper_markets)


if __name__ == "__main__":
    main()
