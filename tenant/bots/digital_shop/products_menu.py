"""
tenant/bots/digital_shop/products_menu.py — منویِ «📦 سرویس‌ها و محصولات» برایِ فروشگاهِ دیجیتال.

=== PATCH_SETTINGS_REGISTRY_V3_PRODUCTS ===
قبلاً `if bot_field == "digital_shop"` داخلِ _sec_products_kb بود.
"""
from __future__ import annotations

from plans import Feature, has_feature
from tenant.settings_registry import ProductsMenu, register_products_menu


async def _opts(bot_id: int, plan: str) -> list[tuple[str, str]]:
    def ok(f): return has_feature(plan, f)
    opts: list[tuple[str, str]] = []
    if ok(Feature.PRODUCT_SHOP):
        opts.append(("🛒 فروشگاه دیجیتال", "tadm:shop"))
        # === PATCH_SHOP_GETS_AD_PANEL_V1 ===
        # پنلِ ارسالِ زمان‌بندی‌شده (ساخته‌شده برایِ رباتِ تبلیغاتی) برایِ تبلیغِ
        # محصولاتِ فروشگاه در کانال/گروه هم در دسترس است.
        if ok(Feature.SCHEDULED_BROADCAST):
            opts.append(("📢 پنل تبلیغِ زمان‌بندی‌شده", "adb:menu"))
    return opts


# ───────────── حالتِ منویِ کیبوردی (PATCH_SETTINGS_REGISTRY_V4_PRODUCTS_KB) ─────────────

async def _kb_shop(msg, row, bot, state):
    from tenant.bots.digital_shop.handlers import shop_admin_kb  # === PATCH_TENANT_SPLIT_PHASE3_DIGITAL_SHOP_V1 ===
    await msg.answer("🛒 <b>مدیریت فروشگاه</b>", reply_markup=shop_admin_kb(row.field))


async def _kb_ad_panel(msg, row, bot, state):
    from tenant.shared.ad_broadcast import _render_panel
    panel_text, panel_kb = await _render_panel(row.id)
    await msg.answer(panel_text, reply_markup=panel_kb, parse_mode="HTML",
                     disable_web_page_preview=True)


register_products_menu("digital_shop", ProductsMenu(
    opts=_opts,
    description="مدیریت فروشگاه، محصولات و دسته‌بندی‌ها:",
    styles={"tadm:shop": "success", "adb:menu": "primary"},
    keyboard={
        "🛒 فروشگاه دیجیتال": _kb_shop,
        "📢 پنل تبلیغِ زمان‌بندی‌شده": _kb_ad_panel,
    },
))
