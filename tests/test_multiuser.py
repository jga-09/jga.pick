"""Multi-user accounts: every allowed Telegram user has their own bankroll, settings, trades and key."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app.database.db import Database
from app.database.repository import AccountRepo, Repository
from app.kalshi.fixtures import FixtureKalshiClient, FixtureUnderlyingProvider
from app.risk.profiles import RiskLevel, load_profiles
from app.runtime import BotRuntime
from app.trading.paper import PaperExecutor
from app.trading.portfolio import Portfolio
from tests.conftest import feed_history, make_info, make_settings, quote_snapshot
from tests.test_paper import ticket

OWNER, OTHER, STRANGER = 4242, 777, 999


@pytest.fixture
def rt2(repo) -> BotRuntime:
    client = FixtureKalshiClient()
    return BotRuntime(make_settings(telegram_admin_ids=f"{OWNER},{OTHER}"), repo, client,
                      underlying=FixtureUnderlyingProvider(client=client), profiles=load_profiles(read_env=False))


async def test_bankrolls_positions_and_pnl_are_separate(rt2):
    snap = quote_snapshot(make_info(), yes_bid=60, yes_ask=62)
    with rt2.as_user(OTHER):
        pos = await rt2.paper.buy(ticket(), snap)
        rt2.paper.settle(pos, "no")  # OTHER loses
        other_balance = rt2.portfolio.paper_balance()
        assert rt2.portfolio.stats("paper").trades == 1
        assert rt2.portfolio.daily_realized("paper") < 0
    with rt2.as_user(OWNER):
        assert rt2.portfolio.paper_balance() == pytest.approx(1000.0)  # untouched by OTHER's loss
        assert rt2.portfolio.stats("paper").trades == 0 and rt2.repo.closed_positions(mode="paper") == []
        assert rt2.portfolio.daily_realized("paper") == 0
    assert other_balance < 1000.0


def test_settings_are_per_user_but_safety_switches_are_global(rt2):
    with rt2.as_user(OTHER):
        rt2.set_risk_level(RiskLevel.HIGH)
        rt2.store.update(auto_trade=True, kelly_sizing=True)
    with rt2.as_user(OWNER):
        assert rt2.state.risk_level != RiskLevel.HIGH or rt2.settings.default_risk_level == "high"
        assert rt2.state.kelly_sizing is None
        rt2.store.update(paused=True)  # bot-wide
    with rt2.as_user(OTHER):
        assert rt2.state.paused and rt2.state.risk_level == RiskLevel.HIGH and rt2.state.kelly_sizing is True


async def test_emergency_stop_turns_off_everyones_auto_trade(rt2):
    for uid in (OWNER, OTHER):
        with rt2.as_user(uid):
            rt2.store.update(auto_trade=True)
    with rt2.as_user(OTHER):
        await rt2.emergency_stop(by=OTHER)
    for uid in (OWNER, OTHER):
        with rt2.as_user(uid):
            assert rt2.state.emergency_stop and not rt2.state.auto_trade


def test_strangers_never_get_an_account(rt2):
    with pytest.raises(PermissionError):
        rt2.account(STRANGER)
    assert STRANGER not in rt2.accounts


async def test_paper_reset_only_clears_own_account(rt2):
    snap = quote_snapshot(make_info(), yes_bid=60, yes_ask=62)
    for uid in (OWNER, OTHER):
        with rt2.as_user(uid):
            rt2.paper.settle(await rt2.paper.buy(ticket(), snap), "yes")
    with rt2.as_user(OTHER):
        rt2.reset_paper_account(by=OTHER)
        assert rt2.portfolio.paper_balance() == pytest.approx(1000.0)
    with rt2.as_user(OWNER):
        assert rt2.portfolio.stats("paper").trades == 1


async def test_auto_trade_runs_for_each_user_with_their_own_sizing(rt2):
    info = make_info()
    for uid, on in ((OWNER, True), (OTHER, False)):
        with rt2.as_user(uid):
            rt2.store.update(running=True, risk_level=RiskLevel.HIGH, auto_trade=on)
    feed_history(rt2, info, "up")
    await rt2.process_snapshot(rt2.market_data.latest[info.ticker])
    with rt2.as_user(OWNER):
        owner_trades = len(rt2.portfolio.open_positions("paper"))
    with rt2.as_user(OTHER):
        assert rt2.portfolio.open_positions("paper") == []  # OTHER's auto trade is off
    assert owner_trades in (0, 1)  # depends on the fixture signal; never leaks into OTHER


async def test_alerts_go_only_to_the_account_owner(rt2):
    sent = []

    class Notes:
        supports_users = True

        async def alert(self, kind, key, text, buttons=None, users=None):
            sent.append((kind, users))

    rt2.notifier = Notes()
    with rt2.as_user(OTHER):
        await rt2.notify("trade_opened", "k", "t")
    await rt2.notify("emergency", "k", "t", everyone=True)
    assert sent == [("trade_opened", [OTHER]), ("emergency", None)]


def test_live_needs_the_users_own_key(repo, tmp_path):
    s = make_settings(telegram_admin_ids=f"{OWNER},{OTHER}", kalshi_key_dir=tmp_path / "keys")
    rt = BotRuntime(s, repo, FixtureKalshiClient(), profiles=load_profiles(read_env=False))
    rt.settings = s.model_copy(update={"live_trading": True, "paper_trading": False})
    with rt.as_user(OTHER):
        rt.live = SimpleNamespace()  # a live executor exists...
        rt.account().client = SimpleNamespace(signer=None)
        assert not rt.mode_permitted("live")  # ...but OTHER has no key of their own
        rt.account().client = SimpleNamespace(signer=object())
        assert rt.mode_permitted("live")
        assert rt.key_store.dir == tmp_path / "keys" / f"user_{OTHER}"
    with rt.as_user(OWNER):
        assert rt.key_store.dir == tmp_path / "keys"  # the owner keeps the original key location


async def test_set_key_is_stored_for_that_user_only(repo, tmp_path):
    from tests.test_keysetup import KEY_ID, _pem

    s = make_settings(telegram_admin_ids=f"{OWNER},{OTHER}", kalshi_key_dir=tmp_path / "keys")
    rt = BotRuntime(s, repo, FixtureKalshiClient(), profiles=load_profiles(read_env=False))
    rt.fixture_mode = False
    with rt.as_user(OTHER):
        rt.account().client = SimpleNamespace(signer=None, auth_failed=False,
                                              get_balance=AsyncMock(return_value={"balance": 500}))
        assert await rt.set_kalshi_key(KEY_ID, _pem().encode()) == 5.0
        assert rt.trading_client.signer is not None
    assert (tmp_path / "keys" / f"user_{OTHER}" / "key_id").is_file()
    assert not (tmp_path / "keys" / "key_id").exists()  # the owner's key slot is untouched
    assert getattr(rt.client, "signer", None) is None  # the shared client never gets OTHER's key


def test_existing_single_user_data_is_adopted_by_the_owner(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'old.db'}")
    repo = Repository(db)
    repo.init()
    pf = Portfolio(repo, 1000)  # pre-multi-user code path: account 0
    paper = PaperExecutor(pf, 0, 0.0)
    import asyncio

    pos = asyncio.run(paper.buy(ticket(), quote_snapshot(make_info(), yes_bid=60, yes_ask=62)))
    paper.settle(pos, "yes")
    rt = BotRuntime(make_settings(telegram_admin_ids=f"{OWNER},{OTHER}", database_url=f"sqlite:///{tmp_path}/old.db"),
                    repo, FixtureKalshiClient(), profiles=load_profiles(read_env=False))
    with rt.as_user(OWNER):
        assert rt.portfolio.stats("paper").trades == 1 and rt.portfolio.daily_realized("paper") > 0
    with rt.as_user(OTHER):
        assert rt.portfolio.stats("paper").trades == 0


def test_migration_adds_account_columns_to_an_old_database(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'legacy.db'}")
    with db.engine.begin() as c:  # the old schema: no account_id anywhere
        c.execute(text("CREATE TABLE positions (id INTEGER PRIMARY KEY, mode VARCHAR(8), ticker VARCHAR(80), "
                       "event_ticker VARCHAR(80), asset VARCHAR(10), label VARCHAR(20), side VARCHAR(4), "
                       "contracts INTEGER, entry_price FLOAT, entry_fee FLOAT, risk_level VARCHAR(10), "
                       "confidence INTEGER, close_time DATETIME, opened_at DATETIME, status VARCHAR(10), "
                       "exit_price FLOAT, exit_fee FLOAT, closed_at DATETIME, realized_pnl FLOAT, "
                       "close_reason VARCHAR(40), client_order_id VARCHAR(64))"))
        c.execute(text("INSERT INTO positions (mode, ticker, event_ticker, asset, label, side, contracts, "
                       "entry_price, entry_fee, risk_level, confidence, opened_at, status, exit_fee, "
                       "client_order_id) VALUES ('paper','KXBTC15M-X','KXBTC15M-X','BTC','BTC 15M','yes',1,50,0,"
                       "'low',0,'2026-11-01 00:00:00','open',0,'a')"))
        c.execute(text("CREATE TABLE daily_pnl (day VARCHAR(10), mode VARCHAR(8), realized_pnl FLOAT, fees FLOAT, "
                       "trades INTEGER, wins INTEGER, losses INTEGER, PRIMARY KEY (day, mode))"))
        c.execute(text("INSERT INTO daily_pnl VALUES ('2026-11-01','paper',1.5,0.1,1,1,0)"))
    repo = Repository(db)
    repo.init()
    assert len(repo.open_positions(account=0)) == 1
    assert repo.daily("paper", "2026-11-01", account=0).realized_pnl == 1.5
    repo.adopt_legacy(OWNER)
    assert AccountRepo(repo, OWNER).open_positions()[0].account_id == OWNER
    assert repo.daily("paper", "2026-11-01", account=OWNER).realized_pnl == 1.5
