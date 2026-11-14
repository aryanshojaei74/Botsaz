"""
tenant/bots/ad_bot/products_menu.py — منویِ «📦 سرویس‌ها و محصولات» برایِ رباتِ تبلیغاتی.

=== PATCH_SETTINGS_REGISTRY_V3_PRODUCTS ===
قبلاً `if bot_field == "ad_bot"` داخلِ _sec_products_kb بود.
"""
from __future__ import annotations

from plans import Feature, has_feature
from tenant.settings_registry import ProductsMenu, register_products_menu


async def _opts(bot_id: int, plan: str) -> list[tuple[str, str]]:
    def ok(f): return has_feature(plan, f)
    opts: list[tuple[str, str]] = []
    # پنلِ اصلیِ باتِ تبلیغاتی — ارسالِ زمان‌بندی‌شده. فقط پلنِ حرفه‌ای.
    if ok(Feature.SCHEDULED_BROADCAST):
        opts.append(("📢 پنل تبلیغِ زمان‌بندی‌شده", "adb:menu"))
    # اگر محصولات هم می‌خواهد بفروشد (شبیهِ فروشگاهِ دیجیتال)
    if ok(Feature.PRODUCT_SHOP):
        opts.append(("🛒 فروشگاه دیجیتال (محصولات)", "tadm:shop"))
    return opts


# ───────────── حالتِ منویِ کیبوردی (PATCH_SETTINGS_REGISTRY_V4_PRODUCTS_KB) ─────────────
# === PATCH_ADBOT_KEYBOARD_MODE_TEXT_HANDLERS_V1 === (قبلاً در زنجیرهٔ مرکزی بود)

async def _kb_shop(msg, row, bot, state):
    from tenant.bots.digital_shop.handlers import shop_admin_kb  # === PATCH_TENANT_SPLIT_PHASE3_DIGITAL_SHOP_V1 ===
    await msg.answer("🛒 <b>مدیریت فروشگاه</b>", reply_markup=shop_admin_kb(row.field))


async def _kb_ad_panel(msg, row, bot, state):
    from tenant.shared.ad_broadcast import _render_panel
    panel_text, panel_kb = await _render_panel(row.id)
    await msg.answer(panel_text, reply_markup=panel_kb, parse_mode="HTML",
                     disable_web_page_preview=True)


register_products_menu("ad_bot", ProductsMenu(
    opts=_opts,
    # === PATCH_AD_BOT_EMPTY_PRODUCTS_SECTION_V1 ===
    description="مدیریت تبلیغِ زمان‌بندی‌شده و محصولاتِ ربات:",
    styles={"adb:menu": "primary", "tadm:shop": "success"},
    keyboard={
        "🛒 فروشگاه دیجیتال (محصولات)": _kb_shop,
        "📢 پنل تبلیغِ زمان‌بندی‌شده": _kb_ad_panel,
    },
))
