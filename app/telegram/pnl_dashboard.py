"""Dashboard-style P&L image (PNG): JGA Picks branding, stat tiles, daily profit bars,
today's result and slogan filler.

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

W, H = 1200, 1190
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


def _crown(d: ImageDraw.ImageDraw, x: float, y: float, w: float, color, width: int = 3) -> None:
    h = w * 0.62
    pts = [(x, y + h), (x, y + h * 0.25), (x + w * 0.27, y + h * 0.55), (x + w * 0.5, y),
           (x + w * 0.73, y + h * 0.55), (x + w, y + h * 0.25), (x + w, y + h)]
    d.line([*pts, pts[0]], fill=color, width=width, joint="curve")
    for px, py in (pts[1], pts[3], pts[5]):
        d.ellipse((px - 4, py - 4, px + 4, py + 4), fill=color)


def _slogan(img: Image.Image, box: tuple[int, int, int, int]) -> None:
    """'GOOD PICKS MAKE MONEY' as tilted, hand-placed filler with a crown and small tags."""
    x0, y0, x1, y1 = box
    layer = Image.new("RGBA", (560, 300), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    f = _font(52, True)
    ld.text((10, 10), "GOOD PICKS", font=f, fill=(*INK, 235))
    ld.text((40, 82), "MAKE MONEY", font=f, fill=(*INK, 235))
    _crown(ld, 400, 168, 74, (*INK, 235), width=4)
    ld.line((40, 158, 360, 158), fill=(*GOOD, 230), width=4)
    layer = layer.rotate(7, resample=Image.BICUBIC, expand=True)
    img.paste(layer, (x0 + 36, y0 - 6), layer)
    d = ImageDraw.Draw(img)
    tags = ["RESEARCH", "ANALYZE", "PROFIT"]
    tf = _font(13, True)
    tx = x0 + 30
    ty = y1 - 48
    for t in tags:
        tw = d.textlength(t, font=tf) + 24
        d.rounded_rectangle((tx, ty, tx + tw, ty + 26), radius=13, outline=BORDER, width=1)
        d.text((tx + 12, ty + 5), t, font=tf, fill=INK_2)
        tx += tw + 10
    note = "DISCIPLINE  ·  PATIENCE  ·  EDGE"
    nf = _font(13, True)
    d.text((x1 - 30 - d.textlength(note, font=nf), ty + 6), note, font=nf, fill=INK_3)
    for k in range(3):  # small corner dots
        d.ellipse((x1 - 40 + k * 10, y0 + 22, x1 - 35 + k * 10, y0 + 27), fill=INK_3)


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

    # ---- daily profit bars (full width) --------------------------------------
    y += th + 18
    lw = W - 2 * M
    ch = 360
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
        if True:
            lab = f"{day:%b} {day.day}"
            d.text((cx - d.textlength(lab, font=_font(13)) / 2, gy1 + 14), lab, font=_font(13), fill=INK_2)

    # ---- bottom row: today + slogan filler -----------------------------------
    y += ch + 18
    bh = H - 92 - y
    tw0 = 360
    _panel(d, (M, y, M + tw0, y + bh), "TODAY")
    today_rows = [r for r in rows if _et_day(r.pos) == data.today]
    tp = sum(r.pos.realized_pnl or 0 for r in today_rows)
    d.text((M + 20, y + 56), _usd(tp), font=_font(40, True), fill=GOOD if tp >= 0 else BAD)
    tw_ = sum(1 for r in today_rows if (r.pos.realized_pnl or 0) > 0)
    d.text((M + 22, y + 110), f"{len(today_rows)} trades · {tw_}W / {len(today_rows) - tw_}L", font=_font(16),
           fill=INK_2)
    d.line((M + 22, y + 146, M + tw0 - 22, y + 146), fill=BORDER, width=1)
    d.text((M + 22, y + 162), "BALANCE", font=_font(14, True), fill=INK_3)
    d.text((M + 22, y + 184), _usd(data.balance, signed=False), font=_font(28, True), fill=INK)

    sx0, sx1 = M + tw0 + 18, W - M
    _panel(d, (sx0, y, sx1, y + bh))
    _slogan(img, (sx0, y, sx1, y + bh))
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
