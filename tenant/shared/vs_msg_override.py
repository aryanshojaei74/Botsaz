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

# === PATCH_VS_CUSTOM_TEXTS_CHAINS_V1 ===
# گزارش فدایی: «برای گیفت و اینا باز پیامی که تنظیم کردیم برای ثبت و لغو نمی‌ده». مالک «ثبت» را گاهی زیر
# «🚀 ثبت روی پنل» و «لغو» را زیر «🚫 لغو توسط پنل»/«↩️ بازگشت وجه» می‌نویسد، ولی هر پیام پیش‌فرض فقط یک کلید
# را می‌خواند. حالا هر پیام یک «زنجیره» از کلیدها دارد: اولین کلیدی که مالک متن دلخواه گذاشته باشد استفاده می‌شود.
# چند پیام تکمیل (گیفت فوری، پرمیوم فوری، تون تایید‌شده) هم که هیچ‌وقت شناخته نمی‌شدند اضافه شدند.
_PLACED = ("order_placed_manual", "sent")
_DONE = ("completed",)
_REJECT = ("rejected", "refunded", "canceled", "failed")
_FAILED = ("failed", "rejected", "canceled", "refunded")
_REFUND = ("refunded", "rejected", "canceled", "failed")
_CANCELED = ("canceled", "refunded", "rejected", "failed")

# الگوی آغاز پیام پیش‌فرض (بدون اعراب؛ مقایسه روی متن بدون اعراب انجام می‌شود) → زنجیرهٔ کلیدها.
# ترتیب مهم است: الگوی خاص‌تر باید قبل از عمومی‌تر بیاید.
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("✅ سفارش شما با موفقیت ثبت شد! 🎉", _PLACED),
    # === PATCH_VS_CUSTOM_TEXTS_PAID_V1 === پیام ثبت سفارش استارز/تون/ری‌اکشن/پریمیوم خودکار (پرداخت از کیف‌پول)
    # و رسید کارت‌به‌کارت
    ("✅ پرداختت ثبت شد — سفارشت", _PLACED),
    ("✅ رسید دریافت شد و برای تایید ادمین", _PLACED),
    ("✅ سفارش شما با موفقیت تکمیل شد! 🎉", _DONE),
    ("✅ گیفت با موفقیت ارسال شد!", _DONE),          # ارسال خودکار فوری گیفت با استارز ربات
    ("✅ گیفتت با موفقیت ارسال شد!", _DONE),         # تایید ادمین برای گیفت دستی
    (re.compile(r"✅ پرمیوم \d+ ماهه با موفقیت فعال شد"), _DONE),   # پرمیوم فوری (نه پیام ادمین «✅ پرمیوم سفارش …»)
    ("✅ سفارش تون تو (", _DONE),                    # تایید ادمین برای تون
    ("❌ متاسفانه سفارشت توسط ارائه‌دهنده لغو شد", _CANCELED),   # لغو TGTools + بازگشت خودکار وجه
    ("❌ متاسفانه ارسال", _FAILED),                  # ارسال گیفت ناموفق بود
    ("❌ متاسفانه فعال", _FAILED),                   # فعال‌سازی پرمیوم ناموفق بود
    ("❌ متاسفانه", _REJECT),
    ("↩️ سفارش تون تو", _REFUND),
)
_MARKS_RE = re.compile("[\u064b-\u0652]")
_TRK_RE = re.compile(r"کد پیگیری(?: سفارش)?:\s*(?:<code>)?([A-Za-z0-9_-]+)(?:</code>)?")
_ID_RE = re.compile(r"#([A-Za-z0-9_-]+)")


def _nm(text: str | None) -> str:
    """متن بدون اعراب (کد برنامه گاهی «برای/تایید» با کسره می‌نویسد)."""
    return _MARKS_RE.sub("", text or "")


def rule_keys(text: str | None) -> tuple[str, ...] | None:
    """زنجیرهٔ کلیدهای متن قابل‌تنظیم برای این پیام پیش‌فرض، یا None اگر پیام سفارش نیست."""
    t = _nm((text or "")[:240])[:160]       # این تابع برای هر پیام خروجی صدا زده می‌شود؛ فقط ابتدای متن لازم است
    for pat, chain in RULES:
        if (pat.match(t) if hasattr(pat, "match") else t.startswith(pat)):
            return chain
    return None


def rule_key(text: str | None) -> str | None:
    """کلید اصلی پیام (اولین کلید زنجیره)."""
    chain = rule_keys(text)
    return chain[0] if chain else None


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


def order_shim(o, chat_id: int | None = None):
    """GiftOrder → شیئی با همان صفت‌هایی که text_vars._order_values می‌خواند."""
    try:
        from tenant.bots.virtual_service.smm import _VS_GIFT_STATUS_LABELS
        status_label = _VS_GIFT_STATUS_LABELS.get(str(getattr(o, "status", "") or "").lower())
    except Exception:
        status_label = None
    return SimpleNamespace(
        id=o.id, tracking_code=o.tracking_code, service_name=_label(o), quantity=int(getattr(o, "quantity", 1) or 1),
        price_toman=int(getattr(o, "price_toman", 0) or 0) * int(getattr(o, "quantity", 1) or 1),
        link=getattr(o, "dest_label", "") or "", service_id=getattr(o, "gift_id", "") or "",
        status=getattr(o, "status", "") or "", created_at=getattr(o, "created_at", None),
        remaining=None, refunded_toman=None, smm_service_pk=None,
        # {{new_balance}} موجودی همین کاربر را از روی این آیدی می‌خواند (قبلا برای استارز/تون/گیفت همیشه «—» بود)
        user_telegram_id=chat_id,
        # همهٔ این سفارش‌ها تلگرامی‌اند؛ {{platform}}/{{category}} (نوع اپلیکیشن/دسته) قبلا «—» می‌شد
        platform_label="تلگرام", category_label="تلگرام", status_label=status_label)


async def override_text(bot_id: int, chat_id: int, text: str) -> str | None:
    """متنِ جایگزین، یا None اگر چیزی عوض نمی‌شود."""
    chain = rule_keys(text)
    if not chain:
        return None
    try:
        from tenant.bots.virtual_service.smm_texts import get_custom
        customs = [(key, await get_custom(bot_id, key)) for key in chain]
        if not any(c for _k, c in customs):
            return None
        from config import Session
        from models import BotUser, GiftOrder
        from sqlalchemy import select
        order = None
        async with Session() as s:
            o = None
            m = _TRK_RE.search(text)
            if m:
                o = await s.scalar(select(GiftOrder).where(
                    GiftOrder.bot_id == bot_id, GiftOrder.tracking_code == m.group(1)))
            if o is None:                        # پیام تون «سفارش تون تو (#کد)» کد پیگیری را فقط با # دارد
                m2 = _ID_RE.search(text)
                if m2:
                    o = await s.scalar(select(GiftOrder).where(
                        GiftOrder.bot_id == bot_id, GiftOrder.tracking_code == m2.group(1)))
            order = order_shim(o, chat_id) if o else None
            bu = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == chat_id))
        # زنجیره فقط برای سفارش‌های استارز/تون/گیفت/… (GiftOrder) است؛ پیامی که سفارشش پیدا نشد (مثلا متن پیش‌فرض
        # سفارش SMM) مثل قبل فقط کلید اصلی را می‌خواند تا متن «ثبت روی پنل» جای آن ننشیند.
        custom = next((c for _k, c in (customs if order is not None else customs[:1]) if c), "")
        if not custom:
            return None
        from tenant.shared.text_vars import render_text
        return await render_text(custom, bot_id=bot_id, user=bu, order=order)
    except Exception:
        logger.exception("vs_msg_override: جایگزینیِ متنِ سفارش ناموفق بود (متنِ پیش‌فرض می‌رود)")
        return None
