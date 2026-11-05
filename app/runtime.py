"""BotRuntime: wires discovery -> market data -> signals -> risk -> execution.

The Telegram layer only talks to this class; it never touches executors,
the Kalshi client or the database directly.
"""

from __future__ import annotations

import asyncio
import shutil
import html
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from app import DISPLAY_NAME
from app.config import Settings
from app.data.settlement_index import SettlementIndexFeed
from app.data.underlying import NullUnderlyingProvider, UnderlyingPriceProvider
from app.accounts import Account, AccountStore
from app.database.repository import AccountRepo, Repository
from app.errors import KalshiAuthError, KalshiError, TradingError
from app.kalshi.auth import KalshiSigner
from app.kalshi.contract import interpret_contract
from app.kalshi.client import KalshiClient
from app.kalshi.keystore import KeyStore
from app.kalshi.market_data import MarketDataService, MarketInfo, MarketSnapshot
from app.kalshi.markets import MarketDiscovery
from app.kalshi.websocket import KalshiWebSocket
from app.research.adaptive import AdaptiveState, assess
from app.research.btc_calibration import CalibrationReport, calibration_report
from app.research.calibration import HistEstimate, HistoricalModel
from app.research.dataset import from_btc_signal, from_signal
from app.research.tradeanalysis import (
    analyze_trade,
    describe,
    entry_snapshot,
    exit_snapshot,
)
from app.risk.manager import RiskDecision, RiskManager, SessionRules
from app.risk.profiles import RiskLevel, RiskProfile, load_profiles
from app.risk.sizing import PositionSizer
from app.state import BotState, StateStore
from app.strategy.btc_decision import (
    UNCALIBRATED,
    BTCInputs,
    BTCParams,
    BTCSignal,
    decide,
)
from app.strategy.btc_model import (
    ModelConfig,
    classify_regime,
    compute_btc_features,
    estimate_probability,
)
from app.strategy.config import StrategyConfig
from app.strategy.engine import SignalEngine
from app.strategy.signals import SignalResult
from app.telegram import messages
from app.telegram.pnl_dashboard import DashboardData, TradeRow, render_dashboard
from app.telegram.pnl_image import ET, PnlCard
from app.trading.executor import TradeExecutor
from app.trading.live import LiveExecutor
from app.trading.paper import PaperExecutor
from app.trading.portfolio import Portfolio
from app.trading.positions import Position
from app.gold.status import GoldStatus, gold_status
from app.trading.reconcile import ReconcileReport, compare_positions, recover_orders
from app.trading.tickets import TicketStore, TradeTicket
from app.utils.logging import log_event
from app.utils.time import day_key, utcnow

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def alert(self, kind: str, key: str, text: str, buttons: list | None = None) -> None: ...


@dataclass(frozen=True)
class MarketView:
    """Everything the UI needs for one market."""

    info: MarketInfo
    snapshot: MarketSnapshot | None
    signal: SignalResult | None
    decision: RiskDecision | None


class BotRuntime:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        client: Any,
        *,
        underlying: UnderlyingPriceProvider | None = None,
        profiles: dict[RiskLevel, RiskProfile] | None = None,
    ) -> None:
        self.settings = settings
        self.base_repo = repo  # shared database; trading data goes through each user's AccountRepo
        self.client = client  # shared market-data client (signs with the owner's key, as before)
        self.fixture_mode = settings.data_source == "fixture"
        self.underlying = underlying or NullUnderlyingProvider()
        self.base_profiles = profiles or load_profiles()

        defaults = BotState(
            risk_level=RiskLevel(settings.default_risk_level),
            auto_trade=settings.auto_trading,
            mode="paper",  # always boot in PAPER; switching to live needs an explicit, confirmed action
        )
        self._defaults = defaults
        self.global_store = StateStore(repo, defaults)  # bot-wide switches + the owner's own settings
        self.owner = settings.owner_id
        if self.owner:
            repo.adopt_legacy(self.owner)  # single-account data from before multi-user support -> the owner
        self.accounts: dict[int, Account] = {}
        self._current: ContextVar[int | None] = ContextVar(f"rt_account_{id(self)}", default=None)

        self.discovery = MarketDiscovery(client, settings)
        self.market_data = MarketDataService(client, settings.orderbook_depth)
        self.strategy_config = StrategyConfig.from_settings(settings)
        self.engine = SignalEngine(settings.signal_history_size, settings.signal_min_confidence,
                                   settings.stale_data_sec, min_history_sec=settings.signal_min_history_sec,
                                   config=self.strategy_config)
        self.btc_mode = settings.strategy_mode == "btc"
        self.btc_cfg = ModelConfig(sigma_window_min=settings.btc_sigma_window_min, basis_bps=settings.btc_basis_bps)
        self.calibration: CalibrationReport | None = None  # BTC's (or the first coin's), for the dashboard
        self.calibrations: dict[str, CalibrationReport] = {}
        self._reconcile: dict[int, ReconcileReport] = {}
        self.gold_provider: Any = None  # PythGoldProvider when GOLD_ENABLED + GOLD_PYTH_FEED_ID (research only)
        self.gold: GoldStatus | None = None
        self.model = self._build_model()
        self.forward_test = self._mark_forward_test()
        self._model_built = time.time()
        self.adaptive = AdaptiveState("NORMAL")
        self._obs_keys: set[tuple[str, int]] = set()
        self._last_signal: dict[str, SignalResult] = {}  # survives market removal (for exit analysis)
        from app.telegram.analytics_views import ResearchCache

        self._research = ResearchCache()
        self._strong: dict[str, str] = {}  # ticker -> direction currently alerted as strong
        self.settlement_index = SettlementIndexFeed()
        self._exit_ticks: dict[int, int] = {}  # position id -> consecutive checks saying "exit"
        self._exit_suggested: set[int] = set()  # live positions already offered a Sell button
        self._early_exits: dict[str, list[Position]] = {}  # ticker -> positions cashed out early (to score later)
        self.risk = RiskManager(PositionSizer(settings.fee_rate), settings.stale_data_sec,
                                settings.order_price_tolerance_cents, ev_gate=settings.ev_gate,
                                strategy_mode=settings.strategy_mode, kelly_sizing=settings.kelly_sizing,
                                fair_value_pure=settings.fair_value_pure,
                                session=SessionRules(
                                    entry_min_left_sec=settings.entry_min_left_sec,
                                    entry_max_left_sec=settings.entry_max_left_sec,
                                    sit_out_after_loss_sec=settings.skip_windows_after_loss
                                    * settings.market_duration_minutes * 60,
                                    max_trades_per_day=settings.max_trades_per_day))
        self.account(self.owner)  # build the owner's account now (fail-safe state checks run here)
        self.ws: KalshiWebSocket | None = None
        self.notifier: Notifier | None = None

        self._tasks: list[asyncio.Task] = []
        self._rediscover = asyncio.Event()
        self.started_at: datetime | None = None
        self.last_poll_at: datetime | None = None
        self.last_error: str | None = None
        self._kalshi_down_notified = False
        self._snap_saved: dict[str, float] = {}
        self._sig_saved: dict[str, tuple[str, float]] = {}
        self._daily_limit_alerted: str | None = None
        self._last_purge = 0.0

    # -------------------------------------------------------------- accounts
    def allowed_user(self, uid: int | None) -> bool:
        return uid is not None and (uid == self.owner or uid in self.settings.admin_ids)

    def account(self, uid: int | None = None) -> Account:
        """The account of ``uid`` (default: the user this task is acting for, else the owner)."""
        uid = self._current.get() if uid is None else uid
        uid = self.owner if uid is None else uid
        acct = self.accounts.get(uid)
        if acct is not None:
            return acct
        if not self.allowed_user(uid):
            raise PermissionError("not an allowed Telegram user")  # never create accounts for strangers
        return self._build_account(uid)

    def _build_account(self, uid: int) -> Account:
        s = self.settings
        owner = uid == self.owner
        arepo = AccountRepo(self.base_repo, uid)
        user_store = self.global_store if owner else StateStore(self.base_repo, replace(self._defaults),
                                                                 key=f"bot_state:user:{uid}")
        store = AccountStore(self.global_store, user_store)
        st = store.state
        if st.mode == "live" and not s.live_allowed:
            st.mode = "paper"  # config no longer allows live -> fail safe
        if st.auto_trade and st.mode == "live" and not s.live_autotrade_allowed:
            st.auto_trade = False
        store.save()
        keys = KeyStore(s.kalshi_key_dir if owner else s.kalshi_key_dir / f"user_{uid}")
        if owner:
            client = self.client
        else:  # each user signs their own orders with their own key (never another user's)
            signer = None
            try:
                signer = keys.load()
            except Exception as exc:  # noqa: BLE001 - a broken stored key must not stop the bot
                log.error("USER_KEY_UNREADABLE user=%s error=%s", uid, type(exc).__name__)
            client = self.client if self.fixture_mode else KalshiClient(s, signer)
        portfolio = Portfolio(arepo, s.paper_starting_balance)
        paper = PaperExecutor(portfolio, s.paper_slippage_cents, s.fee_rate)
        # The live executor is only constructed when configuration permits live trading.
        live = LiveExecutor(client, s, portfolio) if s.live_allowed else None
        acct = Account(uid, arepo, store, portfolio, paper, live, TradeExecutor(store, portfolio, paper, live),
                       TicketStore(), client, keys, owner)
        self.accounts[uid] = acct
        if not owner:
            log.info("ACCOUNT_OPENED user=%s", uid)
        return acct

    def all_accounts(self) -> list[Account]:
        """Owner + every allowed admin (accounts are created on first use)."""
        ids = [self.owner] + sorted(i for i in self.settings.admin_ids if i != self.owner)
        return [self.account(i) for i in ids if self.allowed_user(i) or i == self.owner]

    @contextmanager
    def as_user(self, uid: int | None) -> Iterator[Account]:
        """Act for ``uid`` inside this block (Telegram handlers, per-user trading loops)."""
        acct = self.account(uid)
        token = self._current.set(acct.user_id)
        try:
            yield acct
        finally:
            self._current.reset(token)

    @property
    def current_user(self) -> int:
        return self.account().user_id

    @property
    def reconcile_report(self) -> ReconcileReport | None:
        return self._reconcile.get(self.current_user)

    @reconcile_report.setter
    def reconcile_report(self, report: ReconcileReport | None) -> None:
        if report is None:
            self._reconcile.pop(self.current_user, None)
        else:
            self._reconcile[self.current_user] = report

    def kelly_on(self) -> bool:
        flag = self.state.kelly_sizing
        return self.risk.kelly_sizing if flag is None else bool(flag)

    # ------------------------------------------------------------ properties
    @property
    def repo(self) -> AccountRepo:
        return self.account().repo

    @property
    def store(self) -> AccountStore:
        return self.account().store

    @property
    def state(self) -> Any:
        return self.account().store.state

    @property
    def portfolio(self) -> Portfolio:
        return self.account().portfolio

    @property
    def paper(self) -> PaperExecutor:
        return self.account().paper

    @property
    def live(self) -> LiveExecutor | None:
        return self.account().live

    @live.setter
    def live(self, value: LiveExecutor | None) -> None:  # tests and fixtures swap the live executor
        acct = self.account()
        acct.live = value
        acct.executor.live = value

    @property
    def executor(self) -> TradeExecutor:
        return self.account().executor

    @property
    def tickets(self) -> TicketStore:
        return self.account().tickets

    def profile(self, level: RiskLevel | None = None) -> RiskProfile:
        level = level or self.state.risk_level
        p = self.base_profiles[level].with_overrides(self.state.risk_overrides.get(level.value, {}))
        if self.settings.min_edge_cents > p.min_ev_cents:  # MIN_EDGE_CENTS: a floor no profile can go under
            p = p.with_overrides({"min_ev_cents": self.settings.min_edge_cents})
        return p

    def mode_permitted(self, mode: str | None = None) -> bool:
        mode = mode or self.state.mode
        if mode == "paper":
            return True
        if not (self.settings.live_allowed and self.live is not None):
            return False
        # every user other than the owner trades live only with their OWN verified key
        return self.account().is_owner or getattr(self.trading_client, "signer", None) is not None

    # ------------------------------------------------------- Kalshi API key
    @property
    def key_store(self) -> KeyStore:
        acct = self.account()
        base = self.settings.kalshi_key_dir
        return KeyStore(base if acct.is_owner else base / f"user_{acct.user_id}")

    @property
    def trading_client(self) -> Any:
        """Kalshi client signing with the CURRENT user's key (the owner's is the bot's shared client)."""
        acct = self.account()
        return self.client if acct.is_owner else acct.client

    def kalshi_key_source(self) -> str:
        """'env', 'telegram' or 'none' - which credentials the client is signing with."""
        acct = self.account()
        if getattr(self.trading_client, "signer", None) is None:
            return "none"
        return "env" if acct.is_owner and self.settings.env_key_usable else "telegram"

    async def set_kalshi_key(self, key_id: str, pem: bytes) -> float | None:
        """Verify a key against Kalshi, then store it. Returns the balance in USD (None if unverified).

        Raises KalshiAuthError if the key is invalid or Kalshi rejects it; nothing is stored then."""
        client = self.trading_client
        if self.fixture_mode or not hasattr(client, "signer"):
            raise KalshiAuthError(0, "fixture mode does not use Kalshi credentials")
        signer = KalshiSigner(key_id, pem)  # raises on a bad PEM (message never contains key material)
        old = client.signer
        client.signer = signer
        balance: float | None = None
        try:
            raw = await client.get_balance()
            cents_ = raw.get("balance")
            balance = None if cents_ is None else float(cents_) / 100
        except KalshiAuthError:
            client.signer = old
            client.auth_failed = False
            raise
        except KalshiError:
            pass  # network problem: keep the key, report it as not yet verified
        self.key_store.save(key_id, pem)
        if self.ws is not None and self.account().is_owner:
            self.ws.signer = signer
        self.repo.add_event("KALSHI_KEY_SET", f"user {self.current_user} key {key_id[:4]}... via Telegram")
        log.info("KALSHI_KEY_SET user=%s key=%s... verified=%s", self.current_user, key_id[:4], balance is not None)
        return balance

    async def kalshi_account(self) -> dict[str, Any]:
        """Real Kalshi balance/positions for the account screen. Never raises; never returns key material."""
        out: dict[str, Any] = {"source": self.kalshi_key_source(), "balance": None, "positions": None, "error": None}
        if out["source"] == "none":
            return out
        try:
            client = self.trading_client
            raw = await asyncio.wait_for(client.get_balance(), 8)
            out["balance"] = None if raw.get("balance") is None else float(raw["balance"]) / 100
            pos = await asyncio.wait_for(client.get_positions(), 8)
            out["positions"] = sum(1 for p in pos.get("market_positions", []) if p.get("position"))
        except KalshiAuthError:
            out["error"] = "Kalshi rejected the key"
        except (TimeoutError, KalshiError):
            out["error"] = "couldn't reach Kalshi right now"
        return out

    def clear_kalshi_key(self) -> bool:
        removed = self.key_store.clear()
        if self.kalshi_key_source() == "telegram":
            self.trading_client.signer = None
        if removed:
            self.repo.add_event("KALSHI_KEY_REMOVED", f"user {self.current_user} via Telegram")
        return removed

    def pnl_card(self, mode: str, title: str = "TODAY'S P&L", last: Position | None = None,
                 last_direction: str = "") -> PnlCard:
        midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today = list(reversed(self.repo.closed_positions(mode=mode, since=midnight, limit=10_000)))
        all_time = self.portfolio.stats(mode).realized
        return PnlCard(title, mode, today, self.portfolio.balance(mode), all_time, last, last_direction)

    def pnl_dashboard(self, mode: str, days: int = 14, title: str = "P&L OVERVIEW") -> DashboardData:
        """Real finished trades for ``mode`` over the last ``days`` ET days (plus the period before)."""
        today = utcnow().astimezone(ET).date()
        start_et = datetime.combine(today - timedelta(days=days - 1), datetime.min.time(), tzinfo=ET)
        since = start_et.astimezone(UTC)
        prev_since = (start_et - timedelta(days=days)).astimezone(UTC)
        rows_all = self.repo.closed_positions(mode=mode, since=prev_since, limit=100_000)
        rows = [p for p in rows_all if p.closed_at and p.closed_at >= since]
        prev = [p for p in rows_all if p.closed_at and p.closed_at < since]
        trades = []
        for pos in reversed(rows):  # oldest first
            ctx = self.repo.trade_context(pos.id) if pos.id is not None else None
            trades.append(TradeRow(pos, ((ctx or {}).get("entry") or {}).get("direction", "")))
        prev_pnl = sum(p.realized_pnl or 0 for p in prev) if prev else None
        return DashboardData(mode, trades, prev_pnl, self.portfolio.balance(mode), days, title, today,
                             "/".join(self.settings.asset_list) or "BTC")

    async def send_pnl_image(self, kind: str, key: str, caption: str, mode: str, last: Position | None = None,
                             last_direction: str = "", title: str = "TODAY'S P&L") -> bool:
        """P&L image with ``caption``. Returns False when disabled or failed (caller sends text instead)."""
        if not self.settings.pnl_image or self.notifier is None or not hasattr(self.notifier, "photo"):
            return False
        try:
            data = self.pnl_dashboard(mode, title="SESSION SUMMARY" if title.startswith("SESSION") else "P&L OVERVIEW")
            png = await asyncio.to_thread(render_dashboard, data, DISPLAY_NAME)
            uid = self._current.get()
            if uid is not None and getattr(self.notifier, "supports_users", False):
                return await self.notifier.photo(kind, f"{uid}:{key}", png, caption, users=[uid])
            return await self.notifier.photo(kind, key, png, caption)
        except Exception:
            log.exception("PNL_IMAGE_FAILED kind=%s", kind)
            return False

    async def notify(self, kind: str, key: str, text: str, buttons: list | None = None, *,
                     everyone: bool = False) -> None:
        """Alert the user this task acts for (their trades, P&L, limits) - or every user for bot-wide events."""
        if self.notifier is None:
            return
        uid = None if everyone else self._current.get()
        try:
            if uid is not None and getattr(self.notifier, "supports_users", False):
                await self.notifier.alert(kind, f"{uid}:{key}", text, buttons, users=[uid])
            else:
                await self.notifier.alert(kind, key, text, buttons)
        except Exception:
            log.exception("ALERT_FAILED kind=%s", kind)

    # --------------------------------------------------------------- control
    async def start(self) -> None:
        if self.state.running and self._tasks:
            return
        self.store.update(running=True)
        self.started_at = utcnow()
        self.repo.add_event("BOT_STARTED")
        self._spawn(self._discovery_loop(), "discovery")
        self._spawn(self._poll_loop(), "poll")
        self._spawn(self._maintenance_loop(), "maintenance")
        if self.settings.live_allowed:
            self._spawn(self._reconcile_all(), "reconcile")  # crash recovery: resolve leftover live orders
        signer = getattr(self.client, "signer", None)
        if self.settings.use_websocket and signer is not None and not self.fixture_mode:
            self.ws = KalshiWebSocket(self.settings.ws_url, signer, self._on_ws)
            self._spawn(self.ws.run(), "websocket")

    async def _reconcile_all(self) -> None:
        for acct in self.all_accounts():
            with self.as_user(acct.user_id):
                try:
                    await self.reconcile_live()
                except Exception as exc:
                    log.error("RECONCILE_ERROR user=%s error=%r", acct.user_id, exc, exc_info=exc)

    def _all_auto_trade_off(self) -> None:
        for acct in self.all_accounts():
            if acct.store.state.auto_trade:
                acct.store.update(auto_trade=False)

    def _spawn(self, coro: Awaitable[Any], name: str) -> None:
        task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
        task.add_done_callback(self._task_done)
        self._tasks.append(task)

    def _task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("TASK_CRASHED task=%s error=%r", task.get_name(), exc, exc_info=exc)
            self.last_error = f"{task.get_name()} crashed: {exc!r}"

    async def stop(self) -> None:
        self.store.update(running=False, auto_trade=False)
        self._all_auto_trade_off()  # the scanner is shared: stopping it stops every user's auto trade
        if self.ws:
            self.ws.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.repo.add_event("BOT_STOPPED")
        mode = self.state.mode
        await self.send_pnl_image("session_summary", f"summary:{utcnow():%Y%m%d%H%M}",
                                  f"🏁 <b>Bot stopped</b> · today's results ({'real money' if mode == 'live' else 'paper'})",
                                  mode, title="SESSION SUMMARY · TODAY")

    def should_auto_start(self) -> tuple[bool, str]:
        if not self.settings.auto_start:
            return False, "AUTO_START=false"
        if self.state.mode != "paper":
            return False, "auto-start is paper-only; start live trading manually"
        if self.state.emergency_stop:
            return False, "emergency stop is active"
        return True, "ok"

    def pause(self) -> None:
        self.store.update(paused=True)
        self.repo.add_event("TRADING_PAUSED")

    def resume(self) -> None:
        self.store.update(paused=False)
        self.repo.add_event("TRADING_RESUMED")

    async def emergency_stop(self, by: int | None = None) -> None:
        self.store.update(emergency_stop=True, auto_trade=False)
        self._all_auto_trade_off()  # EMERGENCY STOP is bot-wide: every user's auto trade goes off
        self.repo.add_event("EMERGENCY_STOP", f"by={by}", "CRITICAL")
        log_event(log, "EMERGENCY_STOP", logging.CRITICAL, by=by)

    def reset_emergency(self, by: int | None = None) -> None:
        self.store.update(emergency_stop=False)
        self.repo.add_event("EMERGENCY_RESET", f"by={by}", "WARNING")

    def set_risk_level(self, level: RiskLevel, by: int | None = None) -> None:
        self.store.update(risk_level=level)
        self.repo.add_event("RISK_CHANGED", f"level={level.value} by={by}")

    def adjust_risk(self, field: str, delta: float) -> RiskProfile:
        """Customise a profile parameter within HARD_LIMITS (validated by RiskProfile)."""
        level = self.state.risk_level
        current = self.profile(level)
        new_value = getattr(current, field) + delta
        candidate = current.with_overrides({field: new_value})  # raises ConfigError if unsafe
        overrides = dict(self.state.risk_overrides)
        overrides[level.value] = {**overrides.get(level.value, {}), field: getattr(candidate, field)}
        self.store.update(risk_overrides=overrides)
        return candidate

    def reset_risk_overrides(self) -> None:
        overrides = dict(self.state.risk_overrides)
        overrides.pop(self.state.risk_level.value, None)
        self.store.update(risk_overrides=overrides)

    def set_auto_trade(self, on: bool) -> tuple[bool, str]:
        if on and self.state.mode == "live" and self.btc_mode:
            return False, "BTC engine: live orders always need manual confirmation (auto trade is paper only)."
        if on and self.state.mode == "live" and not self.settings.live_autotrade_allowed:
            return False, "Live auto-trading is disabled (ALLOW_LIVE_AUTOTRADE=false)."
        if on and self.state.emergency_stop:
            return False, "Emergency stop is active."
        self.store.update(auto_trade=on)
        return True, f"Auto trade {'ON' if on else 'OFF'}"

    def set_mode(self, mode: str) -> tuple[bool, str]:
        if mode not in ("paper", "live"):
            return False, "invalid mode"
        if mode == "live" and not self.mode_permitted("live"):
            return False, "Live trading is disabled by configuration (LIVE_TRADING / PAPER_TRADING / credentials)."
        changes: dict[str, Any] = {"mode": mode}
        if mode == "live" and (self.btc_mode or not self.settings.live_autotrade_allowed):
            changes["auto_trade"] = False
        self.store.update(**changes)
        self.repo.add_event("MODE_CHANGED", mode, "WARNING" if mode == "live" else "INFO")
        return True, f"Execution mode: {mode.upper()}"

    def set_focus(self, asset: str) -> None:
        self.store.update(focus_asset=asset)

    # ----------------------------------------------------------------- views
    def markets(self) -> list[MarketView]:
        order = {a: i for i, a in enumerate(self.settings.asset_list)}
        infos = sorted(self.discovery.state.current.values(), key=lambda m: (order.get(m.asset, 99), m.asset))
        return [self.view(m.ticker) for m in infos]  # type: ignore[misc]

    def view(self, ticker: str) -> MarketView | None:
        info = self.discovery.state.by_ticker.get(ticker)
        snap = self.market_data.latest.get(ticker)
        if info is None and snap is None:
            return None
        sig = self.engine.latest.get(ticker)
        decision = self.check_risk(sig, snap) if sig and snap else None
        return MarketView(info or snap.info, snap, sig, decision)  # type: ignore[union-attr]

    def focus_view(self) -> MarketView | None:
        views = self.markets()
        if not views:
            return None
        for v in views:
            if v.info.asset == self.state.focus_asset:
                return v
        return views[0]

    def live_data_ok(self) -> bool:
        if not self.market_data.latest:
            return False
        now = utcnow()
        return any(s.age_sec(now) <= self.settings.stale_data_sec for s in self.market_data.latest.values())

    def health(self) -> dict[str, Any]:
        return {
            "running": self.state.running and bool(self._tasks),
            "kalshi": getattr(self.client, "healthy", False),
            "auth_failed": getattr(self.client, "auth_failed", False),
            "ws": bool(self.ws and self.ws.connected),
            "ws_enabled": self.ws is not None,
            "live_data": self.live_data_ok(),
            "markets": len(self.discovery.state.current),
            "last_poll": self.last_poll_at,
            "last_error": self.last_error or self.discovery.state.last_error,
            "fixture": self.fixture_mode,
            "btc_feed": self.btc_feed_status(),
        }

    def btc_feed_status(self) -> Any:
        status = getattr(self.underlying, "status", None)
        return status() if callable(status) else None

    # ------------------------------------------------------------- BTC engine
    def btc_params(self, profile: RiskProfile | None = None) -> BTCParams:
        p = profile or self.profile()
        s = self.settings
        return BTCParams(
            min_ev_cents=p.min_ev_cents, min_strength=p.min_btc_strength, max_prob_width=p.max_prob_width,
            max_spread_cents=p.max_spread_cents, min_liquidity=p.min_liquidity_contracts,
            min_time_sec=max(p.min_time_remaining_sec, s.entry_min_left_sec), max_time_sec=s.entry_max_left_sec,
            blocked_regimes=p.regimes_blocked,
            max_late_move_cents=p.max_late_move_cents, require_calibration=p.require_known_ev,
            fee_rate=s.fee_rate, slippage_cents=s.paper_slippage_cents, kalshi_stale_sec=s.stale_data_sec,
            btc_stale_sec=s.btc_feed_stale_sec, max_skew_sec=s.max_data_skew_sec,
            min_entry_cents=5.0 if s.fair_value_pure else p.min_entry_price_cents,
            max_entry_cents=95.0 if s.fair_value_pure else p.max_entry_price_cents,
            pure=s.fair_value_pure,
        )

    def forward_test_signature(self) -> str | None:
        """Identifies one forward-test rule set (coins excluded: each coin keeps its own start time);
        None when no fair-value forward test is configured."""
        s = self.settings
        if s.strategy_mode != "btc" or not (s.fair_value_pure or set(s.asset_list) - {"BTC"}):
            return None
        return (f"pure={s.fair_value_pure} edge>={s.min_edge_cents:g} "
                f"entry={s.entry_min_left_sec:g}-{s.entry_max_left_sec:g}s left "
                f"skip_after_loss={s.skip_windows_after_loss} max_per_day={s.max_trades_per_day}")

    def _mark_forward_test(self) -> dict[str, Any] | None:
        """Remember when each coin's forward test started. Changing a RULE starts a new test for every coin; adding a
        coin only starts that coin's clock (scripts/forward_report.py counts each coin's trades from its own start)."""
        sig = self.forward_test_signature()
        if sig is None:
            return None
        now = utcnow().isoformat()
        cur = self.repo.get_setting("forward_test")
        if isinstance(cur, dict) and "coins" not in cur:  # older format: "assets=A,B <rules>"
            m = re.match(r"assets=(\S*) (.*)", str(cur.get("config", "")))
            if m:
                cur = {"config": m.group(2), "start": cur.get("start", now),
                       "coins": {a: cur.get("start", now) for a in m.group(1).split(",") if a}}
        if not isinstance(cur, dict) or cur.get("config") != sig:
            cur = {"config": sig, "start": now, "coins": {}}
            log.info("FORWARD_TEST_START %s", sig)
        coins = dict(cur.get("coins") or {})
        for a in self.settings.asset_list:
            if a not in coins:
                coins[a] = now
                log.info("FORWARD_TEST_COIN_START asset=%s", a)
        cur["coins"] = coins
        self.repo.set_setting("forward_test", cur)
        return cur

    def calibration_for(self, asset: str) -> CalibrationReport | None:
        return self.calibrations.get(asset.upper())

    def _kalshi_move(self, ticker: str, now: datetime, window: float = 300) -> float | None:
        hist = [p for p in self.engine.history(ticker) if p.ts <= now]
        if len(hist) < 2:
            return None
        start = [p for p in hist if p.ts >= now - timedelta(seconds=window)]
        if not start or (hist[-1].ts - start[0].ts).total_seconds() < window * 0.8:
            return None
        return hist[-1].mid - start[0].mid

    async def btc_decision(self, snap: MarketSnapshot, now: datetime | None = None,
                           spot: Any = None) -> BTCSignal:
        now = now or utcnow()
        terms = interpret_contract(snap.info, require_rules=self.settings.require_settlement_rules)
        feed = self.underlying
        asset = (snap.info.asset or "BTC").upper()
        if spot is None and self.settings.btc_spot_source == "index" and not self.fixture_mode:
            spot = await self.settlement_index.latest(self.client, snap.info.event_ticker, now, asset)
        if spot is None:
            spot = await feed.get_price(asset)
        if spot is not None and spot.asset.upper() != asset:
            spot = None  # never price one coin with another coin's feed
        bars_fn = getattr(feed, "bars", None)
        bars = bars_fn(asset) if callable(bars_fn) else []
        t_left = snap.time_remaining(now)
        price = spot.price if spot is not None else None
        feats = compute_btc_features(bars, price, now, self.btc_cfg, terms.threshold, t_left,
                                     terms.averaging_window_sec)
        est = estimate_probability(terms, price, bars, now, t_left, self.btc_cfg)
        book = snap.orderbook is not None
        paused = self.adaptive.reason if self.adaptive.mode == "PAUSED" else ""
        inp = BTCInputs(
            now=now, terms=terms, features=feats, regime=classify_regime(feats, self.btc_cfg), estimate=est,
            yes_ask=snap.yes_ask, no_ask=snap.no_ask, yes_bid=snap.yes_bid, no_bid=snap.no_bid, t_left_sec=t_left,
            kalshi_ts=snap.ts, btc_ts=spot.ts if spot is not None else None,
            liquidity={"yes": snap.ask_liquidity("yes") if book else None,
                       "no": snap.ask_liquidity("no") if book else None},
            kalshi_move_5m=self._kalshi_move(snap.ticker, now),
            calibration=self.calibration_for(asset).state if self.calibration_for(asset) else UNCALIBRATED,
            paused_reason=paused, live=self.state.mode == "live",
        )
        return decide(inp, self.btc_params())

    # -------------------------------------------------------------- research
    def _build_model(self) -> HistoricalModel:
        if self.btc_mode:
            # one calibration per coin: BTC's track record never vouches for HYPE or DOGE
            obs = self.repo.observations(source="btc")
            self.calibrations = {
                a: calibration_report([o for o in obs if str(getattr(o, "asset", "")).upper() == a],
                                      self.settings.hist_min_samples, self.settings.calibration_ece_tol)
                for a in self.settings.asset_list}
            first = self.settings.asset_list[0] if self.settings.asset_list else "BTC"
            self.calibration = self.calibrations.get("BTC") or self.calibrations.get(first)
        return HistoricalModel(
            self.repo.observations(source="live"), self.settings.hist_min_samples,
            self.settings.calibration_prior_strength, self.settings.fee_rate, self.settings.paper_slippage_cents,
        )

    def refresh_model(self) -> HistoricalModel:
        self.model = self._build_model()
        self._model_built = time.time()
        self._research.clear()
        return self.model

    def research(self, key: str) -> Any:
        from app.telegram.analytics_views import compute_research

        # trade analytics read the user's own trades, so each user has their own cache entry
        return self._research.get(f"{self.current_user}:{key}", lambda: compute_research(self, key))

    def estimate_for(self, sig: SignalResult, snap: MarketSnapshot) -> HistEstimate | None:
        a = sig.analysis
        side = sig.leaning.side
        price = snap.entry_price(side) if side else None
        if a is None or side is None or price is None:
            return None
        return self.model.estimate(a.grade, a.regime.value, a.time_bucket, side, price, asset=snap.info.asset)

    def record_observation(self, sig: SignalResult, snap: MarketSnapshot, now: datetime | None = None) -> bool:
        now = now or utcnow()
        obs = from_signal(sig, snap, now)
        if obs is None or (obs.ticker, obs.minute) in self._obs_keys:
            return False
        self._obs_keys.add((obs.ticker, obs.minute))
        return self.repo.add_observation(obs)

    def record_btc_observation(self, btc: BTCSignal, snap: MarketSnapshot, now: datetime | None = None) -> bool:
        now = now or utcnow()
        obs = from_btc_signal(btc, snap, now)
        if obs is None or ("btc:" + obs.ticker, obs.minute) in self._obs_keys:
            return False
        self._obs_keys.add(("btc:" + obs.ticker, obs.minute))
        return self.repo.add_observation(obs)

    async def label_observations(self, now: datetime | None = None) -> int:
        """Attach real market results to observations of closed markets (never guessed)."""
        now = now or utcnow()
        labelled = 0
        for ticker in self.repo.unlabeled_tickers(now - timedelta(seconds=10)):
            try:
                raw = await self.client.get_market(ticker)
            except KalshiError as exc:
                log_event(log, "LABEL_FAILED", logging.DEBUG, ticker=ticker, error=exc)
                continue
            result = str(raw.get("result") or "")
            if result in ("yes", "no"):
                labelled += self.repo.label_observations(ticker, result)
        self._obs_keys = {k for k in self._obs_keys if k[0].removeprefix("btc:") in self.discovery.state.by_ticker}
        return labelled

    def update_adaptive(self) -> AdaptiveState:
        cur = [s.features["volatility"] for s in self.engine.latest.values()
               if s.features.get("volatility") is not None and s.validity.value == "VALID"]
        hist = [o.features["volatility"] for o in self.model.obs if "volatility" in o.features]
        recent = [(bool(c["won"]), float(c["entry"].get("p_model") or 0.5))
                  for c in reversed(self.repo.trade_contexts(limit=50))]
        data_ok = (not self.state.running) or not self.discovery.state.current or self.live_data_ok()
        new = assess(cur, hist, recent, data_ok, min_recent=20)
        if new.mode != self.adaptive.mode:
            log_event(log, "ADAPTIVE_MODE", mode=new.mode, reason=new.reason)
        self.adaptive = new
        return new

    # ------------------------------------------------------------ risk/trade
    def check_risk(self, sig: SignalResult, snap: MarketSnapshot, now: datetime | None = None) -> RiskDecision:
        st = self.state
        ctx = self.portfolio.risk_context(
            mode=st.mode, trading_enabled=st.trading_allowed, disabled_reason=st.disabled_reason,
            mode_permitted=self.mode_permitted(),
        )
        ctx = replace(ctx, adaptive_mode=self.adaptive.mode, adaptive_reason=self.adaptive.reason)
        return self.risk.evaluate(sig, snap, self.profile(), ctx, now, estimate=self.estimate_for(sig, snap),
                                  kelly=self.state.kelly_sizing)  # None = the KELLY_SIZING default

    def make_ticket(self, sig: SignalResult, decision: RiskDecision, snap: MarketSnapshot,
                    origin: str = "manual") -> TradeTicket:
        assert decision.approved and decision.side and decision.limit_price_cents is not None
        direction = sig.btc.leaning if sig.btc is not None else sig.leaning.value
        return self.tickets.add(TradeTicket(
            ticker=snap.ticker, label=snap.info.label, side=decision.side, direction=direction,
            contracts=decision.position_size, limit_price_cents=decision.limit_price_cents,
            quoted_price_cents=decision.price_cents or decision.limit_price_cents,
            cost_usd=decision.cost_usd, fee_usd=decision.fee_usd, risk_level=decision.risk_level.value,
            confidence=sig.confidence, mode=self.state.mode, origin=origin,
        ))

    def propose(self, ticker: str) -> tuple[TradeTicket | None, MarketView | None]:
        view = self.view(ticker)
        if not view or not view.decision or not view.signal or not view.snapshot:
            return None, view
        if not view.decision.approved:
            self.repo.add_risk_decision(ticker, view.decision)
            return None, view
        return self.make_ticket(view.signal, view.decision, view.snapshot), view

    async def _revalidate(self, ticket: TradeTicket) -> tuple[RiskDecision, MarketSnapshot]:
        info = self.discovery.state.by_ticker.get(ticket.ticker)
        snap = await self.market_data.poll(info) if info else self.market_data.latest[ticket.ticker]
        sig = await self.evaluate_snapshot(snap)
        decision = self.check_risk(sig, snap)
        self.repo.add_risk_decision(ticket.ticker, decision)
        log_event(log, "RISK_APPROVED" if decision.approved else "RISK_REJECTED",
                  ticker=ticket.ticker, profile=decision.risk_level.title, contracts=decision.position_size,
                  reason=decision.reason)
        return decision, snap

    async def execute_ticket(self, ticket: TradeTicket, *, confirmed: bool) -> Position:
        pos = await self.executor.execute(ticket, confirmed=confirmed, revalidate=self._revalidate)
        sig = self._last_signal.get(pos.ticker)
        snap = self.market_data.latest.get(pos.ticker)
        if sig is not None and snap is not None and pos.id is not None:
            entry = entry_snapshot(sig, snap, utcnow())
            est = self.estimate_for(sig, snap)
            entry["p_model"] = est.p_model if est and est.p_model is not None else pos.entry_price / 100
            entry["ev_cents"] = est.ev_cents if est else None
            if sig.btc is not None and sig.btc.edge is not None:
                b = sig.btc
                entry["p_model"] = b.edge.p_point
                entry["ev_cents"] = b.edge.ev_cents
                entry["btc"] = {
                    "p_low": b.edge.p_low, "ev_low_cents": b.edge.ev_low_cents, "ask": b.edge.ask,
                    "cost_cents": b.edge.cost_cents, "strength": b.strength, "regime": b.regime.value,
                    "calibration": b.calibration.status, "threshold": b.terms.threshold,
                    "spot": b.features.spot, "dist_sigma": b.features.dist_sigma,
                    "warnings": list(b.warnings), "risk_level": self.state.risk_level.value,
                }
            entry["direction"], entry["why"] = self._card_read(sig, pos)
            self.repo.save_trade_context(pos.id, pos.ticker, entry=entry)
        self.repo.add_event("TRADE_OPENED", f"{pos.mode} {pos.ticker} {pos.side} x{pos.contracts}@{pos.entry_price}")
        await self.notify("trade_opened", f"open:{pos.id}", messages.trade_opened_alert(pos, *self._card_meta(pos)))
        return pos

    async def close_position(self, position_id: int, *, confirmed: bool) -> float:
        pos = self.portfolio.positions.get(position_id)
        if pos is None:
            raise TradingError("position not found or already closed")
        info = self.discovery.state.by_ticker.get(pos.ticker)
        snap = await self.market_data.poll(info) if info else self.market_data.latest.get(pos.ticker)
        if snap is None:
            raise TradingError("no market data for this position")
        pnl = await self.executor.close(pos, snap, confirmed=confirmed)
        await self._after_close(pos)
        return pnl

    @staticmethod
    def _card_read(sig: SignalResult, pos: Position) -> tuple[str, str]:
        """UP/DOWN meaning of the bought side and a one-line reason, for the trade cards."""
        b = sig.btc
        if b is not None and b.terms.kind is not None:
            direction = b.terms.direction_for_side(pos.side)
            if b.edge is not None:
                return direction, (f"bot's estimate {b.edge.p_low * 100:.0f}%+ vs price {b.edge.ask:.0f}¢ "
                                   f"(estimate, not a guarantee)")
            return direction, ""
        direction = "UP" if pos.side == "yes" else "DOWN"
        top = next((r.text for r in sig.reasons if r.positive), "")
        return direction, top

    def _card_meta(self, pos: Position, with_summary: bool = False) -> tuple:
        ctx = self.repo.trade_context(pos.id) if pos.id is not None else None
        entry = (ctx or {}).get("entry", {})
        direction = entry.get("direction") or ("UP" if pos.side == "yes" else "DOWN")
        why = entry.get("why") or ""
        if with_summary:
            return direction, why, (ctx or {}).get("summary") or ""
        return direction, why

    def _analyze_close(self, pos: Position) -> None:
        if pos.id is None:
            return
        ctx = self.repo.trade_context(pos.id)
        if ctx is None:
            return
        exit_ = exit_snapshot(self._last_signal.get(pos.ticker))
        tags = analyze_trade(ctx["entry"], exit_, pos.won)
        self.repo.save_trade_context(pos.id, pos.ticker, exit=exit_, tags=tags, won=pos.won,
                                     summary=describe(tags, pos.won))

    async def _after_close(self, pos: Position) -> None:
        self._analyze_close(pos)
        direction, _why, summary = self._card_meta(pos, with_summary=True)
        text = messages.trade_closed_alert(pos, direction, summary)
        if not await self.send_pnl_image("trade_closed", f"close:{pos.id}", text, pos.mode, pos, direction):
            await self.notify("trade_closed", f"close:{pos.id}", text)
        limit = self.settings.large_loss_alert_usd
        if limit > 0 and (pos.realized_pnl or 0) <= -limit:
            await self.notify("large_loss", f"loss:{pos.id}", messages.large_loss_alert(pos))
        prof = self.profile()
        today = day_key()
        if -self.portfolio.daily_realized(pos.mode) >= prof.max_daily_loss_usd and self._daily_limit_alerted != today:
            self._daily_limit_alerted = today
            await self.notify("daily_limit", f"daily:{today}", messages.daily_limit_alert(prof))

    # ----------------------------------------------------------------- loops
    async def run_discovery(self) -> None:
        before = set(self.discovery.state.by_ticker)
        st = await self.discovery.discover()
        for ticker, info in st.by_ticker.items():
            if ticker not in before:
                self.repo.upsert_market(info)
        for ticker in before - set(st.by_ticker):
            self.market_data.drop(ticker)
            self.engine.forget(ticker)
        keep = set(st.by_ticker) | {p.ticker for p in self.portfolio.open_positions()}
        self._last_signal = {k: v for k, v in self._last_signal.items() if k in keep}
        if self.ws:
            self.ws.set_markets(m.ticker for m in st.current.values())

    async def _discovery_loop(self) -> None:
        attempt = 0
        while True:
            try:
                await self.run_discovery()
                attempt = 0
            except KalshiError as exc:
                attempt += 1
                self.last_error = f"discovery: {exc}"
                log_event(log, "DISCOVERY_FAILED", logging.WARNING, error=exc)
            except Exception as exc:  # unexpected: log with traceback and retry with backoff instead of dying
                attempt += 1
                self.last_error = f"discovery crashed: {exc!r}"
                log.error("DISCOVERY_ERROR error=%r", exc, exc_info=exc)
            self._rediscover.clear()
            timeout = self.settings.discovery_interval_sec * (1 if attempt == 0 else min(8, 2**attempt) / 2)
            try:
                await asyncio.wait_for(self._rediscover.wait(), timeout)
            except TimeoutError:
                pass

    async def poll_once(self) -> list[SignalResult]:
        results = []
        if self.discovery.prune_expired():
            self._rediscover.set()
        for info in list(self.discovery.state.current.values()):
            try:
                snap = await self.market_data.poll(info)
            except KalshiAuthError as exc:
                self.last_error = str(exc)
                await self.notify("auth_failed", "auth", "🚨 <b>KALSHI AUTH FAILED</b>\nCheck API key / private key.")
                continue
            except KalshiError as exc:
                self.last_error = f"{info.ticker}: {exc}"
                log_event(log, "POLL_FAILED", logging.WARNING, ticker=info.ticker, error=exc)
                continue
            try:
                results.append(await self.process_snapshot(snap))
            except Exception as exc:  # one bad market must not stop the scanner; logged loudly, not ignored
                self.last_error = f"{info.ticker}: processing failed: {exc!r}"
                log.error("PROCESS_FAILED ticker=%s error=%r", info.ticker, exc, exc_info=exc)
        self.last_poll_at = utcnow()
        await self._connectivity_alerts()
        return results

    async def _connectivity_alerts(self) -> None:
        healthy = getattr(self.client, "healthy", True)
        if not healthy and not self._kalshi_down_notified and self.discovery.state.current:
            self._kalshi_down_notified = True
            await self.notify("kalshi_down", "kalshi_down", "⚠️ <b>KALSHI DISCONNECTED</b>\nRetrying with backoff…")
        elif healthy and self._kalshi_down_notified:
            self._kalshi_down_notified = False
            await self.notify("kalshi_up", "kalshi_up", "🟢 <b>MARKET DATA RECONNECTED</b>")

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as exc:  # keep scanning; the error is logged with traceback and shown in /health
                self.last_error = f"poll loop: {exc!r}"
                log.error("POLL_LOOP_ERROR error=%r", exc, exc_info=exc)
            await asyncio.sleep(self.settings.poll_interval_sec)

    async def evaluate_snapshot(self, snap: MarketSnapshot) -> SignalResult:
        """The single evaluation path (polling, revalidation before execution)."""
        prof = self.profile()
        spot, spot_hist = None, None
        if self.underlying.name != "none":
            spot = await self.underlying.get_price(snap.info.asset)
            spot_hist = self.underlying.history(snap.info.asset)
        sig = self.engine.evaluate(snap, threshold=prof.min_confidence, min_quality=prof.min_signal_quality,
                                   threshold_label=prof.level.title, spot=spot, spot_history=spot_hist)
        if self.btc_mode:
            # BTC_SPOT_SOURCE=index: let btc_decision read Kalshi's settlement index (falls back to Coinbase)
            btc_spot = None if self.settings.btc_spot_source == "index" else spot
            sig = replace(sig, btc=await self.btc_decision(snap, spot=btc_spot))
            self.engine.latest[snap.ticker] = sig
        self._last_signal[snap.ticker] = sig
        return sig

    async def process_snapshot(self, snap: MarketSnapshot) -> SignalResult:
        sig = await self.evaluate_snapshot(snap)
        self._persist(snap, sig)
        self.record_observation(sig, snap)
        if sig.btc is not None:
            self.record_btc_observation(sig.btc, snap)

        flip = self.engine.detect_flip(sig)
        if flip and self.settings.signal_alerts:
            await self.notify("flip", f"flip:{snap.ticker}", messages.flip_alert(flip), everyone=True)
        # One shared signal; every user's own risk check, sizing, exits and auto trade.
        for acct in self.all_accounts():
            with self.as_user(acct.user_id):
                try:
                    await self._process_for_user(sig, snap)
                except Exception as exc:  # one user's problem must never stop the others
                    log.error("USER_PROCESS_ERROR user=%s ticker=%s error=%r", acct.user_id, snap.ticker, exc,
                              exc_info=exc)
        return sig

    async def _process_for_user(self, sig: SignalResult, snap: MarketSnapshot) -> None:
        decision = self.check_risk(sig, snap)
        uid = self.current_user
        strong = (decision.approved and sig.grade in ("A+", "A")
                  and sig.confidence >= self.settings.strong_signal_alert_min_confidence)
        quiet = not self.settings.signal_alerts and self.state.auto_trade
        if strong and not quiet and self._strong.get(f"{uid}:{snap.ticker}") != sig.direction.value:
            self._strong[f"{uid}:{snap.ticker}"] = sig.direction.value
            text, buttons = messages.strong_signal_alert(sig, snap, decision, self.state)
            await self.notify("strong_signal", f"strong:{snap.ticker}:{sig.direction.value}", text, buttons)
        elif not strong and (sig.grade not in ("A+", "A")
                             or sig.confidence < self.settings.strong_signal_alert_min_confidence - 5):
            self._strong.pop(f"{uid}:{snap.ticker}", None)  # hysteresis: re-arm only after it clearly weakens
        if self.settings.early_exit or self.settings.take_profit_cents or self.settings.take_profit_price_cents:
            await self._manage_exits(sig, snap)
        if self.state.auto_trade and decision.approved:
            await self._auto_trade(sig, snap, decision)

    # ------------------------------------------------------------ early exits
    def exit_reason(self, pos: Position, sig: SignalResult, snap: MarketSnapshot) -> str | None:
        """Why the open position should be cashed out now, or None. Uses only current data."""
        s = self.settings
        bid = snap.exit_price(pos.side)
        if s.early_exit_stop_loss_pct > 0 and bid is not None and \
                bid <= pos.entry_price * (1 - s.early_exit_stop_loss_pct / 100):
            return f"stop-loss: value down {s.early_exit_stop_loss_pct:g}%+"
        if bid is not None and s.take_profit_price_cents > 0 and bid >= s.take_profit_price_cents:
            return f"take-profit: bid {bid:.0f}¢ reached the {s.take_profit_price_cents:.0f}¢ target"
        if bid is not None and s.take_profit_cents > 0 and bid - pos.entry_price >= s.take_profit_cents:
            return f"take-profit: bid {bid:.0f}¢ is {bid - pos.entry_price:.0f}¢ above entry"
        b = sig.btc
        if b is not None and b.p_yes is not None:
            p_side = b.p_yes if pos.side == "yes" else 1 - b.p_yes
            if p_side < s.early_exit_btc_p_side:
                return f"bot's odds for this side fell to {p_side:.0%}"
            return None
        direction = self._card_meta(pos)[0]
        lean = sig.leaning.value
        if lean in ("UP", "DOWN") and lean != direction and sig.confidence >= s.early_exit_flip_confidence:
            return f"signal flipped to {lean} ({sig.confidence}%)"
        return None

    async def _manage_exits(self, sig: SignalResult, snap: MarketSnapshot) -> None:
        s = self.settings
        for pos in [p for p in self.portfolio.open_positions() if p.ticker == snap.ticker and p.id is not None]:
            reason = self.exit_reason(pos, sig, snap)
            if reason is None:
                self._exit_ticks.pop(pos.id, None)
                continue
            n = self._exit_ticks[pos.id] = self._exit_ticks.get(pos.id, 0) + 1
            bid = snap.exit_price(pos.side)
            tp = reason.startswith("take-profit")
            if not tp and not s.early_exit:
                continue  # early exit off: only take-profit rules apply
            if (n < s.early_exit_confirm_ticks or bid is None
                    or (not tp and (bid < s.early_exit_min_bid_cents
                                    or snap.time_remaining() < s.early_exit_min_t_left_sec))):
                continue
            if pos.mode == "live":
                if pos.id not in self._exit_suggested:  # never sell real money without a tap
                    self._exit_suggested.add(pos.id)
                    await self.notify("exit_suggest", f"exit:{pos.id}",
                                      messages.exit_suggestion(pos, self._card_meta(pos)[0], reason, bid),
                                      [[("💰 Sell now", f"pcl:{pos.id}"), ("✋ Keep", "home")]])
                continue
            try:
                await self.executor.close(pos, snap, confirmed=True, reason=f"early exit: {reason}")
            except TradingError as exc:
                log_event(log, "EARLY_EXIT_FAILED", logging.WARNING, ticker=pos.ticker, error=exc)
                continue
            self._exit_ticks.pop(pos.id, None)
            self._early_exits.setdefault(pos.ticker, []).append(pos)
            log_event(log, "EARLY_EXIT", ticker=pos.ticker, side=pos.side, reason=reason, pnl=pos.realized_pnl)
            await self._after_close(pos)

    @property
    def _exit_stats_path(self) -> Path:
        acct = self.account()
        name = "early_exit_stats.json" if acct.is_owner else f"early_exit_stats_{acct.user_id}.json"
        return self.settings.kalshi_key_dir.parent / name

    def early_exit_stats(self) -> dict[str, float]:
        try:
            return json.loads(self._exit_stats_path.read_text())
        except (OSError, ValueError):
            return {"exits": 0, "saved": 0.0, "helped": 0, "hurt": 0}

    async def score_early_exits(self, now: datetime | None = None) -> int:
        """After the market settles, compare each cash-out with simply holding (real result only)."""
        now = now or utcnow()
        scored = 0
        for ticker, plist in list(self._early_exits.items()):
            close = plist[0].close_time
            if close is None or now < close + timedelta(seconds=5):
                continue
            try:
                raw = await self.client.get_market(ticker)
            except KalshiError:
                continue
            result = str(raw.get("result") or "")
            if result not in ("yes", "no"):
                continue
            del self._early_exits[ticker]
            for uid in sorted({p.account_id or self.owner for p in plist}):
                if not self.allowed_user(uid):
                    continue  # user removed from TELEGRAM_ADMIN_IDS: nothing to tell them
                with self.as_user(uid):  # each user's scorecard and alerts are their own
                    scored += await self._score_exits([p for p in plist if (p.account_id or self.owner) == uid],
                                                      result)
        return scored

    async def _score_exits(self, plist: list[Position], result: str) -> int:
        st = self.early_exit_stats()
        scored = 0
        for pos in plist:
            hold = round(pos.contracts * (100 if result == pos.side else 0) / 100 - pos.cost - pos.entry_fee, 2)
            saved = round((pos.realized_pnl or 0) - hold, 2)
            if "take-profit" in (pos.close_reason or ""):  # separate scorecard for take-profit exits
                st["tp_exits"] = st.get("tp_exits", 0) + 1
                st["tp_saved"] = round(st.get("tp_saved", 0.0) + saved, 2)
                key = "tp_helped" if saved > 0 else "tp_hurt"
                st[key] = st.get(key, 0) + (1 if saved != 0 else 0)
            st["exits"] += 1
            st["saved"] = round(st["saved"] + saved, 2)
            st["helped" if saved > 0 else "hurt"] += 1 if saved != 0 else 0
            scored += 1
            await self.notify("exit_check", f"exitcheck:{pos.id}",
                              messages.exit_check(pos, self._card_meta(pos)[0], result, hold, saved, st))
        try:
            self._exit_stats_path.parent.mkdir(parents=True, exist_ok=True)
            self._exit_stats_path.write_text(json.dumps(st))
        except OSError:
            log.warning("EARLY_EXIT_STATS_SAVE_FAILED")
        return scored

    async def _auto_trade(self, sig: SignalResult, snap: MarketSnapshot, decision: RiskDecision) -> None:
        if self.state.mode == "live" and (self.btc_mode or not self.settings.live_autotrade_allowed):
            return  # BTC engine: every live order needs a manual confirmation
        ticket = self.make_ticket(sig, decision, snap, origin="auto")
        self.tickets.discard(ticket.id)
        try:
            await self.execute_ticket(ticket, confirmed=False)
        except TradingError as exc:
            log_event(log, "AUTO_TRADE_SKIPPED", reason=exc)

    def _persist(self, snap: MarketSnapshot, sig: SignalResult) -> None:
        now = time.time()
        if now - self._snap_saved.get(snap.ticker, 0) >= self.settings.snapshot_interval_sec:
            self._snap_saved[snap.ticker] = now
            self.repo.add_snapshot(snap, sig.features.get("book_imbalance"))
        prev = self._sig_saved.get(snap.ticker)
        if prev is None or prev[0] != sig.direction.value or now - prev[1] >= 60:
            self._sig_saved[snap.ticker] = (sig.direction.value, now)
            self.repo.add_signal(sig)

    def _on_ws(self, mtype: str, msg: dict[str, Any]) -> None:
        if mtype == "trade":
            self.market_data.apply_ws_trade(msg)
            return
        snap = self.market_data.apply_ws_ticker(msg)
        if snap:
            self.engine.record(snap)

    async def reconcile_live(self, periodic: bool = False, now: datetime | None = None) -> ReconcileReport | None:
        """Compare the bot's live positions/orders with Kalshi's. Mismatched markets are blocked and reported."""
        client = self.trading_client  # THIS user's key: their orders and positions only
        if self.live is None or getattr(client, "signer", None) is None:
            return None
        now = now or utcnow()
        if periodic and self.reconcile_report and (now - self.reconcile_report.at).total_seconds() < 300:
            return self.reconcile_report
        report = ReconcileReport(at=now)
        try:
            report.recovered, blocked = await recover_orders(client, self.repo)
            report.mismatches = compare_positions(self.portfolio.open_positions("live"),
                                                  await client.get_market_positions())
        except KalshiError as exc:
            report.ok, report.error = False, f"could not reach Kalshi: {exc}"
            self.reconcile_report = report
            return report
        report.blocked = blocked | {m.ticker for m in report.mismatches if m.tracked}
        report.ok = not report.blocked
        self.portfolio.unresolved |= report.blocked
        self.reconcile_report = report
        alerts = [n for n in report.recovered if n.startswith("⚠️")] + [m.text for m in report.mismatches]
        if alerts:
            log_event(log, "RECONCILE_MISMATCH", logging.ERROR, n=len(alerts))
            body = "\n".join(f"• {html.escape(a)}" for a in alerts[:10])
            await self.notify("reconcile", "reconcile:" + "|".join(sorted(alerts))[:200],
                              "🚨 <b>POSITION CHECK vs KALSHI</b>\n" + body +
                              "\n\nMarkets the bot tracks with a mismatch are blocked for new orders.")
        return report

    def reset_paper_account(self, by: int | None = None, keep_backups: int = 2) -> dict[str, Any]:
        """Wipe PAPER history and balance (back to PAPER_STARTING_BALANCE). Live data and research are untouched.

        The SQLite database is backed up first (only the newest ``keep_backups`` reset backups are kept)."""
        if any(p.mode == "paper" for p in self.portfolio.open_positions()) and self.portfolio.inflight:
            raise TradingError("an order is in flight - try again in a moment")
        backup = None
        url = self.settings.database_url
        if url.startswith("sqlite:///"):
            db_file = Path(url.split("sqlite:///", 1)[1])
            if db_file.exists():
                backup = db_file.with_name(f"{db_file.name}.reset-backup-{utcnow():%Y%m%d-%H%M%S}")
                shutil.copy2(db_file, backup)
                for old in sorted(db_file.parent.glob(f"{db_file.name}.reset-backup-*"))[:-keep_backups]:
                    old.unlink(missing_ok=True)
        counts = self.repo.reset_paper()
        for pid, pos in list(self.portfolio.positions.items()):
            if pos.mode == "paper":
                del self.portfolio.positions[pid]
        self.portfolio.last_trade_at.clear()
        self.portfolio.last_loss_at = None
        self.repo.add_event("PAPER_RESET", f"by={by} {counts}", "WARNING")
        log_event(log, "PAPER_RESET", by=by, **counts)
        return {"counts": counts, "backup": backup.name if backup else None,
                "balance": self.portfolio.paper_balance()}

    async def send_daily_recap(self, now: datetime | None = None) -> str | None:
        """After each UTC day closes, send one recap of that day (once; survives restarts). Returns the day."""
        now = now or utcnow()
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        day = start.strftime("%Y-%m-%d")
        key = "daily_recap_last" if self.account().is_owner else f"daily_recap_last:{self.current_user}"
        if self.repo.get_setting(key) == day or self.notifier is None:
            return None
        self.repo.set_setting(key, day)  # mark first: a send failure must never cause spam
        mode = self.state.mode
        closed = [p for p in self.repo.closed_positions(mode=mode, since=start, limit=10_000)
                  if p.closed_at is not None and p.closed_at < end]
        if not closed and not self.state.running:
            return day  # nothing happened and the bot is off: stay quiet
        await self.notify("daily_recap", f"recap:{day}", messages.daily_recap(day, mode, closed))
        return day

    async def refresh_gold(self, force: bool = False) -> GoldStatus | None:
        """Gold research status (discovery + rule check + quotes + price). Never trades."""
        if not self.settings.gold_enabled:
            return None
        if not force and self.gold and (utcnow() - self.gold.at).total_seconds() < 60:
            return self.gold
        cats = [c.strip() for c in self.settings.gold_categories.split(",") if c.strip()]
        self.gold = await gold_status(self.client, self.gold_provider, cats)
        return self.gold

    async def settle_positions(self, now: datetime | None = None) -> list[Position]:
        """Settle every user's positions whose market has closed and has a published result."""
        now = now or utcnow()
        results: dict[str, str | None] = {}  # one Kalshi lookup per market, however many users hold it
        settled: list[Position] = []
        for acct in self.all_accounts():
            with self.as_user(acct.user_id):
                settled += await self._settle_user(now, results)
        await self.score_early_exits(now)
        return settled

    async def _settle_user(self, now: datetime, results: dict[str, str | None]) -> list[Position]:
        settled = []
        for pos in list(self.portfolio.open_positions()):
            if pos.close_time is None or now < pos.close_time + timedelta(seconds=5):
                continue
            if pos.ticker not in results:
                try:
                    raw = await self.client.get_market(pos.ticker)
                except KalshiError as exc:
                    log_event(log, "SETTLEMENT_CHECK_FAILED", logging.WARNING, ticker=pos.ticker, error=exc)
                    continue
                results[pos.ticker] = str(raw.get("result") or "")
            result = results[pos.ticker] or ""
            if result not in ("yes", "no"):
                continue  # not determined yet - never guess an outcome
            async with self.executor.lock_for(pos.ticker):  # no race with a manual/early exit
                if not pos.is_open:
                    continue
                if pos.mode == "paper":
                    self.paper.settle(pos, result)
                else:
                    self.portfolio.close(pos, 100.0 if result == pos.side else 0.0, 0.0,
                                         f"settled {result.upper()}", action="SETTLE")
            settled.append(pos)
            await self._after_close(pos)
        return settled

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await self.settle_positions()
                labelled = await self.label_observations()
                if labelled or time.time() - self._model_built > self.settings.model_refresh_sec:
                    self.refresh_model()
                self.update_adaptive()
                for acct in self.all_accounts():  # each user's own Kalshi balance, with their own key
                    with self.as_user(acct.user_id):
                        if self.state.mode == "live" and self.live is not None \
                                and getattr(self.trading_client, "signer", None) is not None:
                            try:
                                bal = await self.trading_client.get_balance()
                                self.portfolio.live_balance = float(bal.get("balance", 0)) / 100
                            except KalshiError as exc:
                                log_event(log, "LIVE_BALANCE_FAILED", logging.WARNING, user=acct.user_id,
                                          error=exc)
                if time.time() - self._last_purge > 3600:
                    self._last_purge = time.time()
                    purged = self.repo.purge(self.settings.snapshot_retention_days,
                                             self.settings.signal_retention_days,
                                             self.settings.event_retention_days,
                                             self.settings.observation_retention_days)
                    log_event(log, "RETENTION_PURGE", **purged)
            except KalshiError as exc:
                log_event(log, "MAINTENANCE_FAILED", logging.WARNING, error=exc)
            except Exception as exc:  # keep settling and labelling; logged with traceback, shown in /health
                self.last_error = f"maintenance: {exc!r}"
                log.error("MAINTENANCE_ERROR error=%r", exc, exc_info=exc)
            if self.settings.daily_recap:
                for acct in self.all_accounts():
                    with self.as_user(acct.user_id):
                        try:
                            await self.send_daily_recap()
                        except Exception as exc:
                            log.error("DAILY_RECAP_ERROR user=%s error=%r", acct.user_id, exc, exc_info=exc)
            if self.settings.gold_enabled:
                try:
                    await self.refresh_gold()
                except Exception as exc:
                    log.error("GOLD_STATUS_ERROR error=%r", exc, exc_info=exc)
            for acct in self.all_accounts():
                with self.as_user(acct.user_id):
                    if self.state.mode == "live" and self.live is not None:
                        try:
                            await self.reconcile_live(periodic=True)
                        except Exception as exc:
                            log.error("RECONCILE_ERROR user=%s error=%r", acct.user_id, exc, exc_info=exc)
            await asyncio.sleep(15)

    async def shutdown(self) -> None:
        if self._tasks:
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        self.store.state.running = False
        await self.underlying.close()
        if self.gold_provider is not None:
            await self.gold_provider.close()
        await self.client.close()


RuntimeFactory = Callable[[], BotRuntime]
