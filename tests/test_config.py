import pytest

from app.errors import ConfigError
from app.risk.profiles import RiskLevel, load_profiles
from tests.conftest import make_settings


def test_paper_is_default_and_live_disabled():
    s = make_settings()
    assert s.paper_trading is True
    assert s.live_trading is False
    assert s.auto_trading is False
    assert s.allow_live_autotrade is False
    assert s.live_allowed is False


def test_live_requires_both_flags():
    assert make_settings(live_trading=True, paper_trading=True).live_allowed is False
    assert make_settings(live_trading=False, paper_trading=False).live_allowed is False
    assert make_settings(live_trading=True, paper_trading=False).live_allowed is True


def test_missing_telegram_config_fails_safely():
    s = make_settings(telegram_bot_token=None, telegram_admin_ids="")
    with pytest.raises(ConfigError):
        s.validate_runtime()


def test_malformed_telegram_token_rejected_without_echo():
    bad = "1234567890:FAKEfakeFAKEfakeFAKEfakeFAKEfake123."
    with pytest.raises(ConfigError) as ei:
        make_settings(telegram_bot_token=bad).validate_runtime()
    assert "malformed" in str(ei.value) and bad not in str(ei.value)
    make_settings(telegram_bot_token="1234567890:FAKEfakeFAKEfakeFAKEfakeFAKEfake123").validate_runtime()


def test_live_without_credentials_fails():
    s = make_settings(data_source="kalshi", live_trading=True, paper_trading=False)
    with pytest.raises(ConfigError, match="KALSHI_API_KEY_ID"):
        s.validate_runtime()


def test_fixture_data_cannot_go_live(tmp_path):
    key = tmp_path / "k.pem"
    key.write_text("x")
    s = make_settings(live_trading=True, paper_trading=False, kalshi_api_key_id="id", kalshi_private_key_path=key)
    with pytest.raises(ConfigError, match="fixture"):
        s.validate_runtime()


def test_admin_ids_parsing():
    assert make_settings(telegram_admin_ids="1, 2,3").admin_ids == frozenset({1, 2, 3})
    with pytest.raises(ConfigError):
        _ = make_settings(telegram_admin_ids="1,abc").admin_ids


def test_risk_profile_env_overrides(monkeypatch):
    monkeypatch.setenv("LOW_MIN_CONFIDENCE", "85")
    monkeypatch.setenv("HIGH_MAX_POSITION_USD", "25.5")
    profiles = load_profiles()
    assert profiles[RiskLevel.LOW].min_confidence == 85
    assert profiles[RiskLevel.HIGH].max_position_usd == 25.5
    assert profiles[RiskLevel.MEDIUM].min_confidence == 70


def test_unsafe_profile_override_rejected(monkeypatch):
    monkeypatch.setenv("HIGH_MAX_OPEN_POSITIONS", "500")
    with pytest.raises(ConfigError):
        load_profiles()


def test_auto_start_is_paper_only_and_respects_estop(runtime):
    assert runtime.should_auto_start()[0] is False  # default off
    runtime.settings = runtime.settings.model_copy(update={"auto_start": True})
    assert runtime.should_auto_start()[0] is True
    runtime.store.state.emergency_stop = True
    assert runtime.should_auto_start()[0] is False
    runtime.store.state.emergency_stop = False
    runtime.store.state.mode = "live"
    ok, why = runtime.should_auto_start()
    assert ok is False and "paper-only" in why
