"""Gold (XAU/USD) price providers - real, timestamped data only.

Interface: the same ``UnderlyingPriceProvider`` protocol the crypto feeds use
(``name``, ``get_price(asset)``, ``history(asset)``, ``close()``), plus
``bars(start, end)`` for 1-minute history used in research.

Implementation: Pyth Network (free, no key)
  * latest:  Hermes  GET https://hermes.pyth.network/v2/updates/price/latest?ids[]=<feed id>&parsed=true
  * history: Benchmarks (TradingView shim)
             GET https://benchmarks.pyth.network/v1/shims/tradingview/history?symbol=Metal.XAU/USD&resolution=1&from&to
  * feed id: NOT hardcoded - set GOLD_PYTH_FEED_ID (``scripts/gold_check.py`` looks it up for you via
             GET https://hermes.pyth.network/v2/price_feeds?query=XAU&asset_type=metal).
Why Pyth: Kalshi's 15-minute Gold contracts are reported (third-party) to settle on Pyth 1-minute closes. That
is only an assumption until ``app.gold.markets.validate_gold_market`` reads it from Kalshi's own rules text.

Gold does not trade 24/7 (OTC spot: roughly Sun 18:00 - Fri 17:00 New York time, with a daily 17:00-18:00
break). Outside those hours, or when the feed's publish time is old, ``get_price`` returns None - a stale
price is never passed off as current.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.data.underlying import SpotPrice
from app.utils.time import utcnow

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
HERMES = "https://hermes.pyth.network"
BENCHMARKS = "https://benchmarks.pyth.network"
GOLD_ASSETS = ("GOLD", "XAU")


@dataclass(frozen=True)
class GoldBar:
    ts: datetime  # END of the minute (known only from then on)
    open: float
    high: float
    low: float
    close: float


def session_open(ts: datetime) -> bool:
    """Approximate spot-gold trading hours in New York time (Sun 18:00 - Fri 17:00, 17:00-18:00 daily break)."""
    ny = ts.astimezone(NY)
    wd, h = ny.weekday(), ny.hour  # Mon=0 .. Sun=6
    if wd == 5:  # Saturday
        return False
    if wd == 6:
        return h >= 18
    if wd == 4 and h >= 17:  # Friday close
        return False
    return h != 17  # daily maintenance break


def parse_hermes(payload: dict, feed_id: str) -> SpotPrice | None:
    """Parse a Hermes /v2/updates/price/latest reply. None if the feed or fields are missing."""
    want = feed_id.lower().removeprefix("0x")
    for row in payload.get("parsed") or []:
        if str(row.get("id", "")).lower().removeprefix("0x") != want:
            continue
        p = row.get("price") or {}
        try:
            price = int(p["price"]) * 10 ** int(p["expo"])
            ts = datetime.fromtimestamp(int(p["publish_time"]), UTC)
        except (KeyError, TypeError, ValueError):
            return None
        return SpotPrice("GOLD", price, ts, "pyth") if price > 0 else None
    return None


def parse_benchmarks(payload: dict) -> list[GoldBar]:
    """Parse a TradingView-shim history reply ({"s":"ok","t":[..],"o":[..],"h":[..],"l":[..],"c":[..]}).

    ``t`` is the bar START; bars are stored by their END (t + 60 s) so research never sees a bar early."""
    if payload.get("s") != "ok":
        return []
    cols = [payload.get(k) or [] for k in ("t", "o", "h", "l", "c")]
    n = len(cols[0])
    if any(len(c) != n for c in cols):
        return []  # inconsistent reply: use nothing rather than misaligned prices
    out = {}
    for t, o, h, lo, c in zip(*cols, strict=True):
        try:
            bar = GoldBar(datetime.fromtimestamp(int(t) + 60, UTC), float(o), float(h), float(lo), float(c))
        except (TypeError, ValueError):
            continue
        if min(bar.open, bar.high, bar.low, bar.close) > 0:
            out[bar.ts] = bar
    return [out[k] for k in sorted(out)]


class PythGoldProvider:
    name = "pyth_gold"

    def __init__(self, feed_id: str, *, stale_after_sec: float = 120.0, symbol: str = "Metal.XAU/USD",
                 http: httpx.AsyncClient | None = None) -> None:
        if not feed_id:
            raise ValueError("GOLD_PYTH_FEED_ID is not set (run scripts/gold_check.py to find it)")
        self.feed_id = feed_id
        self.symbol = symbol
        self.stale_after_sec = stale_after_sec
        self.http = http or httpx.AsyncClient(timeout=10)
        self._hist: deque[SpotPrice] = deque(maxlen=720)
        self.last_error = ""

    async def get_price(self, asset: str) -> SpotPrice | None:
        if asset.upper() not in GOLD_ASSETS:
            return None
        try:
            r = None
            for fid in dict.fromkeys((self.feed_id, self.feed_id.lower().removeprefix("0x"))):
                r = await self.http.get(f"{HERMES}/v2/updates/price/latest", params={"ids[]": fid, "parsed": "true"})
                if r.status_code < 400:
                    break
            assert r is not None
            if r.status_code >= 400:
                self.last_error = f"HTTP {r.status_code}: {r.text[:120]}"
                log.warning("GOLD_PRICE_FAILED status=%s body=%s", r.status_code, r.text[:120])
                return None
            spot = parse_hermes(r.json(), self.feed_id)
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}"
            log.warning("GOLD_PRICE_FAILED error=%s", type(exc).__name__)
            return None
        if spot is None:
            self.last_error = "feed id not in reply"
            return None
        age = (utcnow() - spot.ts).total_seconds()
        if age > self.stale_after_sec:
            self.last_error = f"stale ({age:.0f}s old{'; market closed' if not session_open(utcnow()) else ''})"
            return None
        self.last_error = ""
        if not self._hist or self._hist[-1].ts != spot.ts:
            self._hist.append(spot)
        return spot

    def history(self, asset: str) -> list[SpotPrice]:
        return list(self._hist) if asset.upper() in GOLD_ASSETS else []

    async def bars(self, start: datetime, end: datetime) -> list[GoldBar]:
        """1-minute history (research). Requests are chunked to stay small."""
        out: list[GoldBar] = []
        a = start
        while a < end:
            b = min(end, a + timedelta(hours=12))
            r = await self.http.get(f"{BENCHMARKS}/v1/shims/tradingview/history",
                                    params={"symbol": self.symbol, "resolution": "1",
                                            "from": int(a.timestamp()), "to": int(b.timestamp())})
            r.raise_for_status()
            out.extend(parse_benchmarks(r.json()))
            a = b
        dedup = {x.ts: x for x in out}
        return [dedup[k] for k in sorted(dedup)]

    async def close(self) -> None:
        await self.http.aclose()
