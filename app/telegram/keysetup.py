"""/setkey: connect a Kalshi API key from Telegram without leaving it in the chat.

* Admins only, private chat only.
* Every message that contains (or is expected to contain) key material is
  deleted the moment it arrives, before anything else happens.
* The key is checked with Kalshi (balance request) and only then saved to the
  bot's data volume with owner-only permissions. It is never logged or echoed.
* Connecting a key never enables real-money trading; that still needs
  LIVE_TRADING=true and PAPER_TRADING=false in .env plus a confirmation per order.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from telegram import Message
from telegram.constants import ParseMode
from telegram.error import TelegramError

from app.errors import KalshiAuthError
from app.kalshi.keystore import KEY_ID_RE, extract_pem, looks_like_secret

if TYPE_CHECKING:
    from app.runtime import BotRuntime

log = logging.getLogger(__name__)

FLOW_TTL_SEC = 300
MAX_KEY_FILE_BYTES = 16_000
UUID_IN_TEXT = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

STEP1 = ("🔑 <b>CONNECT KALSHI</b>\n\n"
         "<b>Step 1 of 2:</b> send your <b>API Key ID</b>\n"
         "(looks like <code>a1b2c3d4-1234-…</code>)\n\n"
         "🫥 Each message you send here is deleted right away.\n"
         "Make a key on kalshi.com → Account &amp; security → API keys.\n"
         "Send /cancel to stop.")
STEP2 = ("✅ Key ID received <i>(deleted from chat)</i>\n\n"
         "<b>Step 2 of 2:</b> paste the whole <b>private key</b>, including the\n"
         "<code>-----BEGIN … PRIVATE KEY-----</code> and <code>-----END …-----</code> lines,\n"
         "or send the <b>.pem file</b>.\n\n"
         "🫥 It will be deleted from the chat right away.")


@dataclass
class _Flow:
    step: str  # "id" | "pem"
    key_id: str = ""
    expires: float = field(default_factory=lambda: time.monotonic() + FLOW_TTL_SEC)


class KeySetup:
    def __init__(self, rt: BotRuntime) -> None:
        self.rt = rt
        self._flows: dict[int, _Flow] = {}

    def _flow(self, uid: int) -> _Flow | None:
        f = self._flows.get(uid)
        if f is not None and f.expires < time.monotonic():
            self._flows.pop(uid, None)
            return None
        return f

    @staticmethod
    async def _delete(msg: Message) -> bool:
        try:
            await msg.delete()
            return True
        except TelegramError:
            return False

    async def _reply(self, msg: Message, text: str) -> None:
        await msg.chat.send_message(text, parse_mode=ParseMode.HTML)

    async def scrub(self, msg: Message) -> bool:
        """Delete any message that looks like a private key, from anyone. Returns True if it did."""
        if not looks_like_secret(msg.text or msg.caption):
            return False
        deleted = await self._delete(msg)
        log.warning("SECRET_MESSAGE_SCRUBBED deleted=%s", deleted)
        return True

    # ----------------------------------------------------------- commands
    async def command(self, msg: Message, uid: int, cmd: str) -> None:
        if cmd == "cancel":
            await self._reply(msg, "Cancelled." if self._flows.pop(uid, None) else "Nothing to cancel.")
            return
        if msg.chat.type != "private":
            await self._reply(msg, "🔒 For safety, use this in a private chat with the bot.")
            return
        if cmd == "removekey":
            removed = self.rt.clear_kalshi_key()
            note = ("🗑 Kalshi key removed from the bot." if removed else "No key was saved from Telegram.")
            if self.rt.kalshi_key_source() == "env":
                note += "\n(The key from .env is still in use.)"
            await self._reply(msg, note)
            return
        # /setkey, possibly with the key pasted on the same line
        rest = (msg.text or "").partition(" ")[2]
        if rest.strip():
            await self._delete(msg)
            await self._consume_text(msg, uid, rest, _Flow("id"))
            return
        self._flows[uid] = _Flow("id")
        await self._reply(msg, STEP1)

    # ----------------------------------------------------------- messages
    async def message(self, msg: Message, uid: int) -> bool:
        """Handle a non-command message during the flow. Returns True if consumed."""
        flow = self._flow(uid)
        if flow is None:
            return False
        await self._delete(msg)  # first thing, always
        if msg.document is not None:
            doc = msg.document
            if (doc.file_size or 0) > MAX_KEY_FILE_BYTES:
                await self._reply(msg, "That file is too big to be a key. Send the .pem file or paste the text.")
                return True
            data = bytes(await (await doc.get_file()).download_as_bytearray())
            await self._consume_text(msg, uid, data.decode("utf-8", "replace"), flow)
            return True
        await self._consume_text(msg, uid, msg.text or msg.caption or "", flow)
        return True

    async def _consume_text(self, msg: Message, uid: int, text: str, flow: _Flow) -> None:
        uuid = UUID_IN_TEXT.search(text)
        pem = extract_pem(text)
        if uuid and not flow.key_id:
            flow.key_id = uuid.group(0)
        if pem is None:
            if flow.key_id and flow.step == "id":
                flow.step = "pem"
                self._flows[uid] = flow
                await self._reply(msg, STEP2)
            elif flow.step == "id":
                self._flows[uid] = flow
                await self._reply(msg, "That doesn't look like a Key ID (format <code>xxxxxxxx-xxxx-xxxx-xxxx-"
                                       "xxxxxxxxxxxx</code>). Try again or /cancel.")
            else:
                self._flows[uid] = flow
                await self._reply(msg, "I couldn't find a private key in that. Paste everything from "
                                       "<code>-----BEGIN</code> to <code>-----END … KEY-----</code>, or /cancel.")
            return
        if not flow.key_id or not KEY_ID_RE.match(flow.key_id):
            self._flows[uid] = _Flow("id")
            await self._reply(msg, "🫥 Deleted. I need the <b>API Key ID</b> first. Send it now, then the key.")
            return
        self._flows.pop(uid, None)
        await self._reply(msg, "🫥 Deleted from chat. Checking the key with Kalshi…")
        try:
            balance = await self.rt.set_kalshi_key(flow.key_id, pem)
        except KalshiAuthError as exc:
            status = getattr(exc, "status_code", 0)
            why = ("Kalshi rejected it. Check that the Key ID belongs to this private key."
                   if status in (401, 403) else "The key couldn't be read. Make sure you pasted the whole key.")
            await self._reply(msg, f"❌ <b>Not connected.</b> {why}\nNothing was saved. Try /setkey again.")
            return
        bal = (f"Balance: <b>${balance:,.2f}</b>" if balance is not None
               else "⚠️ Couldn't reach Kalshi to double-check. It will be used once the connection is back.")
        await self._reply(msg, "✅ <b>Kalshi connected</b>\n" + bal + "\n\n"
                               "🔒 Saved privately on the bot's server. Your messages were deleted.\n"
                               "📝 This does <b>not</b> turn on real-money trading. Paper stays on unless you "
                               "change .env, and every real order still needs your confirmation.\n"
                               "Remove it any time with /removekey.")
