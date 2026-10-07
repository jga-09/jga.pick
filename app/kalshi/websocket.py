"""Kalshi WebSocket market-data stream with reconnect + exponential backoff.

The WS connection is authenticated with the same signed headers as REST
(signing ``GET`` + the WS path). Only the ``ticker`` and ``trade`` channels
are used; payload fields are parsed defensively and REST polling remains the
authoritative fallback, so a schema change degrades to polling instead of
producing bad data.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable
from typing import Any

import websockets

from app.kalshi.auth import KalshiSigner
from app.utils.logging import log_event
from app.utils.retry import backoff_delay

log = logging.getLogger(__name__)

MessageHandler = Callable[[str, dict[str, Any]], None]


class KalshiWebSocket:
    def __init__(self, url: str, signer: KalshiSigner, on_message: MessageHandler) -> None:
        self.url = url
        self.signer = signer
        self.on_message = on_message
        self.connected = False
        self.reconnects = 0
        self._tickers: tuple[str, ...] = ()
        self._resubscribe = asyncio.Event()
        self._stop = asyncio.Event()

    def set_markets(self, tickers: Iterable[str]) -> None:
        new = tuple(sorted(set(tickers)))
        if new != self._tickers:
            self._tickers = new
            self._resubscribe.set()

    def stop(self) -> None:
        self._stop.set()
        self._resubscribe.set()

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if not self._tickers:
                self._resubscribe.clear()
                await self._wait_event(self._resubscribe, 30)
                continue
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.WebSocketException, json.JSONDecodeError) as exc:
                self.connected = False
                self.reconnects += 1
                delay = backoff_delay(attempt, base=1.0, cap=60.0)
                attempt += 1
                log_event(log, "WS_DISCONNECTED", logging.WARNING, error=type(exc).__name__,
                          detail=str(exc)[:120], retry_in=round(delay, 1))
                await self._wait_event(self._stop, delay)
        self.connected = False

    async def _session(self) -> None:
        headers = self.signer.headers("GET", self.url)
        async with websockets.connect(self.url, additional_headers=headers, ping_interval=20) as ws:
            self.connected = True
            self._resubscribe.clear()
            tickers = list(self._tickers)
            await ws.send(json.dumps({
                "id": 1, "cmd": "subscribe",
                "params": {"channels": ["ticker", "trade"], "market_tickers": tickers},
            }))
            log_event(log, "WS_CONNECTED", markets=len(tickers))
            while not self._resubscribe.is_set():
                recv = asyncio.ensure_future(ws.recv())
                flag = asyncio.ensure_future(self._resubscribe.wait())
                done, pending = await asyncio.wait({recv, flag}, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
                if recv in done:
                    self._dispatch(recv.result())
            self.connected = False  # market set changed or stop requested -> reconnect

    def _dispatch(self, raw: str | bytes) -> None:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return
        mtype = data.get("type")
        msg = data.get("msg")
        if mtype == "error":
            log_event(log, "WS_ERROR", logging.WARNING, msg=str(msg)[:200])
        elif isinstance(msg, dict) and mtype in ("ticker", "ticker_v2", "trade"):
            self.on_message(str(mtype), msg)

    @staticmethod
    async def _wait_event(event: asyncio.Event, timeout: float) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            pass
