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

# === PATCH_SELFHEAL_QUIET_V1 === (مدیر: «خیلی زیاد میاد») اصلاحِ وبهوک همچنان هر دور انجام می‌شود، ولی پیامِ مدیر نه:
# هر رباتِ اصلاح‌شده در هر ۲۴ ساعت حداکثر یک اعلان می‌گیرد؛ اگر در همان بازه بارها دوباره اصلاح شد (یعنی جایِ دیگری
# وبهوکش را بازنویسی می‌کند) فقط یک اعلانِ «تکراری» با تعدادِ دفعات می‌رود.
NOTIFY_WINDOW_SEC = 24 * 3600
REPEAT_THRESHOLD = 3
_HEAL_LOG: dict[tuple[int, str], list[float]] = {}     # (bot_id, kind) → زمان‌هایِ اصلاح در بازهٔ اخیر
_NOTIFIED: dict[tuple[int, str], tuple[float, bool]] = {}   # (bot_id, kind) → (زمانِ آخرین اعلان، اعلانِ تکراری رفته؟)


def decide_notice(b_id: int, kind: str, now: float) -> tuple[str | None, int]:
    """«first» برایِ اولین اصلاحِ بازه، «repeat» (یک‌بار) وقتی تعدادِ اصلاح‌ها به REPEAT_THRESHOLD رسید، وگرنه None."""
    key = (b_id, kind)
    log = [t for t in _HEAL_LOG.get(key, []) if now - t < NOTIFY_WINDOW_SEC] + [now]
    _HEAL_LOG[key] = log
    last = _NOTIFIED.get(key)
    if last is None or now - last[0] >= NOTIFY_WINDOW_SEC:
        _NOTIFIED[key] = (now, False)
        return "first", len(log)
    if len(log) >= REPEAT_THRESHOLD and not last[1]:
        _NOTIFIED[key] = (last[0], True)
        return "repeat", len(log)
    return None, len(log)


async def _check_one(b_id: int, token: str, status: str) -> tuple[str, str] | None:
    """برمی‌گرداند (نوعِ اصلاح, @یوزرنیم) اگر چیزی اصلاح شد، وگرنه None."""
    try:
        async with _temp_bot(token) as tb:
            me = await asyncio.wait_for(tb.get_me(), timeout=10)
            expected = f"{WEBHOOK_BASE}/webhook/{token}" if WEBHOOK_BASE else None
            kind = None
            if expected:
                wh = await asyncio.wait_for(tb.get_webhook_info(), timeout=8)
                from webhook_utils import TENANT_ALLOWED_UPDATES   # PATCH_JOIN_CHAT_MEMBER_V1
                if wh.url != expected or "chat_member" not in (wh.allowed_updates or []):
                    await asyncio.wait_for(
                        tb.set_webhook(expected, drop_pending_updates=False,
                                       allowed_updates=TENANT_ALLOWED_UPDATES), timeout=10)
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
        return (kind, me.username or str(b_id), b_id) if kind else None
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
    healed: list[tuple[str, str, int]] = []

    async def _run(r):
        async with sem:
            res = await _check_one(r.id, r.token, r.status)
            if res:
                healed.append(res)

    await asyncio.gather(*[_run(r) for r in rows], return_exceptions=True)
    if healed:
        logger.warning("selfheal: %d ربات خودکار اصلاح شد: %s", len(healed), [(k, u) for k, u, _i in healed[:20]])
        import time as _t
        now = _t.monotonic()
        lines = []
        for k, u, bid in healed:
            why, n = decide_notice(bid, k, now)
            base = ("🔗 وبهوک" if k == "webhook" else "🔑 توکن→فعال") + f" — @{u}"
            if why == "first":
                lines.append(base)
            elif why == "repeat":
                lines.append(base + f" ⚠️ {n} بار در ۲۴ ساعت اصلاح شد؛ احتمالاً جایِ دیگری (سرویسِ دیگر یا خودِ مالک) "
                                    "وبهوکش را عوض می‌کند. تا ۲۴ ساعتِ دیگر برایش اعلان نمی‌آید.")
        if lines and platform_bot and PLATFORM_ADMIN_ID:
            try:
                await platform_bot.send_message(
                    PLATFORM_ADMIN_ID,
                    f"🩺 <b>بازیابیِ خودکارِ ربات‌ها</b> ({len(lines)})\n\n" + "\n".join(lines[:25])
                    + (f"\n… و {len(lines) - 25} مورد دیگر" if len(lines) > 25 else ""))
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
