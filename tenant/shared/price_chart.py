"""price_chart.py — کارت تصویری قیمت با نمودار کندلی (PATCH_PRICE_LOOKUP_V2؛ ظاهر شبیه PriceifyBot).

فقط Pillow (بدون matplotlib). متن‌های تصویر لاتین/عدد است تا به فونت فارسی نیاز نباشد.
"""
from __future__ import annotations

import io
import os
import time

W, H = 1600, 960
PAD = 56
BLUE = (51, 153, 229)
GREEN = (38, 166, 154)
RED = (229, 72, 77)
GRID = (226, 230, 234)
TXT = (17, 17, 17)
MUTED = (110, 116, 124)

_FONT_PATHS = {
    True: ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
           "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    False: ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"),
}
_FONTS: dict = {}


def font(size: int, bold: bool = False):
    from PIL import ImageFont
    k = (size, bold)
    if k in _FONTS:
        return _FONTS[k]
    f = None
    for p in _FONT_PATHS[bold]:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                break
            except OSError:
                continue
    if f is None:
        try:
            f = ImageFont.load_default(size=size)        # Pillow ≥ 10.1
        except TypeError:
            f = ImageFont.load_default()
    _FONTS[k] = f
    return f


def fmt_price(x: float) -> str:
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:,.3f}".rstrip("0").rstrip(".") if x < 100 else f"{x:,.2f}"
    s = f"{x:.10f}".rstrip("0")
    head = len(s) - len(s.lstrip("0.")) if x else 0
    return f"{x:.{max(4, head + 3)}f}".rstrip("0").rstrip(".")


def _axis_labels(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo * 1.01 + 1e-12
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def _tw(d, text, f) -> int:
    b = d.textbbox((0, 0), text, font=f)
    return b[2] - b[0]


def render_card(*, title: str, subtitle: str, price: float, change_pct: float | None, change_usd: float | None,
                candles: list, bot_username: str = "", stamp: str = "") -> bytes:
    """candles: [(ts_sec, o, h, l, c)] — اگر خالی بود کارت بدون نمودار ساخته می‌شود."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), BLUE)
    d = ImageDraw.Draw(img)
    for y in range(H):                                       # پس‌زمینهٔ گرادیان آبی
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(96 - 70 * t), int(178 - 90 * t), int(245 - 95 * t)))
    cx0, cy0, cx1, cy1 = 56, 56, W - 56, H - 56
    d.rounded_rectangle([cx0, cy0, cx1, cy1], radius=40, fill=(255, 255, 255))

    if bot_username:
        u = "@" + bot_username.lstrip("@")
        d.text(((W - _tw(d, u, font(26))) / 2, cy0 + 36), u, fill=MUTED, font=font(26))
    # آیکون الماس ساده + عنوان
    ix, iy = cx0 + 44, cy0 + 86
    d.ellipse([ix, iy, ix + 86, iy + 86], fill=BLUE)
    d.polygon([(ix + 22, iy + 34), (ix + 32, iy + 22), (ix + 54, iy + 22), (ix + 64, iy + 34), (ix + 43, iy + 64)],
              fill=(255, 255, 255))
    d.text((ix + 104, iy + 2), title[:28], fill=TXT, font=font(62, True))
    d.text((ix + 104, iy + 84), subtitle[:70], fill=MUTED, font=font(28, True))
    d.text((ix, iy + 130), "$", fill=BLUE, font=font(120, True))
    d.text((ix + 80, iy + 120), fmt_price(price), fill=TXT, font=font(130, True))
    if change_pct is not None:
        col = GREEN if change_pct >= 0 else RED
        t1 = f"{change_pct:+.2f}%"
        d.text((cx1 - 44 - _tw(d, t1, font(40, True)), iy + 6), t1, fill=col, font=font(40, True))
        if change_usd is not None:
            t2 = f"{'+' if change_pct >= 0 else '-'}${fmt_price(abs(change_usd))}"
            d.text((cx1 - 44 - _tw(d, t2, font(30, True)), iy + 58), t2, fill=col, font=font(30, True))

    # نمودار
    gx0, gy0, gx1, gy1 = cx0 + 36, cy0 + 350, cx1 - 150, cy1 - 96
    d.rectangle([gx0, gy0, gx1, gy1], outline=GRID, width=2)
    cs = [c for c in candles or [] if len(c) >= 5][-96:]
    if len(cs) >= 2:
        lo = min(c[3] for c in cs)
        hi = max(c[2] for c in cs)
        span = (hi - lo) or (hi * 0.01 or 1e-9)
        lo, hi = lo - span * 0.05, hi + span * 0.05
        span = hi - lo

        def y(v):
            return gy1 - (v - lo) / span * (gy1 - gy0)
        for v in _axis_labels(lo, hi):
            yy = y(v)
            d.line([(gx0, yy), (gx1, yy)], fill=GRID, width=2)
            d.text((gx1 + 14, yy - 14), fmt_price(v), fill=MUTED, font=font(24))
        n = len(cs)
        step = (gx1 - gx0) / n
        bw = max(2, step * 0.6)
        for i in range(0, n, max(1, n // 7)):                    # برچسب ساعت (به وقت ایران)
            xx = gx0 + step * (i + 0.5)
            d.line([(xx, gy0), (xx, gy1)], fill=GRID, width=2)
            lbl = time.strftime("%H:%M", time.gmtime(cs[i][0] + 12600))
            d.text((xx - _tw(d, lbl, font(24)) / 2, gy1 + 12), lbl, fill=MUTED, font=font(24))
        for i, (_ts, o, h, l, c) in enumerate(cs):
            xx = gx0 + step * (i + 0.5)
            col = GREEN if c >= o else RED
            d.line([(xx, y(h)), (xx, y(l))], fill=col, width=2)
            top, bot = y(max(o, c)), y(min(o, c))
            if bot - top < 2:
                bot = top + 2
            d.rectangle([xx - bw / 2, top, xx + bw / 2, bot], fill=col)
    else:
        msg = "chart not available"
        d.text(((gx0 + gx1 - _tw(d, msg, font(30))) / 2, (gy0 + gy1) / 2 - 15), msg, fill=MUTED, font=font(30))
    if stamp:
        d.text(((W - _tw(d, stamp, font(26))) / 2, cy1 - 52), stamp, fill=MUTED, font=font(26))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()
