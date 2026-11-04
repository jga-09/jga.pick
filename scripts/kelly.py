"""Kelly bet-size calculator for Kalshi contracts (binary: pay P, receive $1 if right).

    python scripts/kelly.py --win-rate 0.60 --price 50
    python scripts/kelly.py --win-rate 0.80 --price 75 --true-win-rate 0.76

Full Kelly for a contract bought at price c (in dollars, fee included) with win probability p:
        f* = (p - c) / (1 - c)            (1:1 bets are the special case c = 0.50  ->  f* = p - q)
It simulates 1,000 trades from $100 at full, half and quarter Kelly - and, with --true-win-rate, what happens when
the win rate you BELIEVE is higher than the real one (the usual way Kelly blows up an account).
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.trading.fees import estimate_fee


def kelly(p: float, price_cents: float, fee_rate: float = 0.07, contracts: int = 10) -> float:
    c = price_cents / 100 + estimate_fee(contracts, price_cents, fee_rate) / contracts
    return max(0.0, (p - c) / (1 - c)) if c < 1 else 0.0


def simulate(p_true: float, price_cents: float, frac: float, trades: int, seed: int) -> tuple[float, float]:
    """(final balance median, share of runs that lost 90%+) over 300 runs, starting at $100."""
    c = price_cents / 100 + estimate_fee(10, price_cents, 0.07) / 10
    finals, ruined = [], 0
    for r in range(300):
        rng = random.Random(seed + r)
        bal = 100.0
        for _ in range(trades):
            stake = bal * frac
            n = stake / c
            bal += n * (1 - c) if rng.random() < p_true else -stake
            if bal < 0.01:
                bal = 0.0
                break
        finals.append(bal)
        ruined += bal < 10
    finals.sort()
    return finals[len(finals) // 2], ruined / 300


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--win-rate", type=float, required=True, help="the win rate you believe, e.g. 0.60")
    ap.add_argument("--price", type=float, required=True, help="price you pay in cents, e.g. 50")
    ap.add_argument("--true-win-rate", type=float, default=None, help="the real win rate (default = believed)")
    ap.add_argument("--trades", type=int, default=1000)
    a = ap.parse_args()
    p_true = a.true_win_rate if a.true_win_rate is not None else a.win_rate
    f = kelly(a.win_rate, a.price)
    be = a.price / 100 + estimate_fee(10, a.price, 0.07) / 10
    print(f"Price {a.price:.0f}c -> you need a {be:.1%} win rate just to break even (fee included).")
    print(f"Believed win rate {a.win_rate:.0%} -> full Kelly bet = {f:.1%} of bankroll per trade.")
    if f == 0:
        print("Kelly says: bet NOTHING. A win rate at or below the break-even rate is not an edge.")
        return 0
    print(f"\n{a.trades} trades from $100, real win rate {p_true:.0%} (median of 300 runs · chance of losing 90%+):")
    for label, mult in (("full Kelly", 1.0), ("half Kelly", 0.5), ("quarter Kelly", 0.25)):
        med, ruin = simulate(p_true, a.price, f * mult, a.trades, seed=11)
        print(f"   {label:<14} bet {f * mult:5.1%}: median ${med:,.2f} · ruin risk {ruin:.0%}")
    if p_true < a.win_rate:
        print(f"\nYou believed {a.win_rate:.0%} but the truth is {p_true:.0%}: that is what overestimating does.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
