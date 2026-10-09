"""BotRuntime: wires discovery -> market data -> signals -> risk -> execution.

The Telegram layer only talks to this class; it never touches executors,
the Kalshi client or the database directly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.config import Settings
from app.data.underlying import NullUnderlyingProvider, UnderlyingPriceProvider
from app.database.repository import Repository
from app.errors import KalshiAuthError, KalshiError, TradingError
from app.kalshi.market_data import MarketDataService, MarketInfo, MarketSnapshot
from app.kalshi.markets import MarketDiscovery
from app.kalshi.websocket import KalshiWebSocket
from app.risk.manager import RiskDecision, RiskManager
from app.risk.profiles import RiskLevel, RiskProfile, load_profiles
from app.risk.sizing import PositionSizer
from app.state import BotState, StateStore
from app.research.adaptive import AdaptiveState, assess
from app.research.calibration import HistEstimate, HistoricalModel
from app.research.dataset import from_signal
from app.research.tradeanalysis import analyze_trade, describe, entry_snapshot, exit_snapshot
from app.strategy.config import StrategyConfig
from app.strategy.engine import SignalEngine
from app.telegram import messages
from app.strategy.signals import Direction, SignalResult
from app.trading.executor import TradeExecutor
from app.trading.live import LiveExecutor
from app.trading.paper import PaperExecutor
from app.trading.portfolio import Portfolio
from app.trading.positions import Position
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
        self.repo = repo
        self.client = client
        self.fixture_mode = settings.data_source == "fixture"
        self.underlying = underlying or NullUnderlyingProvider()
        self.base_profiles = profiles or load_profiles()

        defaults = BotState(
            risk_level=RiskLevel(settings.default_risk_level),
            auto_trade=settings.auto_trading,
            mode="live" if settings.live_allowed else "paper",
        )
        self.store = StateStore(repo, defaults)
        if self.store.state.mode == "live" and not settings.live_allowed:
            self.store.state.mode = "paper"  # config no longer allows live -> fail safe
        if self.store.state.auto_trade and self.store.state.mode == "live" and not settings.live_autotrade_allowed:
            self.store.state.auto_trade = False
        self.store.save()

        self.discovery = MarketDiscovery(client, settings)
        self.market_data = MarketDataService(client, settings.orderbook_depth)
        self.strategy_config = StrategyConfig.from_settings(settings)
        self.engine = SignalEngine(settings.signal_history_size, settings.signal_min_confidence,
                                   settings.stale_data_sec, min_history_sec=settings.signal_min_history_sec,
                                   config=self.strategy_config)
        self.model = self._build_model()
        self._model_built = time.time()
        self.adaptive = AdaptiveState("NORMAL")
        self._obs_keys: set[tuple[str, int]] = set()
        self._last_signal: dict[str, SignalResult] = {}  # survives market removal (for exit analysis)
        from app.telegram.analytics_views import ResearchCache

        self._research = ResearchCache()
        self._strong: dict[str, str] = {}  # ticker -> direction currently alerted as strong
        self.risk = RiskManager(PositionSizer(settings.fee_rate), settings.stale_data_sec,
                                settings.order_price_tolerance_cents, ev_gate=settings.ev_gate,
                                strategy_mode=settings.strategy_mode)
        self.portfolio = Portfolio(repo, settings.paper_starting_balance)
        self.paper = PaperExecutor(self.portfolio, settings.paper_slippage_cents, settings.fee_rate)
        # The live executor is only constructed when configuration permits live trading.
        self.live = LiveExecutor(client, settings, self.portfolio) if settings.live_allowed else None
        self.executor = TradeExecutor(self.store, self.portfolio, self.paper, self.live)
        self.tickets = TicketStore()
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

    # ------------------------------------------------------------ properties
    @property
    def state(self) -> BotState:
        return self.store.state

    def profile(self, level: RiskLevel | None = None) -> RiskProfile:
        level = level or self.state.risk_level
        return self.base_profiles[level].with_overrides(self.state.risk_overrides.get(level.value, {}))

    def mode_permitted(self, mode: str | None = None) -> bool:
        mode = mode or self.state.mode
        return mode == "paper" or (self.settings.live_allowed and self.live is not None)

    async def notify(self, kind: str, key: str, text: str, buttons: list | None = None) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.alert(kind, key, text, buttons)
        except Exception:  # noqa: BLE001 - alert failures must never kill trading loops
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
        signer = getattr(self.client, "signer", None)
        if self.settings.use_websocket and signer is not None and not self.fixture_mode:
            self.ws = KalshiWebSocket(self.settings.ws_url, signer, self._on_ws)
            self._spawn(self.ws.run(), "websocket")

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
        if self.ws:
            self.ws.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.repo.add_event("BOT_STOPPED")

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
        if mode == "live" and not self.settings.live_autotrade_allowed:
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
        }

    # -------------------------------------------------------------- research
    def _build_model(self) -> HistoricalModel:
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

        return self._research.get(key, lambda: compute_research(self, key))

    def estimate_for(self, sig: SignalResult, snap: MarketSnapshot) -> HistEstimate | None:
        a = sig.analysis
        side = sig.leaning.side
        price = snap.entry_price(side) if side else None
        if a is None or side is None or price is None:
            return None
        return self.model.estimate(a.grade, a.regime.value, a.time_bucket, side, price)

    def record_observation(self, sig: SignalResult, snap: MarketSnapshot, now: datetime | None = None) -> bool:
        now = now or utcnow()
        obs = from_signal(sig, snap, now)
        if obs is None or (obs.ticker, obs.minute) in self._obs_keys:
            return False
        self._obs_keys.add((obs.ticker, obs.minute))
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
        self._obs_keys = {k for k in self._obs_keys if k[0] in self.discovery.state.by_ticker}
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
        return self.risk.evaluate(sig, snap, self.profile(), ctx, now, estimate=self.estimate_for(sig, snap))

    def make_ticket(self, sig: SignalResult, decision: RiskDecision, snap: MarketSnapshot,
                    origin: str = "manual") -> TradeTicket:
        assert decision.approved and decision.side and decision.limit_price_cents is not None
        return self.tickets.add(TradeTicket(
            ticker=snap.ticker, label=snap.info.label, side=decision.side, direction=sig.leaning.value,
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
            self.repo.save_trade_context(pos.id, pos.ticker, entry=entry)
        self.repo.add_event("TRADE_OPENED", f"{pos.mode} {pos.ticker} {pos.side} x{pos.contracts}@{pos.entry_price}")
        await self.notify("trade_opened", f"open:{pos.id}", messages.trade_opened_alert(pos))
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
        await self.notify("trade_closed", f"close:{pos.id}", messages.trade_closed_alert(pos))
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
            results.append(await self.process_snapshot(snap))
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
            await self.poll_once()
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
        self._last_signal[snap.ticker] = sig
        return sig

    async def process_snapshot(self, snap: MarketSnapshot) -> SignalResult:
        sig = await self.evaluate_snapshot(snap)
        self._persist(snap, sig)
        self.record_observation(sig, snap)

        flip = self.engine.detect_flip(sig)
        if flip:
            await self.notify("flip", f"flip:{snap.ticker}", messages.flip_alert(flip))
        decision = self.check_risk(sig, snap)
        strong = (decision.approved and sig.grade in ("A+", "A")
                  and sig.confidence >= self.settings.strong_signal_alert_min_confidence)
        if strong and self._strong.get(snap.ticker) != sig.direction.value:
            self._strong[snap.ticker] = sig.direction.value
            text, buttons = messages.strong_signal_alert(sig, snap, decision, self.state)
            await self.notify("strong_signal", f"strong:{snap.ticker}:{sig.direction.value}", text, buttons)
        elif not strong and (sig.grade not in ("A+", "A")
                             or sig.confidence < self.settings.strong_signal_alert_min_confidence - 5):
            self._strong.pop(snap.ticker, None)  # hysteresis: re-arm only after it clearly weakens
        if self.state.auto_trade and decision.approved:
            await self._auto_trade(sig, snap, decision)
        return sig

    async def _auto_trade(self, sig: SignalResult, snap: MarketSnapshot, decision: RiskDecision) -> None:
        if self.state.mode == "live" and not self.settings.live_autotrade_allowed:
            return
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

    async def settle_positions(self, now: datetime | None = None) -> list[Position]:
        """Settle positions whose market has closed and has a published result."""
        now = now or utcnow()
        settled = []
        for pos in list(self.portfolio.open_positions()):
            if pos.close_time is None or now < pos.close_time + timedelta(seconds=5):
                continue
            try:
                raw = await self.client.get_market(pos.ticker)
            except KalshiError as exc:
                log_event(log, "SETTLEMENT_CHECK_FAILED", logging.WARNING, ticker=pos.ticker, error=exc)
                continue
            result = str(raw.get("result") or "")
            if result not in ("yes", "no"):
                continue  # not determined yet - never guess an outcome
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
                if self.state.mode == "live" and self.live is not None:
                    bal = await self.client.get_balance()
                    self.portfolio.live_balance = float(bal.get("balance", 0)) / 100
                if time.time() - self._last_purge > 3600:
                    self._last_purge = time.time()
                    purged = self.repo.purge(self.settings.snapshot_retention_days,
                                             self.settings.signal_retention_days,
                                             self.settings.event_retention_days,
                                             self.settings.observation_retention_days)
                    log_event(log, "RETENTION_PURGE", **purged)
            except KalshiError as exc:
                log_event(log, "MAINTENANCE_FAILED", logging.WARNING, error=exc)
            await asyncio.sleep(15)

    async def shutdown(self) -> None:
        if self._tasks:
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        self.store.state.running = False
        await self.underlying.close()
        await self.client.close()


RuntimeFactory = Callable[[], BotRuntime]
