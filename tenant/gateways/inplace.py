"""inplace.py — PATCH_GATEWAY_INPLACE_V1

نتیجهٔ «🔍 بررسیِ پرداخت» / «❌ لغو» در فاکتورِ درگاه‌هایِ ربات‌هایِ مشتری (گزارشِ فدایی): به‌جایِ پیامِ جدید،
اعلانِ بالایِ صفحه (toast) یا ویرایشِ همان پیامِ فاکتور.
"""
from __future__ import annotations

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

CANCELLED_TEXT = ("❌ <b>فاکتور لغو شد.</b>\n\nاگر واریز کردی ولی اینجا لغو شد، نگران نباش؛ "
                  "به پشتیبانیِ ربات بگو با همین کدِ پیگیری بررسی کند.")


async def toast(cb, text: str) -> None:
    """اعلانِ کوتاهِ بالایِ صفحه (نه پیامِ چت)."""
    try:
        await cb.answer(text, show_alert=False)
    except Exception:  # noqa: BLE001 — کلیکِ قدیمی/جوابِ تکراری
        logger.debug("inplace: پاسخ به callback ممکن نشد")


async def show(cb, text: str, kb=None) -> None:
    """همان پیامی که دکمه رویش زده شد را ویرایش می‌کند؛ اگر نشد (مثلاً پاک شده) پیامِ جدید می‌فرستد."""
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


async def show_paid(cb, bot_id: int, credit: int, tg_id: int, session_factory) -> None:
    """فاکتور ⇒ «کیف پول شارژ شد» (همان متنِ رسیدِ شارژ)؛ بالانسِ فعلی از دیتابیس."""
    from sqlalchemy import select

    from models import BotUser
    async with session_factory() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
    bal = float(u.balance) if u else credit
    try:
        from tenant.gateways.wallet_receipt import build_wallet_charge_message_for_bot
        t, k = await build_wallet_charge_message_for_bot(bot_id, credit, bal, "card")
    except Exception:  # noqa: BLE001
        t, k = f"✅ {credit:,} تومان به کیفِ پولت اضافه شد.", None
    await show(cb, t, k)
