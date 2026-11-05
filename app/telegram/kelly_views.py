"""Telegram: the user's fractional Kelly sizing card (/kelly)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.risk.kelly import KellyMode
from app.telegram.keyboards import Screen

if TYPE_CHECKING:
    from app.runtime import BotRuntime

SEP = "━━━━━━━━━━━━━━━━━━"


def _pct(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%"


def kelly_screen(rt: BotRuntime, limit: int = 3) -> Screen:
    st = rt.state
    prof = rt.profile()
    mode = KellyMode(st.risk_level.value)
    bankroll = rt.portfolio.balance(st.mode)
    daily_left = max(0.0, prof.max_daily_loss_usd + min(0.0, rt.portfolio.daily_realized(st.mode)))
    on = rt.kelly_on()
    lines = [
        "📐 <b>KELLY SIZING</b>" + ("" if st.mode == "paper" else "  ·  💵 <b>REAL MONEY</b>"),
        "",
        f"Bankroll ({st.mode}): <b>${bankroll:,.2f}</b>",
        f"Mode: <b>{mode.label}</b>",
        f"Kelly sizing: <b>{'ON' if on else 'OFF'}</b>" + ("" if on else " (fixed sizes are used; Kelly shown for reference)"),
        f"Remaining daily risk: <b>${daily_left:,.2f}</b> of ${prof.max_daily_loss_usd:,.2f}",
        "",
        SEP,
    ]
    shown = 0
    for v in rt.markets():
        ks = getattr(v.decision, "kelly", None) if v.decision else None
        if ks is None:
            continue
        shown += 1
        side = (v.decision.side or "").upper() if v.decision else ""
        lines += ["", f"<b>{v.info.label}</b> · {side or '—'}"]
        if ks.p_used is None:
            lines.append(f"⚪ {ks.reason.replace('Kelly: ', '')}")
            continue
        lines += [
            f"Win chance (cautious): <b>{_pct(ks.p_used)}</b>"
            + ("" if ks.calibrated else " · ⚠️ not yet calibrated on this coin"),
            f"Price: {ks.price_cents:.0f}¢ · break-even (fee incl.): {_pct(ks.breakeven)}",
            f"Full Kelly: {_pct(ks.full_kelly, True)} · fee Kelly: {_pct(ks.fee_kelly, True)}",
            f"Fractional Kelly ({mode.multiplier:g}x): <b>{_pct(ks.fractional_kelly)}</b> of bankroll",
        ]
        if ks.ok:
            lines.append(f"✅ Recommended: <b>{ks.contracts} contracts</b> · max loss <b>${ks.max_loss_usd:.2f}</b> "
                         f"(fees incl.) · limited by {ks.limiting_factor}")
        else:
            lines.append(f"⚪ No trade: {ks.reason.replace('Kelly: ', '')}")
        if shown >= limit:
            break
    if not shown:
        lines += ["", "No market data yet - start the bot and check again."]
    lines += ["", "<i>Kelly sizes by the estimated edge. With no proven edge it says 0% - that is the point. "
              "Never full Kelly; the mode cap is a hard limit.</i>"]
    buttons = [
        [("🔴 Kelly OFF" if on else "🟢 Kelly ON", f"kon:{0 if on else 1}")],
        [(f"{'• ' if m == mode else ''}{m.value.title()} {m.multiplier:g}x", f"rset:{m.value}") for m in KellyMode],
        [("🔄 Refresh", "kelly"), ("🏠 Home", "home")],
    ]
    return Screen("\n".join(lines), buttons, route="kelly", refreshable=True)
