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

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.trading.positions import Position

W, H = 1200, 675
ET = ZoneInfo("America/New_York")
LOGO = Path(__file__).resolve().parents[1] / "assets" / "jga_logo.png"

# Futuristic dark theme. Text never wears the status colours (sign + words carry meaning too).
BG = (5, 7, 13)
PANEL = (10, 18, 30, 210)
GRIDC = (0, 229, 255, 14)
CYAN = (0, 229, 255)
MAGENTA = (255, 61, 200)
INK = (234, 246, 255)
INK_2 = (150, 176, 196)
INK_3 = (86, 112, 134)
GOOD = (57, 255, 136)
BAD = (255, 61, 110)

_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu")


def _font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = f"DejaVuSans{'Mono' if mono else ''}{'-Bold' if bold else ''}.ttf"
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


def _glow_text(img: Image.Image, xy: tuple[float, float], text: str, font, color, radius: int = 10) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(xy, text, font=font, fill=(*color, 200))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius)))
    ImageDraw.Draw(img).text(xy, text, font=font, fill=(*color, 255))


def _brackets(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int], color, size: int = 22, width: int = 3) -> None:
    x0, y0, x1, y1 = box
    for (x, y, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        d.line((x, y, x + dx * size, y), fill=color, width=width)
        d.line((x, y, x, y + dy * size), fill=color, width=width)


def _background() -> Image.Image:
    img = Image.new("RGBA", (W, H), (*BG, 255))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((-260, -320, 560, 360), fill=(*CYAN, 46))
    g.ellipse((760, 330, 1500, 980), fill=(*MAGENTA, 40))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(120)))
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for x in range(0, W, 40):
        gd.line((x, 0, x, H), fill=GRIDC, width=1)
    for y in range(0, H, 40):
        gd.line((0, y, W, y), fill=GRIDC, width=1)
    img.alpha_composite(grid)
    return img


def _logo(img: Image.Image, x: int, y: int, width: int) -> None:
    if not LOGO.is_file():
        return
    logo = Image.open(LOGO).convert("RGBA")
    logo = logo.resize((width, int(logo.height * width / logo.width)), Image.LANCZOS)
    alpha = logo.getchannel("A")
    halo = Image.new("RGBA", logo.size, (*CYAN, 0))
    halo.putalpha(alpha.point(lambda v: v * 0.9))
    pad = 24
    canvas = Image.new("RGBA", (logo.width + 2 * pad, logo.height + 2 * pad), (0, 0, 0, 0))
    canvas.alpha_composite(halo, (pad, pad))
    img.alpha_composite(canvas.filter(ImageFilter.GaussianBlur(9)), (x - pad, y - pad))
    tinted = Image.new("RGBA", logo.size, (*INK, 0))
    tinted.putalpha(alpha)
    img.alpha_composite(tinted, (x, y))


def render_pnl_card(card: PnlCard, app_name: str = "JGA Picks") -> bytes:
    img = _background()
    d = ImageDraw.Draw(img)
    pnls = [p.realized_pnl or 0.0 for p in card.trades]
    total = sum(pnls)
    wins = sum(1 for v in pnls if v > 0)
    n = len(pnls)
    tone = GOOD if total >= 0 else BAD
    _brackets(d, (18, 18, W - 18, H - 18), (*CYAN, 160))

    # header: logo left, mode + time right
    _logo(img, 44, 34, 190)
    if not LOGO.is_file():
        d.text((48, 50), app_name.upper(), font=_font(40, True), fill=INK)
    pill = "PAPER // PRACTICE" if card.mode == "paper" else "LIVE // REAL MONEY"
    pf = _font(20, True, mono=True)
    pw = d.textlength(pill, font=pf) + 34
    px0 = W - 48 - pw
    d.rounded_rectangle((px0, 40, W - 48, 78), radius=6, outline=(*(CYAN if card.mode == "paper" else MAGENTA), 255),
                        width=2)
    d.text((px0 + 17, 48), pill, font=pf, fill=CYAN if card.mode == "paper" else MAGENTA)
    now = (card.now or datetime.now(ET)).astimezone(ET)
    stamp = now.strftime("%b %d  %I:%M %p ET").upper().replace(" 0", " ")
    sf = _font(18, mono=True)
    d.text((W - 48 - d.textlength(stamp, font=sf), 92), stamp, font=sf, fill=INK_3)
    d.text((W - 48 - d.textlength("BTC 15M // KALSHI", font=sf), 116), "BTC 15M // KALSHI", font=sf, fill=INK_3)

    # hero
    d.text((48, 176), f"> {card.title}", font=_font(22, True, mono=True), fill=(*CYAN, 255))
    _glow_text(img, (42, 200), _usd(total), _font(96, True), tone, radius=14)

    # stat tiles (2 x 2)
    tiles = [("TRADES", f"{n}  ({wins}W / {n - wins}L)"),
             ("WIN RATE", f"{wins / n * 100:.0f}%" if n else "—"),
             ("BALANCE", _usd(card.balance, signed=False)),
             ("ALL-TIME", _usd(card.all_time))]
    tx0, ty0, tw, th, gap = 640, 170, 250, 72, 12
    for i, (label, value) in enumerate(tiles):
        x = tx0 + (i % 2) * (tw + gap)
        y = ty0 + (i // 2) * (th + gap)
        d.rounded_rectangle((x, y, x + tw, y + th), radius=8, fill=PANEL, outline=(*CYAN, 70), width=1)
        d.line((x + 10, y, x + 46, y), fill=(*CYAN, 230), width=3)
        d.text((x + 16, y + 12), label, font=_font(16, True, mono=True), fill=INK_3)
        d.text((x + 16, y + 34), value, font=_font(24, True), fill=INK)

    if card.last is not None and card.last.realized_pnl is not None:
        lp = card.last.realized_pnl
        word = "WON" if lp > 0 else "LOST"
        what = (f"BTC {'▲' if card.last_direction == 'UP' else '▼'} {card.last_direction}" if card.last_direction
                else f"BUY {card.last.side.upper()}")
        d.text((48, 330), f"LAST TRADE  {word}  ·  {what} @ {card.last.entry_price:.0f}¢  ·  {_usd(lp)}",
               font=_font(22, True, mono=True), fill=INK_2)

    # chart panel
    x0, y0, x1, y1 = 48, 380, W - 48, H - 76
    panel = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle((x0, y0, x1, y1), radius=10, fill=PANEL, outline=(*CYAN, 60), width=1)
    img.alpha_composite(panel)
    d = ImageDraw.Draw(img)
    _brackets(d, (x0, y0, x1, y1), (*CYAN, 220), size=14, width=2)
    d.text((x0 + 20, y0 + 12), "RUNNING P&L // TODAY", font=_font(16, True, mono=True), fill=INK_3)
    cx0, cy0, cx1, cy1 = x0 + 28, y0 + 44, x1 - 150, y1 - 22
    if n == 0:
        d.text((cx0, (cy0 + cy1) // 2 - 14), "AWAITING FIRST FINISHED TRADE...", font=_font(24, True, mono=True),
               fill=INK_3)
    else:
        cum = [0.0]
        for v in pnls:
            cum.append(cum[-1] + v)
        lo, hi = min(cum), max(cum)
        if hi - lo < 0.01:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.14
        lo, hi = lo - pad, hi + pad

        def xy(i: int, v: float) -> tuple[float, float]:
            return (cx0 + (cx1 - cx0) * (i / max(1, len(cum) - 1)),
                    cy1 - (cy1 - cy0) * ((v - lo) / (hi - lo)))

        zy = xy(0, 0.0)[1]
        for xs in range(int(cx0), int(cx1), 14):
            d.line((xs, zy, min(xs + 6, cx1), zy), fill=INK_3, width=1)
        d.text((cx1 + 16, zy - 11), "$0", font=_font(18, mono=True), fill=INK_3)
        pts = [xy(i, v) for i, v in enumerate(cum)]
        area = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(area).polygon([(pts[0][0], zy), *pts, (pts[-1][0], zy)], fill=(*CYAN, 34))
        img.alpha_composite(area)
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).line(pts, fill=(*CYAN, 255), width=10, joint="curve")
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(8)))
        d = ImageDraw.Draw(img)
        d.line(pts, fill=CYAN, width=3, joint="curve")
        for (x, y), v in zip(pts[1:], pnls):
            c = GOOD if v > 0 else BAD
            d.ellipse((x - 11, y - 11, x + 11, y + 11), fill=(*BG, 255), outline=(*c, 255), width=2)
            d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=c)
        ex, ey = pts[-1]
        d.text((ex + 18, ey - 14), _usd(cum[-1]), font=_font(22, True), fill=INK)

    # footer
    d.text((48, H - 56), f"{app_name.upper()} // AUTOMATED BTC SIGNALS", font=_font(16, True, mono=True), fill=INK_3)
    note = "RESULTS FROM FINISHED TRADES ONLY"
    nf = _font(16, mono=True)
    d.text((W - 48 - d.textlength(note, font=nf), H - 56), note, font=nf, fill=INK_3)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
