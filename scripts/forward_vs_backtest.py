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
import logging
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
    # retries after Kalshi's "too many requests" are normal and handled; don't print hundreds of lines
    logging.getLogger("app.utils.retry").setLevel(logging.ERROR)
    print("downloading this period's markets (already-downloaded ones are skipped)...", flush=True)
    s = Settings()
    client = KalshiClient(s)
    dl = HistoryDownloader(client, cache, concurrency=2, trades=False)  # gentle: Kalshi answers 429 above this
    spot = CoinbaseHistory(cache)
    try:
        for asset in assets:
            ser = f"KX{asset}15M"
            markets = await dl.settled_markets(ser, since - timedelta(hours=1), until)
            for i in range(0, len(markets), 10):
                await asyncio.gather(*(dl.market_data(ser, m) for m in markets[i:i + 10]))
                await asyncio.sleep(0.5)
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


FLAG_WORDS = {
    "EXCESSIVE_SPREAD": "spread too wide", "INSUFFICIENT_EV": "bot's edge under the minimum",
    "STALE_DATA": "stale/missing price data", "INSUFFICIENT_LIQUIDITY": "too few contracts at the ask",
    "TOO_EARLY": "too early", "INSUFFICIENT_TIME": "too late", "TRADING_PAUSED": "paused after losses",
    "UNFAVORABLE_REGIME": "volatility regime", "SETTLEMENT_UNVERIFIED": "contract rules unverified",
    "LONGSHOT": "price under 5c", "INSUFFICIENT_EVIDENCE": "not enough history",
    "NOT_CALIBRATED": "not calibrated",
}


def why_missed(obs: list, bt_edge: float) -> str:
    """The bot's own per-minute records (observations) for a market the backtest traded."""
    win = [o for o in obs if 300 <= o.time_remaining <= 690]
    if not win:
        return "bot was not scanning (offline / restarting / no data)"
    if any(o.features.get("approved") for o in win):
        return "bot's model said BUY but a risk rule blocked it (cooldown, loss pause, open positions, limits)"
    flags: dict[str, int] = defaultdict(int)
    for o in win:
        for f in o.hard_flags:
            flags[f] += 1
    if not flags:
        return "no reason recorded"
    top = max(flags.items(), key=lambda kv: kv[1])[0]
    return FLAG_WORDS.get(top, top)


def summary(groups: dict[str, list[float]], total: int) -> list[str]:
    out = []
    for k, xs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        out.append(f"    {len(xs):4} ({len(xs) / max(1, total):4.0%}) avg {sum(xs) / len(xs):+6.1f}c · {k}")
    return out


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
    obs_by: dict[str, list] = defaultdict(list)
    for o in repo.observations(source="btc", settled_only=False, since=since):
        obs_by[o.ticker].append(o)
    stamps = sorted(_aware(o.ts) for os_ in obs_by.values() for o in os_)
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
        missed_why: dict[str, list[float]] = defaultdict(list)
        only_why: dict[str, list[float]] = defaultdict(list)
        spot_bps: list[float] = []
        ev_gap: list[float] = []
        ask_gap: list[float] = []
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
                    near = [o for o in obs_by.get(t, []) if abs((_aware(o.ts) - b["opened"]).total_seconds()) <= 90]
                    o = min(near, key=lambda o: abs((_aware(o.ts) - b["opened"]).total_seconds())) if near else None
                    left = (m["close"] - b["opened"]).total_seconds()
                    if o is not None and o.spread is not None and o.spread > 5:
                        only_why[f"spread {o.spread:.0f}c at entry (backtest only trades spreads <= 5c)"].append(bot_c)
                    elif not 300 <= left <= 660:
                        only_why["entered outside 5-11 min left"].append(bot_c)
                    else:
                        only_why["edge seen between the 1-minute checks / on a different price"].append(bot_c)
            elif bt:
                missed.append(bt[2])
                why = why_missed(obs_by.get(t, []), bt[2])
                missed_why[why].append(bt[2])
                if why.startswith("bot's edge"):
                    at = bt[3]
                    near = [o for o in obs_by.get(t, []) if abs((_aware(o.ts) - at).total_seconds()) <= 60]
                    if near:
                        o = min(near, key=lambda o: abs((_aware(o.ts) - at).total_seconds()))
                        seen = o.yes_ask if bt[0] == "yes" else o.no_ask
                        if seen is not None:
                            ask_gap.append(seen - (bt[1] - 1))  # bt price includes 1c slippage
            # the bot's own spot price and edge vs the backtest's, at each recorded minute of this market
            for o in obs_by.get(t, []):
                i = sp.at(_aware(o.ts))
                if i is not None and o.features.get("spot") and 300 <= o.time_remaining <= 690:
                    spot_bps.append(1e4 * (o.features["spot"] / sp.close[i] - 1))
                    p = G.p_yes(sp.close[i], float(m["strike"]), sp.sigma_per_sec(i), o.time_remaining)
                    if p is not None and o.features.get("p_yes") is not None:
                        ev_gap.append(100 * (o.features["p_yes"] - p))
        unmatched = [t for t in bot if t not in markets]
        print(f"=== {asset}: {len(markets)} markets in the period ===")
        print(line("BOTH: bot's actual result", [x for x, _ in both]))
        print(line("BOTH: backtest result, same markets", [y for _, y in both]))
        if both:
            print(f"  BOTH: same side {same_side}/{len(both)} · when same side, bot paid "
                  f"{sum(diffs) / len(diffs) if diffs else 0:+.1f}c vs the backtest price (+ = worse)")
        print(line("BOT ONLY: trades the rule would skip", bot_only))
        print(line("MISSED: backtest trades the bot skipped", missed))
        if missed_why:
            print("  why the bot skipped them (from its own per-minute records):")
            print("\n".join(summary(missed_why, len(missed))))
        if ask_gap:
            print(f"    on the 'edge under the minimum' ones the bot saw an ask {sum(ask_gap) / len(ask_gap):+.1f}c vs "
                  f"the 1-minute candle ({len(ask_gap)} compared; + = bot saw a worse price)")
        if only_why:
            print("  why the bot took trades the rule skips:")
            print("\n".join(summary(only_why, len(bot_only))))
        if spot_bps:
            spot_bps.sort()
            ab = sorted(abs(x) for x in spot_bps)
            print(f"  bot's spot price vs Coinbase 1-min close: median {spot_bps[len(spot_bps) // 2]:+.1f} bps, "
                  f"typical size {ab[len(ab) // 2]:.1f} bps ({len(spot_bps)} minutes)")
        if ev_gap:
            ev_gap.sort()
            ag = sorted(abs(x) for x in ev_gap)
            print(f"  bot's UP odds vs backtest model, same minute: median {ev_gap[len(ev_gap) // 2]:+.1f} pts, "
                  f"typical gap {ag[len(ag) // 2]:.1f} pts")
        if exits:
            print("  how the bot's trades ended: " + " · ".join(f"{k} {v}" for k, v in sorted(exits.items())))
        if unmatched:
            print(f"  ({len(unmatched)} bot trades on markets without downloaded data)")
        print()
    gaps = [(a, b) for a, b in zip(stamps, stamps[1:]) if (b - a).total_seconds() > 180]
    if stamps:
        span = (until - since).total_seconds()
        off = sum((b - a).total_seconds() for a, b in gaps) + max(0.0, (stamps[0] - since).total_seconds()) \
            + max(0.0, (until - stamps[-1]).total_seconds())
        print(f"BOT DOWNTIME (no market evaluated for 3+ min): {off / 3600:.1f} h of {span / 3600:.0f} h "
              f"({off / span:.0%}) in {len(gaps)} gaps")
        for a, b in sorted(gaps, key=lambda g: g[0] - g[1])[:10]:
            print(f"   {a:%a %m-%d %H:%M} -> {b:%H:%M} UTC  ({(b - a).total_seconds() / 3600:.1f} h)")
        hours: dict[int, float] = defaultdict(float)
        for a, b in gaps:
            t = a
            while t < b:
                nxt = min(b, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
                hours[t.hour] += (nxt - t).total_seconds() / 3600
                t = nxt
        if hours:
            print("   downtime by hour of day (UTC): " + " ".join(f"{h:02d}h {v:.0f}" for h, v in sorted(hours.items())
                                                                  if v >= 0.5))
        print()
    print("Read it: if BOTH matches the backtest but BOT ONLY loses, the bot takes trades the rule skips. If the\n"
          "bot pays more on the same side, fills/timing cost the edge. If MISSED is positive and large, the bot\n"
          "is missing good setups (connection, limits). Research only - nothing traded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
