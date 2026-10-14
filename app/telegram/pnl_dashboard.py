"""Dashboard-style P&L image (PNG): JGA Picks branding, stat tiles, daily profit bars,
trade history and streak.

Only real, finished trades for ONE mode (paper or live) are drawn. Nothing is projected.
No third-party logos are used; Kalshi is named in text only.
"""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from PIL import Image, ImageDraw, ImageFilter

from app.telegram.pnl_image import ET, LOGO, _font
from app.trading.positions import Position

W, H = 1200, 1320
M = 28  # outer margin

BG = (10, 11, 13)
PANEL = (17, 19, 23)
BORDER = (40, 44, 51)
INK = (240, 242, 245)
INK_2 = (170, 176, 186)
INK_3 = (110, 117, 128)
GOOD = (46, 224, 110)
BAD = (255, 77, 77)
GOOD_BG = (14, 52, 30)
BAD_BG = (60, 18, 20)


@dataclass(frozen=True)
class TradeRow:
    pos: Position
    direction: str = ""  # UP / DOWN


@dataclass
class DashboardData:
    mode: str  # paper | live
    trades: list[TradeRow]  # finished trades in the period, oldest first
    prev_period_pnl: float | None  # same-length period before, for the % change
    balance: float
    days: int = 14
    title: str = "P&L OVERVIEW"
    today: date = field(default_factory=lambda: datetime.now(ET).date())


def _usd(v: float, signed: bool = True, cents: bool = True) -> str:
    fmt = f"{abs(v):,.2f}" if cents else f"{abs(v):,.0f}"
    sign = ("+" if v >= 0 else "−") if signed else ("−" if v < 0 else "")
    return f"{sign}${fmt}"


def _nice_step(span: float, target: int = 4) -> float:
    raw = max(span, 0.04) / target
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def _et_day(p: Position) -> date:
    return (p.closed_at or p.opened_at).astimezone(ET).date()


def _panel(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str | None = None) -> None:
    d.rounded_rectangle(box, radius=12, fill=PANEL, outline=BORDER, width=1)
    if title:
        d.text((box[0] + 22, box[1] + 18), title, font=_font(22, True), fill=INK)


def _status(p: Position) -> tuple[str, tuple, tuple]:
    pnl = p.realized_pnl or 0.0
    if not (p.close_reason or "").startswith("settled"):
        return "CASHED", INK_2, (34, 38, 45)
    return ("WIN", GOOD, GOOD_BG) if pnl > 0 else ("LOSS", BAD, BAD_BG)


def _streak(rows: list[TradeRow]) -> tuple[int, bool]:
    n, win = 0, True
    for i, r in enumerate(reversed(rows)):
        w = (r.pos.realized_pnl or 0) > 0
        if i == 0:
            win = w
        if w != win:
            break
        n += 1
    return n, win


def _header(img: Image.Image, d: ImageDraw.ImageDraw, data: DashboardData) -> None:
    # textured dark band (generated noise + soft shapes; no photos or third-party marks)
    band = Image.new("RGB", (W, 200), (16, 17, 20))
    rnd = random.Random(7)
    bd = ImageDraw.Draw(band)
    for _ in range(9):
        x = rnd.randint(-100, W)
        bd.polygon([(x, 200), (x + rnd.randint(120, 320), rnd.randint(30, 120)), (x + rnd.randint(300, 600), 200)],
                   fill=(rnd.randint(24, 34),) * 3)
    band = band.filter(ImageFilter.GaussianBlur(18))
    noise = Image.effect_noise((W, 200), 22).convert("RGB")
    band = Image.blend(band, noise, 0.06)
    img.paste(band, (0, 0))
    d.line((0, 200, W, 200), fill=BORDER, width=1)
    if LOGO.is_file():
        logo = Image.open(LOGO).convert("RGBA")
        lw = 230
        logo = logo.resize((lw, int(logo.height * lw / logo.width)), Image.LANCZOS)
        img.paste(logo, (M + 10, 100 - logo.height // 2), logo)
    else:
        d.text((M + 10, 70), "JGA PICKS", font=_font(48, True), fill=INK)
    d.line((300, 40, 300, 160), fill=BORDER, width=2)
    d.text((332, 70), "R E S E A R C H    /    A N A L Y Z E    /    P R O F I T", font=_font(18, True), fill=INK)
    d.text((332, 104), "Kalshi BTC 15M  ·  Real Data  ·  Smarter Trades", font=_font(18), fill=INK_2)
    pill = "PAPER TRADING" if data.mode == "paper" else "REAL MONEY"
    pf = _font(18, True)
    pw = d.textlength(pill, font=pf) + 36
    col = INK_2 if data.mode == "paper" else BAD
    d.rounded_rectangle((W - M - pw, 64, W - M, 100), radius=18, outline=col, width=2)
    d.text((W - M - pw + 18, 72), pill, font=pf, fill=col)
    start = data.today - timedelta(days=data.days - 1)
    rng = f"{start:%b %d} – {data.today:%b %d, %Y}".replace(" 0", " ")
    rf = _font(16)
    d.text((W - M - d.textlength(rng, font=rf), 114), rng, font=rf, fill=INK_3)


def render_dashboard(data: DashboardData, app_name: str = "JGA Picks") -> bytes:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _header(img, d, data)
    rows = data.trades
    pnls = [r.pos.realized_pnl or 0.0 for r in rows]
    total = sum(pnls)
    wins = sum(1 for v in pnls if v > 0)
    n = len(rows)
    volume = sum(r.pos.cost + r.pos.entry_fee for r in rows)

    # ---- title + stat tiles -------------------------------------------------
    y = 222
    d.text((M, y), data.title, font=_font(30, True), fill=INK)
    d.text((M + d.textlength(data.title, font=_font(30, True)) + 16, y + 10), f"LAST {data.days} DAYS",
           font=_font(16, True), fill=INK_3)
    y += 56
    tw = (W - 2 * M - 3 * 16) // 4
    th = 132
    change = ""
    if data.prev_period_pnl is not None and (data.prev_period_pnl or total):
        diff = total - data.prev_period_pnl
        change = f"{'↑' if diff >= 0 else '↓'} {_usd(diff)} vs previous {data.days} days"
    tiles = [
        ("TOTAL P&L", _usd(total), GOOD if total >= 0 else BAD, change or f"{n} finished trades"),
        ("WIN RATE", f"{wins / n * 100:.1f}%" if n else "—", INK, f"{wins}W / {n - wins}L"),
        ("ROI", f"{'+' if total >= 0 else '−'}{abs(total) / volume * 100:.1f}%" if volume else "—",
         (GOOD if total >= 0 else BAD) if volume else INK, "return on money spent"),
        ("TOTAL TRADES", str(n), INK, f"{_usd(volume, signed=False)} volume"),
    ]
    for i, (label, value, color, sub) in enumerate(tiles):
        x = M + i * (tw + 16)
        _panel(d, (x, y, x + tw, y + th))
        d.text((x + 22, y + 18), label, font=_font(18, True), fill=INK)
        d.text((x + 20, y + 48), value, font=_font(40, True), fill=color)
        d.text((x + 22, y + 100), sub, font=_font(15), fill=INK_2)

    # ---- daily profit bars + right column ------------------------------------
    y += th + 18
    lw = 790
    ch = 330
    _panel(d, (M, y, M + lw, y + ch), "DAILY PROFIT")
    days = [data.today - timedelta(days=k) for k in range(data.days - 1, -1, -1)]
    per_day = {day: 0.0 for day in days}
    for r in rows:
        k = _et_day(r.pos)
        if k in per_day:
            per_day[k] += r.pos.realized_pnl or 0.0
    traded = {_et_day(r.pos) for r in rows}
    gx0, gy0, gx1, gy1 = M + 84, y + 64, M + lw - 22, y + ch - 46
    vals = list(per_day.values())
    step = _nice_step(max(max(vals, default=0), 0) - min(min(vals, default=0), 0))
    top = step * math.ceil(max([0.0, *vals]) / step + 0.15)
    bot = step * math.floor(min([0.0, *vals]) / step - 0.15) if min(vals, default=0) < 0 else 0.0
    if top <= 0:
        top = step

    def vy(v: float) -> float:
        return gy1 - (gy1 - gy0) * (v - bot) / (top - bot)

    ticks = [bot + step * k for k in range(int(round((top - bot) / step)) + 1)]
    for v in ticks:  # recessive grid + labels
        yy = vy(v)
        d.line((gx0, yy, gx1, yy), fill=(30, 33, 39), width=1)
        lab = ("−" if v < 0 else "") + _usd(abs(v), signed=False, cents=step < 1)
        d.text((gx0 - 12 - d.textlength(lab, font=_font(14)), yy - 8), lab, font=_font(14), fill=INK_3)
    zy = vy(0.0)
    d.line((gx0, zy, gx1, zy), fill=INK_3, width=1)
    if not traded:
        msg = "No finished trades yet"
        d.text(((gx0 + gx1 - d.textlength(msg, font=_font(18))) / 2, (gy0 + gy1) / 2 - 10), msg, font=_font(18),
               fill=INK_3)
    slot = (gx1 - gx0) / len(days)
    bw = min(34, slot * 0.6)
    for i, day in enumerate(days):
        cx = gx0 + slot * (i + 0.5)
        v = per_day[day]
        if day in traded and abs(v) >= 0.005:
            yv = vy(v)
            box = (cx - bw / 2, min(yv, zy), cx + bw / 2, max(yv, zy))
            if box[3] - box[1] < 2:
                box = (box[0], zy - 1, box[2], zy + 1)
            d.rounded_rectangle(box, radius=4, fill=GOOD if v > 0 else BAD)
        if i % 2 == 0 or len(days) <= 7:
            lab = f"{day:%b} {day.day}"
            d.text((cx - d.textlength(lab, font=_font(13)) / 2, gy1 + 14), lab, font=_font(13), fill=INK_2)

    rx0 = M + lw + 18
    rx1 = W - M
    # by direction
    _panel(d, (rx0, y, rx1, y + 196), "BY DIRECTION")
    for j, dirn in enumerate(("UP", "DOWN")):
        sel = [r for r in rows if r.direction == dirn]
        p = sum(r.pos.realized_pnl or 0 for r in sel)
        w = sum(1 for r in sel if (r.pos.realized_pnl or 0) > 0)
        yy = y + 62 + j * 62
        d.text((rx0 + 22, yy), f"BTC {'▲' if dirn == 'UP' else '▼'} {dirn}", font=_font(19, True), fill=INK)
        d.text((rx0 + 22, yy + 26), f"{len(sel)} trades · {w} won", font=_font(14), fill=INK_3)
        val = _usd(p) if sel else "—"
        f = _font(20, True)
        d.text((rx1 - 22 - d.textlength(val, font=f), yy + 8), val, font=f,
               fill=(GOOD if p >= 0 else BAD) if sel else INK_3)
    # streak
    sy = y + 196 + 16
    _panel(d, (rx0, sy, rx1, y + ch), "CURRENT STREAK")
    sn, swin = _streak(rows)
    if sn:
        col = GOOD if swin else BAD
        d.ellipse((rx0 + 22, sy + 56, rx0 + 70, sy + 104), outline=col, width=2)
        d.text((rx0 + 37, sy + 63), "▲" if swin else "▼", font=_font(24, True), fill=col)
        d.text((rx0 + 86, sy + 52), f"{'+' if swin else '−'}{sn}", font=_font(30, True), fill=col)
        d.text((rx0 + 88, sy + 88), "WINS" if swin else "LOSSES", font=_font(15, True), fill=INK_2)
    else:
        d.text((rx0 + 22, sy + 64), "No trades yet", font=_font(18), fill=INK_3)

    # ---- trade history + side panel ------------------------------------------
    y += ch + 18
    hh = H - 92 - y
    _panel(d, (M, y, M + lw, y + hh), "TRADE HISTORY")
    cols = [("DATE / TIME (ET)", 22), ("MARKET", 205), ("PICK", 310), ("COST", 415), ("PRICE", 505),
            ("P&L", 585), ("STATUS", 685)]
    hy = y + 62
    for name, cx in cols:
        d.text((M + cx, hy), name, font=_font(13, True), fill=INK_3)
    d.line((M + 1, hy + 26, M + lw - 1, hy + 26), fill=BORDER, width=1)
    max_rows = max(0, (hh - 100) // 40)
    recent = list(reversed(rows))[:max_rows]
    if not recent:
        d.text((M + 22, hy + 50), "No finished trades in this period yet.", font=_font(17), fill=INK_3)
    for k, r in enumerate(recent):
        p = r.pos
        ry = hy + 40 + k * 40
        t = (p.closed_at or p.opened_at).astimezone(ET)
        f = _font(15)
        d.text((M + 22, ry), f"{t:%b} {t.day}, {t:%I:%M %p}".replace(" 0", " "), font=f, fill=INK)
        d.text((M + 205, ry), "BTC 15M", font=f, fill=INK)
        pick = f"{'▲' if r.direction == 'UP' else '▼'} {r.direction}" if r.direction else p.side.upper()
        d.text((M + 310, ry), pick, font=f, fill=INK)
        d.text((M + 415, ry), _usd(p.cost + p.entry_fee, signed=False), font=f, fill=INK)
        d.text((M + 505, ry), f"{p.entry_price:.0f}¢", font=f, fill=INK)
        pv = p.realized_pnl or 0
        d.text((M + 585, ry), _usd(pv), font=_font(15, True), fill=GOOD if pv > 0 else BAD if pv < 0 else INK_2)
        lab, fg, bg = _status(p)
        sf = _font(12, True)
        sw = d.textlength(lab, font=sf) + 22
        d.rounded_rectangle((M + 685, ry - 3, M + 685 + sw, ry + 21), radius=11, fill=bg, outline=fg, width=1)
        d.text((M + 696, ry + 2), lab, font=sf, fill=fg)
        if k < len(recent) - 1:
            d.line((M + 1, ry + 30, M + lw - 1, ry + 30), fill=(26, 29, 34), width=1)

    _panel(d, (rx0, y, rx1, y + hh), "TODAY")
    today_rows = [r for r in rows if _et_day(r.pos) == data.today]
    tp = sum(r.pos.realized_pnl or 0 for r in today_rows)
    d.text((rx0 + 20, y + 58), _usd(tp), font=_font(38, True), fill=GOOD if tp >= 0 else BAD)
    tw_ = sum(1 for r in today_rows if (r.pos.realized_pnl or 0) > 0)
    d.text((rx0 + 22, y + 108), f"{len(today_rows)} trades · {tw_}W / {len(today_rows) - tw_}L", font=_font(16),
           fill=INK_2)
    d.line((rx0 + 22, y + 146, rx1 - 22, y + 146), fill=BORDER, width=1)
    d.text((rx0 + 22, y + 162), "BALANCE", font=_font(14, True), fill=INK_3)
    d.text((rx0 + 22, y + 184), _usd(data.balance, signed=False), font=_font(26, True), fill=INK)
    # slogan
    sl = _font(30, True)
    for i, word in enumerate(("GOOD", "PICKS", "MAKE", "MONEY")):
        d.text((rx0 + 30 + i * 8, y + hh - 190 + i * 38), word, font=sl, fill=(70, 75, 84))

    # ---- footer ---------------------------------------------------------------
    fy = H - 70
    d.line((0, fy - 8, W, fy - 8), fill=BORDER, width=1)
    if LOGO.is_file():
        logo = Image.open(LOGO).convert("RGBA")
        logo = logo.resize((80, int(logo.height * 80 / logo.width)), Image.LANCZOS)
        img.paste(logo, (M, fy + 4), logo)
    ff = _font(13, True)
    d.text((140, fy + 22), "K A L S H I    /    B T C   1 5 M    /    "
           + ("P A P E R" if data.mode == "paper" else "R E A L   M O N E Y"), font=ff, fill=INK_3)
    tag = "D I S C I P L I N E    C R E A T E S    F R E E D O M"
    d.text((W - M - d.textlength(tag, font=ff), fy + 22), tag, font=ff, fill=INK_3)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
