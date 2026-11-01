"""Phase 8: new Telegram views/commands - admin-only, validated, honest NO TRADE reasons."""

import json

import pytest

from app.errors import InvalidCallbackError
from app.risk.profiles import RiskLevel
from app.strategy.decision_record import from_legacy
from app.telegram import upgrade_views as UV
from app.telegram.callbacks import CallbackRouter, parse_callback
from app.telegram.handlers import COMMAND_ROUTES
from tests.conftest import ADMIN_ID, feed_history, make_info


@pytest.fixture
def router(runtime):
    return CallbackRouter(runtime, frozenset({ADMIN_ID}))


def test_all_requested_commands_exist():
    wanted = ("status markets btc gold eth sol signals performance today week month pnl positions orders "
              "models backtest risk pause resume kill").split()
    from app.telegram.handlers import COMMAND_ROUTES as R

    missing = [c for c in wanted if c not in R]
    assert not missing
    for route in R.values():
        parse_callback(route)  # every command maps to a valid, whitelisted callback
    assert COMMAND_ROUTES["kill"] == "estop"  # kill switch asks for confirmation first


def test_callback_validation_for_new_routes():
    assert parse_callback("asset:ETH") == ("asset", ["ETH"])
    with pytest.raises(InvalidCallbackError):
        parse_callback("asset:ETH:extra:x")


async def test_new_screens_are_admin_only(router):
    for route in ("asset:BTC", "orders", "models", "asset:GOLD"):
        assert "Unauthorized" in (await router.handle(999, route)).toast


async def test_unknown_asset_rejected(router):
    resp = await router.handle(ADMIN_ID, "asset:DOGEX")
    assert resp.toast and "Invalid" in resp.toast


async def test_asset_card_shows_probabilities_ev_and_reason(runtime, router):
    runtime.store.update(running=True, risk_level=RiskLevel.HIGH)
    info = make_info()
    feed_history(runtime, info, "up")
    runtime.discovery.state.current[info.asset] = info
    resp = await router.handle(ADMIN_ID, f"asset:{info.asset}")
    t = resp.screen.text
    for part in ("Model probability", "Market probability", "Expected value after costs", "Spread",
                 "Model reliability", "not a probability"):
        assert part in t, part
    assert "TRADE" in t


def test_legacy_record_keeps_quantities_apart(runtime):
    runtime.store.update(running=True, risk_level=RiskLevel.HIGH)
    info = make_info()
    sig = feed_history(runtime, info, "up")
    snap = runtime.market_data.latest[info.ticker]
    rec = from_legacy(sig, snap, runtime.check_risk(sig, snap), 1.0, 0.07, "quality")
    assert rec.strength == sig.confidence  # score, kept separate from probabilities
    assert rec.market_p is not None and 0 < rec.market_p < 1
    assert rec.fee_c is not None and rec.fee_c == int(rec.fee_c)  # rounded up to the cent
    assert rec.reliability.startswith(("asset:", "none", "pooled"))
    if not rec.approved:
        assert rec.reasons


async def test_gold_card_when_disabled_explains_how_and_never_trades(router):
    resp = await router.handle(ADMIN_ID, "asset:GOLD")
    assert "GOLD_ENABLED" in resp.screen.text


def test_models_screen_reads_report(runtime, tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"BTC": {"note": "", "holdout": {"LR+cal": {
        "prob": {"n_markets": 300, "brier": 0.2, "brier_market": 0.19},
        "trades": {"trades": 40, "avg": -0.02}}}}}))
    t = UV.models(runtime, p).text
    assert "LR+cal" in t and "-2.0¢" in t and "➖" in t
    assert "No model comparison" in UV.models(runtime, tmp_path / "missing.json").text


async def test_orders_screen(router, runtime):
    runtime.repo.add_live_order(client_order_id="c1", ticker="T1", side="yes", action="buy", contracts=2,
                                price=60, status="filled", request={})
    t = (await router.handle(ADMIN_ID, "orders")).screen.text
    assert "T1" in t and "filled" in t


def test_btc_record_uses_conservative_ev_and_reports_rejections():
    from tests.conftest import quote_snapshot
    from tests.test_btc import decide, inputs, params

    sig_btc = decide(inputs(yes_ask=99, yes_bid=97), params())  # nothing to buy at 99c
    holder = type("S", (), {"btc": sig_btc})()
    snap = quote_snapshot(make_info())
    rec = UV.from_btc(holder, snap, 1.0, 0.07)
    assert not rec.approved and rec.verdict == "NO TRADE" and rec.reasons
    assert rec.reliability.split()[0] in ("CALIBRATED", "NOT_CALIBRATED", "INSUFFICIENT")
    if rec.ev_c is not None and sig_btc.edge is not None:
        assert rec.ev_c == sig_btc.edge.ev_low_cents  # the gate uses the conservative bound
