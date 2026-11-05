"""loop.py — یادآوریِ خودکارِ نوبت‌ها (PATCH_BOOKING_V2): ۲۴ ساعت و ۲ ساعت قبل (قابلِ تنظیم) + پاک‌سازیِ نوبت‌هایِ منتظرِ گذشته."""
from __future__ import annotations

import asyncio
import datetime as _dt

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update

from config import Session
from models import BkAppt, Bot as BotRow, PlatformSetting

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
            BkAppt.status == "confirmed", BkAppt.tg_id > 0, BkAppt.date_str >= today.isoformat(),
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
                    if not await _claim(a.id, k):          # پروسهٔ دیگری همین یادآوری را گرفته (یا قبلاً فرستاده شده)
                        continue
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
                        await _unclaim(a.id, k)             # ارسال نشد ⇒ دورِ بعد دوباره تلاش شود
            finally:
                if not bot_factory:
                    await bot.session.close()
        except Exception:      # noqa: BLE001
            logger.exception("booking loop: خطا (bot=%s)", bot_id)
    await _expire_stale(now, bot_factory)
    return sent


async def _claim(appt_id: int, kind: int) -> bool:
    """«ثبتِ اتمی» پرچمِ یادآوری قبل از ارسال: حتی اگر دو پروسه هم‌زمان بالا باشند (ری‌استارت/اجرایِ دوباره) فقط یکی می‌فرستد.
    یادآوریِ دوم یعنی اولی هم دیگر لازم نیست."""
    async with Session() as s:
        if kind == 2:
            res = await s.execute(update(BkAppt).where(BkAppt.id == appt_id, BkAppt.remind2_sent.is_(False))
                                  .values(remind2_sent=True, remind1_sent=True))
        else:
            res = await s.execute(update(BkAppt).where(BkAppt.id == appt_id, BkAppt.remind1_sent.is_(False), BkAppt.remind2_sent.is_(False))
                                  .values(remind1_sent=True))
        await s.commit()
        return (res.rowcount or 0) == 1


async def _unclaim(appt_id: int, kind: int) -> None:
    try:
        async with Session() as s:
            await s.execute(update(BkAppt).where(BkAppt.id == appt_id).values(**({"remind2_sent": False} if kind == 2 else {"remind1_sent": False})))
            await s.commit()
    except Exception:      # noqa: BLE001
        logger.exception("booking: برگرداندنِ پرچمِ یادآوری ناموفق")


BEAT_KEY = "bk_worker_beat"


async def beat() -> None:
    """ضربانِ ورکر: داشبوردِ مالک اگر ضربان قدیمی باشد هشدار می‌دهد (ورکر خاموش/نصب‌نشده)."""
    try:
        async with Session() as s:
            r = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == BEAT_KEY))
            val = _dt.datetime.utcnow().isoformat()
            if r:
                r.value = val
            else:
                s.add(PlatformSetting(key=BEAT_KEY, value=val))
            await s.commit()
    except Exception:      # noqa: BLE001
        logger.exception("booking: ثبتِ ضربانِ ورکر ناموفق")


async def worker_alive(max_age_min: int = 15) -> bool:
    try:
        async with Session() as s:
            r = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == BEAT_KEY))
        return bool(r and r.value and (_dt.datetime.utcnow() - _dt.datetime.fromisoformat(r.value)) < _dt.timedelta(minutes=max_age_min))
    except Exception:      # noqa: BLE001
        return False


async def _expire_stale(now: _dt.datetime, bot_factory=None) -> None:
    """۱) نوبتِ «در انتظارِ تأیید» که زمانش گذشته ⇒ لغو و بیعانه (اگر هست) کامل برگردد. ۲) «منتظرِ بیعانه»هایِ منقضی ⇒ لغو (پولی نرفته)."""
    try:
        stale: list[tuple[int, int, int]] = []          # (bot_id, appt_id, tg_id)
        async with Session() as s:
            aps = (await s.scalars(select(BkAppt).where(BkAppt.status == "pending", BkAppt.date_str <= now.date().isoformat()))).all()
            for a in aps:
                if E.appt_start_dt(a) <= now:
                    a.status, a.cancel_reason = "cancelled", "زمانِ نوبت بدونِ تأیید گذشت"
                    stale.append((a.bot_id, a.id, a.tg_id))
            if stale:
                await s.commit()
        cfgs: dict[int, dict] = {}
        for bot_id, aid, tg in stale:
            cfg = cfgs.get(bot_id) or await E.get_cfg(bot_id)
            cfgs[bot_id] = cfg
            refunded = await E.refund_deposit(bot_id, aid, cfg, force_full=True)
            if refunded and E.has_telegram(tg):
                try:
                    async with Session() as s:
                        row = await s.get(BotRow, bot_id)
                    if row and row.token:
                        bot = bot_factory(row) if bot_factory else Bot(token=row.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                        try:
                            await bot.send_message(tg, f"ℹ️ نوبتِ #{aid} تأیید نشد و زمانش گذشت؛ بیعانهٔ <b>{refunded:,}</b> تومان به کیف پولت برگشت.", parse_mode="HTML")
                        finally:
                            if not bot_factory:
                                await bot.session.close()
                except Exception:      # noqa: BLE001
                    logger.info("booking: خبرِ بازگشتِ بیعانه به مشتری نرسید (tg=%s)", tg)
        # منتظرِ بیعانه‌هایِ منقضی (هر ربات با مهلتِ خودش)
        async with Session() as s:
            hold_bots = [r for r in (await s.scalars(select(BkAppt.bot_id).where(BkAppt.status == "hold").distinct())).all()]
        for bot_id in hold_bots:
            cfg = cfgs.get(bot_id) or await E.get_cfg(bot_id)
            await E.expire_holds(bot_id, cfg)
    except Exception:      # noqa: BLE001
        logger.exception("booking: پاک‌سازیِ نوبت‌هایِ منقضی ناموفق")


async def booking_loop() -> None:
    await asyncio.sleep(45)
    while True:
        try:
            await beat()
            await run_once()
        except Exception:      # noqa: BLE001
            logger.exception("booking_loop failed")
        await asyncio.sleep(INTERVAL)
