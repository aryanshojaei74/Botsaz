# -*- coding: utf-8 -*-
"""reactivation_link.py — PATCH_PARTNER_REACTIVATION_LINK_TENANT_V1

لینکِ «فعال‌سازیِ ربات» در پیامِ بایگانی: برایِ رباتی که از طریقِ یک «ربات همکار» فروخته شده باید به همان ربات همکار ببرد
(payload: pubot_<id>)، نه به بات‌سازِ اصلی. جدا از main_tenant.py تا بدونِ import کردنِ کلِ سرویس قابلِ تست باشد.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

logger = logging.getLogger(__name__)


async def partner_bot_link(brow, session_factory) -> str | None:
    """لینکِ ربات همکار برایِ این رباتِ فروخته‌شده؛ None اگر مستقیم از پلتفرم ساخته شده یا خطایی بود."""
    try:
        from models import Bot as BotRow, PartnerBot, PartnerSale
        async with session_factory() as s:
            sale = await s.scalar(select(PartnerSale).where(PartnerSale.sold_bot_id == brow.id))
            if sale:
                pbot = await s.get(PartnerBot, sale.partner_bot_id)
                if pbot:
                    prow = await s.get(BotRow, pbot.bot_id)
                    if prow and prow.username:
                        return f"https://t.me/{prow.username}?start=pubot_{brow.id}"
    except Exception:
        logger.exception("ساختِ لینکِ فعال‌سازیِ ربات همکار ناموفق بود؛ به لینکِ پلتفرم برمی‌گردیم")
    return None
