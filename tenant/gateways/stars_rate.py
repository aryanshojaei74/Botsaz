"""
stars_rate.py — PATCH_AUTO_RATE_V1

نرخِ لحظه‌ایِ استارز و دلار برایِ حالتِ «نرخ خودکار».
منبعِ دلار: get_usd_to_toman_rate (والکس → بایننس P2P → …).
قیمتِ پایهٔ هر استارز به دلار از Fragment؛ در .env قابل تغییر است.
"""
from __future__ import annotations

import os
import time

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

STAR_USD = float(os.getenv("STAR_USD_PRICE", "0.015"))
_cache: dict[str, tuple[float, float]] = {}
_TTL = 300.0


async def usd_toman() -> float:
    """نرخِ دلار به تومان؛ صفر یعنی هیچ منبعی جواب نداد."""
    c = _cache.get("usd")
    if c and (time.time() - c[1]) < _TTL:
        return c[0]
    try:
        from tenant.gateways.crypto_pay import get_usd_to_toman_rate
        r = float(await get_usd_to_toman_rate() or 0)
    except Exception:
        logger.exception("stars_rate: usd fetch failed")
        r = 0.0
    if r > 0:
        _cache["usd"] = (r, time.time())
    return r


async def star_toman(margin_pct: float = 0.0, bot_id: int | None = None) -> float:
    """قیمتِ هر استارز به تومان با درصدِ سود. صفر = ناموفق.

    اگر bot_id داده شود، «قیمتِ پایهٔ ⭐ به دلار» (stars_base_usd) و «نرخِ
    دستیِ دلار» (stars_usd_toman) همان ربات، در صورتِ تنظیم، جایگزینِ
    پیش‌فرض‌ها می‌شوند.
    """
    base_usd = STAR_USD
    usd = 0.0
    if bot_id is not None:
        from tenant import get_setting
        try:
            b = float(await get_setting(bot_id, "stars_base_usd", "0") or 0)
            if b > 0:
                base_usd = b
        except Exception:
            logger.debug("stars_base_usd نامعتبر است؛ نرخِ پیش‌فرض", exc_info=True)
        try:
            usd = float(await get_setting(bot_id, "stars_usd_toman", "0") or 0)
        except Exception:
            usd = 0.0
    if usd <= 0:
        usd = await usd_toman()
    if usd <= 0:
        return 0.0
    return base_usd * usd * (1.0 + margin_pct / 100.0)


async def auto_on(bot_id: int, key: str) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, key, "0")) == "1"


async def margin_of(bot_id: int, key: str, default: str = "0") -> float:
    from tenant import get_setting
    try:
        return float(await get_setting(bot_id, key, default) or default)
    except Exception:
        return 0.0


# === PATCH_AUTO_RATE_TON_V1 ===
# درخواستِ ادمین: «استارز نرخِ خودکار داره، تون هم داشته باشه».
#
# زیرساختش از قبل بود و فقط به هم وصل نشده بود:
#   • `ton_direct.ton_usd_price()` قیمتِ لحظه‌ایِ تون به دلار را می‌دهد
#     (با کشِ داخلیِ خودش) — برایِ پرداختِ مستقیمِ تون استفاده می‌شد.
#   • `usd_toman()` بالا، نرخِ دلار به تومان را می‌دهد.
# ضربِ این دو، به‌علاوهٔ درصدِ سود، همان چیزی است که برایِ استارز داریم.
async def ton_toman(margin_pct: float = 0.0) -> float:
    """قیمتِ هر ۱ تون به تومان با درصدِ سود. صفر = ناموفق."""
    usd = await usd_toman()
    if usd <= 0:
        return 0.0
    try:
        from tenant.gateways.ton_direct import ton_usd_price
        ton_usd = float(await ton_usd_price() or 0)
    except Exception:
        logger.exception("stars_rate: قیمتِ تون گرفته نشد")
        return 0.0
    if ton_usd <= 0:
        return 0.0
    return ton_usd * usd * (1.0 + margin_pct / 100.0)
