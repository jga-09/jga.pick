from unittest.mock import AsyncMock, MagicMock

import pytest

from app.risk.profiles import RiskLevel

from app.errors import DuplicateOrderError, LiveTradingDisabledError, OrderRejectedError, TradingDisabledError
from app.kalshi.execution import build_entry_order
from app.trading.live import LiveExecutor
from app.trading.paper import PaperExecutor
from app.trading.portfolio import Portfolio
from app.trading.tickets import TicketStore, TradeTicket
from tests.conftest import feed_history, make_info, make_settings, quote_snapshot


def ticket(**kw) -> TradeTicket:
    base = dict(ticker="KXBTC15M-TEST", label="BTC 15M", side="yes", direction="UP", contracts=5,
                limit_price_cents=64, quoted_price_cents=62, cost_usd=3.1, fee_usd=0.08, risk_level="low",
                confidence=85, mode="paper")
    base.update(kw)
    return TradeTicket(**base)


async def test_paper_buy_and_settle_pnl(repo):
    pf = Portfolio(repo, 1000)
    paper = PaperExecutor(pf, slippage_cents=1, fee_rate=0.07)
    snap = quote_snapshot(make_info(), yes_bid=60, yes_ask=62)
    pos = await paper.buy(ticket(), snap)
    assert pos.entry_price == 63 and pos.contracts == 5
    assert pos.entry_fee == pytest.approx(0.09)  # ceil(0.07*5*0.63*0.37*100)/100
    assert pf.paper_balance() == pytest.approx(1000 - 3.15 - 0.09)
    pnl = paper.settle(pos, "yes")
    assert pnl == pytest.approx(5 * (100 - 63) / 100 - 0.09)
    assert pf.paper_balance() == pytest.approx(1000 + pnl)
    assert pf.stats("paper").wins == 1


async def test_paper_loss_and_exit(repo):
    pf = Portfolio(repo, 1000)
    paper = PaperExecutor(pf, slippage_cents=0, fee_rate=0.0)
    snap = quote_snapshot(make_info(), yes_bid=60, yes_ask=62)
    pos = await paper.buy(ticket(), snap)
    assert paper.settle(pos, "no") == pytest.approx(-3.10)
    pos2 = await paper.buy(ticket(), snap)
    exit_snap = quote_snapshot(make_info(), yes_bid=70, yes_ask=72)
    assert await paper.sell(pos2, exit_snap) == pytest.approx(5 * (70 - 62) / 100)
    assert pf.stats("paper").trades == 2


async def test_paper_rejects_price_moved(repo):
    paper = PaperExecutor(Portfolio(repo, 1000), slippage_cents=1)
    with pytest.raises(OrderRejectedError, match="price moved"):
        await paper.buy(ticket(limit_price_cents=62), quote_snapshot(make_info(), yes_bid=63, yes_ask=65))


async def test_paper_mode_never_invokes_live_execution(runtime):
    runtime.live = MagicMock(spec=LiveExecutor)
    runtime.live.buy = AsyncMock()
    runtime.executor.live = runtime.live
    runtime.client.create_order_v2 = AsyncMock()
    runtime.store.update(running=True, risk_level=RiskLevel.HIGH)
    info = make_info()
    feed_history(runtime, info, "up")
    t, view = runtime.propose(info.ticker)
    assert t is not None, view.decision.reason
    pos = await runtime.execute_ticket(runtime.tickets.consume(t.id), confirmed=True)
    assert pos.mode == "paper"
    runtime.live.buy.assert_not_called()
    runtime.client.create_order_v2.assert_not_called()
    assert not hasattr(runtime.paper, "client")


async def test_live_executor_disabled_when_live_trading_false(repo):
    client = MagicMock()
    client.signer = object()
    client.create_order_v2 = AsyncMock()
    for kw in ({"live_trading": False, "paper_trading": False}, {"live_trading": True, "paper_trading": True}):
        live = LiveExecutor(client, make_settings(**kw), Portfolio(repo, 1000))
        with pytest.raises(LiveTradingDisabledError):
            await live.buy(ticket(mode="live"), quote_snapshot(make_info()), confirmed=True)
    client.create_order_v2.assert_not_called()


async def test_live_executor_requires_confirmation_and_builds_v2_order(repo):
    client = MagicMock()
    client.signer = object()
    client.create_order_v2 = AsyncMock(return_value={
        "order_id": "o1", "fill_count": "5.00", "remaining_count": "0.00", "average_fill_price": "0.3800",
        "average_fee_paid": "0.0200", "ts_ms": 1})
    settings = make_settings(data_source="kalshi", live_trading=True, paper_trading=False)
    live = LiveExecutor(client, settings, Portfolio(repo, 1000))
    t = ticket(mode="live", side="no", direction="DOWN", limit_price_cents=64)
    with pytest.raises(LiveTradingDisabledError, match="confirmation"):
        await live.buy(t, quote_snapshot(make_info()), confirmed=False)
    client.create_order_v2.assert_not_called()
    pos = await live.buy(t, quote_snapshot(make_info()), confirmed=True)
    body = client.create_order_v2.call_args.args[0]
    assert body["side"] == "ask" and body["price"] == "0.3600"  # buy NO @64c == sell YES @36c
    assert body["time_in_force"] == "immediate_or_cancel" and body["count"] == "5.00"
    assert pos.entry_price == pytest.approx(62) and pos.contracts == 5 and pos.entry_fee == pytest.approx(0.10)


def test_build_entry_order_yes_side():
    b = build_entry_order(ticker="T", outcome_side="yes", contracts=3, limit_price_cents=64, client_order_id="c")
    assert b["side"] == "bid" and b["price"] == "0.6400"
    with pytest.raises(ValueError):
        build_entry_order(ticker="T", outcome_side="yes", contracts=0, limit_price_cents=64, client_order_id="c")


async def test_duplicate_trade_prevention(runtime):
    runtime.store.update(running=True, risk_level=RiskLevel.HIGH)
    info = make_info()
    feed_history(runtime, info, "up")
    t, _ = runtime.propose(info.ticker)
    assert t is not None
    consumed = runtime.tickets.consume(t.id)
    assert runtime.tickets.consume(t.id) is None  # second tap gets nothing
    await runtime.execute_ticket(consumed, confirmed=True)
    with pytest.raises(DuplicateOrderError):
        await runtime.execute_ticket(consumed, confirmed=True)  # replay of same client_order_id
    t2, view = runtime.propose(info.ticker)
    assert t2 is None and "Already holding" in " ".join(view.decision.failures)
    assert len(runtime.portfolio.open_positions()) == 1


def test_ticket_store_expiry():
    store = TicketStore()
    t = store.add(ticket())
    object.__setattr__(t, "created_at", t.created_at.replace(year=2000))
    assert store.consume(t.id) is None


async def test_emergency_stop_blocks_orders(runtime):
    runtime.store.update(running=True, risk_level=RiskLevel.HIGH)
    info = make_info()
    feed_history(runtime, info, "up")
    t, _ = runtime.propose(info.ticker)
    assert t is not None
    await runtime.emergency_stop(by=1)
    assert runtime.state.auto_trade is False
    with pytest.raises(TradingDisabledError):
        await runtime.execute_ticket(runtime.tickets.consume(t.id), confirmed=True)
    assert not runtime.portfolio.open_positions()
    # Persisted: a fresh state load keeps the stop.
    assert runtime.repo.get_setting("bot_state")["emergency_stop"] is True


async def test_settlement_waits_for_real_result(runtime, monkeypatch):
    runtime.store.update(running=True, risk_level=RiskLevel.HIGH)
    info = make_info()
    feed_history(runtime, info, "up")
    t, _ = runtime.propose(info.ticker)
    pos = await runtime.execute_ticket(runtime.tickets.consume(t.id), confirmed=True)
    runtime.client.get_market = AsyncMock(return_value={"status": "closed", "result": ""})
    later = info.close_time.replace(microsecond=0) + (info.close_time - info.close_time).__class__(seconds=30)
    assert await runtime.settle_positions(now=later) == []
    assert pos.is_open
    runtime.client.get_market = AsyncMock(return_value={"status": "finalized", "result": "yes"})
    assert len(await runtime.settle_positions(now=later)) == 1
    assert not pos.is_open and pos.realized_pnl > 0
