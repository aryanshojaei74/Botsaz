"""platform_hosting_autodel.py — حذفِ خودکارِ «سرور فعال‌سازی» (HostedApp) بعد از N روز از تمام‌شدنِ مهلت (PATCH_HOSTING_AUTODEL_V1).

درخواست: «تو سرورای اختصاصی، وقتی سروری تایمش تموم میشه و متوقف میشه، بعد ۲ روز پاک بشه».
  • «متوقف» = وضعیتِ suspended (⏸ مهلت تمام؛ ناظرِ سرویسِ میزبانی کانتینر را خاموش می‌کند).
  • مبنایِ زمان: paid_until؛ اگر هیچ‌وقت پولی نداده بود، first_started_at + مهلتِ رایگانِ یک‌ساعته؛ نبودِ هر دو ⇒ رد می‌شود (حذف نمی‌کند).
  • کلیدِ تنظیمات: hosted_autodel_days (پیش‌فرض «2»؛ «0» = خاموش). از «⚡️ سرور فعال‌سازی ربات» قابلِ تغییر است.
  • حذف از راهِ APIِ سرویسِ میزبانی (destroy) = همان «حذفِ کامل» دستیِ مدیر؛ مالک اطلاع می‌گیرد و مدیرِ اصلی گزارشِ دسته‌ای.
  • هر دور حداکثر ۵ مورد؛ خطا ⇒ همان مورد دور بعد دوباره تلاش می‌شود و بقیه ادامه می‌دهند.
اگر اپ بعد از تمام‌شدنِ مهلت، قبل از حذف تمدید شود (paid_until در آینده)، دیگر شرایط برقرار نیست و حذف نمی‌شود.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html as _html

from sqlalchemy import select

from config import PLATFORM_ADMIN_ID, Session
from models import HostedApp

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

KEY = "hosted_autodel_days"
DEFAULT_DAYS = 2
OPTIONS = (0, 1, 2, 3, 7, 14)
FREE_GRACE = dt.timedelta(hours=1)
MAX_PER_ROUND = 5


async def get_days() -> int:
    from platform_bot import get_pset
    try:
        return max(0, min(int(float(await get_pset(KEY, str(DEFAULT_DAYS)) or DEFAULT_DAYS)), 60))
    except (TypeError, ValueError):
        return DEFAULT_DAYS


def label(days: int) -> str:
    return "⛔️ خاموش" if not days else f"{days} روز بعد از تمام‌شدنِ مهلت"


def ref_time(a: HostedApp) -> dt.datetime | None:
    if a.paid_until:
        return a.paid_until
    if a.first_started_at:
        return a.first_started_at + FREE_GRACE
    return None


def is_due(a: HostedApp, days: int, now: dt.datetime) -> bool:
    if not days or a.status != "suspended":
        return False
    ref = ref_time(a)
    if ref is None or ref > now:                  # هنوز مهلت دارد (پرداخت‌شده تا آینده)
        return False
    return (now - ref) >= dt.timedelta(days=days)


async def autodel_once(now: dt.datetime | None = None, bot=None) -> list[tuple[int, str]]:
    """اپ‌هایِ مشمول را حذف می‌کند؛ فهرستِ (id, name) حذف‌شده‌ها. خطایِ API ⇒ همان مورد بماند."""
    from platform_bot import _vip_api
    days = await get_days()
    if not days:
        return []
    now = now or dt.datetime.now()
    async with Session() as s:
        apps = (await s.scalars(select(HostedApp).where(HostedApp.status == "suspended").order_by(HostedApp.id))).all()
    due = [a for a in apps if is_due(a, days, now)][:MAX_PER_ROUND]
    done: list[tuple[int, str]] = []
    for a in due:
        ok, data = await _vip_api("POST", f"/api/hosting/admin/{a.id}/destroy")
        if not ok:
            logger.warning("hosting autodel: حذفِ اپِ %s (%s) ناموفق: %s", a.id, a.name, data)
            continue
        done.append((a.id, a.name or ""))
        logger.info("hosting autodel: اپِ %s (%s) %s روز بعد از تمامِ مهلت حذف شد", a.id, a.name, days)
        if bot is not None:
            try:
                await bot.send_message(
                    a.owner_id,
                    f"⚠️ سرورِ فعال‌سازیِ شما (<code>{_html.escape(a.name or '')}</code>) به‌دلیلِ تمدید‌نشدن، "
                    f"{days} روز بعد از تمام‌شدنِ مهلتش به‌صورتِ خودکار حذف شد.\n"
                    "فایل‌ها و تنظیماتِ آن قابلِ بازیابی نیست؛ در صورتِ نیاز می‌توانید دوباره سرور بسازید.",
                    parse_mode="HTML")
            except Exception:      # noqa: BLE001
                logger.debug("hosting autodel: اطلاع به مالکِ %s ممکن نشد", a.owner_id)
    if done and bot is not None and PLATFORM_ADMIN_ID:
        try:
            await bot.send_message(PLATFORM_ADMIN_ID,
                                   f"🗑 حذفِ خودکارِ {len(done)} سرور فعال‌سازی (مهلتِ تمام + {days} روز):\n"
                                   + "\n".join(f"• <code>{_html.escape(n)}</code>" for _i, n in done), parse_mode="HTML")
        except Exception:      # noqa: BLE001
            logger.debug("hosting autodel: گزارش به مدیر نرسید")
    return done


async def hosting_autodel_loop(platform_bot=None, interval_sec: int = 1800):
    await asyncio.sleep(150)
    while True:
        try:
            await autodel_once(bot=platform_bot)
        except Exception:      # noqa: BLE001
            logger.exception("hosting_autodel_loop خطا داد (حلقه ادامه می‌دهد)")
        await asyncio.sleep(interval_sec)
