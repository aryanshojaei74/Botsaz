"""
tenant/bots/virtual_service/settings.py — آیتم‌هایِ منویِ تنظیماتِ *مخصوصِ* رباتِ خدماتِ مجازی.

=== PATCH_SETTINGS_REGISTRY_V1 ===
قبلاً این چهار گزینه با `if _is_virtual` داخلِ tenant/__init__.py (منویِ
مشترکِ همهٔ انواعِ ربات) ساخته می‌شدند. حالا این‌جا زندگی می‌کنند:
اگر فردا گزینه‌ای برایِ خدماتِ مجازی خواستید، فقط همین فایل را عوض کنید.

هندلرها lazy import می‌شوند (الگوی معمولِ پروژه) تا چرخهٔ import پیش نیاید.
callback_dataها دقیقاً همانِ قبلی‌اند — کیبوردهایِ باز در چتِ کاربران کار می‌کنند.
"""
from __future__ import annotations

from tenant.settings_registry import SettingsItem, register_settings_items, section_key

KIND = "virtual_service"


def _has(feature_name: str):
    """(plan) -> bool با Feature.<feature_name>؛ lazy تا import سبک بماند."""
    def _check(plan: str) -> bool:
        from plans import Feature, has_feature
        return has_feature(plan, getattr(Feature, feature_name))
    return _check


async def _profile_footer(cb, bot, state):
    # هندلرِ اصلی مشترک است (هابِ همکار هم از آن استفاده می‌کند)
    from tenant import set_profile_footer_start
    await set_profile_footer_start(cb, bot, state)


async def _api_key(cb, bot):
    from tenant.gateways.custom_domain import tadm_apikey_show
    await tadm_apikey_show(cb, bot)


async def _group_settings(cb, bot):
    from tenant.shared.group_wallet import tadm_groupsettings_show
    await tadm_groupsettings_show(cb, bot)


async def _nft_miniapp(cb, bot):
    # لِیزی تا circular import پیش نیاید (miniapp_admin خودش از tenant import می‌کند)
    from tenant.shared.miniapp_admin import miniapp_nft_entry
    await miniapp_nft_entry(cb, bot)


register_settings_items(KIND, [
    # درخواستِ آقایِ فدایی: متنِ اختصاصیِ پایینِ «مشخصاتِ شما»
    SettingsItem(
        label="👤 متنِ اختصاصیِ «مشخصاتِ شما»",
        callback="set:profile_footer",
        handler=_profile_footer, needs_state=True,
        after=("set:homemenutext",),
    ),
    SettingsItem(
        label="🔑 API ربات",
        callback="tadm:apikey",
        handler=_api_key, owner_only=True,
        deny_text="🔒 مدیریت API ربات فقط برای مالک ربات در دسترس است.",
        after=("set:profile_footer", "set:homemenutext"),
    ),
    SettingsItem(
        label="⚙️ تنظیماتِ گروه",
        callback="tadm:groupsettings",
        handler=_group_settings,
        after=("tadm:backup:menu", "tadm:apikey", "set:profile_footer", "set:homemenutext"),
    ),
    SettingsItem(
        label="🖼 تنظیماتِ مینی‌اپ حرفه‌ای",
        callback="miniapp:nft",
        handler=_nft_miniapp,
        visible=_has("NFT_MARKET"),
        # دقیقاً زیرِ دکمهٔ مینی‌اپ (چیدمانِ منو هم همین ترتیب را تضمین می‌کند)
        after=("miniapp:panel", "tadm:groupsettings"),
    ),
])


# ───────────── بخشِ «💰 مالی و کیف پول» — فقط خدماتِ مجازی ─────────────
# === PATCH_SETTINGS_REGISTRY_V5_SECTIONS ===
# قبلاً `if field == "virtual_service"` داخلِ _sec_finance_opts بود. ترتیبِ قبلی با
# anchor حفظ می‌شود (بعد از آخرین گزینهٔ موجود از میانِ گزینه‌هایِ قبلی‌اش).

async def _smm_order_channel(cb, bot):
    from tenant.gateways.receipt_channel import smm_order_channel_menu
    await smm_order_channel_menu(cb, bot)


async def _hesabpay(cb, bot):
    from tenant.gateways.hesabpay import hesabpay_admin_start
    await hesabpay_admin_start(cb, bot)


register_settings_items(section_key("finance", KIND), [
    # === PATCH_SMM_ORDER_CHANNEL_V1 === در بقیهٔ انواع دیده نمی‌شود
    SettingsItem(
        label="🛰 متصل‌کردنِ کانالِ انجامِ سفارشات", callback="tadm:smmorderchannel",
        handler=_smm_order_channel,
        after=("set:abangateway", "set:uniquepay", "set:apay", "set:kyc", "set:txlog", "tadm:receiptchannel"),
    ),
    SettingsItem(
        label="🇦🇫 درگاه حساب‌پی (پرداخت افغانی)", callback="set:hesabpay",
        handler=_hesabpay,
        visible=_has("GATEWAY_HESABPAY"),
        after=("set:crypto", "set:toggle_payphone", "set:phonelog",
               "set:wallet_receipt_buttons", "tadm:smmorderchannel"),
    ),
])


# ───────────── خانهٔ پنلِ مدیریت — فقط خدماتِ مجازی ─────────────
# === PATCH_SETTINGS_REGISTRY_V6_HOME_HUB ===
# قبلاً `if bot_field == "virtual_service"` داخلِ tadmin_kb (هر دو حالتِ کیبوردی و شیشه‌ای)
# و یک بلوکِ جدا در tenant_buttons بود. محتوایِ «راهنما و دستیار» درباره‌ٔ سرویس‌ها و
# سفارش‌هایِ SMM است و در VPN/فروشگاه بی‌ربط بود (PATCH_HELP_ONLY_VIRTUAL_SERVICE_V1).
# نکته: در حالتِ کیبوردی، ادمین/مالک شدن قبل از اجرا چک می‌شود (قبلاً هر کاربرِ ربات با
# تایپِ این متن می‌توانست منو را باز کند).

async def _help_assistant(cb, bot, state):
    from tenant.shared.help_ai import hlp_menu
    await hlp_menu(cb, state)


register_settings_items(section_key("home", KIND), [
    SettingsItem(
        label="📘 راهنما و دستیارِ هوشمند", callback="hlp:menu",
        handler=_help_assistant, needs_state=True,
        label_prefix="📘 راهنما و دستیار",   # کیبوردِ قدیمیِ گوشیِ ادمین هم کار کند
        style="success",
    ),
])
