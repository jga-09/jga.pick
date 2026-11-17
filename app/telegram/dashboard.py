"""Dashboard message management (edit-in-place) and throttled alerts."""

from __future__ import annotations

import asyncio
import html
import re
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
    last_user_action: float = 0.0  # monotonic time of the user's last tap/command in this chat


class DashboardManager:
    def __init__(self, bot: Any, router: CallbackRouter, refresh_sec: float) -> None:
        self.bot = bot
        self.router = router
        self.refresh_sec = refresh_sec
        self.chats: dict[int, ChatView] = {}
        self._locks: dict[int, asyncio.Lock] = {}  # one per chat: a slow edit in one chat never blocks another

    USER_PRIORITY_SEC = 5.0  # auto-refresh stays out of the way this long after the user's last tap
    AUTO_TIMEOUT_SEC = 3.0  # auto-refresh gives up quickly on a slow connection (a tap would wait behind it)

    def _lock_for(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    async def show(self, chat_id: int, user_id: int, screen: Screen, *, message_id: int | None = None,
                   new: bool = False, auto: bool = False) -> None:
        view = self.chats.setdefault(chat_id, ChatView(user_id))
        view.user_id = user_id
        lock = self._lock_for(chat_id)
        if auto:
            # the user always goes first: skip this refresh if they are acting or just acted
            if lock.locked() or time.monotonic() - view.last_user_action < self.USER_PRIORITY_SEC:
                return
        else:
            view.last_user_action = time.monotonic()
        target = None if new else (message_id or view.message_id)
        async with lock:
            if target is not None and await self._edit(chat_id, target, screen, auto=auto):
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

    async def _edit(self, chat_id: int, message_id: int, screen: Screen, auto: bool = False) -> bool:
        try:
            if _bad_html(screen.text):
                raise BadRequest("Can't parse entities: pre-check")
            timeouts = ({"read_timeout": self.AUTO_TIMEOUT_SEC, "write_timeout": self.AUTO_TIMEOUT_SEC,
                         "connect_timeout": self.AUTO_TIMEOUT_SEC} if auto else {})
            await self.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=screen.text,
                                             parse_mode=ParseMode.HTML, reply_markup=to_markup(screen),
                                             disable_web_page_preview=True, **timeouts)
            return True
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            if "parse entities" in str(exc).lower():
                return await self._edit_plain(chat_id, message_id, screen, exc)
            log.warning("DASHBOARD_EDIT_FAILED chat=%s error=%s", chat_id, exc)
            return False
        except RetryAfter as exc:
            if auto:  # never sleep while holding the chat lock for a background refresh
                return True
            await asyncio.sleep(float(getattr(exc, "retry_after", 1)))
            return True
        except (TimedOut, NetworkError) as exc:
            log.warning("DASHBOARD_EDIT_NETWORK chat=%s error=%s", chat_id, exc)
            return True

    async def _edit_plain(self, chat_id: int, message_id: int, screen: Screen, exc: Exception) -> bool:
        log.error("TELEGRAM_HTML_INVALID route=%s error=%s - sending as plain text", screen.route, exc)
        try:
            await self.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=_plain(screen.text),
                                             reply_markup=to_markup(screen), disable_web_page_preview=True)
            return True
        except BadRequest as exc2:
            return "not modified" in str(exc2).lower()
        except (TimedOut, NetworkError):
            return True

    async def _send(self, chat_id: int, screen: Screen) -> Any:
        try:
            if _bad_html(screen.text):
                raise BadRequest("Can't parse entities: pre-check")
            return await self.bot.send_message(chat_id=chat_id, text=screen.text, parse_mode=ParseMode.HTML,
                                               reply_markup=to_markup(screen), disable_web_page_preview=True)
        except BadRequest as exc:
            if "parse entities" not in str(exc).lower():
                log.warning("TELEGRAM_SEND_FAILED chat=%s error=%s", chat_id, exc)
                return None
            # Never lose a dashboard/alert to a formatting bug: fall back to plain text.
            log.error("TELEGRAM_HTML_INVALID route=%s error=%s - sending as plain text", screen.route, exc)
            try:
                return await self.bot.send_message(chat_id=chat_id, text=_plain(screen.text),
                                                   reply_markup=to_markup(screen), disable_web_page_preview=True)
            except (Forbidden, BadRequest, TimedOut, NetworkError) as exc2:
                log.warning("TELEGRAM_SEND_FAILED chat=%s error=%s", chat_id, exc2)
                return None
        except (Forbidden, TimedOut, NetworkError) as exc:
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
                    await self.show(chat_id, view.user_id, screen, auto=True)


_ALLOWED_TAG = re.compile(r"</?(b|i|code)>")


def _bad_html(text: str) -> bool:
    """True if text has a '<' that is not one of the tags we use (Telegram would reject it)."""
    stripped = _ALLOWED_TAG.sub("", text)
    return "<" in stripped or ">" in stripped


def _plain(text: str) -> str:
    return html.unescape(_ALLOWED_TAG.sub("", text))


class AlertManager:
    """Sends alerts to admin chats with per-key cooldowns and a global rate cap."""

    supports_users = True  # runtime may target one user's alerts (their own trades / P&L / limits)
    NO_COOLDOWN = {"trade_opened", "trade_closed", "large_loss", "daily_limit", "emergency", "exit_suggest",
                   "exit_check"}

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

    async def alert(self, kind: str, key: str, text: str, buttons: list | None = None,
                    users: list[int] | None = None) -> None:
        if not self.should_send(kind, key):
            return
        screen = Screen(text, buttons or [])
        targets = self.admin_ids if users is None else [u for u in users if u in self.admin_ids]
        for chat_id in targets:
            msg = await self.dashboard._send(chat_id, screen)
            if msg is not None:
                self.dashboard.chats.setdefault(chat_id, _new_view(chat_id))
                self.dashboard.track(chat_id, msg.message_id)


    async def photo(self, kind: str, key: str, png: bytes, caption: str, buttons: list | None = None,
                    users: list[int] | None = None) -> bool:
        """Send an image with an HTML caption. Returns False if it could not be sent (caller falls back to text)."""
        if not self.should_send(kind, key):
            return True
        ok = False
        targets = self.admin_ids if users is None else [u for u in users if u in self.admin_ids]
        for chat_id in targets:
            try:
                msg = await self.bot.send_photo(chat_id=chat_id, photo=png, caption=caption[:1024],
                                                parse_mode=ParseMode.HTML,
                                                reply_markup=to_markup(Screen(caption, buttons or [])))
            except (Forbidden, BadRequest, TimedOut, NetworkError) as exc:
                log.warning("TELEGRAM_PHOTO_FAILED chat=%s error=%s", chat_id, exc)
                continue
            ok = True
            self.dashboard.chats.setdefault(chat_id, _new_view(chat_id))
            self.dashboard.track(chat_id, msg.message_id)
        return ok


def _new_view(chat_id: int) -> ChatView:
    return ChatView(user_id=chat_id, refreshable=False)
