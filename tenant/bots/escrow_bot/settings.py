"""تنظیماتِ مخصوصِ رباتِ واسطه‌گری — PATCH_ESCROW_BOT_KIND_V1."""
from __future__ import annotations

from tenant.settings_registry import SettingsItem, register_settings_items

KIND = "escrow_bot"


def _has(feature_name: str):
    def _check(plan: str) -> bool:
        from plans import Feature, has_feature
        return has_feature(plan, getattr(Feature, feature_name))
    return _check


async def _escrow_admin(cb, bot, state):
    from tenant.shared.escrow import admin_menu     # لِیزی تا import دایره‌ای پیش نیاید
    await admin_menu(cb, bot, state)


register_settings_items(KIND, [
    SettingsItem(
        label="🤝 واسطه‌گری (معامله با واسطه)", callback="set:escrow",
        handler=_escrow_admin, needs_state=True,
        visible=_has("ESCROW"),
        after=("set:welcome",),
    ),
])
