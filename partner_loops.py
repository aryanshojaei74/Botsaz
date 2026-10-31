"""
partner_loops.py — حلقه‌هایِ پس‌زمینهٔ «ربات همکار».

=== PATCH_PARTNER_LOOPS_V1 ===
تا قبل از این فایل، قواعدِ عددیِ همکاری فقط رویِ کاغذ بودند: تایمرِ
LOW_STOCK_GRACE_DAYS روزه ست می‌شد ولی هیچ‌کس هرگز سررسیدش را چک نمی‌کرد.

نکتهٔ مهم: انقضایِ خودِ ربات‌ها (رباتِ همکار، ربات‌های فروخته‌شدهٔ
۳۰ روزه، و ربات‌های تستِ ۲ روزه) **از قبل** توسطِ subscription_loop در
main_platform.py مدیریت می‌شود، چون همه‌شان BotRow با plan_expires_at
هستند. این فایل آن کار را دوباره انجام نمی‌دهد؛ فقط چیزهایی را پوشش
می‌دهد که مخصوصِ همکاری‌اند:

  ۱. قانونِ حدِ نصاب: زیرِ MIN_ACTIVE_SALES رباتِ فعال → مهلتِ
     LOW_STOCK_GRACE_DAYS روزه → غیرفعال‌سازیِ خودکارِ ربات همکار
  ۲. اخطارِ روزانه به همکار در طولِ مهلت (روزی یک‌بار، نه بیشتر)
  ۳. هم‌گام‌سازیِ PartnerBot.status با انقضایِ PartnerBot.expires_at
"""
from __future__ import annotations

import asyncio
import datetime

from sqlalchemy import select, func

from config import Session, PLATFORM_ADMIN_ID
from models import Bot as BotRow, PartnerBot
from partner_rules import (LOW_STOCK_GRACE_DAYS, MIN_ACTIVE_SALES,
                           min_active_sales, partner_term_enabled)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

# از چند روز مانده به پایانِ مهلت، اخطارِ روزانه شروع شود
WARN_WITHIN_DAYS = 7


async def _active_sales(session, pb_id: int) -> int:
    from models import PartnerSale
    return int(await session.scalar(
        select(func.count(PartnerSale.id))
        .join(BotRow, BotRow.id == PartnerSale.sold_bot_id)
        .where(PartnerSale.partner_bot_id == pb_id,
               PartnerSale.kind == "sold",
               BotRow.status == "active",
               BotRow.archived_at.is_(None))) or 0)


async def _deactivate(session, pb: PartnerBot, reason: str) -> None:
    """رباتِ همکار را می‌خواباند. ربات‌هایِ فروخته‌شده دست‌نخورده می‌مانند —
    آن‌ها مالِ خریدارانِ بی‌گناه‌اند و خواباندنشان تنبیهِ اشتباهی است.
    برایِ سوءاستفادهٔ عمدی، دکمهٔ توقفِ دسته‌جمعی در پنلِ نظارتی هست."""
    pb.status = "expired"
    br = await session.get(BotRow, pb.bot_id)
    if br:
        br.status = "paused"
        br.lock_reason = reason


async def partner_rules_loop(interval_sec: int = 3600, platform_bot=None):
    """هر ساعت یک‌بار قواعدِ همکاری را اعمال می‌کند.

    platform_bot: نمونهٔ Bot ربات مادر برای اطلاع‌رسانی (اختیاری).
    """
    await asyncio.sleep(45)  # بگذار سرویس کامل بالا بیاید
    while True:
        try:
            await _run_once(platform_bot)
        except Exception:
            logger.exception("partner_rules_loop خطا داد (حلقه ادامه می‌دهد)")
        await asyncio.sleep(interval_sec)


async def _run_once(platform_bot=None) -> None:
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    notices: list[tuple[int, str]] = []   # (chat_id, text)
    # === PATCH_PARTNER_NO_TERM_EXPIRY_V1 ===
    # یک‌بار در هر پاس خوانده می‌شود، نه به‌ازای هر همکار.
    _term_on = await partner_term_enabled()

    async with Session() as s:
        partners = (await s.scalars(
            select(PartnerBot).where(PartnerBot.status == "active"))).all()

        for pb in partners:
            # ── ۱. انقضایِ خودِ اشتراکِ همکاری ──
            # === PATCH_PARTNER_NO_TERM_EXPIRY_V1 ===
            # وقتی مدلِ اشتراکِ دوره‌ای خاموش است (پیش‌فرض)، تاریخِ
            # expires_atِ ست‌شده در لحظهٔ فعال‌سازی نادیده گرفته می‌شود؛
            # بقایِ همکار فقط با قاعدهٔ حدِ نصابِ پایین تعیین می‌شود.
            if _term_on and pb.expires_at and now > pb.expires_at:
                await _deactivate(
                    s, pb, "اشتراک همکاری شما به پایان رسیده است.")
                notices.append((
                    pb.owner_id,
                    "⏳ <b>اشتراک همکاری شما به پایان رسید</b>\n\n"
                    "ربات همکار غیرفعال شد. ربات‌هایی که فروخته‌ای "
                    "دست‌نخورده باقی می‌مانند.\n"
                    "برای تمدید، از بخش پشتیبانی بات‌ساز پیام بده."))
                continue

            # ── ۲. قانونِ حدِ نصاب ──
            n = await _active_sales(s, pb.id)
            # === PATCH_PARTNER_MIN_ACTIVE_SETTING_V1 ===
            _min_active = await min_active_sales()

            if n >= _min_active:
                if pb.low_stock_since is not None:
                    pb.low_stock_since = None
                    pb.low_stock_notice = None
                    notices.append((
                        pb.owner_id,
                        f"✅ تعداد ربات‌های فعال شما به حد نصاب "
                        f"({_min_active}) رسید. مهلت لغو شد."))
                continue

            # زیرِ حدِ نصاب
            if pb.low_stock_since is None:
                pb.low_stock_since = now
                pb.low_stock_notice = today
                notices.append((
                    pb.owner_id,
                    f"⚠️ <b>هشدار حد نصاب</b>\n\n"
                    f"ربات‌های فعال شما ({n}) کمتر از "
                    f"<b>{_min_active}</b> تاست.\n"
                    f"از امروز <b>{LOW_STOCK_GRACE_DAYS} روز</b> فرصت داری؛ "
                    "در غیر این صورت ربات همکار خودکار غیرفعال می‌شود."))
                continue

            deadline = pb.low_stock_since + datetime.timedelta(
                days=LOW_STOCK_GRACE_DAYS)
            # رو به بالا گرد می‌شود: «۴ روز و ۲۳ ساعت» یعنی ۵ روزِ مانده،
            # نه ۴. با گردِ رو به پایین، اخطارها گاهی یک روز را جا
            # می‌انداختند و کاربر بی‌خبر غیرفعال می‌شد.
            remaining = deadline - now
            days_left = -(-remaining.total_seconds() // 86400)
            days_left = max(0, int(days_left))

            if now > deadline:
                await _deactivate(
                    s, pb,
                    f"تعداد ربات‌های فعال کمتر از {_min_active} بود.")
                notices.append((
                    pb.owner_id,
                    "🚫 <b>ربات همکار شما غیرفعال شد</b>\n\n"
                    f"مهلت {LOW_STOCK_GRACE_DAYS} روزه تمام شد و تعداد "
                    f"ربات‌های فعالت ({n}) به {_min_active} نرسید.\n\n"
                    "ربات‌هایی که فروخته‌ای دست‌نخورده باقی می‌مانند. "
                    "برای بررسی مجدد از پشتیبانی بات‌ساز پیام بده."))
                if PLATFORM_ADMIN_ID:
                    notices.append((
                        PLATFORM_ADMIN_ID,
                        f"🚫 ربات همکار <code>{pb.owner_id}</code> بابت "
                        f"نرسیدن به حد نصاب ({n}/{_min_active}) "
                        "خودکار غیرفعال شد."))
                continue

            # اخطارِ روزانه در هفتهٔ آخر — حداکثر یک‌بار در روز.
            # شرطِ تاریخ (نه شرطِ عددِ روز) یعنی هیچ روزی جا نمی‌افتد.
            if days_left <= WARN_WITHIN_DAYS and pb.low_stock_notice != today:
                pb.low_stock_notice = today
                notices.append((
                    pb.owner_id,
                    f"⏳ <b>{days_left} روز</b> تا پایان مهلت باقی مانده.\n\n"
                    f"ربات‌های فعال شما: <b>{n}</b> از {_min_active}\n"
                    "برای جلوگیری از غیرفعال‌شدن، ربات‌های بیشتری بفروش."))

        await s.commit()

    # ارسالِ پیام‌ها *بعد* از بستنِ تراکنش — تا اگر تلگرام کند بود یا خطا
    # داد، قفلِ دیتابیس بی‌جهت باز نماند.
    if platform_bot is None:
        return
    for chat_id, text in notices:
        try:
            await platform_bot.send_message(chat_id, text)
        except Exception:
            logger.exception("ارسالِ اطلاعیهٔ همکاری به %s ناموفق بود", chat_id)
        await asyncio.sleep(0.05)   # رعایتِ محدودیتِ نرخِ تلگرام
