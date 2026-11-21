"""crypto_exempt.py — «ارز دیجیتال بدونِ سقف و احرازِ هویت» (تنظیمِ هر ربات؛ پیش‌فرض خاموش)."""
from __future__ import annotations

KEY = "crypto_no_limits"
CRYPTO_GATEWAYS = ("crypto", "swap")
CRYPTO_KINDS = ("swap_charge", "crypto_charge", "ton_direct", "tron_direct")


async def is_exempt(bot_id: int) -> bool:
    try:
        from tenant import get_setting
        return (await get_setting(int(bot_id), KEY, "0")) == "1"
    except Exception:
        return False
