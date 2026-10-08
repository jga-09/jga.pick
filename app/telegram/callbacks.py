"""Callback routing: validation, authorisation, confirmations.

Transport-agnostic (no python-telegram-bot imports) so it is unit-testable.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from app.errors import ConfigError, InvalidCallbackError, TradingError
from app.risk.profiles import RiskLevel
from app.runtime import BotRuntime
from app.telegram import analytics_views as AV
from app.telegram import messages as M
from app.telegram.keyboards import Screen
from app.utils.logging import log_event

log = logging.getLogger(__name__)

CALLBACK_RE = re.compile(r"^[a-z]{2,8}(?::[A-Za-z0-9_.+-]{1,48}){0,2}$")
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.-]{1,47}$")

# action -> number of args allowed
ACTIONS: dict[str, tuple[int, ...]] = {
    "home": (0,), "start": (0,), "stop": (0,), "pause": (0,), "resume": (0,), "status": (0,),
    "signals": (0,), "mkt": (1,), "det": (1,), "buy": (1,), "tok": (1,), "tno": (1,),
    "trades": (0, 1), "pos": (0,), "pcl": (1,), "pclok": (1,), "risk": (0,), "rset": (1,), "rok": (1,),
    "rcust": (0,), "radj": (2,), "rrst": (0,), "hist": (1,), "strat": (0,), "ind": (0,), "cfg": (0,),
    "assets": (0,), "focus": (1,), "possz": (0,), "auto": (0, 1), "mode": (0, 1), "modeok": (1,),
    "why": (1,), "chk": (1,), "anl": (0,), "anld": (0,), "cal": (0,), "loss": (0,), "wins": (0,),
    "exp": (0,), "filt": (0,), "feat": (0,),
    "clear": (0,), "estop": (0,), "estopok": (0,), "ereset": (0,), "eresetok": (0,), "pnl": (0,),
}


@dataclass
class Response:
    screen: Screen | None
    toast: str | None = None
    clear: bool = False
    alert: bool = False  # show toast as a modal alert


def parse_callback(data: str | None) -> tuple[str, list[str]]:
    if not data or len(data.encode()) > 64 or not CALLBACK_RE.match(data):
        raise InvalidCallbackError("malformed callback data")
    action, *args = data.split(":")
    if action not in ACTIONS or len(args) not in ACTIONS[action]:
        raise InvalidCallbackError(f"unknown callback {action!r}")
    return action, args


class CallbackGuard:
    """Drops redelivered callback ids and rapid identical taps (double-clicks)."""

    def __init__(self, debounce_sec: float = 1.5, max_ids: int = 5000) -> None:
        self.debounce = debounce_sec
        self._ids: dict[str, float] = {}
        self._last: dict[tuple[int, str], float] = {}
        self._max = max_ids

    def accept(self, callback_id: str, user_id: int, data: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        if callback_id in self._ids:
            return False
        self._ids[callback_id] = now
        key = (user_id, data)
        last = self._last.get(key)
        self._last[key] = now
        if len(self._ids) > self._max:
            cutoff = now - 600
            self._ids = {k: v for k, v in self._ids.items() if v > cutoff}
            self._last = {k: v for k, v in self._last.items() if v > cutoff}
        return not (last is not None and now - last < self.debounce)


class CallbackRouter:
    def __init__(self, rt: BotRuntime, admin_ids: frozenset[int]) -> None:
        self.rt = rt
        self.admin_ids = admin_ids

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids

    async def handle(self, user_id: int | None, data: str | None) -> Response:
        if not self.is_admin(user_id):
            log_event(log, "UNAUTHORIZED_CALLBACK", logging.WARNING, user=user_id)
            return Response(M.unauthorized(), toast="⛔ Unauthorized", alert=True)
        try:
            action, args = parse_callback(data)
        except InvalidCallbackError:
            log_event(log, "INVALID_CALLBACK", logging.WARNING, user=user_id, data=str(data)[:70])
            return Response(None, toast="Invalid or expired button", alert=True)
        try:
            return await getattr(self, f"_on_{action}")(user_id, *args)
        except InvalidCallbackError:
            log_event(log, "INVALID_CALLBACK", logging.WARNING, user=user_id, data=str(data)[:70])
            return Response(None, toast="Invalid or expired button", alert=True)
        except (TradingError, ConfigError) as exc:
            log_event(log, "ACTION_REJECTED", logging.WARNING, action=action, reason=exc)
            return Response(None, toast=f"❌ {exc}"[:190], alert=True)

    async def render(self, route: str, user_id: int | None) -> Screen:
        """Re-render a refreshable route (used by the dashboard auto-refresh)."""
        resp = await self.handle(user_id, route)
        return resp.screen or M.home(self.rt)

    # ---------------------------------------------------------- navigation
    async def _on_home(self, uid: int) -> Response:
        return Response(M.home(self.rt))

    async def _on_status(self, uid: int) -> Response:
        return Response(M.status(self.rt))

    async def _on_signals(self, uid: int) -> Response:
        return Response(M.markets_list(self.rt))

    def _ticker(self, t: str) -> str:
        if not TICKER_RE.match(t):
            raise InvalidCallbackError("bad ticker")
        return t

    async def _on_mkt(self, uid: int, t: str) -> Response:
        return Response(M.signal_card(self.rt, self._ticker(t)))

    async def _on_det(self, uid: int, t: str) -> Response:
        return Response(M.signal_details(self.rt, self._ticker(t)))

    async def _on_why(self, uid: int, t: str) -> Response:
        return Response(AV.why(self.rt, self._ticker(t)))

    async def _on_chk(self, uid: int, t: str) -> Response:
        return Response(AV.checklist(self.rt, self._ticker(t)))

    async def _on_anl(self, uid: int) -> Response:
        return Response(await asyncio.to_thread(AV.analytics, self.rt))

    async def _on_anld(self, uid: int) -> Response:
        return Response(AV.detailed(self.rt))

    async def _on_cal(self, uid: int) -> Response:
        return Response(AV.calibration(self.rt))

    async def _on_loss(self, uid: int) -> Response:
        return Response(AV.loss_analysis(self.rt))

    async def _on_wins(self, uid: int) -> Response:
        return Response(AV.win_analysis(self.rt))

    async def _on_exp(self, uid: int) -> Response:
        return Response(await asyncio.to_thread(AV.experiments, self.rt))

    async def _on_filt(self, uid: int) -> Response:
        return Response(await asyncio.to_thread(AV.filters, self.rt))

    async def _on_feat(self, uid: int) -> Response:
        return Response(await asyncio.to_thread(AV.features, self.rt))

    async def _on_trades(self, uid: int, page: str = "0") -> Response:
        p = int(page) if page.isdigit() else 0
        return Response(M.trades(self.rt, min(p, 1000)))

    async def _on_pos(self, uid: int) -> Response:
        return Response(M.positions(self.rt))

    async def _on_pnl(self, uid: int) -> Response:
        return Response(M.pnl(self.rt))

    async def _on_hist(self, uid: int, period: str) -> Response:
        return Response(M.history(self.rt, period if period in M.PERIODS else "today"))

    async def _on_strat(self, uid: int) -> Response:
        return Response(M.strategy(self.rt))

    async def _on_ind(self, uid: int) -> Response:
        return Response(M.indicators(self.rt))

    async def _on_cfg(self, uid: int) -> Response:
        return Response(M.config(self.rt))

    async def _on_assets(self, uid: int) -> Response:
        return Response(M.assets(self.rt))

    async def _on_focus(self, uid: int, asset: str) -> Response:
        if asset not in self.rt.settings.asset_list:
            raise InvalidCallbackError("unknown asset")
        self.rt.set_focus(asset)
        return Response(M.assets(self.rt), toast=f"Focus: {asset}")

    async def _on_possz(self, uid: int) -> Response:
        return Response(M.position_settings(self.rt))

    async def _on_clear(self, uid: int) -> Response:
        return Response(M.home(self.rt), toast="🧹 Cleared", clear=True)

    # ------------------------------------------------------------ control
    async def _on_start(self, uid: int) -> Response:
        await self.rt.start()
        return Response(M.home(self.rt), toast="🟢 Bot started")

    async def _on_stop(self, uid: int) -> Response:
        await self.rt.stop()
        return Response(M.home(self.rt), toast="🛑 Bot stopped")

    async def _on_pause(self, uid: int) -> Response:
        self.rt.pause()
        return Response(M.home(self.rt), toast="⏸ Trading paused (monitoring continues)")

    async def _on_resume(self, uid: int) -> Response:
        self.rt.resume()
        return Response(M.home(self.rt), toast="▶️ Trading resumed")

    async def _on_estop(self, uid: int) -> Response:
        return Response(M.emergency(self.rt))

    async def _on_estopok(self, uid: int) -> Response:
        await self.rt.emergency_stop(by=uid)
        return Response(M.emergency(self.rt), toast="🚨 EMERGENCY STOP ACTIVE", alert=True)

    async def _on_ereset(self, uid: int) -> Response:
        return Response(M.emergency_reset_confirm())

    async def _on_eresetok(self, uid: int) -> Response:
        self.rt.reset_emergency(by=uid)
        return Response(M.home(self.rt), toast="🔓 Emergency stop reset")

    # --------------------------------------------------------------- risk
    async def _on_risk(self, uid: int) -> Response:
        return Response(M.risk_menu(self.rt))

    def _level(self, v: str) -> RiskLevel:
        try:
            return RiskLevel(v)
        except ValueError as exc:
            raise InvalidCallbackError("bad risk level") from exc

    async def _on_rset(self, uid: int, level: str) -> Response:
        """Selecting a level only shows a confirmation; nothing changes yet."""
        new = self._level(level)
        if new == self.rt.state.risk_level:
            return Response(M.risk_menu(self.rt), toast=f"Already {new.title}")
        return Response(M.risk_confirm(self.rt, new))

    async def _on_rok(self, uid: int, level: str) -> Response:
        new = self._level(level)
        self.rt.set_risk_level(new, by=uid)
        return Response(M.risk_menu(self.rt), toast=f"Risk set to {new.title}")

    async def _on_rcust(self, uid: int) -> Response:
        return Response(M.risk_customize(self.rt))

    async def _on_radj(self, uid: int, code: str, sign: str) -> Response:
        if code not in M.RISK_FIELDS or sign not in ("+", "-"):
            raise InvalidCallbackError("bad adjustment")
        field, label, step = M.RISK_FIELDS[code]
        try:
            self.rt.adjust_risk(field, step if sign == "+" else -step)
            note = ""
        except ConfigError as exc:
            note = f"⚠️ {exc}"
        return Response(M.risk_customize(self.rt, note))

    async def _on_rrst(self, uid: int) -> Response:
        self.rt.reset_risk_overrides()
        return Response(M.risk_customize(self.rt), toast="Profile reset to defaults")

    # ------------------------------------------------------- auto / mode
    async def _on_auto(self, uid: int, v: str = "") -> Response:
        if not v:
            return Response(M.auto_trade(self.rt))
        if v not in ("on", "off"):
            raise InvalidCallbackError("bad value")
        _, msg = self.rt.set_auto_trade(v == "on")
        return Response(M.auto_trade(self.rt, msg))

    async def _on_mode(self, uid: int, v: str = "") -> Response:
        if not v:
            return Response(M.mode_screen(self.rt))
        if v == "paper":
            _, msg = self.rt.set_mode("paper")
            return Response(M.mode_screen(self.rt, msg))
        if v == "live":
            if not self.rt.mode_permitted("live"):
                return Response(M.mode_screen(self.rt, "Live trading is disabled by configuration."))
            return Response(M.live_mode_confirm())
        raise InvalidCallbackError("bad mode")

    async def _on_modeok(self, uid: int, v: str) -> Response:
        if v != "live":
            raise InvalidCallbackError("bad mode")
        _, msg = self.rt.set_mode("live")
        return Response(M.mode_screen(self.rt, msg))

    # ------------------------------------------------------------- trades
    async def _on_buy(self, uid: int, t: str) -> Response:
        ticker = self._ticker(t)
        if not self.rt.state.trading_allowed:
            return Response(None, toast=f"❌ Trading disabled: {self.rt.state.disabled_reason}", alert=True)
        ticket, view = self.rt.propose(ticker)
        if ticket is None:
            reason = view.decision.reason if view and view.decision else "no actionable signal"
            return Response(M.signal_card(self.rt, ticker), toast=f"❌ {reason}"[:190], alert=True)
        return Response(M.ticket_screen(ticket))

    async def _on_tok(self, uid: int, ticket_id: str) -> Response:
        ticket = self.rt.tickets.consume(ticket_id)  # one-shot: duplicates get None
        if ticket is None:
            return Response(None, toast="Ticket expired or already used", alert=True)
        pos = await self.rt.execute_ticket(ticket, confirmed=True)
        return Response(M.trade_result(pos), toast="✅ Order filled")

    async def _on_tno(self, uid: int, ticket_id: str) -> Response:
        self.rt.tickets.discard(ticket_id)
        return Response(M.home(self.rt), toast="Cancelled")

    async def _on_pcl(self, uid: int, pid: str) -> Response:
        pos = self.rt.portfolio.positions.get(int(pid)) if pid.isdigit() else None
        if pos is None:
            return Response(M.positions(self.rt), toast="Position not found")
        return Response(M.close_confirm(self.rt, pos))

    async def _on_pclok(self, uid: int, pid: str) -> Response:
        if not pid.isdigit():
            raise InvalidCallbackError("bad id")
        pnl = await self.rt.close_position(int(pid), confirmed=True)
        return Response(M.positions(self.rt), toast=f"Closed · P&L {M.money(pnl)}")
