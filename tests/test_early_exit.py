"""Early exit: cash out when the signal flips against an open trade, then score it vs holding."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app.strategy.signals import Direction
from app.trading.positions import Position
from app.utils.time import utcnow
from tests.conftest import make_info, quote_snapshot


class Notes:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list | None]] = []

    async def alert(self, kind, key, text, buttons=None):
        self.sent.append((kind, text, buttons))


def _open(runtime, mode: str = "paper", side: str = "yes", info=None) -> Position:
    info = info or make_info()
    pos = Position(mode=mode, ticker=info.ticker, event_ticker=info.event_ticker, asset="BTC", label=info.label,
                   side=side, contracts=10, entry_price=62.0, entry_fee=0.17, risk_level="low",
                   close_time=info.close_time)
    runtime.portfolio.add_open(pos)
    return pos


def _sig(lean: str, conf: int = 70):
    return SimpleNamespace(btc=None, leaning=Direction(lean), confidence=conf)


async def test_paper_position_cashes_out_after_confirmed_flip(runtime):
    runtime.notifier = Notes()
    info = make_info()
    pos = _open(runtime, info=info)
    snap = quote_snapshot(info, yes_bid=40, yes_ask=42)
    await runtime._manage_exits(_sig("DOWN"), snap)
    assert pos.is_open  # one check is not enough (noise guard)
    await runtime._manage_exits(_sig("DOWN"), snap)
    assert not pos.is_open and pos.close_reason.startswith("early exit: signal flipped to DOWN")
    assert pos.exit_price is not None and pos.exit_price < 40  # sold at the bid minus paper slippage
    assert runtime._early_exits[info.ticker] == [pos]


async def test_no_exit_when_signal_agrees_or_is_weak(runtime):
    runtime.notifier = Notes()
    info = make_info()
    pos = _open(runtime, info=info)
    snap = quote_snapshot(info, yes_bid=60, yes_ask=62)
    for sig in (_sig("UP", 90), _sig("DOWN", 55), _sig("WAIT", 90)):
        for _ in range(3):
            await runtime._manage_exits(sig, snap)
    assert pos.is_open


async def test_live_position_only_gets_a_sell_button(runtime):
    runtime.notifier = Notes()
    info = make_info()
    pos = _open(runtime, mode="live", info=info)
    snap = quote_snapshot(info, yes_bid=40, yes_ask=42)
    for _ in range(4):
        await runtime._manage_exits(_sig("DOWN"), snap)
    assert pos.is_open  # real money is never sold without a tap
    kinds = [k for k, _, _ in runtime.notifier.sent]
    assert kinds == ["exit_suggest"] and f"pcl:{pos.id}" in str(runtime.notifier.sent[0][2])


async def test_early_exit_is_scored_against_holding(runtime, tmp_path, monkeypatch):
    runtime.notifier = Notes()
    runtime.settings = runtime.settings.model_copy(update={"kalshi_key_dir": tmp_path / "kalshi"})
    info = make_info()
    pos = _open(runtime, info=info)
    pos.close_time = utcnow() - timedelta(minutes=1)
    runtime.portfolio.close(pos, 38.0, 0.1, "early exit: test")
    runtime._early_exits[info.ticker] = [pos]

    async def get_market(ticker):
        return {"result": "no"}

    monkeypatch.setattr(runtime.client, "get_market", get_market)
    assert await runtime.score_early_exits() == 1
    hold = round(0 - pos.cost - pos.entry_fee, 2)
    st = runtime.early_exit_stats()
    assert st["exits"] == 1 and st["saved"] == round(pos.realized_pnl - hold, 2) > 0 and st["helped"] == 1
    assert any(k == "exit_check" and "saved" in t for k, t, _ in runtime.notifier.sent)
    assert info.ticker not in runtime._early_exits
