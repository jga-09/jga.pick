"""P&L card image (PNG) sent to Telegram after each finished trade and when the bot stops.

Only real recorded results are drawn: today's closed positions for the current mode
(paper and live are never mixed). Nothing is projected or estimated.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from app.trading.positions import Position

W, H = 1200, 675
ET = ZoneInfo("America/New_York")

# Dark surface tokens (matches Telegram dark). Text never uses the status colours.
BG = "#17212B"
PANEL = "#1E2C3A"
GRID = "#2C3D4E"
INK = "#E9EEF2"
INK_2 = "#A3B1BE"
INK_3 = "#6F8091"
LINE = "#4EA1F3"
GOOD = "#3FB950"
BAD = "#F85149"

_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in _FONT_DIRS:
        p = Path(d) / name
        if p.is_file():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size=size)


def _usd(v: float, signed: bool = True) -> str:
    sign = ("+" if v >= 0 else "−") if signed else ("−" if v < 0 else "")
    return f"{sign}${abs(v):,.2f}"


@dataclass(frozen=True)
class PnlCard:
    title: str  # e.g. "TODAY'S P&L" or "SESSION SUMMARY"
    mode: str  # paper | live
    trades: list[Position]  # today's closed trades, oldest first
    balance: float
    all_time: float
    last: Position | None = None
    last_direction: str = ""  # UP / DOWN meaning of the last trade
    now: datetime | None = None


def render_pnl_card(card: PnlCard, app_name: str = "JGA Picks") -> bytes:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    pnls = [p.realized_pnl or 0.0 for p in card.trades]
    total = sum(pnls)
    wins = sum(1 for v in pnls if v > 0)
    n = len(pnls)

    # header
    d.text((48, 40), f"{app_name.upper()}  ·  BTC 15M", font=_font(26, True), fill=INK_2)
    pill = "PAPER" if card.mode == "paper" else "LIVE · REAL MONEY"
    pf = _font(22, True)
    pw = d.textlength(pill, font=pf) + 32
    d.rounded_rectangle((W - 48 - pw, 34, W - 48, 74), radius=20, outline=INK_3, width=2)
    d.text((W - 48 - pw + 16, 42), pill, font=pf, fill=INK_2)

    # hero number
    d.text((48, 110), card.title, font=_font(24, True), fill=INK_3)
    d.text((44, 142), _usd(total), font=_font(96, True), fill=GOOD if total >= 0 else BAD)
    rate = f"{wins / n * 100:.0f}% win rate" if n else "no trades yet"
    d.text((48, 262), f"{n} trade{'' if n == 1 else 's'}  ·  {wins} won  ·  {n - wins} lost  ·  {rate}",
           font=_font(26), fill=INK_2)
    if card.last is not None and card.last.realized_pnl is not None:
        lp = card.last.realized_pnl
        word = "WON" if lp > 0 else "LOST"
        what = (f"BTC {'▲' if card.last_direction == 'UP' else '▼'} {card.last_direction}" if card.last_direction
                else f"BUY {card.last.side.upper()}")
        d.text((48, 306), f"Last trade: {word}  ·  {what} @ {card.last.entry_price:.0f}¢  ·  {_usd(lp)}",
               font=_font(26), fill=INK)

    # cumulative P&L chart (single series: no legend; markers show each trade's result)
    x0, y0, x1, y1 = 48, 370, W - 48, H - 96
    d.rounded_rectangle((x0, y0, x1, y1), radius=16, fill=PANEL)
    d.text((x0 + 24, y0 + 16), "Running P&L today", font=_font(22, True), fill=INK_3)
    cx0, cy0, cx1, cy1 = x0 + 32, y0 + 56, x1 - 150, y1 - 28
    if n == 0:
        d.text((cx0, (cy0 + cy1) // 2 - 14), "No finished trades yet today", font=_font(26), fill=INK_3)
    else:
        cum = [0.0]
        for v in pnls:
            cum.append(cum[-1] + v)
        lo, hi = min(cum), max(cum)
        if hi - lo < 0.01:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.12
        lo, hi = lo - pad, hi + pad

        def xy(i: int, v: float) -> tuple[float, float]:
            x = cx0 + (cx1 - cx0) * (i / max(1, len(cum) - 1))
            y = cy1 - (cy1 - cy0) * ((v - lo) / (hi - lo))
            return x, y

        zy = xy(0, 0.0)[1]
        for xs in range(int(cx0), int(cx1), 18):  # dashed zero baseline
            d.line((xs, zy, min(xs + 9, cx1), zy), fill=GRID, width=2)
        d.text((cx1 + 16, zy - 13), "$0", font=_font(20), fill=INK_3)
        pts = [xy(i, v) for i, v in enumerate(cum)]
        d.line(pts, fill=LINE, width=4, joint="curve")
        for (x, y), v in zip(pts[1:], pnls):
            r = 8
            d.ellipse((x - r - 3, y - r - 3, x + r + 3, y + r + 3), fill=PANEL)  # surface ring
            d.ellipse((x - r, y - r, x + r, y + r), fill=GOOD if v > 0 else BAD)
        ex, ey = pts[-1]
        d.text((ex + 18, ey - 15), _usd(cum[-1]), font=_font(24, True), fill=INK)

    # footer
    now = (card.now or datetime.now(ET)).astimezone(ET)
    d.text((48, H - 64), f"Balance {_usd(card.balance, signed=False)}  ·  All-time {_usd(card.all_time)}",
           font=_font(24), fill=INK_2)
    stamp = now.strftime("%b %d · %I:%M %p ET").replace(" 0", " ")
    sf = _font(22)
    d.text((W - 48 - d.textlength(stamp, font=sf), H - 62), stamp, font=sf, fill=INK_3)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
