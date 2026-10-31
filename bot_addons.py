"""افزونه‌های پولی ربات‌های مستأجر (فعال‌سازی از ربات اصلی، اجرا در ربات مستأجر).

=== PATCH_WAGER_ADDON_V1 ===
ربات اصلی با پرداخت از کیف‌پول بات‌ساز یک ردیف BotAddon می‌سازد؛ پروسه‌های ربات‌های مستأجر فقط می‌خوانند.
نتیجهٔ بررسی چند ثانیه کش می‌شود (پروسه‌ها جدا هستند)؛ پس فعال‌سازی حداکثر ~۱۵ ثانیه بعد دیده می‌شود.
"""
from __future__ import annotations

import time

from sqlalchemy import select

import config
from models import BotAddon

WAGER_ADDON = "wager_games"
_TTL = 15.0
_cache: dict[tuple[int, str], tuple[bool, float]] = {}


def wager_eligible(field: str | None, plan: str | None) -> bool:
    """همان قاعدهٔ tenant.shared.group_wallet._group_feature_allowed (قابلیت‌های گروه کیف‌پولی):
    خدمات مجازی، یا VPN با پلن رشد/حرفه‌ای. (تست برابری در tests/test_wager_tenant.py)"""
    return bool(field == "virtual_service" or (field == "vpn_sales" and plan in ("growth", "pro")))


async def addon_active(bot_id: int, addon: str = WAGER_ADDON) -> bool:
    key = (int(bot_id), addon)
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and hit[1] > now:
        return hit[0]
    async with config.Session() as s:
        row = await s.scalar(select(BotAddon.id).where(BotAddon.bot_id == int(bot_id), BotAddon.addon == addon))
    val = row is not None
    _cache[key] = (val, now + _TTL)
    return val


def invalidate(bot_id: int | None = None) -> None:
    """پاک‌کردن کش (همین پروسه)."""
    if bot_id is None:
        _cache.clear()
        return
    for k in [k for k in _cache if k[0] == int(bot_id)]:
        _cache.pop(k, None)


async def active_count(addon: str = WAGER_ADDON) -> int:
    from sqlalchemy import func
    async with config.Session() as s:
        return int(await s.scalar(select(func.count(BotAddon.id)).where(BotAddon.addon == addon)) or 0)
