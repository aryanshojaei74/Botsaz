"""tenant/bots/giveaway/loop.py — پایانِ خودکارِ قرعه‌کشی‌ها و تازه‌کردنِ شمارندهٔ اعلان‌ها (PATCH_GIVEAWAY_BOT_V1)."""
from __future__ import annotations

import asyncio
import time

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import select

from config import Session
from models import Bot as BotRow, Giveaway, GiveawayPost

from tenant.bots.giveaway import announce as A
from tenant.bots.giveaway import engine as E

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

REFRESH_EVERY = 600.0
_last_refresh: dict[int, float] = {}


async def end_due_once() -> int:
    """قرعه‌کشی‌هایِ فعالی که ends_at‌شان رسیده را پایان می‌دهد؛ تعدادِ انجام‌شده."""
    done = 0
    for gid, bot_id in await E.due_for_auto_end():
        try:
            async with Session() as s:
                row = await s.get(BotRow, bot_id)
            if row is None or not row.token or row.status != "active":
                continue
            bot = Bot(token=row.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                res = await A.finish_giveaway(bot, row, gid, notify_admin_chat=row.owner_id)
                done += 1 if res.ok else 0
            finally:
                await bot.session.close()
        except Exception:      # noqa: BLE001
            logger.exception("giveaway auto-end failed (gid=%s)", gid)
    return done


async def refresh_counters_once(now: float | None = None) -> int:
    """شمارندهٔ شرکت‌کنندگان در اعلان‌هایِ گروه/کانالِ قرعه‌کشی‌هایِ فعال (هر ۱۰ دقیقه)."""
    now = time.monotonic() if now is None else now
    n = 0
    async with Session() as s:
        gids = list((await s.scalars(select(GiveawayPost.giveaway_id).distinct())).all())
        gs = [g for g in (await s.scalars(select(Giveaway).where(Giveaway.id.in_(gids), Giveaway.status == "active"))).all()] if gids else []
    for g in gs:
        if now - _last_refresh.get(g.id, -1e9) < REFRESH_EVERY:
            continue
        _last_refresh[g.id] = now
        try:
            async with Session() as s:
                row = await s.get(BotRow, g.bot_id)
            if row is None or not row.token:
                continue
            bot = Bot(token=row.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                n += await A.refresh_posts(bot, g)
            finally:
                await bot.session.close()
        except Exception:      # noqa: BLE001
            logger.exception("giveaway refresh counters failed (gid=%s)", g.id)
    return n


async def giveaway_loop(interval: int = 30) -> None:
    while True:
        try:
            await end_due_once()
            await refresh_counters_once()
        except Exception:      # noqa: BLE001
            logger.exception("giveaway_loop failed")
        await asyncio.sleep(interval)
