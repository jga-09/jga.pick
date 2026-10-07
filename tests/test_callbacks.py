import pytest

from app.errors import InvalidCallbackError
from app.risk.profiles import RiskLevel
from app.telegram.callbacks import CallbackGuard, CallbackRouter, parse_callback
from tests.conftest import ADMIN_ID, feed_history, make_info


@pytest.fixture
def router(runtime):
    return CallbackRouter(runtime, frozenset({ADMIN_ID}))


async def test_unauthorized_user_cannot_trade(runtime, router):
    runtime.store.update(running=True)
    info = make_info()
    feed_history(runtime, info, "up")
    resp = await router.handle(999, f"buy:{info.ticker}")
    assert "Unauthorized" in resp.toast
    t, _ = runtime.propose(info.ticker)
    resp = await router.handle(999, f"tok:{t.id}")
    assert "Unauthorized" in resp.toast
    assert runtime.tickets.get(t.id) is not None  # not consumed by the intruder
    assert not runtime.portfolio.open_positions()
    for action in ("start", "estopok", "rok:high", "mode:live", "auto:on"):
        assert "Unauthorized" in (await router.handle(None, action)).toast
    assert runtime.state.risk_level is RiskLevel.LOW and not runtime.state.emergency_stop


async def test_risk_change_requires_confirmation(runtime, router):
    resp = await router.handle(ADMIN_ID, "rset:high")
    assert runtime.state.risk_level is RiskLevel.LOW
    assert "CHANGE RISK LEVEL" in resp.screen.text
    assert any(data == "rok:high" for row in resp.screen.buttons for _, data in row)
    await router.handle(ADMIN_ID, "risk")  # cancel
    assert runtime.state.risk_level is RiskLevel.LOW
    await router.handle(ADMIN_ID, "rok:high")
    assert runtime.state.risk_level is RiskLevel.HIGH


async def test_admin_paper_trade_flow(runtime, router):
    await router.handle(ADMIN_ID, "resume")
    runtime.store.update(running=True)
    info = make_info()
    feed_history(runtime, info, "up")
    resp = await router.handle(ADMIN_ID, f"buy:{info.ticker}")
    assert "PAPER TRADE" in resp.screen.text
    tok = resp.screen.buttons[0][0][1]
    assert tok.startswith("tok:")
    resp = await router.handle(ADMIN_ID, tok)
    assert "FILLED" in resp.screen.text
    again = await router.handle(ADMIN_ID, tok)
    assert again.screen is None and "already used" in again.toast
    assert len(runtime.portfolio.open_positions()) == 1


@pytest.mark.parametrize("bad", [None, "", "x" * 80, "buy", "buy:a:b:c", "rm -rf", "evil:1", "mkt:<script>",
                                 "trades:1:2", "auto:maybe"])
async def test_invalid_callback_data_rejected(router, bad):
    resp = await router.handle(ADMIN_ID, bad)
    assert resp.screen is None and "Invalid" in resp.toast


def test_parse_callback():
    assert parse_callback("mkt:KXBTC15M-26OCT07-T1") == ("mkt", ["KXBTC15M-26OCT07-T1"])
    with pytest.raises(InvalidCallbackError):
        parse_callback("radj:conf")


def test_callback_guard_drops_duplicates():
    g = CallbackGuard(debounce_sec=1.5)
    assert g.accept("q1", 1, "tok:abc", now=100.0)
    assert not g.accept("q1", 1, "tok:abc", now=100.1)  # redelivered id
    assert not g.accept("q2", 1, "tok:abc", now=100.5)  # double tap
    assert g.accept("q3", 1, "tok:abc", now=103.0)
    assert g.accept("q4", 2, "tok:abc", now=103.1)


async def test_live_mode_blocked_when_not_permitted(runtime, router):
    resp = await router.handle(ADMIN_ID, "mode:live")
    assert "disabled by configuration" in resp.screen.text
    await router.handle(ADMIN_ID, "modeok:live")
    assert runtime.state.mode == "paper"


async def test_emergency_reset_requires_confirmation(runtime, router):
    await router.handle(ADMIN_ID, "estopok")
    assert runtime.state.emergency_stop
    resp = await router.handle(ADMIN_ID, "ereset")
    assert runtime.state.emergency_stop and "RESET" in resp.screen.text
    await router.handle(ADMIN_ID, "eresetok")
    assert not runtime.state.emergency_stop


async def test_risk_customize_respects_hard_limits(runtime, router):
    for _ in range(10):
        await router.handle(ADMIN_ID, "radj:conf:+")
    assert runtime.profile().min_confidence == 99 or runtime.profile().min_confidence <= 99
    resp = await router.handle(ADMIN_ID, "radj:conf:+")
    assert runtime.profile().min_confidence <= 99
    assert "CUSTOMIZE" in resp.screen.text


async def test_all_screens_render(runtime, router):
    runtime.store.update(running=True)
    info = make_info()
    feed_history(runtime, info, "up")
    for route in ("home", "status", "signals", f"mkt:{info.ticker}", f"det:{info.ticker}", "trades:0", "pos",
                  "risk", "rcust", "hist:7d", "strat", "ind", "cfg", "assets", "possz", "auto", "mode",
                  "estop", "pnl"):
        resp = await router.handle(ADMIN_ID, route)
        assert resp.screen is not None and resp.screen.text, route
        for row in resp.screen.buttons:
            assert 1 <= len(row) <= 2
            for _, data in row:
                parse_callback(data)  # every button we render must pass our own validation
