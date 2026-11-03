"""List every 15-minute market series Kalshi offers right now, and whether the bot can use it.

    python scripts/list_15m.py

Read-only (public Kalshi + Coinbase data, no key, nothing changed). For each 15-minute series it shows:
  * open markets and their 24h volume / open interest (is anyone trading it?)
  * the settlement source Kalshi declares
  * whether Coinbase has a spot price for it (the bot's crypto price feed)
  * what to put in ASSETS to scan it (crypto only - other kinds are research-only)
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import Settings
from app.errors import KalshiError
from app.kalshi.client import KalshiClient
from app.kalshi.market_data import to_float

CRYPTO_HINTS = ("crypto", "coin", "bitcoin", "ether", "solana", "token")


def is_15m(s: dict) -> bool:
    t = str(s.get("ticker", "")).upper()
    text = f"{s.get('frequency', '')} {s.get('title', '')}".lower()
    return bool(re.search(r"(?<!\d)15M$", t)) or "fifteen_min" in text or "15 min" in text or "15-min" in text


def asset_code(ticker: str) -> str:
    """KXBTC15M -> BTC, KXGOLD15M -> GOLD."""
    m = re.match(r"^KX([A-Z0-9]+?)15M$", ticker.upper())
    return m.group(1) if m else ticker.upper()


async def coinbase_has(http: httpx.AsyncClient, code: str) -> bool:
    try:
        r = await http.get(f"https://api.exchange.coinbase.com/products/{code}-USD")
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def main() -> int:
    client = KalshiClient(Settings())
    http = httpx.AsyncClient(timeout=15, headers={"User-Agent": "jgapicks-research"})
    try:
        series = [s for s in await client.get_series_list() if is_15m(s)]
        if not series:
            print("No 15-minute series found on Kalshi.")
            return 1
        rows = []
        for s in sorted(series, key=lambda x: str(x.get("ticker"))):
            t = str(s["ticker"])
            try:
                markets = await client.get_markets(series_ticker=t, status="open", limit=20, max_pages=1)
            except KalshiError as exc:
                markets = []
                print(f"  {t}: {exc}")
            vol = sum(to_float(m.get("volume_24h_fp", m.get("volume_24h"))) or 0 for m in markets)
            oi = sum(to_float(m.get("open_interest_fp", m.get("open_interest"))) or 0 for m in markets)
            cat = str(s.get("category", ""))
            code = asset_code(t)
            crypto = cat.lower() == "crypto" or any(h in str(s.get("title", "")).lower() for h in CRYPTO_HINTS)
            spot = await coinbase_has(http, code) if crypto else False
            src = ", ".join(str(x.get("name") or x) for x in s.get("settlement_sources") or []) or "?"
            rows.append((t, code, cat, len(markets), vol, oi, src, crypto, spot, s.get("title", "")))
        print(f"{'series':<14} {'code':<6} {'category':<12} {'open':>4} {'24h vol':>9} {'open int':>9}  "
              f"{'Coinbase':<8} settlement / title")
        for t, code, cat, n, vol, oi, src, crypto, spot, title in rows:
            cb = ("yes" if spot else "no") if crypto else "-"
            print(f"{t:<14} {code:<6} {cat[:12]:<12} {n:>4} {vol:>9,.0f} {oi:>9,.0f}  {cb:<8} {src[:30]} · {title[:40]}")
        usable = [r[1] for r in rows if r[7] and r[3] > 0]
        print("\nCrypto series with open markets (can be scanned):", ", ".join(usable) or "none")
        print("To scan them (STRATEGY_MODE=quality or confirm): ASSETS=" + ",".join(usable))
        print("Non-crypto 15-minute series (e.g. Gold) are research-only in this bot - never traded.")
        print("Low 24h volume = wide spreads and few fills: those markets are worse, not better, for the bot.")
    finally:
        await client.close()
        await http.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
