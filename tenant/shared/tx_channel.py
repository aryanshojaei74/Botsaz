"""
tx_channel.py — PATCH_TX_LOG_CHANNEL_V1

کانالِ لاگِ تراکنش‌ها: هر شارژِ کیفِ پول، از هر درگاهی، در کانالی که
مالکِ ربات تنظیم کرده پست می‌شود. عمداً یک تابعِ واحد است تا اضافه‌شدنِ
درگاهِ جدید فقط یک خط صدازدن باشد و هیچ درگاهی از قلم نیفتد.

کلیدِ تنظیمات: tx_log_channel  (آیدیِ عددیِ کانال، مثلاً -1001234567890)
"""
from __future__ import annotations

import html as _html

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

# برچسبِ هشتگِ هر درگاه
_TAGS = {
    "zarinpal": "زرین‌پال",
    "card":     "کارت‌به‌کارت",
    "apay":     "آپی (کارت‌به‌کارتِ آنی)",   # PATCH_APAY_LOG_CHANNEL_V1
    "crypto":   "ارز دیجیتال",
    "ton":      "تون",
    "stars":    "استارز تلگرام",
    "admin":    "شارژ دستی ادمین",
    # === PATCH_TX_LOG_ALL_MOVEMENTS_V1 ===
    # تا اینجا فقط درگاه‌هایِ پرداخت لاگ می‌شدند، ولی مالکِ ربات هر
    # جابه‌جاییِ کیفِ پول را می‌خواهد — از جمله آن‌هایی که اصلاً درگاه
    # نیستند (تنظیمِ دستیِ ادمین، پاداشِ دعوت، شارژِ گروهی).
    "admin_add":    "افزایشِ دستیِ ادمین",
    "admin_deduct": "کسرِ دستیِ ادمین",
    "referral":     "پاداشِ دعوت",
    "bulk":         "شارژِ گروهیِ ادمین",
}

# کدام برچسب‌ها «کاهش»اند — برایِ انتخابِ هشتگ و ایموجیِ درست
_DEDUCT_TAGS = {"admin_deduct"}

# درگاه‌هایِ واقعیِ پرداخت — بقیه افزایشِ بدونِ درگاه‌اند (پاداش، تنظیمِ
# دستی، شارژِ گروهی) و هشتگِ «#پرداخت از» برایشان بی‌معناست.
_GATEWAYS = {"zarinpal", "card", "crypto", "ton", "stars", "admin", "apay"}


def _fa(n) -> str:
    try:
        return f"{int(float(n)):,}".replace(",", "٬")
    except Exception:
        return str(n)


async def notify_log_or_owner(bot, bot_id: int, owner_id, text: str) -> None:
    """پیامِ «پرداخت جدید» را به کانالِ لاگِ تراکنش‌ها می‌فرستد (PATCH_DIRECT_PAY_TO_LOG_CHANNEL_V1).

    قبلاً اعلانِ واریزِ مستقیم (TON/ترون/تتر) فقط به پیویِ مالک می‌رفت، نه به
    کانالی که مالک برایِ لاگِ تراکنش‌ها تنظیم کرده. حالا اگر کانال تنظیم باشد
    همان‌جا پست می‌شود؛ فقط اگر کانال تنظیم نباشد یا ارسال ناموفق باشد، مثلِ قبل
    به پیویِ مالک می‌رود تا اعلان گم نشود."""
    try:
        from tenant import get_setting
        ch = (await get_setting(bot_id, "tx_log_channel", "") or "").strip()
    except Exception:
        ch = ""
    if ch:
        try:
            try:
                chat_id = int(ch)
            except Exception:
                chat_id = ch
            await bot.send_message(chat_id, text, parse_mode="HTML")
            _mark_ok(bot_id)
            return
        except Exception as e:
            logger.warning("tx_log: ارسالِ اعلانِ واریزِ مستقیم به کانال ناموفق بود "
                           "(bot_id=%s): %s", bot_id, e)
            await _warn_owner(bot, bot_id, e)
    if owner_id:
        await bot.send_message(owner_id, text)


async def tx_log(bot, bot_id: int, *, buyer=None, amount=0,
                 gateway: str = "", old_balance=None, new_balance=None,
                 card: str = "", ref: str = "", note: str = "") -> None:
    """پستِ لاگِ یک تراکنش در کانالِ تنظیم‌شده. هر خطایی بی‌صدا رد می‌شود
    تا هیچ‌وقت جلوی خودِ شارژ را نگیرد."""
    try:
        from tenant import get_setting
        ch = (await get_setting(bot_id, "tx_log_channel", "") or "").strip()
        if not ch:
            return

        label = _TAGS.get(gateway, gateway or "نامشخص")
        # === PATCH_TX_LOG_ALL_MOVEMENTS_V1 ===
        # مبلغِ منفی یعنی کاهشِ موجودی. قبلاً همه‌چیز «#پرداخت» و «مبلغ
        # پرداخت» بود، که برایِ کسر بی‌معنا و گمراه‌کننده است.
        try:
            _amt_f = float(amount)
            _is_deduct = _amt_f < 0
            _abs_amount = abs(_amt_f)
        except (TypeError, ValueError):
            # مبلغِ غیرعددی (نباید پیش بیاید) — همان‌طور که هست نمایش بده
            _is_deduct, _abs_amount = False, amount
        _is_deduct = _is_deduct or gateway in _DEDUCT_TAGS
        if _is_deduct:
            lines = [f"#کسر — <b>{_html.escape(label)}</b>", ""]
        elif gateway in _GATEWAYS:
            lines = [f"#پرداخت از <b>{_html.escape(label)}</b>", ""]
        else:
            # افزایشی که از درگاهِ پرداخت نیامده (پاداش، تنظیمِ دستی، شارژِ
            # گروهی) — «#پرداخت از پاداشِ دعوت» بی‌معنا بود.
            lines = [f"#افزایش — <b>{_html.escape(label)}</b>", ""]

        if buyer is not None:
            from tenant.shared.buyer_ident import get_mode, tx_lines     # پیشنهادِ #187
            lines += tx_lines(await get_mode(bot_id), getattr(buyer, "full_name", None),
                              getattr(buyer, "username", None), getattr(buyer, "telegram_id", "—"))

        lines.append(
            f"{'➖ مبلغ کسرشده' if _is_deduct else '💰 مبلغ پرداخت'}: {_fa(_abs_amount)} تومان")
        if old_balance is not None:
            lines.append(f"📉 موجودی قبلی: {_fa(old_balance)} تومان")
        if new_balance is not None:
            lines.append(f"📈 موجودی جدید: {_fa(new_balance)} تومان")
        if card:
            lines.append(f"💳 شماره کارت: <code>{_html.escape(str(card))}</code>")
        if ref:
            lines.append(f"🧾 کد پیگیری: <code>{_html.escape(str(ref))}</code>")
        # === PATCH_TX_LOG_ALL_MOVEMENTS_V1 ===
        if note:
            lines.append(f"📝 توضیح: {_html.escape(str(note))}")

        try:
            chat_id = int(ch)
        except Exception:
            chat_id = ch

        await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
        _mark_ok(bot_id)
    except Exception as e:
        # === PATCH_TX_LOG_FAILURE_VISIBLE_V1 ===
        # قبلاً هر خطا فقط در فایلِ لاگِ سرور می‌نشست. نتیجه: مالکِ ربات
        # می‌دید «تراکنش‌ها دیگه به کانال نمی‌رن» ولی هیچ سرنخی نداشت و
        # فکر می‌کرد کد خراب شده — در حالی که تقریباً همیشه علت بیرونی
        # است (ربات از کانال حذف شده، دسترسیِ «ارسالِ پیام» از او گرفته
        # شده، یا کانال پاک/تبدیل شده). حالا خودِ مالک باخبر می‌شود.
        logger.warning("tx_log: ارسال به کانالِ لاگ ناموفق بود (bot_id=%s): %s",
                       bot_id, e)
        await _warn_owner(bot, bot_id, e)


async def tx_log_purchase(bot, bot_id: int, *, buyer=None, section: str = "", detail: str = "", amount=0,
                          tracking_code=None, source: str = "") -> None:
    """پستِ لاگِ «خریدِ سرویس/محصول» در همان کانالِ لاگِ تراکنش‌ها (فقط اگر مالک «لاگِ خریدها» را روشن کرده باشد).
    هر خطایی بی‌صدا رد می‌شود تا جلوی خودِ خرید را نگیرد."""
    try:
        from tenant import get_setting
        if (await get_setting(bot_id, "tx_log_purchases", "0")) != "1":
            return
        ch = (await get_setting(bot_id, "tx_log_channel", "") or "").strip()
        if not ch:
            return
        lines = ["#خرید — <b>" + _html.escape(section or "سرویس") + "</b>", ""]
        if buyer is not None:
            from tenant.shared.buyer_ident import get_mode, tx_lines
            lines += tx_lines(await get_mode(bot_id), getattr(buyer, "full_name", None),
                              getattr(buyer, "username", None), getattr(buyer, "telegram_id", "—"))
        if detail:
            lines.append(f"📦 مشخصات: {detail}")
        if source:
            lines.append(f"🖥 {_html.escape(source)}")
        lines.append(f"💰 مبلغ: {_fa(amount)} تومان")
        if tracking_code:
            lines.append(f"🧾 کد پیگیری: <code>{_html.escape(str(tracking_code))}</code>")
        try:
            chat_id = int(ch)
        except Exception:
            chat_id = ch
        await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
        _mark_ok(bot_id)
    except Exception as e:
        logger.warning("tx_log_purchase: ارسال ناموفق بود (bot_id=%s): %s", bot_id, e)
        await _warn_owner(bot, bot_id, e)


# === PATCH_TX_LOG_FAILURE_VISIBLE_V1 ===
# برایِ اینکه اگر کانال خراب باشد، مالک با هر تراکنش یک هشدار نگیرد.
_WARN_COOLDOWN_SEC = 3600.0
_last_warn: dict[int, float] = {}


def _mark_ok(bot_id: int) -> None:
    """ارسالِ موفق یعنی مشکل حل شده؛ هشدارِ بعدی دوباره مجاز است."""
    _last_warn.pop(bot_id, None)


def _reason_fa(e: Exception) -> str:
    """پیامِ خطایِ تلگرام را به توضیحِ فارسیِ قابلِ‌اقدام ترجمه می‌کند."""
    t = str(e).lower()
    if "chat not found" in t:
        return ("کانال پیدا نشد — یا ربات از کانال حذف شده، یا کانال پاک شده، "
                "یا آیدیِ ذخیره‌شده دیگر درست نیست.")
    if "not enough rights" in t or "need administrator" in t or "can't post" in t:
        return "ربات دیگر دسترسیِ «ارسالِ پیام» در کانال را ندارد."
    if "forbidden" in t or "kicked" in t or "bot was blocked" in t:
        return "ربات از کانال حذف شده یا دسترسی‌اش گرفته شده است."
    if "upgraded to a supergroup" in t or "migrate" in t:
        return "گروه به سوپرگروه ارتقا یافته و آیدیِ آن عوض شده است."
    return str(e)[:200]


async def _warn_owner(bot, bot_id: int, e: Exception) -> None:
    """یک‌بار در ساعت به مالکِ ربات خبر می‌دهد که کانالِ لاگ کار نمی‌کند."""
    try:
        import time as _time
        now = _time.monotonic()
        last = _last_warn.get(bot_id, 0.0)
        if now - last < _WARN_COOLDOWN_SEC:
            return
        _last_warn[bot_id] = now

        from config import Session
        from models import Bot as BotRow
        from sqlalchemy import select
        async with Session() as s:
            owner_id = await s.scalar(select(BotRow.owner_id).where(BotRow.id == bot_id))
        if not owner_id:
            return
        await bot.send_message(
            owner_id,
            "⚠️ <b>کانالِ لاگِ تراکنش‌ها کار نمی‌کند</b>\n\n"
            f"علت: {_html.escape(_reason_fa(e))}\n\n"
            "تراکنش‌ها عادی ثبت می‌شوند و پولی گم نشده — فقط در کانال پست نمی‌شوند.\n\n"
            "برایِ رفعش: ربات را دوباره در کانال <b>ادمین</b> کن و از بخشِ "
            "«کانالِ لاگِ تراکنش‌ها» دکمهٔ تست را بزن.\n\n"
            "<i>تا وقتی مشکل حل نشده، این هشدار حداکثر ساعتی یک‌بار تکرار می‌شود.</i>",
            parse_mode="HTML")
    except Exception:
        logger.exception("tx_log: اطلاع‌رسانیِ خرابیِ کانال به مالک هم ناموفق بود")
