"""
tenant/gateways/wallet_limits.py — حداقل و حداکثرِ واریزِ هر درگاهِ شارژِ کیف‌پول.

=== PATCH_WALLET_LIMITS_V1 ===
درخواستِ ادمین (فدایی): «گزینهٔ حداقل واریز و حداکثر واریز برای هر درگاه نیست که بتونیم تنظیم کنیم».

وضعیتِ قبل: بعضی درگاه‌ها تنظیمِ خودشان را داشتند (آبان‌گیت‌وی، یونیک‌پی، سواپ، پرداختِ مستقیمِ کوین،
حداقلِ تون‌پیز) و بقیه (کارت‌به‌کارت، زرین‌پال، زیبال، حساب‌پی، ارز دیجیتال) حداقل را ثابت داخلِ کد
داشتند و حداکثر اصلاً نداشتند.

این ماژول یک لایهٔ مشترک است:
  • برایِ درگاه‌هایی که از قبل کلیدِ تنظیمات دارند، *همان کلیدها* خوانده/نوشته می‌شوند (دو منبعِ حقیقت
    ساخته نمی‌شود، و صفحهٔ خودِ آن درگاه‌ها هم هم‌چنان درست کار می‌کند).
  • برایِ بقیه کلیدهایِ تازهٔ wl_min_<درگاه> / wl_max_<درگاه> اضافه می‌شود. پیش‌فرضِ حداقل دقیقاً
    همان عددِ ثابتِ قبلی است و پیش‌فرضِ حداکثر «بدونِ سقف»، پس تا وقتی ادمین چیزی تنظیم نکند هیچ رفتاری
    عوض نمی‌شود.
  • تنها یک تابعِ بررسی (`check`) که هر درگاه پیش از ساختِ فاکتور صدا می‌زند.

واحد: تومان. حداقلِ ۰/خالی = پیش‌فرض؛ حداکثرِ ۰ = بدونِ سقف.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)


@dataclass(frozen=True)
class Gateway:
    key: str
    label: str
    min_key: str
    max_key: str
    default_min: int          # همان عددِ ثابتِ قبلیِ کد (رفتار بدونِ تغییر)
    floor_min: int = 1        # پایین‌ترین حداقلی که ادمین مجاز است بگذارد (کفِ فنیِ خودِ درگاه)
    note: str = ""            # توضیحِ کوتاه در صفحهٔ درگاه
    enabled: Optional[Callable[[int], Awaitable[bool]]] = None


async def _s(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _enabled_card(bot_id: int) -> bool:
    return bool(await _s(bot_id, "card_number", "")) and (await _s(bot_id, "card_enabled", "1")) == "1"


async def _enabled_zarinpal(bot_id: int) -> bool:
    return (await _s(bot_id, "zarinpal_enabled", "0")) == "1" and bool(await _s(bot_id, "zarinpal_merchant_id", ""))


async def _enabled_zibal(bot_id: int) -> bool:
    return (await _s(bot_id, "zibal_enabled", "0")) == "1" and bool(await _s(bot_id, "zibal_merchant_id", ""))


async def _enabled_hesabpay(bot_id: int) -> bool:
    from tenant.gateways.hesabpay import is_available
    return bool(await is_available(bot_id))


async def _enabled_tonpays(bot_id: int) -> bool:
    from tenant.gateways.tonpays import is_available
    return bool(await is_available(bot_id))


async def _enabled_uniquepay(bot_id: int) -> bool:
    from tenant.gateways.uniquepay import is_available
    return bool(await is_available(bot_id))


async def _enabled_apay(bot_id: int) -> bool:
    from tenant.gateways.apay import is_available
    return bool(await is_available(bot_id))


async def _enabled_abangateway(bot_id: int) -> bool:
    from tenant.gateways.abangateway import is_available
    return bool(await is_available(bot_id))


async def _enabled_hooshpay(bot_id: int) -> bool:
    from tenant.gateways.hooshpay import is_available
    return bool(await is_available(bot_id))


async def _enabled_swap(bot_id: int) -> bool:
    return bool(await _s(bot_id, "swap_api_key", "")) and bool(
        await _s(bot_id, "swap_addr_usdt", "") or await _s(bot_id, "swap_addr_ton", "")
        or await _s(bot_id, "swap_addr_trx", ""))


async def _enabled_crypto(bot_id: int) -> bool:
    return bool(await _s(bot_id, "nowpay_api_key", "")) and (await _s(bot_id, "nowpay_enabled", "1")) == "1"


# ترتیبِ این فهرست = ترتیبِ نمایش در صفحهٔ تنظیم
GATEWAYS: dict[str, Gateway] = {g.key: g for g in (
    Gateway("card", "💳 کارت‌به‌کارت", "wl_min_card", "wl_max_card", 10_000, floor_min=1_000,
            enabled=_enabled_card),
    Gateway("zarinpal", "🔐 زرین‌پال", "wl_min_zarinpal", "wl_max_zarinpal", 20_000, floor_min=1_000,
            note="کفِ فنیِ زرین‌پال ۱٬۰۰۰ تومان است.", enabled=_enabled_zarinpal),
    Gateway("zibal", "🔐 زیبال", "wl_min_zibal", "wl_max_zibal", 10_000, floor_min=1_000,
            note="کفِ فنیِ زیبال ۱٬۰۰۰ تومان است.", enabled=_enabled_zibal),
    Gateway("hesabpay", "🇦🇫 حساب‌پی", "wl_min_hesabpay", "wl_max_hesabpay", 1_000, floor_min=1,
            enabled=_enabled_hesabpay),
    # ── درگاه‌هایی که از قبل کلیدِ خودشان را دارند: همان کلیدها ──
    Gateway("tonpays", "💠 تون‌پیز", "tonpays_min_amount", "wl_max_tonpays", 20_000,
            floor_min=1_000, enabled=_enabled_tonpays),
    # پیش‌فرضِ حداقلِ یونیک‌پی در خودِ درگاه ۵۱٬۰۰۰ است (MIN_AMOUNT)، نه ۱۰٬۰۰۰
    Gateway("uniquepay", "💳 یونیک‌پی", "uniquepay_min_amount", "uniquepay_max_amount", 51_000,
            floor_min=1_000, enabled=_enabled_uniquepay),
    Gateway("apay", "💳 آپی (کارت‌به‌کارت آنی)", "apay_min_amount", "apay_max_amount", 10_000,
            floor_min=1_000, note="کفِ فنیِ آپی ۱٬۰۰۰ و سقفش ۱۰۰ میلیون تومان است.", enabled=_enabled_apay),
    Gateway("abangateway", "💳 آبان گیت‌وی", "abangateway_min_amount", "abangateway_max_amount", 10_000,
            floor_min=1_000, enabled=_enabled_abangateway),
    Gateway("hooshpay", "💳 هوش‌پی", "hooshpay_min_amount", "hooshpay_max_amount", 10_000,
            floor_min=1_000, note="کفِ فنیِ هوش‌پی ۱٬۰۰۰ تومان است.", enabled=_enabled_hooshpay),
    Gateway("swap", "🟦 سواپ‌ولت", "swap_min_toman", "swap_max_toman", 10_000,
            floor_min=1_000, enabled=_enabled_swap),
    Gateway("crypto", "🪙 ارز دیجیتال (ورودیِ تومان)", "wl_min_crypto", "wl_max_crypto", 100_000,
            floor_min=10_000, note="این حد فقط برایِ ورودِ مبلغ به «تومان» است.", enabled=_enabled_crypto),
)}


def _to_int(v) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


async def get_limits(bot_id: int, gw_key: str) -> tuple[int, int]:
    """(حداقل، حداکثر) مؤثر برایِ این ربات. حداکثر ۰ = بدونِ سقف."""
    g = GATEWAYS[gw_key]
    mn = _to_int(await _s(bot_id, g.min_key, ""))
    if mn <= 0:
        mn = g.default_min
    mx = _to_int(await _s(bot_id, g.max_key, "0"))
    if mx < 0:
        mx = 0
    if mx and mx < mn:
        # ناسازگاریِ دست‌ساز (مثلاً از صفحهٔ قدیمیِ یک درگاه): سقفِ کمتر از کف معنی ندارد؛ بی‌اثرش می‌کنیم
        # تا همهٔ واریزها مسدود نشوند، و در لاگ می‌آید تا ادمین ببیند.
        logger.warning("wallet_limits[%s/%s]: حداکثر (%s) کمتر از حداقل (%s) است؛ سقف نادیده گرفته شد",
                       bot_id, gw_key, mx, mn)
        mx = 0
    return mn, mx


async def check(bot_id: int, gw_key: str, amount) -> Optional[str]:
    """None اگر مبلغ مجاز است؛ وگرنه متنِ خطایِ آمادهٔ نمایش به کاربر."""
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return "❌ مبلغ نامعتبر است."
    mn, mx = await get_limits(bot_id, gw_key)
    if amount < mn:
        return f"❌ حداقلِ مبلغِ شارژ با این درگاه <b>{mn:,} تومان</b> است."
    if mx and amount > mx:
        return f"❌ حداکثرِ مبلغِ شارژ با این درگاه <b>{mx:,} تومان</b> است."
    return None


async def range_hint(bot_id: int, gw_key: str) -> str:
    mn, mx = await get_limits(bot_id, gw_key)
    return f"حداقل {mn:,}" + (f" و حداکثر {mx:,}" if mx else "") + " تومان"


async def allowed_presets(bot_id: int, gw_key: str, presets) -> list[int]:
    """دکمه‌هایِ مبلغِ ثابت را به بازهٔ مجاز محدود می‌کند (دکمهٔ غیرمجاز اصلاً نشان داده نمی‌شود)."""
    mn, mx = await get_limits(bot_id, gw_key)
    return [p for p in presets if p >= mn and (not mx or p <= mx)]


# ─────────────────────────── تنظیم (ادمین) ───────────────────────────

class LimitError(Exception):
    """مقدارِ واردشده مجاز نیست؛ پیامش برایِ ادمین قابلِ‌نمایش است."""


async def set_min(bot_id: int, gw_key: str, value) -> int:
    """حداقل را ذخیره می‌کند؛ 0 = برگشت به پیش‌فرض. مقدارِ مؤثرِ جدید را برمی‌گرداند."""
    from tenant import set_setting
    g = GATEWAYS[gw_key]
    n = _to_int(value)
    if n < 0:
        raise LimitError("عدد نمی‌تواند منفی باشد.")
    if n == 0:
        await set_setting(bot_id, g.min_key, "")
        return (await get_limits(bot_id, gw_key))[0]
    if n < g.floor_min:
        raise LimitError(f"حداقل نمی‌تواند از {g.floor_min:,} تومان کمتر باشد"
                         + (f" ({g.note})" if g.note else "") + ".")
    _, mx = await get_limits(bot_id, gw_key)
    if mx and n > mx:
        raise LimitError(f"حداقل ({n:,}) نمی‌تواند از حداکثرِ فعلی ({mx:,}) بیشتر باشد. "
                         "اول حداکثر را بالا ببر یا بردار.")
    await set_setting(bot_id, g.min_key, str(n))
    return n


async def set_max(bot_id: int, gw_key: str, value) -> int:
    """حداکثر را ذخیره می‌کند؛ 0 = بدونِ سقف. مقدارِ جدید را برمی‌گرداند."""
    from tenant import set_setting
    g = GATEWAYS[gw_key]
    n = _to_int(value)
    if n < 0:
        raise LimitError("عدد نمی‌تواند منفی باشد.")
    if n == 0:
        await set_setting(bot_id, g.max_key, "0")
        return 0
    mn, _ = await get_limits(bot_id, gw_key)
    if n < mn:
        raise LimitError(f"حداکثر ({n:,}) نمی‌تواند از حداقلِ فعلی ({mn:,}) کمتر باشد. "
                         "اول حداقل را پایین بیاور.")
    await set_setting(bot_id, g.max_key, str(n))
    return n


async def reset(bot_id: int, gw_key: str) -> None:
    """هر دو را به پیش‌فرض برمی‌گرداند (حداقلِ قبلیِ کد، بدونِ سقف)."""
    from tenant import set_setting
    g = GATEWAYS[gw_key]
    await set_setting(bot_id, g.min_key, "")
    await set_setting(bot_id, g.max_key, "0")


async def active_gateways(bot_id: int) -> list[Gateway]:
    """درگاه‌هایی که همین حالا برایِ این ربات فعال‌اند. خطا در تشخیصِ یکی، بقیه را نمی‌اندازد."""
    out = []
    for g in GATEWAYS.values():
        try:
            if g.enabled is None or await g.enabled(bot_id):
                out.append(g)
        except Exception:
            logger.exception("wallet_limits: تشخیصِ فعال‌بودنِ %s ناموفق بود", g.key)
    return out


async def customized(bot_id: int, gw_key: str) -> bool:
    """آیا ادمین حداقل را از پیش‌فرض عوض کرده یا سقفی گذاشته؟ (برایِ اینکه متنِ پیش‌فرضِ درگاه‌ها
    تا وقتی چیزی تنظیم نشده دقیقاً مثلِ قبل بماند.)"""
    mn, mx = await get_limits(bot_id, gw_key)
    return bool(mx) or mn != GATEWAYS[gw_key].default_min


async def guard(message, bot_id: int, gw_key: str, amount) -> bool:
    """True اگر مبلغ مجاز است. وگرنه پیامِ خطا را به کاربر می‌دهد و False برمی‌گرداند."""
    err = await check(bot_id, gw_key, amount)
    if err is None:
        return True
    try:
        await message.answer(err, parse_mode="HTML")
    except Exception:  # noqa: BLE001 — پیامِ خطا نباید خودش جریان را بشکند
        logger.exception("wallet_limits: ارسالِ پیامِ خطایِ محدودیت ناموفق بود")
    return False


def chunk_rows(items, per_row: int = 2):
    return [list(items[i:i + per_row]) for i in range(0, len(items), per_row)]
