"""
qr_brand.py — «عکسِ دلخواه پشتِ بارکد» (PATCH_QR_BACKGROUND_V1).

مالکِ رباتِ VPN می‌تواند یک عکس آپلود کند تا از آن به بعد هر QR ای که ربات
می‌سازد، روی همان عکس بنشیند: بارکد داخلِ یک کارتِ سفیدِ گِرد در وسط، و
عکسِ مالک به‌عنوانِ پس‌زمینه.

عکس به‌صورتِ base64 در جدولِ Setting همان ربات ذخیره می‌شود (کلیدِ
`qr_bg_image`) — نه به‌صورتِ file_id، چون برایِ ترکیب‌کردن به بایت‌هایِ
واقعیِ عکس نیاز داریم و file_id هر بار دانلودِ تازه می‌خواهد.

اگر عکسی ثبت نشده باشد، همان QR سادهٔ سیاه‌وسفیدِ قبلی ساخته می‌شود؛ پس
ربات‌هایِ موجود هیچ تغییری نمی‌بینند.
"""
from __future__ import annotations

import base64
import io
import os
import time

import qrcode
from PIL import Image, ImageDraw

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

BG_KEY = "qr_bg_image"

# اندازهٔ نهاییِ تصویر و نسبتِ کارتِ سفیدِ وسط
CANVAS = 1000
CARD_RATIO = 0.58        # ضلعِ کارتِ سفید نسبت به کلِ تصویر
CARD_RADIUS = 56         # گردیِ گوشه‌هایِ کارت
QR_PAD_RATIO = 0.12      # حاشیهٔ داخلیِ کارت تا خودِ بارکد

# سقفِ حجمِ عکسِ ذخیره‌شده (بعد از فشرده‌سازی). Setting.value متنی است و
# base64 حدودِ ۳۳٪ حجم اضافه می‌کند، پس محافظه‌کارانه محدودش می‌کنیم.
MAX_BG_BYTES = 900_000


# ═══════════════ ذخیره و خواندنِ پس‌زمینه ═══════════════

def normalize_bg(raw: bytes) -> bytes:
    """عکسِ ورودی را به مربعِ CANVAS برش می‌دهد و فشرده می‌کند."""
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    elif img.mode == "RGBA":
        flat = Image.new("RGB", img.size, (0, 0, 0))
        flat.paste(img, mask=img.split()[3])
        img = flat

    # برشِ مربعیِ وسط‌چین، بعد تغییرِ اندازه
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2,
                    (w - side) // 2 + side, (h - side) // 2 + side))
    img = img.resize((CANVAS, CANVAS), Image.LANCZOS)

    for quality in (85, 70, 55, 40):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= MAX_BG_BYTES:
            return data
    return data


# === PATCH_QR_BG_CACHE_V1 ===
# اندازه‌گیریِ زنده: «کانفیگ‌هایِ تکی» با ۱۹ کانفیگ ۳۰ ثانیه طول کشید؛ هر QR یک‌بار کلِ عکسِ پس‌زمینه
# (base64، تا ~۱٫۲MB) را از جدولِ settings می‌خواند (۱۹ خوانشِ هم‌زمان = «SELECT settings…» تا ۳ثانیه) و
# خروجیِ PNG هم ~۱–۲MB بود (۱۹ آپلودِ سنگین). پس‌زمینه دقیقه‌ها عوض نمی‌شود ⇒ در حافظه کش می‌شود.
_BG_CACHE: dict[int, tuple[float, bytes | None]] = {}
_BG_TTL = 300.0


async def get_bg(bot_id: int) -> bytes | None:
    from sqlalchemy import select

    from config import Session
    from models import Setting
    hit = _BG_CACHE.get(bot_id)
    if hit and time.monotonic() - hit[0] < _BG_TTL:
        return hit[1]
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == BG_KEY))
    data = None
    if row and row.value:
        try:
            data = base64.b64decode(row.value)
        except Exception:
            logger.exception("پس‌زمینهٔ QR خراب است (bot_id=%s)", bot_id)
            data = None
    _BG_CACHE[bot_id] = (time.monotonic(), data)
    return data


async def set_bg(bot_id: int, data: bytes | None) -> None:
    from sqlalchemy import select

    from config import Session
    from models import Setting
    value = base64.b64encode(data).decode() if data else ""
    _BG_CACHE.pop(bot_id, None)   # PATCH_QR_BG_CACHE_V1
    async with Session() as s:
        row = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == BG_KEY))
        if row:
            row.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=BG_KEY, value=value))
        await s.commit()


# ═══════════════ ساختِ خودِ تصویر ═══════════════

# === PATCH_QR_ERROR_CORRECTION_LADDER_V1 ===
# سطحِ تصحیحِ خطا هرچه بالاتر، ظرفیتِ QR کمتر. با ERROR_CORRECT_H یک
# کانفیگِ vless/vmess بلند در هیچ نسخه‌ای (حداکثر ۴۰) جا نمی‌شود و qrcode
# با «Invalid version (was 41, expected 1 to 40)» می‌ترکد — ۲۲ بار در
# error.log. پله‌پله سطح را پایین می‌آوریم تا جا شود؛ QR بزرگ‌ترِ خوانا
# بهتر از QRِ ساخته‌نشده است.
_EC_LADDER = [
    qrcode.constants.ERROR_CORRECT_H,   # ~30% — کیفیتِ دلخواه
    qrcode.constants.ERROR_CORRECT_Q,   # ~25%
    qrcode.constants.ERROR_CORRECT_M,   # ~15%
    qrcode.constants.ERROR_CORRECT_L,   # ~7%  — بیشترین ظرفیت
]


def _make_qr_image(payload: str, *, box_size: int = 10, border: int = 0):
    """QR را می‌سازد و اگر payload جا نشد، سطحِ تصحیحِ خطا را پایین می‌آورد.
    اگر حتی با L هم جا نشد، همان استثنا را بالا می‌دهد (فراخوان تصمیم بگیرد)."""
    last_err: Exception | None = None
    for ec in _EC_LADDER:
        try:
            qr = qrcode.QRCode(error_correction=ec, box_size=box_size, border=border)
            qr.add_data(payload)
            qr.make(fit=True)
            return qr.make_image(fill_color="black", back_color="white").convert("RGB")
        except Exception as e:      # DataOverflowError یا ValueError نسخهٔ ۴۱
            last_err = e
    # === PATCH_QR_TOO_LONG_IS_NOT_AN_ERROR_V1 ===
    # ظرفیتِ بیشینهٔ استانداردِ QR (نسخهٔ ۴۰) با کمترین سطحِ تصحیحِ خطا
    # ۲۹۵۳ بایت است. کانفیگ‌هایِ بلند (چند اینباند در یک payload) از این
    # هم ردّ می‌شوند و *هیچ* QR ای برایشان وجود ندارد — این محدودیتِ خودِ
    # استاندارد است، نه باگِ ما. پس به‌جایِ raise (که در error.log به‌شکلِ
    # ERROR ثبت می‌شد و شبیهِ خرابی به نظر می‌رسید) None برمی‌گردانیم تا
    # فراخوان همان متنِ کانفیگ را بفرستد.
    logger.warning("payload برایِ QR بیش‌ازحد بلند است (%d بایت > ۲۹۵۳)؛ "
                   "به‌جایش متن فرستاده می‌شود.", len(payload))
    return None


def render(payload: str, bg: bytes | None = None) -> bytes | None:
    """QR را می‌سازد؛ با bg رویِ عکس می‌نشیند، بدونِ آن همان QR سادهٔ قبلی."""
    def _plain() -> bytes | None:
        img = _make_qr_image(payload, border=4)
        if img is None:
            return None                      # payload از ظرفیتِ QR بزرگ‌تر است
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    if not bg:
        return _plain()
    try:
        out = _render_on_bg(payload, bg)
        if out is not None:
            return out
    except Exception:
        logger.exception("ترکیبِ QR با پس‌زمینه ناموفق بود؛ QR ساده ارسال می‌شود")
    return _plain()


def _render_on_bg(payload: str, bg: bytes) -> bytes:
    canvas = Image.open(io.BytesIO(bg)).convert("RGB")
    if canvas.size != (CANVAS, CANVAS):
        canvas = canvas.resize((CANVAS, CANVAS), Image.LANCZOS)

    card = int(CANVAS * CARD_RATIO)
    x0 = y0 = (CANVAS - card) // 2

    # کارتِ سفیدِ گِرد پشتِ بارکد — بدونِ آن، اسکنر رویِ عکسِ شلوغ QR را
    # نمی‌خواند (کنتراست لازم است).
    plate = Image.new("RGBA", (card, card), (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle(
        (0, 0, card - 1, card - 1), radius=CARD_RADIUS, fill=(255, 255, 255, 255))
    canvas.paste(plate, (x0, y0), plate)

    # بارکد با تصحیحِ خطایِ بالا، تا اگر گوشه‌ای خط افتاد باز هم خوانده شود
    # === PATCH_QR_ERROR_CORRECTION_LADDER_V1 ===
    qr_img = _make_qr_image(payload, box_size=10, border=0)
    if qr_img is None:
        return None                          # PATCH_QR_TOO_LONG_IS_NOT_AN_ERROR_V1

    inner = card - int(card * QR_PAD_RATIO * 2)
    qr_img = qr_img.resize((inner, inner), Image.NEAREST)
    canvas.paste(qr_img, (x0 + (card - inner) // 2, y0 + (card - inner) // 2))

    out = io.BytesIO()
    # === PATCH_QR_BG_JPEG_V1 ===
    # روی عکسِ پس‌زمینه PNG بی‌دلیل سنگین است (۱–۲٫۵MB؛ تلگرام عکس را خودش به JPEG تبدیل می‌کند). JPEG با
    # کیفیتِ ۸۸ و بدونِ کاهشِ رنگ ~۵ برابر سبک‌تر و برایِ اسکنِ QR (ماژول‌هایِ ~۴px روی کارتِ سفید) کافی است.
    # TG_QR_BG_PNG=1 رفتارِ قبلی را برمی‌گرداند.
    if os.getenv("TG_QR_BG_PNG", "0") == "1":
        canvas.save(out, format="PNG", optimize=True)
    else:
        canvas.save(out, format="JPEG", quality=88, subsampling=0, optimize=False)
    return out.getvalue()


async def render_for(bot_id: int, payload: str) -> bytes:
    """میان‌بُرِ پرکاربرد: خواندنِ پس‌زمینهٔ ربات + ساختِ تصویر."""
    import asyncio
    bg = await get_bg(bot_id)
    return await asyncio.to_thread(render, payload, bg)
