"""Why isn't the bot trading?  python scripts/diagnose.py

Reads the bot's own database (no network, changes nothing) and explains,
in plain language, what is stopping trades right now.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.research.calibration import HistoricalModel  # noqa: E402
from app.risk.profiles import RiskLevel, load_profiles  # noqa: E402
from app.state import STATE_KEY  # noqa: E402
from app.strategy.quality import FILTER_TEXT  # noqa: E402
from app.utils.time import utcnow  # noqa: E402


def main() -> None:
    s = Settings()
    repo = Repository(Database(s.database_url))
    repo.init()
    st = repo.get_setting(STATE_KEY) or {}
    level = RiskLevel(st.get("risk_level", s.default_risk_level))
    prof = load_profiles()[level].with_overrides((st.get("risk_overrides") or {}).get(level.value, {}))
    findings: list[str] = []

    print("=== BOT STATE (last saved) ===")
    print(f"scanner running: {st.get('running')} | paused: {st.get('paused')} | emergency stop: "
          f"{st.get('emergency_stop')} | auto trade: {st.get('auto_trade')} | mode: {st.get('mode')} | "
          f"risk: {level.value.upper()}")
    if not st.get("running"):
        findings.append("Scanner is STOPPED -> in Telegram send /menu and tap 🟢 Start (needed after every restart).")
    if st.get("paused"):
        findings.append("Trading is PAUSED -> tap ▶️ Resume.")
    if st.get("emergency_stop"):
        findings.append("EMERGENCY STOP is active -> 🚨 E-Stop -> 🔓 Reset Stop.")
    if not st.get("auto_trade"):
        findings.append("Auto Trade is OFF -> the bot only trades when you tap 💰 BUY on a signal card. "
                        "Turn it on: ⚙️ Config -> 🤖 Auto Trade -> 🟢 Turn ON.")

    print(f"\n=== {level.value.upper()} PROFILE GATES ===")
    print(f"confidence ≥ {prof.min_confidence} | quality ≥ {prof.min_signal_quality} | grades {prof.allowed_grades} | "
          f"stability ≥ {prof.min_stability} | max warnings {prof.max_soft_flags} | "
          f"EV ≥ {prof.min_ev_cents}¢ | EV required: {prof.require_known_ev} | entry ≤ {prof.max_entry_price_cents:g}¢")

    now = utcnow()
    all_obs = repo.observations(source="live", settled_only=False)
    recent = [o for o in all_obs if o.ts >= now - timedelta(hours=24)]
    settled = [o for o in all_obs if o.outcome]
    print("\n=== DATA ===")
    print(f"observations: {len(all_obs):,} total | {len(recent):,} in last 24h | settled: {len(settled):,} "
          f"across {len({o.ticker for o in settled}):,} markets")
    if not recent:
        findings.append("No setups recorded in the last 24h -> the bot is not seeing markets. Check 📊 Status "
                        "(Kalshi CONNECTED? Market Data?) and that the Chromebook stayed awake.")

    if recent:
        grades = Counter(o.grade for o in recent)
        print("grades (24h):", ", ".join(f"{g} {n}" for g, n in grades.most_common()))
        hard = Counter(f for o in recent for f in o.hard_flags)
        soft = Counter(f for o in recent for f in o.soft_flags)
        print("top hard blockers (24h):", "; ".join(f"{FILTER_TEXT.get(k, k)} {v}" for k, v in hard.most_common(6)))
        print("top warnings (24h):", "; ".join(f"{FILTER_TEXT.get(k, k)} {v}" for k, v in soft.most_common(4)))
        grades_ok = prof.grades
        passing = [o for o in recent if o.grade in grades_ok and o.quality >= prof.min_signal_quality
                   and o.confidence >= prof.min_confidence and len(o.soft_flags) <= prof.max_soft_flags
                   and (o.entry_price or 100) <= prof.max_entry_price_cents]
        pct = len(passing) / len(recent) * 100
        print(f"setups passing the {level.value.upper()} signal gates (before EV): {len(passing)} of {len(recent)} "
              f"({pct:.1f}%) in {len({o.ticker for o in passing})} markets")
        if not passing:
            q = sorted(o.quality for o in recent)
            findings.append(f"NO setup passed the {level.value.upper()} quality/grade gates in 24h (best quality seen: "
                            f"{q[-1]}, median {q[len(q) // 2]}). The filters may be too strict for real markets - "
                            f"paste this output to Claude before changing anything.")

    model = HistoricalModel(settled, s.hist_min_samples, s.calibration_prior_strength, s.fee_rate,
                            s.paper_slippage_cents)
    print("\n=== EVIDENCE (settled markets) ===")
    for g in ("A+", "A", "B", "C"):
        stt = model.stat("grade", g)
        if stt is None:
            print(f"grade {g}: no data")
            continue
        ok = stt.n_markets >= s.hist_min_samples
        print(f"grade {g}: {stt.n_markets} markets (need {s.hist_min_samples}) | WR {stt.win_rate:.0%} vs avg price "
              f"{stt.avg_price:.0f}¢ | raw EV {stt.ev_cents:+.1f}¢" + ("" if ok else "  ⚠️ insufficient"))
    allowed_ready = [g for g in prof.grades if model.sufficient(model.stat("grade", g))]
    if prof.require_known_ev and not allowed_ready:
        need = min((s.hist_min_samples - (model.stat("grade", g).n_markets if model.stat("grade", g) else 0))
                   for g in prof.grades)
        findings.append(f"{level.value.upper()} requires a supported EV estimate and no allowed grade has "
                        f"{s.hist_min_samples} settled markets yet (~{max(need, 0)} more needed). This is the "
                        f"data-collection phase; keep the bot running, or use HIGH / "
                        f"{level.value.upper()}_REQUIRE_KNOWN_EV=false to paper-trade meanwhile.")
    for g in allowed_ready:
        stt = model.stat("grade", g)
        if stt and stt.ev_cents < prof.min_ev_cents:
            findings.append(f"Evidence so far says grade {g} setups LOSE money after costs (raw EV "
                            f"{stt.ev_cents:+.1f}¢ over {stt.n_markets} markets). Not trading them is correct.")

    trades = repo.closed_positions(mode=st.get("mode", "paper"), since=now - timedelta(hours=24), limit=1000)
    print(f"\n=== TRADES === closed in last 24h: {len(trades)} | open now: {len(repo.open_positions())}")

    print("\n=== WHY NO TRADES ===")
    for i, f in enumerate(findings or ["No blocking problem found - the bot simply hasn't seen a qualifying setup."], 1):
        print(f"{i}. {f}")


if __name__ == "__main__":
    main()
