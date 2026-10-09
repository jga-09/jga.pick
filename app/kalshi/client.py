"""Async Kalshi Trade API v2 REST client (httpx).

Endpoints and field names follow the official Kalshi OpenAPI spec
(as shipped in the ``kalshi_python_sync`` SDK).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import Settings
from app.errors import (
    KalshiAuthError,
    KalshiHTTPError,
    KalshiRateLimitError,
    KalshiTransientError,
    MalformedResponseError,
)
from app.kalshi.auth import KalshiSigner
from app.utils.logging import log_event
from app.utils.retry import AsyncRateLimiter, retry_async

log = logging.getLogger(__name__)


class KalshiClient:
    def __init__(
        self,
        settings: Settings,
        signer: KalshiSigner | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.signer = signer
        self.base_url = settings.rest_url
        self._http = httpx.AsyncClient(timeout=settings.http_timeout_sec, transport=transport)
        self._read_limiter = AsyncRateLimiter(settings.kalshi_read_rps)
        self._write_limiter = AsyncRateLimiter(settings.kalshi_write_rps, burst=1)
        # Health tracking for the status screen
        self.last_success: float | None = None
        self.consecutive_failures = 0
        self.auth_failed = False

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def healthy(self) -> bool:
        return (
            self.last_success is not None
            and time.time() - self.last_success < 60
            and self.consecutive_failures < 3
        )

    # ------------------------------------------------------------------ core
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        if auth or self.signer:
            if not self.signer:
                raise KalshiAuthError(0, "this endpoint requires Kalshi API credentials", path)
            headers.update(self.signer.headers(method, url))
        limiter = self._write_limiter if method.upper() != "GET" else self._read_limiter
        await limiter.acquire()
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._http.request(method, url, params=clean_params, json=json, headers=headers)
        except httpx.TimeoutException as exc:
            self._fail()
            raise KalshiTransientError(f"timeout on {path}") from exc
        except httpx.TransportError as exc:
            self._fail()
            raise KalshiTransientError(f"connection error on {path}: {type(exc).__name__}") from exc

        if resp.status_code in (401, 403):
            self._fail()
            self.auth_failed = True
            log_event(log, "KALSHI_AUTH_FAILED", logging.ERROR, status=resp.status_code, path=path)
            raise KalshiAuthError(resp.status_code, _err_text(resp), path)
        if resp.status_code == 429:
            self._fail()
            retry_after = _float_or_none(resp.headers.get("Retry-After"))
            raise KalshiRateLimitError(_err_text(resp), path, retry_after)
        if resp.status_code >= 500:
            self._fail()
            raise KalshiTransientError(f"HTTP {resp.status_code} on {path}")
        if resp.status_code >= 400:
            self._fail()
            raise KalshiHTTPError(resp.status_code, _err_text(resp), path)
        try:
            data = resp.json()
        except ValueError as exc:
            self._fail()
            raise MalformedResponseError(f"non-JSON response from {path}") from exc
        if not isinstance(data, dict):
            raise MalformedResponseError(f"unexpected JSON shape from {path}")
        self.last_success = time.time()
        self.consecutive_failures = 0
        if auth:
            self.auth_failed = False
        return data

    def _fail(self) -> None:
        self.consecutive_failures += 1

    async def _get(self, path: str, params: dict[str, Any] | None = None, auth: bool = False) -> dict[str, Any]:
        return await retry_async(lambda: self._request("GET", path, params=params, auth=auth), label=path)

    # ------------------------------------------------------------- market data
    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Generic read (retried, rate-limited) for endpoints without a dedicated helper."""
        return await self._get(path, params)

    async def get_exchange_status(self) -> dict[str, Any]:
        return await self._get("/exchange/status")

    async def get_series_list(self, category: str | None = None) -> list[dict[str, Any]]:
        data = await self._get("/series", {"category": category})
        return _list(data, "series")

    async def get_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = "open",
        limit: int = 100,
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            data = await self._get(
                "/markets",
                {
                    "series_ticker": series_ticker,
                    "event_ticker": event_ticker,
                    "status": status,
                    "limit": limit,
                    "cursor": cursor,
                },
            )
            markets.extend(_list(data, "markets"))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return markets

    async def get_market(self, ticker: str) -> dict[str, Any]:
        data = await self._get(f"/markets/{ticker}")
        market = data.get("market")
        if not isinstance(market, dict):
            raise MalformedResponseError(f"missing 'market' for {ticker}")
        return market

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        data = await self._get(f"/markets/{ticker}/orderbook", {"depth": depth})
        book = data.get("orderbook_fp")
        if not isinstance(book, dict):
            raise MalformedResponseError(f"missing 'orderbook_fp' for {ticker}")
        return book

    async def get_trades(self, ticker: str, *, limit: int = 100, min_ts: int | None = None) -> list[dict[str, Any]]:
        data = await self._get("/markets/trades", {"ticker": ticker, "limit": limit, "min_ts": min_ts})
        return _list(data, "trades")

    # ------------------------------------------------------- portfolio (auth)
    async def get_balance(self) -> dict[str, Any]:
        return await self._get("/portfolio/balance", auth=True)

    async def get_positions(self) -> dict[str, Any]:
        return await self._get("/portfolio/positions", auth=True)

    async def create_order_v2(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /portfolio/events/orders. Never retried automatically (no double orders)."""
        return await self._request("POST", "/portfolio/events/orders", json=body, auth=True)


def _list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise MalformedResponseError(f"expected list for {key!r}")
    return [v for v in value if isinstance(v, dict)]


def _err_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or err)[:200]
        return str(body)[:200]
    except ValueError:
        return resp.text[:200]


def _float_or_none(v: str | None) -> float | None:
    try:
        return float(v) if v else None
    except ValueError:
        return None
