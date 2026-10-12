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
from app.kalshi.fixtures import (  # noqa: E402
    FIXTURE_STRIKES,
    FIXTURE_TRENDS,
    FixtureKalshiClient,
    FixtureUnderlyingProvider,
    fixture_spot_history,
    make_snapshot,
)
from app.kalshi.market_data import MarketInfo  # noqa: E402
from app.risk.profiles import RiskLevel, load_profiles  # noqa: E402
from app.runtime import BotRuntime  # noqa: E402
from app.strategy.signals import Direction  # noqa: E402
from app.telegram import analytics_views as AV  # noqa: E402
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
    settings = Settings(_env_file=None, data_source="fixture", database_url="sqlite://", strategy_mode="quality",
                        assets="BTC,ETH,SOL", telegram_admin_ids=str(ADMIN), paper_starting_balance=1000)
    client = FixtureKalshiClient(clock=lambda: clock["now"])
    repo = Repository(Database(settings.database_url))
    repo.init()
    rt = BotRuntime(settings, repo, client, underlying=FixtureUnderlyingProvider(lambda: clock["now"], client),
                    profiles=load_profiles(read_env=False))
    router = CallbackRouter(rt, settings.admin_ids)

    # 1) Sample markets: 15-minute windows with 8:42 remaining.
    now = clock["now"]
    infos = {}
    for asset, trend in FIXTURE_TRENDS.items():
        info = MarketInfo(
            ticker=f"KX{asset}15M-SMOKE", event_ticker=f"KX{asset}15M-SMOKE", series_ticker=f"KX{asset}15M",
            asset=asset, title=f"{asset} up in 15 min?", subtitle="", open_time=now - timedelta(seconds=378),
            close_time=now + timedelta(seconds=522), expiration_time=None, status="active",
            floor_strike=FIXTURE_STRIKES[asset],
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
            spot = fixture_spot_history(asset, trend, infos[asset], ts)  # synthetic spot feed
            rt._last_signal[snap.ticker] = rt.engine.evaluate(
                snap, threshold=rt.profile().min_confidence, min_quality=rt.profile().min_signal_quality,
                threshold_label="LOW", now=ts, spot=spot[-1], spot_history=spot)

    sigs = {a: rt.engine.latest[i.ticker] for a, i in infos.items()}
    show("HOME (bot stopped)", M.home(rt))
    show("MARKETS", M.markets_list(rt))
    def desc(s):  # type: ignore[no-untyped-def]
        return f"conf {s.confidence}%, quality {s.analysis.quality}, {s.analysis.grade}, {s.analysis.regime.value}"

    check(sigs["BTC"].leaning is Direction.UP, f"BTC leans UP ({desc(sigs['BTC'])})")
    check(sigs["SOL"].leaning is Direction.DOWN, f"SOL leans DOWN ({desc(sigs['SOL'])})")
    check(sigs["ETH"].direction is Direction.WAIT and not sigs["ETH"].analysis.tradeable,
          f"ETH is NO TRADE: {', '.join(sigs['ETH'].analysis.reasons[:3])}")

    # 3) Risk: rejection while stopped, rejection for WAIT, approval once running.
    await router.handle(ADMIN, "resume")
    d = rt.view(infos["BTC"].ticker).decision
    check(d is not None and not d.approved and "disabled" in d.reason.lower(), f"Rejected while stopped: {d.reason}")
    rt.store.update(running=True)  # equivalent to Start, without background network loops
    d_eth = rt.view(infos["ETH"].ticker).decision
    check(not d_eth.approved, f"ETH WAIT rejected: {d_eth.reason}")
    d_low = rt.view(infos["BTC"].ticker).decision
    check(not d_low.approved, f"LOW rejects BTC: {d_low.reason}")
    show("SIGNAL CARD · BTC under LOW (NO TRADE)", M.signal_card(rt, infos["BTC"].ticker))
    show("CHECKLIST · BTC under LOW", AV.checklist(rt, infos["BTC"].ticker))
    rt.store.update(risk_level=RiskLevel.HIGH)
    d_btc = rt.view(infos["BTC"].ticker).decision
    check(d_btc.approved, f"HIGH approves BTC (paper): {d_btc.position_size} contracts @ {d_btc.price_cents:.0f}¢ "
                          f"| warnings: {'; '.join(d_btc.warnings)}")
    show("SIGNAL CARD · BTC under HIGH", M.signal_card(rt, infos["BTC"].ticker))
    show("WHY? · BTC", AV.why(rt, infos["BTC"].ticker))
    show("SIGNAL CARD · ETH (NO TRADE)", M.signal_card(rt, infos["ETH"].ticker))
    rt.store.update(risk_level=RiskLevel.LOW)

    # Risk change requires confirmation.
    resp = await router.handle(ADMIN, "rset:high")
    check(rt.state.risk_level is RiskLevel.LOW, "Selecting HIGH only shows confirmation")
    show("RISK CONFIRM", resp.screen)
    await router.handle(ADMIN, "rok:high")
    check(rt.state.risk_level is RiskLevel.HIGH, "Confirm switches risk to HIGH")
    # 4) Paper trade through the real Telegram callback flow (HIGH risk).
    resp = await router.handle(ADMIN, f"buy:{infos['BTC'].ticker}")
    show("PAPER TICKET", resp.screen)
    ticket_id = resp.screen.buttons[0][0][1].split(":")[1]
    resp = await router.handle(ADMIN, f"tok:{ticket_id}")
    show("FILL", resp.screen)
    dup = await router.handle(ADMIN, f"tok:{ticket_id}")
    check(dup.screen is None and "already used" in (dup.toast or ""), "Duplicate confirm is rejected")
    opens = rt.portfolio.open_positions("paper")
    check(len(opens) == 1, f"1 open paper position (balance {M.money(rt.portfolio.paper_balance(), False)})")
    rt.store.update(risk_level=RiskLevel.LOW)
    d2 = rt.view(infos["SOL"].ticker).decision
    check(not d2.approved, f"SOL blocked under LOW: {d2.reason}")
    rt.store.update(risk_level=RiskLevel.HIGH)
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
    show("ANALYTICS", AV.analytics(rt))
    show("LOSS/WIN ANALYSIS", AV.win_analysis(rt))

    # 6) Emergency stop blocks orders.
    await router.handle(ADMIN, "estopok")
    resp = await router.handle(ADMIN, f"buy:{infos['SOL'].ticker}")
    check(resp.screen is None and "disabled" in (resp.toast or "").lower(), "Emergency stop blocks new orders")
    show("EMERGENCY", M.emergency(rt))
    show("HOME (final)", M.home(rt))
    unauth = await router.handle(999, "start")
    check("Unauthorized" in (unauth.toast or ""), "Unauthorized user is refused")
    await btc_section()
    print("\n🎉 SMOKE TEST PASSED")


async def btc_section() -> None:
    """STRATEGY_MODE=btc: dashboard, signal card, NO TRADE reasons, approved paper trade, settlement."""
    import math
    from dataclasses import replace as dc_replace

    from app.data.btc_feed import MinuteBar
    from app.data.underlying import SpotPrice
    from app.kalshi.market_data import MarketSnapshot, OrderBook
    from app.research.btc_calibration import CalibrationReport
    from app.strategy.btc_decision import CalibrationState

    print("\n" + "=" * 20 + " BTC ENGINE (STRATEGY_MODE=btc, synthetic data) " + "=" * 20)
    clock = {"now": utcnow()}
    end = clock["now"].replace(second=0, microsecond=0)

    class Feed:  # synthetic BTC: ~60,300 with 5 bps/min noise, i.e. 50 bps above a 60,000 threshold
        name = "smoke"
        _bars = [MinuteBar(end - timedelta(minutes=119 - i), *(4 * [60300 * math.exp((5 if i % 2 else -5) / 1e4)]), 10.0)
                 for i in range(120)]

        async def get_price(self, asset):  # type: ignore[no-untyped-def]
            return SpotPrice("BTC", 60300.0, clock["now"], "smoke")

        def history(self, asset):  # type: ignore[no-untyped-def]
            return []

        def bars(self):  # type: ignore[no-untyped-def]
            return self._bars

        def status(self):  # type: ignore[no-untyped-def]
            from app.data.btc_feed import FeedStatus
            return FeedStatus("FIXTURE", "synthetic", clock["now"], 0.0)

        async def close(self) -> None:
            return None

    settings = Settings(_env_file=None, data_source="fixture", database_url="sqlite://", telegram_admin_ids=str(ADMIN))
    repo = Repository(Database("sqlite://"))
    repo.init()
    rt = BotRuntime(settings, repo, FixtureKalshiClient(clock=lambda: clock["now"]), underlying=Feed(),
                    profiles=load_profiles(read_env=False))
    router = CallbackRouter(rt, settings.admin_ids)
    await router.handle(ADMIN, "start")
    for t in rt._tasks:
        t.cancel()
    rt._tasks.clear()
    now = clock["now"]
    info = MarketInfo(ticker="KXBTC15M-SMOKE", event_ticker="KXBTC15M-SMOKE", series_ticker="KXBTC15M", asset="BTC",
                      title="Bitcoin price above 60,000?", subtitle="", open_time=now - timedelta(seconds=360),
                      close_time=now + timedelta(seconds=540), expiration_time=None, status="active",
                      floor_strike=60000.0, strike_type="greater_or_equal",
                      rules_primary="[SMOKE] If the simple average of the sixty seconds of CF Benchmarks' Bitcoin "
                                    "Real-Time Index (BRTI) before close is above 60000.00, the market resolves to Yes.")
    rt.discovery.state.current["BTC"] = info
    rt.discovery.state.by_ticker[info.ticker] = info

    def snap(ask: float, bid: float) -> MarketSnapshot:
        book = OrderBook(yes_bids=((bid, 200.0),), no_bids=((100 - ask, 200.0),))
        return MarketSnapshot(info=info, ts=clock["now"], yes_bid=bid, yes_ask=ask, no_bid=100 - ask, no_ask=100 - bid,
                              orderbook=book)

    # a) Uncalibrated model under LOW: NO TRADE with the exact reason.
    s1 = snap(70, 68)
    rt.market_data.latest[info.ticker] = s1
    await rt.process_snapshot(s1)
    d = rt.view(info.ticker).decision
    check(not d.approved, f"LOW + uncalibrated model -> NO TRADE: {d.reason}")
    show("BTC DASHBOARD", M.home(rt))

    # b) Price already near certainty: insufficient remaining value.
    rt.store.update(risk_level=RiskLevel.HIGH)
    s2 = snap(97, 95)
    rt.market_data.latest[info.ticker] = s2
    await rt.process_snapshot(s2)
    d = rt.view(info.ticker).decision
    check(not d.approved and any(k in d.reason.lower() for k in ("expected value", "reflects the move")),
          f"97c entry rejected: {d.reason}")

    # c) Calibrated model, cheap YES: approved -> paper trade.
    rt.calibration = CalibrationReport(CalibrationState("CALIBRATED", 120, 0.02, 0.20, 0.21), (), 120, 900)
    s3 = snap(70, 68)
    rt.market_data.latest[info.ticker] = s3

    async def fresh(_info):  # type: ignore[no-untyped-def]
        rt.market_data.latest[info.ticker] = dc_replace(s3, ts=clock["now"])
        return rt.market_data.latest[info.ticker]

    rt.market_data.poll = fresh  # type: ignore[method-assign]
    await rt.process_snapshot(s3)
    d = rt.view(info.ticker).decision
    check(d.approved, f"Calibrated model + 70c YES approved: {d.position_size} contracts")
    show("BTC SIGNAL", (await router.handle(ADMIN, "btc")).screen)
    resp = await router.handle(ADMIN, f"buy:{info.ticker}")
    tid = next(data.split(":")[1] for row in resp.screen.buttons for _, data in row if data.startswith("tok:"))
    resp = await router.handle(ADMIN, f"tok:{tid}")
    pos = rt.portfolio.open_positions("paper")
    check(len(pos) == 1, f"Paper trade filled: {pos[0].side.upper()} x{pos[0].contracts} @ {pos[0].entry_price:.0f}c")
    pnl = rt.paper.settle(pos[0], "yes")
    check(pnl > 0, f"Settled YES -> paper P&L {M.money(pnl)} (synthetic)")
    show("BTC ANALYTICS", (await router.handle(ADMIN, "banl")).screen)


if __name__ == "__main__":
    asyncio.run(main())
