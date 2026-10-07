"""Dashboard message management (edit-in-place) and throttled alerts."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from app.telegram.callbacks import CallbackRouter
from app.telegram.keyboards import Screen, to_markup

log = logging.getLogger(__name__)


@dataclass
class ChatView:
    user_id: int
    message_id: int | None = None
    route: str = "home"
    refreshable: bool = True
    last_text: str = ""
    extra_messages: deque[int] = field(default_factory=lambda: deque(maxlen=200))


class DashboardManager:
    def __init__(self, bot: Any, router: CallbackRouter, refresh_sec: float) -> None:
        self.bot = bot
        self.router = router
        self.refresh_sec = refresh_sec
        self.chats: dict[int, ChatView] = {}
        self._lock = asyncio.Lock()

    async def show(self, chat_id: int, user_id: int, screen: Screen, *, message_id: int | None = None,
                   new: bool = False) -> None:
        view = self.chats.setdefault(chat_id, ChatView(user_id))
        view.user_id = user_id
        target = None if new else (message_id or view.message_id)
        async with self._lock:
            if target is not None and await self._edit(chat_id, target, screen):
                if message_id is None or message_id == view.message_id or view.message_id is None:
                    view.message_id = target
                    self._remember(view, screen)
                elif message_id != view.message_id:
                    view.extra_messages.append(message_id)
                return
            msg = await self._send(chat_id, screen)
            if msg is not None:
                if view.message_id and view.message_id != msg.message_id:
                    view.extra_messages.append(view.message_id)
                view.message_id = msg.message_id
                self._remember(view, screen)

    @staticmethod
    def _remember(view: ChatView, screen: Screen) -> None:
        view.route, view.refreshable, view.last_text = screen.route, screen.refreshable, screen.text

    async def _edit(self, chat_id: int, message_id: int, screen: Screen) -> bool:
        try:
            await self.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=screen.text,
                                             parse_mode=ParseMode.HTML, reply_markup=to_markup(screen),
                                             disable_web_page_preview=True)
            return True
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            log.warning("DASHBOARD_EDIT_FAILED chat=%s error=%s", chat_id, exc)
            return False
        except RetryAfter as exc:
            await asyncio.sleep(float(getattr(exc, "retry_after", 1)))
            return True
        except (TimedOut, NetworkError) as exc:
            log.warning("DASHBOARD_EDIT_NETWORK chat=%s error=%s", chat_id, exc)
            return True

    async def _send(self, chat_id: int, screen: Screen) -> Any:
        try:
            return await self.bot.send_message(chat_id=chat_id, text=screen.text, parse_mode=ParseMode.HTML,
                                               reply_markup=to_markup(screen), disable_web_page_preview=True)
        except (Forbidden, BadRequest, TimedOut, NetworkError) as exc:
            log.warning("TELEGRAM_SEND_FAILED chat=%s error=%s", chat_id, exc)
            return None

    def track(self, chat_id: int, message_id: int) -> None:
        view = self.chats.get(chat_id)
        if view:
            view.extra_messages.append(message_id)

    async def clear(self, chat_id: int) -> int:
        view = self.chats.get(chat_id)
        if not view:
            return 0
        deleted = 0
        while view.extra_messages:
            mid = view.extra_messages.popleft()
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=mid)
                deleted += 1
            except (BadRequest, Forbidden):
                continue  # already deleted / too old (Telegram allows ~48h)
            except (TimedOut, NetworkError) as exc:
                log.warning("CLEAR_FAILED chat=%s error=%s", chat_id, exc)
        return deleted

    async def refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_sec)
            for chat_id, view in list(self.chats.items()):
                if not view.refreshable or view.message_id is None:
                    continue
                try:
                    screen = await self.router.render(view.route, view.user_id)
                except Exception:  # noqa: BLE001 - a render bug must not kill the refresher
                    log.exception("DASHBOARD_RENDER_FAILED route=%s", view.route)
                    continue
                if screen.text != view.last_text:
                    await self.show(chat_id, view.user_id, screen)


class AlertManager:
    """Sends alerts to admin chats with per-key cooldowns and a global rate cap."""

    NO_COOLDOWN = {"trade_opened", "trade_closed", "large_loss", "daily_limit", "emergency"}

    def __init__(self, bot: Any, dashboard: DashboardManager, admin_ids: frozenset[int],
                 cooldown_sec: float, max_per_minute: int = 12) -> None:
        self.bot = bot
        self.dashboard = dashboard
        self.admin_ids = admin_ids
        self.cooldown = cooldown_sec
        self.max_per_minute = max_per_minute
        self._last: dict[str, float] = {}
        self._sent: deque[float] = deque()

    def should_send(self, kind: str, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        if kind not in self.NO_COOLDOWN and now - self._last.get(key, -1e9) < self.cooldown:
            return False
        while self._sent and now - self._sent[0] > 60:
            self._sent.popleft()
        if len(self._sent) >= self.max_per_minute and kind not in self.NO_COOLDOWN:
            return False
        self._last[key] = now
        self._sent.append(now)
        return True

    async def alert(self, kind: str, key: str, text: str, buttons: list | None = None) -> None:
        if not self.should_send(kind, key):
            return
        screen = Screen(text, buttons or [])
        for chat_id in self.admin_ids:
            msg = await self.dashboard._send(chat_id, screen)
            if msg is not None:
                self.dashboard.chats.setdefault(chat_id, _new_view(chat_id))
                self.dashboard.track(chat_id, msg.message_id)


def _new_view(chat_id: int) -> ChatView:
    return ChatView(user_id=chat_id, refreshable=False)
