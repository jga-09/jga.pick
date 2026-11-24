"""Compare the bot's real paper trades with the backtest's pick for the SAME markets.

    docker exec kalshi-directional-bot python scripts/forward_vs_backtest.py
    docker exec kalshi-directional-bot python scripts/forward_vs_backtest.py --assets HYPE --since 2026-11-17

For every market in the forward-test period it downloads Kalshi's 1-minute candles and Coinbase prices (cached
in data/fwd_cache), finds what the backtest rule (fair value, mid-window, 6c edge) would have bought, and puts
that next to what the bot actually bought. Groups:

  BOTH        bot and backtest traded the market: same side? price difference? P&L of each
  BOT ONLY    the bot traded, the backtest would not have (the bot took trades the rule skips)
  MISSED      the backtest traded, the bot did not (setups the bot never caught)

If BOT ONLY / a worse price explain the gap, the problem is execution, not the strategy. Read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.kalshi.client import KalshiClient  # noqa: E402
from app.research.model_data import load_crypto_bars, load_markets  # noqa: E402
from app.research.replay import CoinbaseHistory, HistoryDownloader  # noqa: E402

_spec = importlib.util.spec_from_file_location("exit_test", ROOT / "scripts" / "exit_test.py")
E = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(E)  # type: ignore[union-attr]
G = E.G


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def download(assets: list[str], since: datetime, until: datetime, cache: Path) -> None:
    s = Settings()
    client = KalshiClient(s)
    dl = HistoryDownloader(client, cache, concurrency=6, trades=False)
    spot = CoinbaseHistory(cache)
    try:
        for asset in assets:
            ser = f"KX{asset}15M"
            markets = await dl.settled_markets(ser, since - timedelta(hours=1), until)
            for i in range(0, len(markets), 40):
                await asyncio.gather(*(dl.market_data(ser, m) for m in markets[i:i + 40]))
            day = since.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
            while day <= until:
                await spot.day(asset, day)
                day += timedelta(days=1)
            print(f"{asset}: {len(markets)} settled markets checked/downloaded")
    finally:
        await spot.close()
        await client.close()


def line(name: str, xs: list[float]) -> str:
    if not xs:
        return f"  {name:<34} 0"
    ci = G.boot(xs)
    rng = f" ({ci[0]:+.1f}..{ci[1]:+.1f}c)" if ci else ""
    return f"  {name:<34} {len(xs):4} · avg {sum(xs) / len(xs):+5.1f}c/contract{rng}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", default="HYPE,BTC")
    ap.add_argument("--since", help="YYYY-MM-DD (default: the forward-test start)")
    ap.add_argument("--cache", default=str(ROOT / "data" / "fwd_cache"))
    ap.add_argument("--no-download", action="store_true")
    a = ap.parse_args()
    assets = [x.strip().upper() for x in a.assets.split(",") if x.strip()]
    repo = Repository(Database(Settings().database_url))
    marker = repo.get_setting("forward_test") or {}
    if a.since:
        since = datetime.fromisoformat(a.since).replace(tzinfo=UTC)
    elif marker.get("start"):
        since = _aware(datetime.fromisoformat(marker["start"]))
    else:
        since = datetime.now(UTC) - timedelta(days=7)
    until = datetime.now(UTC)
    cache = Path(a.cache)
    if not a.no_download:
        asyncio.run(download(assets, since, until, cache))

    rows = [p for p in repo.closed_positions(mode="paper", since=since, limit=1_000_000)
            if p.asset.upper() in assets and _aware(p.opened_at) >= since and p.realized_pnl is not None]
    print(f"\nFORWARD vs BACKTEST since {since:%Y-%m-%d %H:%M} UTC · {len(rows)} closed bot trades\n")
    for asset in assets:
        bot: dict[str, dict] = {}
        for p in rows:
            if p.asset.upper() != asset:
                continue
            b = bot.setdefault(p.ticker, {"side": p.side, "price": p.entry_price, "contracts": 0, "pnl": 0.0,
                                          "opened": _aware(p.opened_at), "close": p.close_time,
                                          "reasons": set()})
            b["contracts"] += p.contracts
            b["pnl"] += p.realized_pnl or 0.0
            b["reasons"].add((p.close_reason or "")[:30])
        bars = load_crypto_bars(cache, asset)
        markets = {m["ticker"]: m for m in load_markets(cache, f"KX{asset}15M-")
                   if m["close"] >= since and m["result"] in ("yes", "no") and m["strike"]}
        if not bars or not markets:
            print(f"{asset}: no downloaded data")
            continue
        sp = G.Spot(bars)
        both, bot_only, missed = [], [], []
        same_side = price_diff = 0
        diffs: list[float] = []
        exits = defaultdict(int)
        for t, m in markets.items():
            e = E.entry(m, G.quotes(m), sp, 6, 1)
            bt = None
            if e:
                k, side, paid = e
                won = m["result"] == side
                bt = (side, paid, (100 if won else 0) - paid - G.fee_c(paid - 1), m["close"] - timedelta(minutes=k))
            b = bot.get(t)
            if b:
                for r in b["reasons"]:
                    exits[r or "settled"] += 1
                bot_c = 100 * b["pnl"] / max(1, b["contracts"])
                if bt:
                    both.append((bot_c, bt[2]))
                    same_side += b["side"] == bt[0]
                    if b["side"] == bt[0]:
                        diffs.append(b["price"] - bt[1])
                    price_diff += 1
                else:
                    bot_only.append(bot_c)
            elif bt:
                missed.append(bt[2])
        unmatched = [t for t in bot if t not in markets]
        print(f"=== {asset}: {len(markets)} markets in the period ===")
        print(line("BOTH: bot's actual result", [x for x, _ in both]))
        print(line("BOTH: backtest result, same markets", [y for _, y in both]))
        if both:
            print(f"  BOTH: same side {same_side}/{len(both)} · when same side, bot paid "
                  f"{sum(diffs) / len(diffs) if diffs else 0:+.1f}c vs the backtest price (+ = worse)")
        print(line("BOT ONLY: trades the rule would skip", bot_only))
        print(line("MISSED: backtest trades the bot skipped", missed))
        if exits:
            print("  how the bot's trades ended: " + " · ".join(f"{k} {v}" for k, v in sorted(exits.items())))
        if unmatched:
            print(f"  ({len(unmatched)} bot trades on markets without downloaded data)")
        print()
    print("Read it: if BOTH matches the backtest but BOT ONLY loses, the bot takes trades the rule skips. If the\n"
          "bot pays more on the same side, fills/timing cost the edge. If MISSED is positive and large, the bot\n"
          "is missing good setups (connection, limits). Research only - nothing traded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
