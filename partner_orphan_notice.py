"""
partner_orphan_notice.py — PATCH_PARTNER_ORPHAN_RENEW_NOTICE_V1

خریدارانِ رباتِ همکار: وقتی رباتِ همکارِ فروشنده غیرفعال/بایگانی شده باشد و سررسیدِ ربات
خریدارِ آن برسد، باید بدانند که تمدید از این به بعد در «بات‌سازِ اصلی» انجام می‌شود.
پیام از طریقِ خودِ رباتِ خریدار (که مالکش آن را استارت کرده) فرستاده می‌شود، چون رباتِ
اصلی به کسی که هرگز استارتش نکرده نمی‌تواند پیام بدهد.

کسانی که قبلاً خودشان در بات‌سازِ اصلی تمدید/خرید کرده‌اند (PlanOrder برایِ همان ربات)
این پیام را نمی‌گیرند؛ آن‌ها راهش را بلدند.
"""
from __future__ import annotations

from sqlalchemy import select

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)


async def partner_is_gone(s, sold_bot_id: int) -> bool:
    """آیا این ربات را همکاری فروخته که اکنون غیرفعال/متوقف/منقضی/بایگانی است؟
    (فقط فروش‌هایِ واقعی؛ رباتِ تستِ همکار مشمول نیست.)"""
    from models import Bot as BotRow, PartnerBot, PartnerSale
    sale = await s.scalar(select(PartnerSale).where(
        PartnerSale.sold_bot_id == sold_bot_id, PartnerSale.kind == "sold"))
    if not sale:
        return False
    pb = await s.get(PartnerBot, sale.partner_bot_id)
    if pb is None:
        return True
    if (pb.status or "active") != "active":
        return True
    prow = await s.get(BotRow, pb.bot_id)
    if prow is None or prow.archived_at is not None or (prow.status or "active") != "active":
        return True
    return False


async def renewed_at_main_before(s, bot_id: int) -> bool:
    """مالک قبلاً همین ربات را در بات‌سازِ اصلی تمدید/ارتقا داده؟"""
    from models import PlanOrder
    n = await s.scalar(select(PlanOrder.id).where(
        PlanOrder.target_bot_id == bot_id, PlanOrder.amount > 0).limit(1))
    return bool(n)


async def notice_due(s, b) -> bool:
    """باید برایِ مالکِ این ربات پیامِ «تمدید در بات‌سازِ اصلی» برود؟"""
    try:
        return (await partner_is_gone(s, b.id)) and not (await renewed_at_main_before(s, b.id))
    except Exception:
        logger.exception("partner_orphan_notice: بررسی ناموفق بود (bot=%s)", getattr(b, "id", None))
        return False


def build_notice(*, username: str | None, days_left: int | None, archived: bool = False) -> str:
    who = f"@{username}" if username else "ربات شما"
    if archived:
        when = f"ربات <b>{who}</b> به‌دلیلِ سررسید به «بایگانی» رفت."
    elif days_left is None:
        when = f"سررسیدِ ربات <b>{who}</b> نزدیک است."
    elif days_left > 0:
        when = f"ربات <b>{who}</b> تا <b>{days_left}</b> روزِ دیگر به سررسید می‌رسد."
    else:
        when = f"ربات <b>{who}</b> امروز به سررسید می‌رسد."
    return ("📢 <b>اطلاعیهٔ مهمِ تمدید</b>\n\n"
            f"{when}\n\n"
            "ربات همکاری که این ربات را از او خریده‌اید دیگر فعال نیست و فعالیتش قطع شده است.\n"
            "✅ از این به بعد <b>تمدید و ارتقای ربات شما در «بات‌سازِ اصلی»</b> انجام می‌شود؛ "
            "ربات و اطلاعاتتان بدون تغییر می‌ماند.\n\n"
            "برای تمدید، دکمهٔ زیر را بزنید و از «🗂 ربات‌های من» همین ربات را تمدید کنید 👇")


async def send_notice(b, platform_bot, *, days_left: int | None, archived: bool = False) -> bool:
    """پیام را با توکنِ خودِ رباتِ خریدار به مالکش می‌فرستد؛ دکمهٔ شیشه‌ای به بات‌سازِ اصلی."""
    from aiogram.enums import ButtonStyle
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from bot_utils import _temp_bot
    try:
        me = await platform_bot.get_me()
        url = f"https://t.me/{me.username}"
    except Exception:
        logger.exception("partner_orphan_notice: نامِ کاربریِ بات‌ساز خوانده نشد")
        return False
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 تمدید در بات‌سازِ اصلی", url=url, style=ButtonStyle.SUCCESS)]])
    text = build_notice(username=b.username, days_left=days_left, archived=archived)
    try:
        async with _temp_bot(b.token) as tb:
            await tb.send_message(b.owner_id, text, reply_markup=kb)
        return True
    except Exception as e:
        logger.info("partner_orphan_notice: ارسال به مالکِ ربات %s ناموفق بود: %s", b.id, e)
        return False
