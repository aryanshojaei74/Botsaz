"""
wallet_receipt.py — پیامِ یکدست و حرفه‌ایِ «کیف پولت شارژ شد» برای همه‌ی
روش‌های پرداخت (کارت‌به‌کارت، آقای پرداخت، ارز دیجیتال، سواپ‌ولت، استارزِ
تلگرام، تأیید دستیِ ادمین).

قبلاً هر روشِ پرداخت پیامِ خیلی سادهٔ خودش رو داشت (فقط یه خطِ «✅ کیف پول
شما X شارژ شد.»)؛ همین باعث می‌شد پیام بی‌کیفیت به‌نظر برسه. این ماژول یک
تابعِ مشترک می‌سازه که شاملِ مبلغِ واریزی، موجودیِ فعلی، روشِ پرداخت،
تاریخ/ساعتِ شمسیِ تهران، و یک دکمه‌ی «رفتن به کیف پول» است.
"""
from __future__ import annotations

import datetime as _dt

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

try:
    import jdatetime as _jdt
except Exception:
    _jdt = None


def _rial(n) -> str:
    return f"{int(n):,} تومان"


def tehran_date_time(dt: "_dt.datetime | None" = None) -> tuple[str, str]:
    """تاریخ/ساعتِ شمسیِ تهران را (تاریخ, ساعت) برمی‌گرداند. dt باید UTC باشد
    (همان فرضی که کل پروژه برای datetime.utcnow()/ستون‌های DateTime دارد)؛
    اگر ندهی، همین لحظه استفاده می‌شود."""
    dt = dt or _dt.datetime.utcnow()
    local = dt + _dt.timedelta(hours=3, minutes=30)
    time_str = local.strftime("%H:%M:%S")
    if _jdt:
        date_str = _jdt.datetime.fromgregorian(datetime=local).strftime("%Y/%m/%d")
    else:
        date_str = local.strftime("%Y/%m/%d")
    return date_str, time_str


METHOD_LABELS = {
    "card": "💳 کارت‌به‌کارت",
    "crypto": "🪙 ارز دیجیتال",
    "swap": "🔄 سواپ‌ولت",
    "stars": "⭐️ استارز تلگرام",
    "admin": "👮 تأیید دستیِ ادمین",
    # === PATCH_ZARINPAL_RECEIPT_LABEL_V1 ===
    # قبلاً "zarinpal" کلیدی توی METHOD_LABELS نداشت، پس همون رشتهٔ خامِ
    # انگلیسی مستقیم توی پیامِ کاربر («روش پرداخت: zarinpal») نشون داده
    # می‌شد.
    "zarinpal": "🔐 زرین‌پال",
    "hesabpay": "🇦🇫 حساب‌پی",
}


def build_wallet_deduct_message(amount, new_balance, reason: str | None = None,
                                when: "_dt.datetime | None" = None) -> tuple[str, InlineKeyboardMarkup]:
    """پیامِ حرفه‌ایِ «کسر از کیف پول» (برای تنظیمِ دستیِ موجودی توسط ادمین).

    amount: مبلغِ کسرشده (تومان، عددِ مثبت)
    new_balance: موجودیِ کیف‌پول *بعد از* این کسر (تومان)
    reason: دلیلِ کسر (اختیاری؛ اگر ادمین دلیلی نداده باشد، این خط اصلاً نمایش داده نمی‌شود)
    """
    date_str, time_str = tehran_date_time(when)
    reason_line = f"📝 دلیل: {reason}\n" if reason else ""
    text = (
        "⚠️ <b>مبلغی از کیف پولت کسر شد</b>\n"
        "┈┅━━⊷⊰💳⊱⊶━━┅┈\n"
        f"➖ مبلغ کسرشده: <b>{_rial(amount)}</b>\n"
        f"👛 موجودی فعلی: <b>{_rial(new_balance)}</b>\n"
        f"{reason_line}"
        f"📅 تاریخ: {date_str}   🕒 ساعت: {time_str}\n"
        "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        "اگه این کسر برات سؤاله یا فکر می‌کنی اشتباه شده، با پشتیبانی در تماس باش."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👛 مشاهده کیف پول", callback_data="w:back"),
    ]])
    return text, kb


def build_wallet_charge_message(amount, new_balance, method: str,
                                when: "_dt.datetime | None" = None,
                                virtual: bool = False,
                                ref_id: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """پیامِ حرفه‌ایِ شارژِ کیف‌پول + دکمه‌ی «رفتن به کیف پول» را می‌سازد.

    amount: مبلغِ شارژشده در همین تراکنش (تومان)
    new_balance: موجودیِ کیف‌پول *بعد از* این شارژ (تومان)
    method: یکی از کلیدهای METHOD_LABELS، یا هر متنِ دلخواهِ دیگر (مستقیم نمایش داده می‌شود)
    when: زمانِ واقعیِ شارژ (UTC)؛ اگر ندهی از همین لحظه استفاده می‌شود
    virtual: فقط برای ربات‌های «خدمات مجازی» (field == "virtual_service") True است.
    ref_id: کدِ پیگیریِ عددیِ درگاه (مثلاً ref_id زرین‌پال)، اگر باشد به‌عنوانِ
        خطِ تأییدیه/رهگیریِ تراکنش نمایش داده می‌شود. اختیاری — اگر ندهی این
        خط اصلاً نمی‌آید.

    === PATCH_VIRTUAL_CARD_COPY_SCOPE_V1 ===
    متنِ ویژهٔ کارت‌به‌کارت که از خرید «استارز/پرمیوم/بوست/TON» حرف می‌زند
    مخصوصِ ربات‌های خدمات مجازی است. قبلاً این متن برای هر ربات (از جمله
    ربات‌های VPN) نمایش داده می‌شد. حالا فقط وقتی virtual=True باشد استفاده
    می‌شود؛ در غیر این صورت همان پیامِ خنثیِ قدیمی (که ربات‌های VPN داشتند)
    برمی‌گردد. تصمیمِ virtual در build_wallet_charge_message_for_bot بر اساسِ
    نوعِ ربات گرفته می‌شود.
    """
    date_str, time_str = tehran_date_time(when)
    method_label = METHOD_LABELS.get(method, method)
    # === PATCH_ZARINPAL_RECEIPT_LABEL_V1 ===
    ref_line = f"🔢 کد پیگیریِ تراکنش: <code>{ref_id}</code>\n" if ref_id else ""
    if method == "card" and virtual:
        text = (
            "✅ <b>واریز شما با موفقیت تأیید شد!</b>\n\n"
            f"💰 مبلغ: <b>{_rial(amount)}</b> به موجودی شما اضافه شد.\n"
            f"📅 تاریخ و ساعت: {date_str} – {time_str}\n\n"
            "🙏 از اعتماد شما سپاسگزاریم!\n\n"
            "🚀 همین حالا از موجودی خود برای خرید استارز، پرمیوم، بوست تلگرام، گیفت استارزی یا ارز TON استفاده کنید.\n\n"
            "🎉 برای شروع خرید، روی دکمهٔ «🛍 خرید محصول» کلیک کنید 👇"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🛍 خرید محصول", callback_data="w:buy_products"),
        ]])
    else:
        text = (
            "✅ <b>افزایش موجودیِ کیف پول با موفقیت انجام شد</b>\n"
            "┈┅━━⊷⊰💳⊱⊶━━┅┈\n"
            f"💰 مبلغ واریزی: <b>{_rial(amount)}</b>\n"
            f"👛 موجودی فعلی: <b>{_rial(new_balance)}</b>\n"
            f"🧾 روش پرداخت: {method_label}\n"
            f"{ref_line}"
            f"📅 تاریخ: {date_str}   🕒 ساعت: {time_str}\n"
            "┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
            "🙏 ممنون از اعتمادت! هر وقت خواستی خرید کنی، از همین‌جا شروع کن 👇"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👛 مشاهده کیف پول", callback_data="w:back"),
        ]])
    return text, kb


async def build_wallet_charge_message_for_bot(bot_id, amount, new_balance, method: str):
    # === PATCH_VIRTUAL_CARD_COPY_SCOPE_V1 ===
    # نوعِ ربات را بخوان تا فقط برای ربات‌های خدمات مجازی متنِ استارز/TON برود.
    virtual = False
    try:
        from config import Session
        from models import Bot as _BotRow
        async with Session() as _s:
            _brow = await _s.get(_BotRow, int(bot_id))
            virtual = bool(_brow is not None and _brow.field == "virtual_service")
    except Exception:
        # اگر به هر دلیل نتوانستیم نوع را تشخیص دهیم، امنِ پیش‌فرض = خنثی
        # (یعنی ربات‌های VPN هرگز اشتباهی متنِ خدمات مجازی نگیرند).
        virtual = False
    text, keyboard = build_wallet_charge_message(amount, new_balance, method, virtual=virtual)
    if method == "card":
        from tenant.gateways.wallet_receipt_buttons import wallet_receipt_keyboard
        selected = await wallet_receipt_keyboard(int(bot_id))
        if selected is not None:
            keyboard = selected
    return text, keyboard
