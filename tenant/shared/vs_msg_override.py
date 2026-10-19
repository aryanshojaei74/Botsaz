"""vs_msg_override.py — متن‌هایِ اطلاع‌رسانیِ سفارش (ثبت / تکمیل / رد) که مالک تنظیم کرده، برایِ *همهٔ* انواعِ سفارش.

=== PATCH_VS_CUSTOM_TEXTS_ALL_ORDERS_V1 ===
گزارشِ فدایی: «چرا پیامِ ثبت و تکمیل و لغوِ استارز و تون و گیفت و ری‌اکشنِ استارزی همون متنی که تنظیم کردیم نیست؟»
متن‌هایِ سفارشِ ربات (smm_txt_*: ثبت/تکمیل/رد) فقط برایِ سفارش‌هایِ SMM خوانده می‌شدند؛ استارز/تون/گیفت/پریمیوم/ری‌اکشن
حدود ۲۰ پیامِ ثابتِ داخلِ کد داشتند. به‌جایِ دست‌زدن به همهٔ آن‌ها، پیامِ خروجی را همان لحظهٔ ارسال می‌شناسیم
(با پیشوندِ پیش‌فرض + «کد پیگیری») و اگر مالک متنِ دلخواه گذاشته باشد جایگزین می‌کنیم. اگر متنی تنظیم نشده باشد
هیچ تغییری نمی‌کند. پیامِ تحویلِ «شمارهٔ مجازی» (که خودِ شماره را دارد) عمداً دست‌نخورده می‌ماند.
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace

logger = logging.getLogger(__name__)

# پیشوندِ پیامِ پیش‌فرض → کلیدِ متنِ قابل‌تنظیم (smm_texts.TEXTS)
RULES: tuple[tuple[str, str], ...] = (
    ("✅ سفارش شما با موفقیت ثبت شد! 🎉", "order_placed_manual"),
    # === PATCH_VS_CUSTOM_TEXTS_PAID_V1 === گزارشِ فدایی: پیامِ ثبتِ سفارشِ استارز/تون/ری‌اکشن/پریمیومِ خودکار با «✅ پرداختت ثبت شد…»
    # (پرداخت از کیف‌پول) و رسیدِ کارت‌به‌کارت با «✅ رسید دریافت شد…» شروع می‌شد و متنِ سفارشیِ مالک جایگزینش نمی‌شد.
    ("✅ پرداختت ثبت شد — سفارشت", "order_placed_manual"),
    ("✅ رسید دریافت شد و برایِ تاییدِ ادمین", "order_placed_manual"),
    ("✅ سفارش شما با موفقیت تکمیل شد! 🎉", "completed"),
    ("❌ متاسفانه", "rejected"),
    ("↩️ سفارشِ تونِ تو", "rejected"),
)
_TRK_RE = re.compile(r"کد پیگیری(?: سفارش)?:\s*<code>([^<\s]+)</code>")
_ID_RE = re.compile(r"#(\d+)")


def rule_key(text: str | None) -> str | None:
    t = text or ""
    for prefix, key in RULES:
        if t.startswith(prefix):
            return key
    return None


def _label(o) -> str:
    gid = str(getattr(o, "gift_id", "") or "")
    if gid.startswith("starsell_"):
        return f"⭐ {int(getattr(o, 'star_count', 0) or 0) * int(getattr(o, 'quantity', 1) or 1):,} استارز"
    if gid.startswith("premium_"):
        return "تلگرام پریمیوم"
    if gid.startswith("tonsell_"):
        return "TON"
    if gid.startswith("srx_"):
        return "ری‌اکشنِ استارزی"
    if gid.startswith("manual"):
        return "گیفتِ سفارشی"
    return f"گیفت {getattr(o, 'gift_emoji', '') or '🎁'}".strip()


def order_shim(o):
    """GiftOrder → شیئی با همان صفت‌هایی که text_vars._order_values می‌خواند."""
    return SimpleNamespace(
        id=o.id, tracking_code=o.tracking_code, service_name=_label(o), quantity=int(getattr(o, "quantity", 1) or 1),
        price_toman=int(getattr(o, "price_toman", 0) or 0) * int(getattr(o, "quantity", 1) or 1),
        link=getattr(o, "dest_label", "") or "", service_id=getattr(o, "gift_id", "") or "",
        status=getattr(o, "status", "") or "", created_at=getattr(o, "created_at", None),
        remaining=None, refunded_toman=None, smm_service_pk=None)


async def override_text(bot_id: int, chat_id: int, text: str) -> str | None:
    """متنِ جایگزین، یا None اگر چیزی عوض نمی‌شود."""
    key = rule_key(text)
    if not key:
        return None
    try:
        from tenant.bots.virtual_service.smm_texts import get_custom
        custom = await get_custom(bot_id, key)
        if not custom:
            return None
        from config import Session
        from models import BotUser, GiftOrder
        from sqlalchemy import select
        order = None
        async with Session() as s:
            m = _TRK_RE.search(text)
            if m:
                o = await s.scalar(select(GiftOrder).where(
                    GiftOrder.bot_id == bot_id, GiftOrder.tracking_code == m.group(1)))
                order = order_shim(o) if o else None
            bu = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == chat_id))
        from tenant.shared.text_vars import render_text
        return await render_text(custom, bot_id=bot_id, user=bu, order=order)
    except Exception:
        logger.exception("vs_msg_override: جایگزینیِ متنِ سفارش ناموفق بود (متنِ پیش‌فرض می‌رود)")
        return None
