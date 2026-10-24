"""منویِ «📦 سرویس‌ها و محصولات» برایِ رباتِ واسطه‌گری — PATCH_ESCROW_BOT_KIND_V1."""
from __future__ import annotations

from tenant.settings_registry import ProductsMenu, register_products_menu


async def _opts(bot_id: int, plan: str) -> list[tuple[str, str]]:
    from plans import Feature, has_feature
    return [("🤝 تنظیماتِ واسطه‌گری", "set:escrow"), ("📂 معاملات", "esc:adm:deals")] if has_feature(plan, Feature.ESCROW) \
        else [("🔒 واسطه‌گری (پلنِ رشد/حرفه‌ای)", "set:escrow")]


async def _kb_settings(msg, row, bot, state):
    from tenant.shared.escrow import show_menu
    await show_menu(msg, bot, row.id)


register_products_menu("escrow_bot", ProductsMenu(
    opts=_opts,
    description="مدیریتِ واسطه‌گری و معاملاتِ ربات:",
    styles={"set:escrow": "primary", "esc:adm:deals": "primary"},
    keyboard={"🤝 تنظیماتِ واسطه‌گری": _kb_settings, "📂 معاملات": _kb_settings},
))
