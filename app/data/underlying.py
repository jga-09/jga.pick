"""Optional underlying spot-price providers (decoupled from Kalshi code).

If no provider is configured the strategy runs on Kalshi data only and marks
underlying-based indicators as unavailable. Prices are never synthesized.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

from app.utils.time import utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpotPrice:
    asset: str
    price: float
    ts: datetime
    source: str


class UnderlyingPriceProvider(Protocol):
    name: str

    async def get_price(self, asset: str) -> SpotPrice | None: ...

    def history(self, asset: str) -> list[SpotPrice]: ...

    async def close(self) -> None: ...


class NullUnderlyingProvider:
    """No external feed configured."""

    name = "none"

    async def get_price(self, asset: str) -> SpotPrice | None:
        return None

    def history(self, asset: str) -> list[SpotPrice]:
        return []

    async def close(self) -> None:
        return None


class CoinbaseSpotProvider:
    """Public Coinbase spot price (``/v2/prices/{ASSET}-USD/spot``). Opt-in only."""

    name = "coinbase"
    URL = "https://api.coinbase.com/v2/prices/{asset}-USD/spot"

    def __init__(self, timeout: float = 5.0, history_len: int = 600) -> None:
        self._http = httpx.AsyncClient(timeout=timeout)
        self._hist: dict[str, deque[SpotPrice]] = {}
        self._history_len = history_len

    async def get_price(self, asset: str) -> SpotPrice | None:
        try:
            resp = await self._http.get(self.URL.format(asset=asset.upper()))
            resp.raise_for_status()
            amount = float(resp.json()["data"]["amount"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            log.warning("UNDERLYING_UNAVAILABLE provider=coinbase asset=%s error=%s", asset, type(exc).__name__)
            return None
        sp = SpotPrice(asset, amount, utcnow(), self.name)
        self._hist.setdefault(asset, deque(maxlen=self._history_len)).append(sp)
        return sp

    def history(self, asset: str) -> list[SpotPrice]:
        return list(self._hist.get(asset, ()))

    async def close(self) -> None:
        await self._http.aclose()


def build_provider(name: str) -> UnderlyingPriceProvider:
    if name == "coinbase":
        return CoinbaseSpotProvider()
    return NullUnderlyingProvider()
