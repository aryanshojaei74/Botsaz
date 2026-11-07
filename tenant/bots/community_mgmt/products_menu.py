"""
tenant/bots/community_mgmt/products_menu.py — منویِ «📦 سرویس‌ها و محصولات» برایِ مدیریتِ گروه/کانال.

=== PATCH_SETTINGS_REGISTRY_V3_PRODUCTS ===
قبلاً `if bot_field == "community_mgmt"` داخلِ _sec_products_kb بود
(PATCH_COMMUNITY_MGMT_V1 / PATCH_GROUP_AI_COMMUNITY_MGMT_V1).
"""
from __future__ import annotations

from plans import Feature, has_feature
from tenant.settings_registry import ProductsMenu, register_products_menu


async def _opts(bot_id: int, plan: str) -> list[tuple[str, str]]:
    def ok(f): return has_feature(plan, f)
    opts: list[tuple[str, str]] = []
    if ok(Feature.COMMUNITY_MGMT):
        opts.append(("🛡 مدیریت گروه و کانال", "cmty:menu"))
        if ok(Feature.SCHEDULED_BROADCAST):
            opts.append(("📅 زمان‌بندیِ پست", "adb:menu"))
        if ok(Feature.POLL):
            opts.append(("📋 نظرسنجی‌ها", "tadm:polls"))
        # === PATCH_GROUP_AI_COMMUNITY_MGMT_V1 ===
        if ok(Feature.GROUP_AI_ASSISTANT):
            opts.append(("🤖 دستیارِ هوشِ مصنوعیِ گروه", "smmai:menu:cmty"))
    return opts


# ───────────── حالتِ منویِ کیبوردی (PATCH_SETTINGS_REGISTRY_V4_PRODUCTS_KB) ─────────────
# === PATCH_COMMUNITY_MGMT_KEYBOARD_MODE_V1 === (قبلاً در زنجیرهٔ مرکزی بود)

async def _kb_cmty(msg, row, bot, state):
    from tenant import _FakeCb
    from tenant.bots.community_mgmt.handlers import cmty_menu  # === PATCH_TENANT_SPLIT_PHASE1_COMMUNITY_V1 ===
    await cmty_menu(_FakeCb(msg), bot, state)


async def _kb_schedule_post(msg, row, bot, state):
    from tenant.shared.ad_broadcast import _render_panel
    panel_text, panel_kb = await _render_panel(row.id)
    await msg.answer(panel_text, reply_markup=panel_kb, parse_mode="HTML",
                     disable_web_page_preview=True)


async def _kb_group_ai(msg, row, bot, state):
    """=== PATCH_SETTINGS_REGISTRY_V4_PRODUCTS_KB ===
    قبلاً «🤖 دستیارِ هوشِ مصنوعیِ گروه» در حالتِ کیبوردی هیچ هندلری نداشت
    (دکمهٔ مرده)؛ همان صفحهٔ اینلاین (smmai:menu:cmty) را باز می‌کند."""
    from tenant import _FakeCb
    from tenant.bots.virtual_service.smm_ai import smmai_menu
    await smmai_menu(_FakeCb(msg, "smmai:menu:cmty"), bot)


register_products_menu("community_mgmt", ProductsMenu(
    opts=_opts,
    # توضیحِ اختصاصی نداشت؛ متنِ عمومیِ پیش‌فرض نشان داده می‌شود (description خالی)
    description="",
    styles={"cmty:menu": "success", "adb:menu": "primary", "tadm:polls": "success"},
    keyboard={
        "🛡 مدیریت گروه و کانال": _kb_cmty,
        "📅 زمان‌بندیِ پست": _kb_schedule_post,
        "🤖 دستیارِ هوشِ مصنوعیِ گروه": _kb_group_ai,
    },
    # «📋 نظرسنجی‌ها» همان برچسبِ منویِ «ادمین‌ها» برایِ همهٔ انواع است و هندلرش
    # مشترک، در زنجیرهٔ مرکزی می‌ماند.
    keyboard_shared=("📋 نظرسنجی‌ها",),
))
