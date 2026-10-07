"""End-to-end smoke test with fixture data - no Telegram or Kalshi credentials needed.

    python scripts/smoke_test.py

Demonstrates: dashboard rendering, sample markets, UP / DOWN / WAIT signals,
risk approval + rejection, a paper trade, settlement and the P&L update.
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.kalshi.fixtures import FIXTURE_TRENDS, FixtureKalshiClient, make_snapshot  # noqa: E402
from app.kalshi.market_data import MarketInfo  # noqa: E402
from app.risk.profiles import RiskLevel, load_profiles  # noqa: E402
from app.runtime import BotRuntime  # noqa: E402
from app.strategy.signals import Direction  # noqa: E402
from app.telegram import messages as M  # noqa: E402
from app.telegram.callbacks import CallbackRouter  # noqa: E402
from app.utils.logging import setup_logging  # noqa: E402
from app.utils.time import utcnow  # noqa: E402

ADMIN = 111


def plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def show(title: str, screen) -> None:  # type: ignore[no-untyped-def]
    print(f"\n┌── {title} " + "─" * max(0, 50 - len(title)))
    print(plain(screen.text))
    for row in screen.buttons:
        print("  " + "  ".join(f"[ {label} ]" for label, _ in row))
    print("└" + "─" * 56)


def check(cond: bool, label: str) -> None:
    print(f"{'✅' if cond else '❌'} {label}")
    if not cond:
        raise SystemExit(f"SMOKE TEST FAILED: {label}")


async def main() -> None:
    setup_logging("WARNING")
    clock = {"now": utcnow()}
    settings = Settings(_env_file=None, data_source="fixture", database_url="sqlite://",
                        telegram_admin_ids=str(ADMIN), paper_starting_balance=1000)
    client = FixtureKalshiClient(clock=lambda: clock["now"])
    repo = Repository(Database(settings.database_url))
    repo.init()
    rt = BotRuntime(settings, repo, client, profiles=load_profiles(read_env=False))
    router = CallbackRouter(rt, settings.admin_ids)

    # 1) Sample markets: 15-minute windows with 8:42 remaining.
    now = clock["now"]
    infos = {}
    for asset, trend in FIXTURE_TRENDS.items():
        info = MarketInfo(
            ticker=f"KX{asset}15M-SMOKE", event_ticker=f"KX{asset}15M-SMOKE", series_ticker=f"KX{asset}15M",
            asset=asset, title=f"{asset} up in 15 min?", subtitle="", open_time=now - timedelta(seconds=378),
            close_time=now + timedelta(seconds=522), expiration_time=None, status="active",
        )
        client.register(info, trend)
        rt.discovery.state.current[asset] = info
        rt.discovery.state.by_ticker[info.ticker] = info
        infos[asset] = info

    # 2) Feed 3 minutes of price history (5s cadence) through the signal engine.
    for i in range(37):
        ts = now - timedelta(seconds=(36 - i) * 5)
        for asset, trend in FIXTURE_TRENDS.items():
            snap = make_snapshot(asset, trend, ts, info=infos[asset], ts=ts)
            rt.market_data.latest[snap.ticker] = snap
            rt.engine.evaluate(snap, threshold=rt.profile().min_confidence, threshold_label="LOW", now=ts)

    sigs = {a: rt.engine.latest[i.ticker] for a, i in infos.items()}
    show("HOME (bot stopped)", M.home(rt))
    show("MARKETS", M.markets_list(rt))
    check(sigs["BTC"].direction is Direction.UP, f"BTC signal UP ({sigs['BTC'].confidence}%)")
    check(sigs["SOL"].direction is Direction.DOWN, f"SOL signal DOWN ({sigs['SOL'].confidence}%)")
    check(sigs["ETH"].direction is Direction.WAIT, f"ETH signal WAIT ({sigs['ETH'].confidence}%)")

    # 3) Risk: rejection while stopped, rejection for WAIT, approval once running.
    await router.handle(ADMIN, "resume")
    d = rt.view(infos["BTC"].ticker).decision
    check(d is not None and not d.approved and "disabled" in d.reason.lower(), f"Rejected while stopped: {d.reason}")
    rt.store.update(running=True)  # equivalent to Start, without background network loops
    d_eth = rt.view(infos["ETH"].ticker).decision
    check(not d_eth.approved, f"ETH WAIT rejected: {d_eth.reason}")
    d_btc = rt.view(infos["BTC"].ticker).decision
    check(d_btc.approved, f"BTC approved under LOW: {d_btc.position_size} contracts @ {d_btc.price_cents:.0f}¢")
    show("SIGNAL CARD · BTC (UP)", M.signal_card(rt, infos["BTC"].ticker))
    show("SIGNAL CARD · SOL (DOWN)", M.signal_card(rt, infos["SOL"].ticker))
    show("SIGNAL CARD · ETH (WAIT)", M.signal_card(rt, infos["ETH"].ticker))

    # Risk change requires confirmation.
    resp = await router.handle(ADMIN, "rset:high")
    check(rt.state.risk_level is RiskLevel.LOW, "Selecting HIGH only shows confirmation")
    show("RISK CONFIRM", resp.screen)
    await router.handle(ADMIN, "rok:high")
    check(rt.state.risk_level is RiskLevel.HIGH, "Confirm switches risk to HIGH")
    await router.handle(ADMIN, "rok:low")

    # 4) Paper trade through the real Telegram callback flow.
    resp = await router.handle(ADMIN, f"buy:{infos['BTC'].ticker}")
    show("PAPER TICKET", resp.screen)
    ticket_id = resp.screen.buttons[0][0][1].split(":")[1]
    resp = await router.handle(ADMIN, f"tok:{ticket_id}")
    show("FILL", resp.screen)
    dup = await router.handle(ADMIN, f"tok:{ticket_id}")
    check(dup.screen is None and "already used" in (dup.toast or ""), "Duplicate confirm is rejected")
    opens = rt.portfolio.open_positions("paper")
    check(len(opens) == 1, f"1 open paper position (balance {M.money(rt.portfolio.paper_balance(), False)})")
    d2 = rt.view(infos["SOL"].ticker).decision
    check(not d2.approved, f"Second trade blocked under LOW: {d2.reason}")
    show("POSITIONS", M.positions(rt))

    # 5) Market closes -> settlement -> P&L update.
    clock["now"] = infos["BTC"].close_time + timedelta(seconds=10)
    settled = await rt.settle_positions(now=clock["now"])
    check(len(settled) == 1, f"Position settled: {settled[0].close_reason}, P&L {M.money(settled[0].realized_pnl)}")
    stats = rt.portfolio.stats("paper", days=0)
    check(stats.trades == 1 and stats.realized == settled[0].realized_pnl, f"Today P&L {M.money(stats.realized)}")
    show("TRADES", M.trades(rt))
    show("HISTORY · TODAY", M.history(rt, "today"))
    show("STATUS", M.status(rt))

    # 6) Emergency stop blocks orders.
    await router.handle(ADMIN, "estopok")
    resp = await router.handle(ADMIN, f"buy:{infos['SOL'].ticker}")
    check(resp.screen is None and "disabled" in (resp.toast or "").lower(), "Emergency stop blocks new orders")
    show("EMERGENCY", M.emergency(rt))
    show("HOME (final)", M.home(rt))
    unauth = await router.handle(999, "start")
    check("Unauthorized" in (unauth.toast or ""), "Unauthorized user is refused")
    print("\n🎉 SMOKE TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
