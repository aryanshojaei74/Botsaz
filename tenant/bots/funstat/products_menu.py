"""
tenant/bots/funstat/products_menu.py — منویِ «📦 سرویس‌ها و محصولات» برایِ رباتِ Fun Stat.

=== PATCH_FUNSTAT_BOT_V1 ===
"""
from __future__ import annotations

from tenant.settings_registry import ProductsMenu, register_products_menu


async def _opts(bot_id: int, plan: str) -> list[tuple[str, str]]:
    return [("📊 آمارِ جست‌وجوها", "fsb:stats")]


async def _kb_stats(msg, row, bot, state):
    from tenant.bots.funstat.handlers import stats_text
    await msg.answer(await stats_text(row), parse_mode="HTML")


register_products_menu("funstat", ProductsMenu(
    opts=_opts,
    description="آمارِ جست‌وجوهایِ کاربرانِ رباتِ Fun Stat:",
    styles={"fsb:stats": "primary"},
    keyboard={"📊 آمارِ جست‌وجوها": _kb_stats},
))
