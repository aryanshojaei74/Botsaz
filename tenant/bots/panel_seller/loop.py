"""loop.py — حلقهٔ پس‌زمینهٔ اعمالِ سقف‌ها/انقضا و هشدارها (PATCH_PANEL_SELLER_V1)."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from config import Session
from models import Bot as BotRow

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

INTERVAL = 600


async def run_once() -> int:
    """یک دور برایِ همهٔ رباتِ پنل‌فروشیِ فعال. خروجی: تعدادِ پیام‌هایِ فرستاده‌شده."""
    from tenant.bots.panel_seller import service as SV
    from tenant.bots.panel_seller.common import sales_gate
    sent = 0
    async with Session() as s:
        rows = (await s.scalars(select(BotRow).where(BotRow.field == "panel_seller", BotRow.status == "active"))).all()
    for row in rows:
        try:
            ok, _ = await sales_gate(row)
            if not ok:                     # پایانِ تست/پلن: اعمالِ سقف‌ها را هم متوقف می‌کنیم تا چیزی ناخواسته خاموش نشود
                continue
            events = await SV.enforce_once(row.id)
            if not events:
                continue
            from aiogram import Bot
            bot = Bot(token=row.token)
            try:
                for tg, text in events:
                    try:
                        await bot.send_message(tg or row.owner_id, text)
                        sent += 1
                    except Exception:      # noqa: BLE001
                        logger.info("panel_seller: پیام به %s نرسید", tg or row.owner_id)
            finally:
                await bot.session.close()
        except Exception:      # noqa: BLE001
            logger.exception("panel_seller loop: خطا (bot=%s)", row.id)
    return sent


async def panel_seller_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await run_once()
        except Exception:      # noqa: BLE001
            logger.exception("panel_seller loop crashed; retrying")
        await asyncio.sleep(INTERVAL)
