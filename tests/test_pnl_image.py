"""P&L image: real closed trades only, sent after each finished trade and on stop."""

from __future__ import annotations

from app.telegram.pnl_image import PnlCard, render_pnl_card
from app.trading.positions import Position


def _pos(pnl: float, side: str = "yes") -> Position:
    p = Position(mode="paper", ticker="T", event_ticker="E", asset="BTC", label="BTC 15M", side=side,
                 contracts=2, entry_price=60, entry_fee=0.04, risk_level="low")
    p.status, p.realized_pnl = "closed", pnl
    return p


def test_render_png_with_and_without_trades():
    trades = [_pos(0.8), _pos(-1.2, "no"), _pos(0.5)]
    png = render_pnl_card(PnlCard("TODAY'S P&L", "paper", trades, 1000.1, 0.1, trades[-1], "UP"))
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000
    empty = render_pnl_card(PnlCard("TODAY'S P&L", "live", [], 250.0, 0.0))
    assert empty[:4] == b"\x89PNG"


class FakeNotifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.photos: list[tuple[str, str]] = []
        self.texts: list[str] = []

    async def photo(self, kind, key, png, caption, buttons=None):
        self.photos.append((kind, caption))
        return self.ok

    async def alert(self, kind, key, text, buttons=None):
        self.texts.append(kind)


async def test_trade_close_sends_image_with_card(runtime):
    runtime.notifier = FakeNotifier()
    pos = _pos(0.8)
    pos.id, pos.close_reason, pos.exit_price = 7, "settled YES", 100.0
    await runtime._after_close(pos)
    kinds = [k for k, _ in runtime.notifier.photos]
    assert kinds == ["trade_closed"] and "WON" in runtime.notifier.photos[0][1]
    assert "trade_closed" not in runtime.notifier.texts


async def test_image_failure_falls_back_to_text(runtime):
    runtime.notifier = FakeNotifier(ok=False)
    pos = _pos(-0.5)
    pos.id, pos.close_reason, pos.exit_price = 8, "settled NO", 0.0
    await runtime._after_close(pos)
    assert "trade_closed" in runtime.notifier.texts


async def test_stop_sends_session_summary(runtime):
    runtime.notifier = FakeNotifier()
    await runtime.stop()
    assert [k for k, _ in runtime.notifier.photos] == ["session_summary"]
