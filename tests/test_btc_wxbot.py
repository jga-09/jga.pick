from datetime import UTC, datetime, timedelta

from app.data.btc_feed import MinuteBar
from app.research.btc_wxbot import _rsi, indicators, p_up


def _bars(closes):
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    return [MinuteBar(t0 + timedelta(minutes=i), c, c + 1, c - 1, c, 1.0) for i, c in enumerate(closes)]


def test_rsi_extremes():
    assert _rsi(list(range(100, 120))) == 100.0
    assert _rsi(list(range(120, 100, -1))) == 0.0
    assert _rsi([100.0] * 20) == 50.0


def test_rising_prices_give_up_with_convergence():
    ind = indicators(_bars([60000 + 10 * i for i in range(60)]))
    p, conv = p_up(ind, 0.50)
    assert conv and 0.5 < p <= 0.65


def test_falling_prices_give_down_and_clip():
    ind = indicators(_bars([60000 - 50 * i for i in range(60)]))
    p, conv = p_up(ind, 0.50)
    assert conv and 0.35 <= p < 0.5


def test_flat_has_no_convergence_and_skew_fades_market():
    ind = indicators(_bars([60000.0] * 60))
    p, conv = p_up(ind, 0.80)
    assert not conv and p < 0.5  # skew term fades an expensive UP price


def test_needs_enough_bars():
    assert indicators(_bars([1.0] * 10)) is None
