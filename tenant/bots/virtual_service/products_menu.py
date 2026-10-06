"""
tenant/bots/virtual_service/products_menu.py — منویِ «📦 سرویس‌ها و محصولات» برایِ خدماتِ مجازی.

=== PATCH_SETTINGS_REGISTRY_V3_PRODUCTS ===
قبلاً `if bot_field == "virtual_service"` داخلِ _sec_products_kb بود.
callback_dataها بدونِ تغییر؛ فقط برچسب/چیدمانِ قبلی حفظ شده:
  • «مشاهده سفارشات» (tadm:vs_orders) — برچسب عوض شده بود ولی callback ثابت ماند.
  • «ارائه‌دهندگان» تنها زیرِ سفارشات؛ «مدیریتِ محصولات» + «مدیریتِ دسته‌ها» یک ردیف؛
    «تنظیم قیمت» + «سرویس‌های دستی» یک ردیف (row_pairs).
  • ارائه‌دهنده/محصولات/دسته‌ها از پلنِ رشد به بالا (SMM_API_ACCESS)؛ پلنِ رایگان فقط
    سرویس‌های دستی + قیمت + سفارشات می‌بیند و یک دکمهٔ قفل.
  • «افزودنِ پنلِ دومِ به‌بعد» (SMM_MULTI_PANEL) داخلِ خودِ smm.py چک می‌شود، نه این‌جا.
"""
from __future__ import annotations

from plans import Feature, has_feature
from tenant.settings_registry import ProductsMenu, register_products_menu


async def _opts(bot_id: int, plan: str) -> list[tuple[str, str]]:
    opts: list[tuple[str, str]] = []
    opts.append(("🗂 مشاهده سفارشات", "tadm:vs_orders"))
    if has_feature(plan, Feature.SMM_API_ACCESS):
        opts.append(("🏢 ارائه‌دهندگان", "smm:panels"))
        opts.append(("📱 افزودن سرویس شمارهٔ مجازی خودکار", "ozv:menu"))
        opts.append(("📦 مدیریتِ محصولات", "smm:products:menu"))
        # === PATCH_SMM_MANUAL_CATEGORY_V1 ===
        opts.append(("📁 مدیریتِ دسته‌ها", "smm:cat:menu:top"))
    else:
        opts.append(("🔒 ارائه‌دهنده و مدیریتِ محصولات (پلنِ رشد به بالا)", "tadm:sec:products:lockedinfo"))
    opts.append(("💰 تنظیم قیمت سرویس‌ها", "tadm:vs_prices"))
    opts.append(("✍️ سرویس‌های دستی", "smm:manlist"))
    opts.append(("📘 راهنما", "tadm:products:guide"))
    return opts


# ───────────── حالتِ منویِ کیبوردی (PATCH_SETTINGS_REGISTRY_V4_PRODUCTS_KB) ─────────────
# کدِ هر هندلر عیناً از زنجیرهٔ tenant_buttons منتقل شده؛ نام‌هایِ مشترک lazy از tenant.

_LOCK_MSG = ("🔒 اتصال به ارائه‌دهنده/پنل فقط از پلنِ رشد به بالا فعاله.\n"
             "پلنِ رایگان فقط «سرویس‌های دستی» رو داره.")


async def _kb_orders(msg, row, bot, state):
    # === PATCH_VS_ORDERS_SUBMENU_V1 ===
    from tenant import _vs_orders_content
    text, kb = await _vs_orders_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _kb_prices(msg, row, bot, state):
    from tenant import _vs_prices_content
    text, kb = await _vs_prices_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _kb_providers(msg, row, bot, state):
    # === PATCH_SMM_API_ACCESS_V1 ===
    if not has_feature(row.plan, Feature.SMM_API_ACCESS):
        await msg.answer(_LOCK_MSG)
        return
    from tenant.bots.virtual_service.smm import smm_panels_content
    text, kb = await smm_panels_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _kb_vnum(msg, row, bot, state):
    if not has_feature(row.plan, Feature.SMM_API_ACCESS):
        await msg.answer("🔒 این قابلیت از پلنِ رشد به بالا فعال است.")
        return
    from tenant.bots.virtual_service.ozvinoo_vnum import ozvinoo_menu_for_text
    text, kb = await ozvinoo_menu_for_text(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _kb_manual(msg, row, bot, state):
    from tenant.bots.virtual_service.smm import manual_services_content
    text, kb = await manual_services_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _kb_products_hub(msg, row, bot, state):
    # === PATCH_SMM_PRODUCTS_HUB_V1 ===
    if not has_feature(row.plan, Feature.SMM_API_ACCESS):
        await msg.answer(_LOCK_MSG)
        return
    from tenant.bots.virtual_service.smm import smm_products_content
    text, kb = await smm_products_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _kb_categories(msg, row, bot, state):
    # === PATCH_SMM_MANUAL_CATEGORY_V1 ===
    if not has_feature(row.plan, Feature.SMM_API_ACCESS):
        await msg.answer(_LOCK_MSG)
        return
    from tenant.bots.virtual_service.smm import smm_categories_content
    from tenant.bots.virtual_service.smm import _set_setting as _smm_set_setting
    # === PATCH_SMM_CAT_ORIGIN_V1 === ورودیِ «بیرونی»؛ مبدأ را صریح ست می‌کند
    await _smm_set_setting(row.id, "smm_cat_origin", "top")
    text, kb = await smm_categories_content(row.id)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _kb_locked(msg, row, bot, state):
    """دکمهٔ قفلِ پلنِ رایگان. === PATCH_SETTINGS_REGISTRY_V4_PRODUCTS_KB ===
    قبلاً در حالتِ کیبوردی هیچ هندلری نداشت (دکمهٔ مرده)؛ حالا پیامِ ارتقا نشان می‌دهد."""
    await msg.answer(_LOCK_MSG)


async def _kb_guide(msg, row, bot, state):
    from tenant.bots.vpn.products_menu import kb_products_guide
    await kb_products_guide(msg, row, bot, state)


register_products_menu("virtual_service", ProductsMenu(
    opts=_opts,
    description="مدیریت سفارش‌های خدمات مجازی تلگرام:",
    row_pairs={
        "smm:products:menu": "smm:cat:menu:top",
        "tadm:vs_prices": "smm:manlist",
    },
    styles={
        "tadm:vs_orders": "primary", "smm:panels": "success", "ozv:menu": "primary",
        "tadm:vs_prices": "success", "smm:menu": "primary", "smm:manlist": "success",
        "tadm:products:guide": "primary",
    },
    keyboard={
        "🗂 مشاهده سفارشات": _kb_orders,
        "💰 تنظیم قیمت سرویس‌ها": _kb_prices,
        "🏢 ارائه‌دهندگان": _kb_providers,
        "📱 افزودن سرویس شمارهٔ مجازی خودکار": _kb_vnum,
        "✍️ سرویس‌های دستی": _kb_manual,
        "📦 مدیریتِ محصولات": _kb_products_hub,
        "📁 مدیریتِ دسته‌ها": _kb_categories,
        "🔒 ارائه‌دهنده و مدیریتِ محصولات (پلنِ رشد به بالا)": _kb_locked,
        "📘 راهنما": _kb_guide,
    },
))
