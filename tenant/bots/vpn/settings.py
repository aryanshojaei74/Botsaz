"""
tenant/bots/vpn/settings.py — آیتم‌هایِ منویِ تنظیماتِ *مخصوصِ* رباتِ فروشِ VPN.

=== PATCH_SETTINGS_REGISTRY_V2_VPN ===
قبلاً این هفت گزینه با `if _is_vpn` داخلِ tenant/__init__.py (منویِ مشترک) ساخته
می‌شدند و دیسپچرِ حالتِ کیبوردی هم برایشان سه جایِ جدا کد داشت. حالا هر
گزینه یک‌بار، با هندلرِ حالتِ کیبوردی‌اش، این‌جا ثبت می‌شود.

callback_dataها دقیقاً همانِ قبلی‌اند. هندلرها lazy import می‌شوند.
نکته: BTN_CFG_GUIDE در tenant/__init__.py تعریف می‌شود (بعد از import این
ماژول)، پس متنش این‌جا تکرار شده و تست (tests/test_settings_registry.py)
برابری‌شان را چک می‌کند.
"""
from __future__ import annotations

from tenant.settings_registry import SettingsItem, register_settings_items, section_key

KIND = "vpn_sales"

# باید دقیقاً با tenant/__init__.py: BTN_CFG_GUIDE یکی باشد (تست دارد)
_CFG_GUIDE_LABEL = "📖 دکمه آموزش اتصال چسبیده به کانفیگ خریداری‌شده"
_BUY_ONHOLD_PREFIX = "⏳ شروعِ زمانِ اشتراکِ فروشی"


def _has(feature_name: str):
    def _check(plan: str) -> bool:
        from plans import Feature, has_feature
        return has_feature(plan, getattr(Feature, feature_name))
    return _check


def _plan_in(*plans: str):
    return lambda plan: plan in plans


async def _buy_onhold_label(bot_id: int) -> str:
    from tenant import get_setting
    on = await get_setting(bot_id, "purchase_on_hold", "0") == "1"
    return _BUY_ONHOLD_PREFIX + ": " + ("⏸ از اولین اتصال" if on else "▶️ از لحظهٔ خرید")


async def _edit_guides(cb, bot):
    from tenant import tadm_edit_guides
    await tadm_edit_guides(cb, bot)


async def _cfg_guide(cb, bot):
    from tenant import tadm_cfgguide
    await tadm_cfgguide(cb, bot)


async def _cfg_name_prompt(cb, bot, state):
    from tenant import tadm_cfg_name_prompt
    await tadm_cfg_name_prompt(cb, bot, state)


async def _trial_menu(cb, bot):
    from tenant import set_trial_menu
    await set_trial_menu(cb, bot)


async def _stock_check(cb, bot):
    from tenant.shared.stock_alarm import stk_menu
    await stk_menu(cb, bot)


async def _buy_onhold(cb, bot):
    from tenant import set_buy_onhold
    await set_buy_onhold(cb, bot)


async def _qr_background(cb, bot, state):
    from tenant.bots.vpn.vpn_admin import qrbg_menu
    await qrbg_menu(cb, bot, state)


async def _group_settings(cb, bot):
    from tenant.shared.group_wallet import tadm_groupsettings_show
    await tadm_groupsettings_show(cb, bot)


async def _refund_cfg(cb, bot, state):
    from tenant.bots.vpn.vpn_admin import vpn_refund_cfg
    await vpn_refund_cfg(cb, bot, state)


async def _page_titles(cb, bot, state):
    from tenant.bots.vpn.vpn_admin import vpn_titles_menu
    await vpn_titles_menu(cb, bot, state)


register_settings_items(KIND, [
    # === PATCH_VPN_TITLES_IN_SETTINGS_V1 ===
    # گزارشِ ادمین: «گزینهٔ ویرایشِ تیترِ صفحه‌ها را پیدا نکردم». این گزینه فقط در «هابِ
    # تنظیماتِ عمومیِ» رباتِ همکار بود و مالکِ رباتِ عادی هیچ‌جا نمی‌دیدش.
    SettingsItem(
        label="✏️ تیترِ صفحه‌هایِ خرید و سرویس‌ها", callback="vtl:menu",
        handler=_page_titles, needs_state=True,
        after=("tadm:cfgnameprompt", "tadm:cfgguide", "tadm:edit_guides"),
    ),
    SettingsItem(
        label="💸 برگشت وجه سرویس", callback="vrf:cfg",
        handler=_refund_cfg, needs_state=True,
        after=("vtl:menu",),
    ),
    SettingsItem(
        label="✏️ ویرایش متن آموزش اتصال", callback="tadm:edit_guides",
        handler=_edit_guides,
        after=("tadm:btn_colors", "tadm:homebtns"),
    ),
    SettingsItem(
        label=_CFG_GUIDE_LABEL, callback="tadm:cfgguide",
        handler=_cfg_guide,
        after=("tadm:edit_guides",),
    ),
    SettingsItem(
        label="✏️ متنِ «نامِ دلخواهِ کانفیگ»", callback="tadm:cfgnameprompt",
        handler=_cfg_name_prompt, needs_state=True,
        after=("tadm:cfgguide",),
    ),
    SettingsItem(
        label="🩺 بررسی باگ و انبار", callback="stk:menu",
        handler=_stock_check,
        after=("vrf:cfg", "vtl:menu", "miniapp:panel", "tadm:groupsettings"),
    ),
    SettingsItem(
        label="🎁 تنظیماتِ تست رایگان", callback="set:trial",
        handler=_trial_menu,
        after=("tadm:cfgnameprompt",),
    ),
    SettingsItem(
        label=_BUY_ONHOLD_PREFIX + ": ▶️ از لحظهٔ خرید", callback="set:buy_onhold",
        handler=_buy_onhold,
        label_fn=_buy_onhold_label, label_prefix=_BUY_ONHOLD_PREFIX,
        after=("gwname:menu", "set:trial"),
    ),
    SettingsItem(
        label="🖼 عکسِ پشتِ بارکد (QR)", callback="qrbg:menu",
        handler=_qr_background, needs_state=True,
        visible=_has("QR_BACKGROUND"),
        after=("dga:menu", "set:productrating", "rpt:menu", "set:ref_reward",
               "vso:menu", "set:startreact", "set:welcome", "set:buy_onhold", "gwname:menu"),
    ),
    SettingsItem(
        label="⚙️ تنظیماتِ گروه", callback="tadm:groupsettings",
        handler=_group_settings,
        visible=_plan_in("growth", "pro"),
        after=("tadm:backup:menu", "set:homemenutext"),
    ),
])


# ───────────── بخشِ «👮 ادمین‌ها و پشتیبانی» — فقط VPN ─────────────
# === PATCH_SETTINGS_REGISTRY_V5_SECTIONS ===
# قبلاً `if field == "vpn_sales" and ok(RESELLER_TIERS)` داخلِ _sec_admins_kb بود.
# درست بعد از «عضویت اجباری» می‌نشیند، *قبل* از «ارسال پیام همگانی/نظرسنجی‌ها»
# (چیدمانِ آن منو به این بسته است که دو گزینهٔ آخر همان دو باشند).

async def _reseller(cb, bot):
    from tenant.shared.reseller import rsl_menu
    await rsl_menu(cb, bot)


register_settings_items(section_key("admins", KIND), [
    SettingsItem(
        label="🏷 نمایندگی خودکار", callback="rsl:menu",
        handler=_reseller,
        visible=_has("RESELLER_TIERS"),
        after=("tadm:forcejoin",),
    ),
])


# ───────────── هابِ «تنظیماتِ عمومیِ ربات» (همکار) — فقط VPN ─────────────
# === PATCH_SETTINGS_REGISTRY_V6_HOME_HUB ===
# قبلاً `if _hbrow.field == "vpn_sales"` داخلِ _tn_general_hub_kb بود (PATCH_VPN_EDITABLE_TITLES_V1).
# این هاب فقط شیشه‌ای است؛ پس handlerِ حالتِ کیبوردی لازم نیست (خودِ vtl:menu در vpn_admin.py).
register_settings_items(section_key("hub", KIND), [
    SettingsItem(
        label="✏️ تیترِ صفحه‌هایِ خرید و سرویس‌ها", callback="vtl:menu",
        style="primary",
    ),
])


# === PATCH_BALANCE_TRANSFER_V1 === انتقالِ موجودی بینِ کاربران (مالی و کیف پول)
async def _balance_transfer(cb, bot):
    from tenant.shared.balance_transfer import admin_menu
    await admin_menu(cb, bot)


register_settings_items(section_key("finance", KIND), [
    SettingsItem(
        label="🔁 انتقال موجودی بینِ کاربران", callback="btr:admin",
        handler=_balance_transfer,
        after=("set:swap", "set:crypto", "set:kyc", "set:txlog", "tadm:receiptchannel"),
    ),
])
