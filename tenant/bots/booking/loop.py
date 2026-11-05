"""loop.py — یادآوریِ خودکارِ نوبت‌ها (PATCH_BOOKING_V2): ۲۴ ساعت و ۲ ساعت قبل (قابلِ تنظیم) + پاک‌سازیِ نوبت‌هایِ منتظرِ گذشته."""
from __future__ import annotations

import asyncio
import datetime as _dt

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from config import Session
from models import BkAppt, Bot as BotRow

from tenant.bots.booking import engine as E

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

INTERVAL = 300


async def run_once(now: _dt.datetime | None = None, bot_factory=None) -> int:
    """یک دور: یادآوری‌هایِ سررسیده را می‌فرستد و پرچمشان را می‌زند. خروجی: تعدادِ پیامِ فرستاده‌شده."""
    from tenant.bots.booking.bk_user import _appt_card
    now = now or E.iran_now()
    today = now.date()
    sent = 0
    async with Session() as s:
        aps = (await s.scalars(select(BkAppt).where(
            BkAppt.status == "confirmed", BkAppt.date_str >= today.isoformat(),
            BkAppt.date_str <= (today + _dt.timedelta(days=8)).isoformat()))).all()
    by_bot: dict[int, list[BkAppt]] = {}
    for a in aps:
        by_bot.setdefault(a.bot_id, []).append(a)
    for bot_id, items in by_bot.items():
        try:
            async with Session() as s:
                row = await s.get(BotRow, bot_id)
            if not row or row.status != "active" or not row.token:
                continue
            cfg = await E.get_cfg(bot_id)
            due = [(a, E.reminder_due(a, cfg, now)) for a in items]
            due = [(a, k) for a, k in due if k]
            if not due:
                continue
            bot = bot_factory(row) if bot_factory else Bot(token=row.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                for a, k in due:
                    left = E.appt_start_dt(a) - now
                    hrs = max(1, round(left.total_seconds() / 3600))
                    when = "کمتر از یک ساعت" if left < _dt.timedelta(hours=1) else f"حدود {hrs} ساعت"
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغوِ نوبت", callback_data=f"bk:x:{a.id}")]])
                    try:
                        await bot.send_message(a.tg_id, f"⏰ <b>یادآوریِ نوبت</b>\n{when} دیگر نوبتِ شماست:\n\n{_appt_card(a)}",
                                               reply_markup=kb, parse_mode="HTML")
                        sent += 1
                    except Exception:      # noqa: BLE001
                        logger.info("booking: یادآوری به %s نرسید", a.tg_id)
                    async with Session() as s:
                        a2 = await s.get(BkAppt, a.id)
                        if a2:
                            # یادآوریِ دوم یعنی اولی هم دیگر لازم نیست
                            a2.remind2_sent = a2.remind2_sent or k == 2
                            a2.remind1_sent = True
                            await s.commit()
            finally:
                if not bot_factory:
                    await bot.session.close()
        except Exception:      # noqa: BLE001
            logger.exception("booking loop: خطا (bot=%s)", bot_id)
    await _expire_stale(now)
    return sent


async def _expire_stale(now: _dt.datetime) -> None:
    """نوبتِ «در انتظارِ تأیید» که زمانش گذشته بی‌پایان نماند ⇒ لغو (علت: زمان گذشت)."""
    try:
        async with Session() as s:
            aps = (await s.scalars(select(BkAppt).where(BkAppt.status == "pending", BkAppt.date_str <= now.date().isoformat()))).all()
            n = 0
            for a in aps:
                if E.appt_start_dt(a) <= now:
                    a.status, a.cancel_reason = "cancelled", "زمانِ نوبت بدونِ تأیید گذشت"
                    n += 1
            if n:
                await s.commit()
    except Exception:      # noqa: BLE001
        logger.exception("booking: پاک‌سازیِ نوبت‌هایِ منتظرِ گذشته ناموفق")


async def booking_loop() -> None:
    await asyncio.sleep(45)
    while True:
        try:
            await run_once()
        except Exception:      # noqa: BLE001
            logger.exception("booking_loop failed")
        await asyncio.sleep(INTERVAL)
