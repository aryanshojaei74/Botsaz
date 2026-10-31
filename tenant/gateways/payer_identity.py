"""
tenant/gateways/payer_identity.py — تشخیصِ *پرداخت‌کنندهٔ واقعی* در جریان‌هایِ شارژِ کیف‌پول.

=== PATCH_PAYER_IDENTITY_V1 ===
گزارشِ ادمین (فدایی): «پرداخت می‌کنن، پرداخت تأیید میشه، ربات میگه شارژ شد ولی حسابشون شارژ نشده».

علتِ ریشه‌ای: در دکمه‌هایِ *مبلغِ ثابت* (۵۰٬۰۰۰ / ۱۰۰٬۰۰۰ …) هندلر `cb.message` را به تابعِ ساختِ
فاکتور می‌داد و آن تابع کاربر را از `message.from_user` می‌خواند. اما `cb.message` پیامی است که *خودِ
ربات* فرستاده، پس `from_user`ِ آن خودِ ربات است، نه کسی که دکمه را زده. نتیجه: ربات برایِ خودش یک
BotUser می‌ساخت (نام = نامِ ربات، آیدی = آیدیِ ربات) و فاکتور/شارژ به حسابِ همان می‌رفت؛ به مشتری
هم «شارژ شد» گفته می‌شد چون پیامِ تأیید در همان چت می‌رفت. (مسیرِ «مبلغ دلخواه» که مشتری تایپ می‌کند
درست بود، چون آن `msg` پیامِ خودِ مشتری است.)

این ماژول یک تابعِ واحد می‌دهد که هر جریانِ پرداخت باید برایِ تعیینِ پرداخت‌کننده صدا بزند و در برابرِ
این اشتباه ایمن است: اگر کاربرِ معرفی‌شده یک ربات باشد، خطا می‌دهد (به‌جایِ اینکه بی‌صدا حسابِ
اشتباه شارژ شود).
"""
from __future__ import annotations

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)


class PayerIdentityError(Exception):
    """پرداخت‌کننده مشخص نیست یا خودِ یک ربات است؛ نباید پولی به نامش ثبت شود."""


def resolve_payer(message, from_user=None):
    """کاربرِ واقعیِ پرداخت‌کننده را برمی‌گرداند.

    from_user: اگر جریان از یک callback آمده، باید `cb.from_user` (کسی که دکمه را زد) را بدهد؛
               در غیرِ این صورت به `message.from_user` برمی‌گردیم که فقط برایِ پیامِ خودِ کاربر درست است.
    خطا: PayerIdentityError اگر کاربر پیدا نشد یا `is_bot` بود.
    """
    payer = from_user if from_user is not None else getattr(message, "from_user", None)
    if payer is None or getattr(payer, "is_bot", False):
        logger.error(
            "[payer_identity] پرداخت‌کنندهٔ نامعتبر (id=%s, is_bot=%s) — احتمالاً cb.message به‌جایِ cb.from_user؛ "
            "شارژ متوقف شد.", getattr(payer, "id", None), getattr(payer, "is_bot", None), stack_info=True)
        raise PayerIdentityError("پرداخت‌کنندهٔ نامعتبر")
    return payer


PAYER_ERROR_TEXT = ("❌ حسابِ شما شناسایی نشد و برایِ امنیتِ پولت پرداخت ساخته نشد.\n"
                    "لطفاً دوباره از منویِ «شارژ کیف پول» اقدام کن؛ اگر تکرار شد به پشتیبانی خبر بده.")
