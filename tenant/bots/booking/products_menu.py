"""
tenant/bots/booking/products_menu.py — منویِ «📦 سرویس‌ها و محصولات» برایِ رباتِ رزرو.

=== PATCH_SETTINGS_REGISTRY_V3_PRODUCTS ===
قبلاً `if bot_field == "booking"` داخلِ _sec_products_kb بود.
"""
from __future__ import annotations

from plans import Feature, has_feature
from tenant.settings_registry import ProductsMenu, register_products_menu


async def _opts(bot_id: int, plan: str) -> list[tuple[str, str]]:
    if has_feature(plan, Feature.APPOINTMENT):
        return [("📅 سیستم رزرو و نوبت", "tadm:booking")]
    return []


# ───────────── حالتِ منویِ کیبوردی (PATCH_SETTINGS_REGISTRY_V4_PRODUCTS_KB) ─────────────

async def _kb_booking(msg, row, bot, state):
    from tenant.bots.booking.handlers import booking_admin_kb  # === PATCH_TENANT_SPLIT_PHASE2_BOOKING_V1 ===
    await msg.answer("📅 <b>مدیریت رزرو</b>", reply_markup=booking_admin_kb())


register_products_menu("booking", ProductsMenu(
    opts=_opts,
    description="مدیریت سیستم رزرو و نوبت‌دهی:",
    styles={"tadm:booking": "primary"},
    keyboard={"📅 سیستم رزرو و نوبت": _kb_booking},
))
