"""Phase 6: live order lifecycle - fills confirmed from Kalshi, partial fills, retried exits, recovery."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors import KalshiTransientError, MalformedResponseError, OrderRejectedError
from app.trading.live import (
    LiveExecutor,
    exit_order_id,
    fill_from_fills,
    fill_from_order,
)
from app.trading.portfolio import Portfolio
from tests.conftest import make_info, make_settings, quote_snapshot
from tests.test_paper import ticket


def _client(create):
    c = MagicMock()
    c.signer = object()
    c.create_order_v2 = create
    c.get_fills = AsyncMock(return_value=[])
    c.get_order = AsyncMock(return_value={"order_id": "o1", "fill_count_fp": "0.00", "remaining_count_fp": "0.00"})
    c.get_orders_all = AsyncMock(return_value=[])
    return c


def _live(repo, client):
    s = make_settings(data_source="kalshi", live_trading=True, paper_trading=False)
    live = LiveExecutor(client, s, Portfolio(repo, 1000))
    live.retry_delay = 0
    return live


def _fill(n, no_price, fee, oid="o1"):
    return {"order_id": oid, "count_fp": f"{n:.2f}", "yes_price_dollars": f"{(100 - no_price) / 100:.4f}",
            "no_price_dollars": f"{no_price / 100:.4f}", "fee_cost": f"{fee:.4f}", "outcome_side": "no"}


async def test_fill_quantity_price_and_fee_come_from_kalshi_fills_not_the_post_reply(repo):
    # The POST reply claims 5 @ 62c with no fee; Kalshi's fills say 3 @ 63c and 0.06 fees.
    c = _client(AsyncMock(return_value={"order_id": "o1", "fill_count": "5.00", "average_fill_price": "0.3800"}))
    c.get_fills = AsyncMock(return_value=[_fill(2, 63, 0.04), _fill(1, 63, 0.02)])
    pos = await _live(repo, c).buy(ticket(mode="live", side="no", direction="DOWN", limit_price_cents=64),
                                   quote_snapshot(make_info()), confirmed=True)
    assert pos.contracts == 3 and pos.entry_price == pytest.approx(63) and pos.entry_fee == pytest.approx(0.06)
    row = repo.live_orders()[0]
    assert row["status"] == "filled" and row["position_id"] == pos.id and row["fee"] == pytest.approx(0.06)


async def test_accepted_but_unfilled_ioc_is_recorded_as_no_fill(repo):
    c = _client(AsyncMock(return_value={"order_id": "o1", "fill_count": "0.00"}))
    with pytest.raises(OrderRejectedError, match="not filled"):
        await _live(repo, c).buy(ticket(mode="live"), quote_snapshot(make_info()), confirmed=True)
    assert repo.live_orders()[0]["status"] == "no_fill"


async def test_unreadable_2xx_is_treated_as_unknown_and_reconciled(repo):
    t = ticket(mode="live")
    c = _client(AsyncMock(side_effect=MalformedResponseError("non-JSON response")))
    c.get_orders_all = AsyncMock(return_value=[{"client_order_id": t.client_order_id, "order_id": "o7",
                                                "fill_count_fp": "2.00", "remaining_count_fp": "3.00",
                                                "yes_price_dollars": "0.6400"}])
    c.get_order = AsyncMock(side_effect=KalshiTransientError("down"))  # use the order object found above
    pos = await _live(repo, c).buy(t, quote_snapshot(make_info()), confirmed=True)
    assert c.create_order_v2.await_count == 1 and pos.contracts == 2  # partial entry tracked, not resubmitted
    assert repo.live_orders()[0]["status"] == "partial"


async def test_not_placed_needs_two_clean_lookups(repo):
    c = _client(AsyncMock(side_effect=KalshiTransientError("timeout")))
    c.get_orders_all = AsyncMock(side_effect=[KalshiTransientError("x"), KalshiTransientError("x"),
                                              KalshiTransientError("x"), []])
    from app.errors import TradingError

    with pytest.raises(TradingError, match="UNKNOWN"):  # only one clean miss: cannot conclude
        await _live(repo, c).buy(ticket(mode="live"), quote_snapshot(make_info()), confirmed=True)


async def test_partial_exit_books_pnl_and_fees_and_retry_uses_a_new_order_id(repo):
    entry = _client(AsyncMock(return_value={"order_id": "o1"}))
    entry.get_fills = AsyncMock(return_value=[_fill(4, 60, 0.08)])
    live = _live(repo, entry)
    pos = await live.buy(ticket(mode="live", side="no", direction="DOWN", limit_price_cents=64),
                         quote_snapshot(make_info()), confirmed=True)
    assert pos.contracts == 4
    # first exit: only 1 of 4 sells at 70c
    entry.create_order_v2 = AsyncMock(return_value={"order_id": "x1"})
    entry.get_fills = AsyncMock(return_value=[_fill(1, 70, 0.02, "x1")])
    with pytest.raises(OrderRejectedError, match="partial exit: 1/4"):
        await live.sell(pos, quote_snapshot(make_info()), confirmed=True)
    assert pos.contracts == 3 and pos.is_open
    closed = repo.closed_positions(mode="live")
    assert len(closed) == 1 and closed[0].contracts == 1
    assert closed[0].realized_pnl == pytest.approx(0.10 - 0.02 - 0.02)  # +10c, minus 1/4 entry fee, exit fee
    assert repo.daily("live").realized_pnl == pytest.approx(closed[0].realized_pnl)
    # second exit attempt must not collide with the first (client_order_id is unique)
    entry.create_order_v2 = AsyncMock(return_value={"order_id": "x2"})
    entry.get_fills = AsyncMock(return_value=[_fill(3, 72, 0.05, "x2")])
    await live.sell(pos, quote_snapshot(make_info()), confirmed=True)
    assert not pos.is_open
    ids = [r["client_order_id"] for r in repo.live_orders() if r["action"] == "sell"]
    assert len(ids) == len(set(ids)) == 2


def test_closing_twice_is_refused(repo):
    from app.trading.positions import Position

    pf = Portfolio(repo, 1000)
    p = pf.add_open(Position(mode="paper", ticker="T", event_ticker="E", asset="BTC", label="x", side="yes",
                             contracts=1, entry_price=50, entry_fee=0.02, risk_level="low"))
    pf.close(p, 100, 0, "settled")
    with pytest.raises(ValueError, match="already closed"):
        pf.close(p, 100, 0, "settled again")


def test_fill_helpers_and_exit_ids():
    assert fill_from_fills([], "yes") is None
    f = fill_from_order({"fill_count_fp": "2.00", "taker_fill_cost_dollars": "1.2000",
                         "taker_fees_dollars": "0.0400", "remaining_count_fp": "0.00"}, "yes")
    assert f.contracts == 2 and f.price == pytest.approx(60) and f.fee == pytest.approx(0.04) and f.complete
    a, b = exit_order_id("x" * 70), exit_order_id("x" * 70)
    assert a != b and len(a) <= 64


# --------------------------------------------------------------- reconciliation
def test_compare_positions_flags_mismatches_and_external_positions():
    from app.trading.positions import Position
    from app.trading.reconcile import compare_positions

    mine = [Position(mode="live", ticker="A", event_ticker="E", asset="BTC", label="x", side="yes", contracts=3,
                     entry_price=50, entry_fee=0, risk_level="low"),
            Position(mode="live", ticker="B", event_ticker="E", asset="BTC", label="x", side="no", contracts=2,
                     entry_price=50, entry_fee=0, risk_level="low")]
    exch = [{"ticker": "A", "position_fp": "3.00"}, {"ticker": "B", "position_fp": "-1.00"},
            {"ticker": "C", "position_fp": "5.00"}]
    out = {m.ticker: m for m in compare_positions(mine, exch)}
    assert set(out) == {"B", "C"}  # A matches
    assert out["B"].tracked and out["B"].local == -2 and out["B"].exchange == -1
    assert not out["C"].tracked  # someone else's position: reported, not blocked


async def test_crash_recovery_blocks_market_with_untracked_fill(repo):
    from app.trading.reconcile import recover_orders

    repo.add_live_order(client_order_id="c1", ticker="T1", side="yes", action="buy", contracts=2, price=60,
                        status="submitting", request={})
    repo.add_live_order(client_order_id="c2", ticker="T2", side="yes", action="buy", contracts=2, price=60,
                        status="unknown", request={})
    c = MagicMock()
    c.get_orders_all = AsyncMock(side_effect=lambda t, min_ts=None: (
        [{"client_order_id": "c1", "order_id": "o1", "fill_count_fp": "2.00", "remaining_count_fp": "0.00",
          "yes_price_dollars": "0.6000"}] if t == "T1" else []))
    c.get_fills = AsyncMock(return_value=[])
    notes, block = await recover_orders(c, repo)
    assert block == {"T1"} and any("FILLED 2" in n for n in notes)
    st = {r["client_order_id"]: r["status"] for r in repo.live_orders()}
    assert st == {"c1": "filled", "c2": "not_placed"}


async def test_runtime_reconcile_blocks_mismatched_market(runtime):
    from app.trading.positions import Position

    runtime.live = MagicMock()
    runtime.client.signer = object()
    runtime.client.get_orders_all = AsyncMock(return_value=[])
    runtime.client.get_market_positions = AsyncMock(return_value=[])  # Kalshi: nothing held
    runtime.portfolio.add_open(Position(mode="live", ticker="T9", event_ticker="E", asset="BTC", label="x",
                                        side="yes", contracts=1, entry_price=50, entry_fee=0, risk_level="low"))
    rep = await runtime.reconcile_live()
    assert not rep.ok and "T9" in rep.blocked and "T9" in runtime.portfolio.unresolved


def test_bot_always_boots_in_paper(repo):
    from app.runtime import BotRuntime
    from app.kalshi.fixtures import FixtureKalshiClient

    s = make_settings(data_source="kalshi", live_trading=True, paper_trading=False)
    rt = BotRuntime(s, repo, FixtureKalshiClient())
    assert rt.state.mode == "paper"
