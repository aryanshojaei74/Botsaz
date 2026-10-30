"""منویِ «📦 سرویس‌ها و محصولات» برایِ رباتِ پنل‌فروشی — PATCH_PANEL_SELLER_V1."""
from __future__ import annotations

from tenant.settings_registry import ProductsMenu, register_products_menu


async def _opts(bot_id: int, plan: str) -> list[tuple[str, str]]:
    return [("🧩 مدیریتِ پنل‌فروشی", "psel:adm")]


async def _kb_menu(msg, row, bot, state):
    from tenant.bots.panel_seller.admin import show_menu
    await state.clear()
    await show_menu(msg, bot, row)


register_products_menu("panel_seller", ProductsMenu(
    opts=_opts,
    description="اتصالِ پنل، بسته‌هایِ فروش، ادمین‌هایِ فروخته‌شده، ریستارتِ پنل و دسترسی‌هایِ مشتری:",
    styles={"psel:adm": "success"},
    keyboard={"🧩 مدیریتِ پنل‌فروشی": _kb_menu},
))
