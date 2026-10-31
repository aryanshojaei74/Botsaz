"""
invoice_msg.py — PATCH_INVOICE_MSG_EDIT_V1

درخواستِ ادمین (فدایی): «وقتی فاکتور تأیید شد، همان پیامِ فاکتور ادیت بشه و بشه
"فاکتور تأیید شد و حسابت شارژ شد" و دکمه‌ها برداشته بشه تا کسی دوباره روی تأیید نزنه
و «این فاکتور قبلاً تأیید شده» بگیره.»

شناسهٔ پیامِ فاکتور (chat_id:message_id) در جدولِ settings با کلیدِ کوتاهِ
`inv_<gw>_<order_id>` نگه داشته می‌شود (بدونِ تغییرِ اسکیمای دیتابیس و بدونِ دست‌زدن به
receipt_text که چند جا پارس می‌شود). هر مسیرِ تسویه (دکمهٔ دستی، حلقهٔ پس‌زمینه، کالبکِ
درگاه) بعد از شارژ `finalize` را صدا می‌زند؛ همه‌چیز best-effort و idempotent است و هرگز
نباید جلوی شارژ/اطلاع‌رسانی را بگیرد.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import delete, select

from config import Session
from models import Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)


def _key(gw: str, order_id: int) -> str:
    return f"inv_{gw}_{int(order_id)}"[:60]


def confirmed_text(amount, ref_id=None, *, already: bool = False) -> str:
    head = ("✅ <b>این فاکتور تأیید شد و حسابت شارژ شده است.</b>" if already
            else "✅ <b>فاکتور تأیید شد و حسابت شارژ شد.</b>")
    lines = [head]
    try:
        if amount:
            lines.append(f"\n💰 مبلغ: <b>{int(amount):,} تومان</b>")
    except (TypeError, ValueError):
        pass
    ref = (str(ref_id).split("|", 1)[0] if ref_id else "").strip()
    if ref:
        lines.append(f"🔢 کد پیگیری: <code>{ref}</code>")
    return "\n".join(lines)


async def remember(bot_id: int, gw: str, order_id: int, chat_id: int, message_id: int) -> None:
    try:
        async with Session() as s:
            k = _key(gw, order_id)
            row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == k))
            val = f"{chat_id}:{message_id}"
            if row:
                row.value = val
            else:
                s.add(Setting(bot_id=bot_id, key=k, value=val))
            await s.commit()
    except Exception:
        logger.exception("invoice_msg: ذخیرهٔ شناسهٔ پیامِ فاکتور ناموفق بود")


async def _pop(bot_id: int, gw: str, order_id: int):
    try:
        async with Session() as s:
            k = _key(gw, order_id)
            row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == k))
            if not row:
                return None
            val = row.value or ""
            await s.execute(delete(Setting).where(Setting.id == row.id))
            await s.commit()
        chat, _, mid = val.partition(":")
        return int(chat), int(mid)
    except Exception:
        logger.exception("invoice_msg: خواندنِ شناسهٔ پیامِ فاکتور ناموفق بود")
        return None


async def edit_message_confirmed(msg, amount, ref_id=None, *, already: bool = False) -> bool:
    """پیامِ داده‌شده (aiogram Message) را به «تأیید شد» تبدیل و دکمه‌ها را حذف می‌کند."""
    try:
        await msg.edit_text(confirmed_text(amount, ref_id, already=already),
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
                            parse_mode="HTML")
        return True
    except Exception as e:
        if "not modified" in str(e).lower():
            return True
        logger.warning("invoice_msg: ادیتِ پیامِ فاکتور ناموفق بود: %s", e)
        return False


async def finalize(tbot, bot_id: int, gw: str, order_id, amount, ref_id=None) -> None:
    """از رویِ شناسهٔ ذخیره‌شده، پیامِ فاکتورِ کاربر را ادیت می‌کند (اگر هنوز نشده)."""
    if not order_id:
        return
    ref = await _pop(bot_id, gw, order_id)
    if not ref:
        return
    chat_id, message_id = ref
    try:
        await tbot.edit_message_text(
            confirmed_text(amount, ref_id), chat_id=chat_id, message_id=message_id,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]), parse_mode="HTML")
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.warning("invoice_msg: ادیتِ پیامِ فاکتور ناموفق بود (order=%s): %s", order_id, e)
