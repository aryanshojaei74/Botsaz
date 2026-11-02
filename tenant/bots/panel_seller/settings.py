"""tenant/bots/panel_seller/settings.py — آیتم‌هایِ منویِ تنظیماتِ مخصوصِ رباتِ «🧩 پنل‌فروشی» (PATCH_PSEL_PARITY_V2).

درخواستِ پشتیبانی: قابلیت‌هایِ رباتِ فروشِ VPN در پنل‌فروشی هم باشد. «انتقالِ موجودی بینِ کاربران» از قبل در
کیف‌پولِ کاربر کار می‌کرد ولی مالکِ پنل‌فروشی هیچ دکمه‌ای برایِ روشن‌کردنش نداشت (پیش‌فرض خاموش).
"""
from __future__ import annotations

from tenant.settings_registry import SettingsItem, register_settings_items, section_key

KIND = "panel_seller"


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
