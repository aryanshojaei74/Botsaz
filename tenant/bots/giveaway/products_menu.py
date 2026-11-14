"""منویِ «📦 سرویس‌ها و محصولات» برایِ رباتِ قرعه‌کشی — PATCH_GIVEAWAY_BOT_V1."""
from __future__ import annotations

from tenant.settings_registry import ProductsMenu, register_products_menu


async def _opts(bot_id: int, plan: str) -> list[tuple[str, str]]:
    return [("🎰 مدیریتِ قرعه‌کشی", "gw:adm")]


async def _kb_menu(msg, row, bot, state):
    from tenant.bots.giveaway.admin import show_menu
    await state.clear()
    await show_menu(msg, bot, row)


register_products_menu("giveaway", ProductsMenu(
    opts=_opts,
    description="ساخت و مدیریتِ قرعه‌کشی‌ها، اعلان به گروه/کانال، قرعه و تحویلِ جایزه:",
    styles={"gw:adm": "success"},
    keyboard={"🎰 مدیریتِ قرعه‌کشی": _kb_menu},
))
