"""
bot_selfheal.py — بازیابیِ خودکارِ رباتِ «بی‌دلیل خاموش‌شده».

=== PATCH_BOT_SELFHEAL_V1 ===
گزارش #144 (Sajad): «بعضی وقت‌ها ربات خاموش می‌شود و باید بیای بات‌ساز دوباره فعال کنی».
دو دلیلِ فنیِ شناخته‌شده که ربات را بی‌صدا از کار می‌اندازند و تا الان فقط با دکمهٔ دستیِ
«🟢 فعال‌سازی / اتصال مجدد» در پنلِ ادمین درست می‌شدند:

  ۱) وبهوکِ ربات در تلگرام پاک یا عوض شده (خطای شبکه، ری‌ست‌شدنِ وبهوک، ...). ربات سالم است
     ولی آپدیتی به ما نمی‌رسد → «خاموش».
  ۲) status = disabled_bad_token: یک بررسیِ سلامت (یا خطایِ لحظه‌ایِ 401 از تلگرام) توکن را
     «باطل» حساب کرده، در حالی که توکن هنوز معتبر است. وبهوک این ربات‌ها عمداً رد می‌شود.

این حلقه همان کارِ دکمهٔ دستی را خودکار انجام می‌دهد:
  • ربات‌هایِ فعال: اگر وبهوکشان با آدرسِ درست فرق دارد، دوباره ست می‌شود (بدونِ دور ریختنِ آپدیت‌ها).
  • ربات‌هایِ disabled_bad_token (غیرِ بایگانی): اگر getMe الان موفق است، دوباره active می‌شوند.
مالک‌خاموش‌کردن (paused)، قفل/غیرفعال‌سازیِ ادمین و بایگانی دست‌نخورده می‌ماند.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from config import Session, WEBHOOK_BASE, PLATFORM_ADMIN_ID
from models import Bot as BotRow
from bot_utils import _temp_bot

logger = logging.getLogger("bot_selfheal")

CYCLE_SEC = 15 * 60
CONCURRENCY = 8


async def _check_one(b_id: int, token: str, status: str) -> tuple[str, str] | None:
    """برمی‌گرداند (نوعِ اصلاح, @یوزرنیم) اگر چیزی اصلاح شد، وگرنه None."""
    try:
        async with _temp_bot(token) as tb:
            me = await asyncio.wait_for(tb.get_me(), timeout=10)
            expected = f"{WEBHOOK_BASE}/webhook/{token}" if WEBHOOK_BASE else None
            kind = None
            if expected:
                wh = await asyncio.wait_for(tb.get_webhook_info(), timeout=8)
                if wh.url != expected:
                    await asyncio.wait_for(
                        tb.set_webhook(expected, drop_pending_updates=False), timeout=10)
                    kind = "webhook"
        if status == "disabled_bad_token":
            async with Session() as s:
                br = await s.get(BotRow, b_id)
                if br and br.status == "disabled_bad_token" and br.archived_at is None:
                    br.status = "active"
                    await s.commit()
                    kind = "token"
            try:
                import tenant_status_bus
                await tenant_status_bus.publish_tenant_invalidate(token)
            except Exception:
                logger.debug("invalidate پس از بازیابی ناموفق بود (بی‌اهمیت)")
        return (kind, me.username or str(b_id)) if kind else None
    except Exception as e:
        # Unauthorized واقعی، timeout، شبکه: دست نمی‌زنیم؛ دور بعد دوباره امتحان می‌شود.
        logger.debug("selfheal bot %s: %s", b_id, str(e)[:100])
        return None


async def selfheal_once(platform_bot=None) -> int:
    async with Session() as s:
        rows = (await s.execute(
            select(BotRow.id, BotRow.token, BotRow.status).where(
                BotRow.status.in_(("active", "disabled_bad_token")),
                BotRow.archived_at.is_(None)))).all()
    sem = asyncio.Semaphore(CONCURRENCY)
    healed: list[tuple[str, str]] = []

    async def _run(r):
        async with sem:
            res = await _check_one(r.id, r.token, r.status)
            if res:
                healed.append(res)

    await asyncio.gather(*[_run(r) for r in rows], return_exceptions=True)
    if healed:
        logger.warning("selfheal: %d ربات خودکار اصلاح شد: %s", len(healed), healed[:20])
        if platform_bot and PLATFORM_ADMIN_ID:
            lines = [("🔗 وبهوک" if k == "webhook" else "🔑 توکن→فعال") + f" — @{u}" for k, u in healed[:25]]
            try:
                await platform_bot.send_message(
                    PLATFORM_ADMIN_ID,
                    f"🩺 <b>بازیابیِ خودکارِ ربات‌ها</b> ({len(healed)})\n\n" + "\n".join(lines)
                    + (f"\n… و {len(healed) - 25} مورد دیگر" if len(healed) > 25 else ""))
            except Exception:
                logger.debug("اعلانِ بازیابی به مدیر ناموفق بود (بی‌اهمیت)")
    return len(healed)


async def bot_selfheal_loop(platform_bot=None, interval_sec: int = CYCLE_SEC):
    await asyncio.sleep(120)
    while True:
        try:
            await selfheal_once(platform_bot)
        except Exception:
            logger.exception("selfheal loop error")
        await asyncio.sleep(interval_sec)
