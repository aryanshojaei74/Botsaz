"""
gift_sync.py — «🎁 هم‌گام‌سازیِ خودکارِ گیفت‌ها» (PATCH_GIFT_EMOJI_V1).

هر ۳۰ دقیقه برایِ رباتِ خدماتِ مجازی‌ای که فروشِ گیفت دارد، فهرستِ گیفت‌هایِ تلگرام خوانده می‌شود: گیفتِ تازه (مثلاً تدیِ هالووین)
غیرفعال اضافه می‌شود، استیکرِ واقعیِ همهٔ گیفت‌ها ذخیره می‌شود و فقط یک‌بار به مالک خبر می‌رسد تا فعالش کند یا ایموجی بگذارد.
اگر مالک «گیفت‌هایِ تازه خودکار فعال شوند» (gift_auto_activate=1) را بگذارد، گیفتِ تازه مستقیم فعال می‌شود.
"""
from __future__ import annotations

import asyncio
from collections import Counter

from sqlalchemy import select

from config import Session
from models import Bot as BotRow, TelegramGiftConfig

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

INTERVAL = 1800


async def sync_bot(bot, bot_id: int) -> list[tuple[str, str, int]]:
    """گیفت‌هایِ تازه را اضافه و استیکرها را ذخیره می‌کند. خروجی: [(gift_id, ایموجی, استارز)] برایِ گیفت‌هایِ تازه."""
    from tenant import get_setting, set_setting
    result = await bot.get_available_gifts()
    auto = (await get_setting(bot_id, "gift_auto_activate", "0")) == "1"
    new: list[tuple[str, str, int]] = []
    async with Session() as s:
        existing = {g.gift_id: g for g in (await s.scalars(select(TelegramGiftConfig).where(
            TelegramGiftConfig.bot_id == bot_id))).all()}
        for g in result.gifts:
            emoji = getattr(getattr(g, "sticker", None), "emoji", None)
            if g.id in existing:
                existing[g.id].star_count = g.star_count
                existing[g.id].emoji = emoji
            else:
                s.add(TelegramGiftConfig(bot_id=bot_id, gift_id=g.id, star_count=g.star_count, emoji=emoji, active=auto))
                new.append((g.id, emoji or "🎁", g.star_count))
        await s.commit()
    for g in result.gifts:
        fid = getattr(getattr(g, "sticker", None), "file_id", None)
        if fid:
            await set_setting(bot_id, f"gift_sticker:{g.id}", fid)
    return new


def notice(new: list[tuple[str, str, int]], gifts_emojis: list[str], auto: bool) -> str:
    cnt = Counter(gifts_emojis)
    lines = [f"• {e}{' ⚠️ ایموجیِ تکراری' if cnt.get(e, 0) > 1 else ''} — {sc:,} استارز" for _g, e, sc in new[:15]]
    return ("🎁 <b>گیفتِ تازه از تلگرام آمد:</b>\n" + "\n".join(lines) + "\n\n"
            + ("✅ طبقِ تنظیمِ شما خودکار فعال شد.\n" if auto else
               "⏸ فعلاً غیرفعال است: از «🎁 فروشِ گیفت ← 🗂 مدیریتِ گیفت‌ها» فعالش کن.\n")
            + "برایِ تفاوتِ ظاهری از «🎨 ایموجیِ گیفت‌ها» ایموجیِ دلخواه بگذار؛ مشتری استیکرِ واقعیِ گیفت را هم می‌بیند.")


async def run_once() -> int:
    from aiogram import Bot
    from tenant import get_setting
    sent = 0
    async with Session() as s:
        rows = (await s.scalars(select(BotRow).where(BotRow.field == "virtual_service", BotRow.status == "active"))).all()
        sellers = {r[0] for r in (await s.execute(select(TelegramGiftConfig.bot_id).distinct())).all()}
    for row in rows:
        if row.id not in sellers:
            continue
        try:
            bot = Bot(token=row.token)
            try:
                new = await sync_bot(bot, row.id)
                if new:
                    auto = (await get_setting(row.id, "gift_auto_activate", "0")) == "1"
                    emojis = [e for _g, e, _s in new]
                    await bot.send_message(row.owner_id, notice(new, emojis, auto), parse_mode="HTML")
                    sent += 1
            finally:
                await bot.session.close()
        except Exception:      # noqa: BLE001
            logger.exception("gift_sync: خطا (bot=%s)", row.id)
    return sent


async def gift_sync_loop() -> None:
    await asyncio.sleep(90)
    while True:
        try:
            await run_once()
        except Exception:      # noqa: BLE001
            logger.exception("gift_sync loop crashed; retrying")
        await asyncio.sleep(INTERVAL)
