"""
tenant/bots/vpn/products_menu.py — منویِ «📦 سرویس‌ها و محصولات» برایِ رباتِ فروشِ VPN.

=== PATCH_SETTINGS_REGISTRY_V3_PRODUCTS ===
قبلاً `if bot_field == "vpn_sales"` داخلِ _sec_products_kb (tenant/__init__.py) بود.
callback_dataها بدونِ تغییر.
"""
from __future__ import annotations

from plans import Feature, has_feature
from tenant.settings_registry import ProductsMenu, register_products_menu


async def _opts(bot_id: int, plan: str) -> list[tuple[str, str]]:
    def ok(f): return has_feature(plan, f)
    opts: list[tuple[str, str]] = []
    if ok(Feature.MANUAL_INVENTORY):
        opts.append(("📦 انبار کانفیگ‌های دستی", "tadm:cfg"))
    if ok(Feature.PANEL_INTEGRATION):
        opts.append(("🤖 پنل VPN خودکار (X-UI)", "tvpn:menu"))
    # === PATCH_VPN_PANEL_MARKET_V1 ===
    if ok(Feature.VPN_PANEL_MARKET):
        opts.append(("🏷 ثبت و فروش پنل", "pnlmkt:menu"))
    # === PATCH_PANEL_YAR_MOVE_V1 ===
    if ok(Feature.PANEL_YAR):
        opts.append(("🗂 پنل‌یار", "pyar:menu"))
    opts.append(("📘 راهنما", "tadm:products:guide"))
    return opts


# ───────────── حالتِ منویِ کیبوردی (PATCH_SETTINGS_REGISTRY_V4_PRODUCTS_KB) ─────────────
# کدِ هر هندلر عیناً از زنجیرهٔ tenant_buttons (tenant/__init__.py) منتقل شده.
# نام‌هایِ مشترک (Session, select, …) lazy از خودِ tenant گرفته می‌شوند.

async def _kb_pnlmkt(msg, row, bot, state):
    # === PATCH_VPN_PANEL_MARKET_V1 ===
    if not has_feature(row.plan, Feature.VPN_PANEL_MARKET):
        return False
    from tenant import _pnlmkt_menu_content
    text, kb = await _pnlmkt_menu_content(row)
    await msg.answer(text, reply_markup=kb)


async def _kb_panelyar(msg, row, bot, state):
    # === PATCH_PANEL_YAR_MOVE_V1 ===
    if not has_feature(row.plan, Feature.PANEL_YAR):
        return False
    from tenant.shared.panel_clients.panelyar import show_panels_menu
    await show_panels_menu(msg, row.id, show_back=True)


async def _kb_cfg_inventory(msg, row, bot, state):
    from tenant import Session, select, func, Config, get_setting, cfg_manage_kb
    async with Session() as s:
        total_stock = await s.scalar(select(func.count()).select_from(Config).where(
            Config.bot_id == row.id, Config.status == "in_stock"))
        total_sold = await s.scalar(select(func.count()).select_from(Config).where(
            Config.bot_id == row.id, Config.status == "sold"))
        vols = await get_setting(row.id, "volume_options", "10,30,50,100")
        days_ = await get_setting(row.id, "day_options", "30,60,90")
        counts_ = await get_setting(row.id, "count_options", "1,2,3")
        unl_days = await get_setting(row.id, "unlimited_day_options", "30,60,90")
    await msg.answer(
        f"📦 <b>مدیریت کانفیگ‌ها</b>\n\n"
        f"✅ موجود: <b>{total_stock}</b> کانفیگ\n"
        f"🛒 فروخته: <b>{total_sold}</b> کانفیگ\n\n"
        f"📏 بازه حجم (گیگ): {vols}\n"
        f"📅 بازه روز: {days_}\n"
        f"🔢 حداکثر تعداد: {counts_}\n\n"
        f"♾️ بازه روز نامحدود: {unl_days}",
        reply_markup=cfg_manage_kb(row.plan))


async def _kb_vpn_panels(msg, row, bot, state):
    from tenant.bots.vpn.vpn_admin import get_all_panels, _panels_info_text, menu_kb
    panels = await get_all_panels(row.id)
    panels_info = await _panels_info_text(panels)
    if panels:
        intro = f"🛰 <b>VPN خودکار (چند پنل)</b>\n\n{panels_info}\n\nبرای مدیریت هر پنل روی دکمه‌اش بزن:"
    else:
        intro = ("🛰 <b>VPN خودکار (چند پنل)</b>\n\nهنوز هیچ پنلی متصل نشده.\n"
                 "از دکمه‌های زیر یک پنل انتخاب و اطلاعاتش را وارد کن:")
    await msg.answer(intro, reply_markup=await menu_kb(panels, row.id))


async def kb_products_guide(msg, row, bot, state):
    """«📘 راهنما» — مشترکِ vpn_sales و virtual_service."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
    from tenant import _products_help_text
    await msg.answer(_products_help_text(row.field), parse_mode="HTML",
                     reply_markup=ReplyKeyboardMarkup(
                         keyboard=[[KeyboardButton(text="⬅️ بازگشت به سرویس‌ها و محصولات")]],
                         resize_keyboard=True))


register_products_menu("vpn_sales", ProductsMenu(
    opts=_opts,
    description="مدیریت کانفیگ‌ها، پنل VPN و آموزش اتصال:",
    styles={
        "tadm:cfg": "success", "tvpn:menu": "primary", "tadm:products:guide": "primary",
        "pnlmkt:menu": "success", "pyar:menu": "success",
    },
    keyboard={
        "🏷 ثبت و فروش پنل": _kb_pnlmkt,
        "🗂 پنل‌یار": _kb_panelyar,
        "📦 انبار کانفیگ‌های دستی": _kb_cfg_inventory,
        "🤖 پنل VPN خودکار (X-UI)": _kb_vpn_panels,
        "📘 راهنما": kb_products_guide,
    },
))
