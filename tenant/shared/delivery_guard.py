"""
delivery_guard.py — PATCH_DELIVERY_GUARD_V1

گزارشِ مشتری (تازیان): «رسید تأیید می‌کنی، دکمه‌ها نمی‌رن، دوباره تأیید بزنی می‌گه رسیدگی شده؛ بخشِ مالی
تأییدشده ولی چیزی برایِ مشتری نرفته.» الگو: وضعیتِ سفارش اول در دیتابیس «پرداخت‌شده» ثبت می‌شود، بعد
چند مرحلهٔ جانبی (پاداشِ دعوت، ویرایشِ پیام، ارسالِ کانفیگ/پیام به مشتری) بدونِ محافظ اجرا می‌شود؛ یک خطای
گذرا (شبکهٔ تلگرام، مشتری ربات را بلاک کرده، …) وسطِ راه همه را می‌کُشد و ادمین هیچ‌چیز نمی‌فهمد.

این ماژول دو کار می‌کند: ۱) خطایِ مرحلهٔ جانبی کلِ تأیید را نمی‌کُشد، ۲) اگر تحویل به مشتری شکست خورد،
ادمین همان لحظه خبر می‌گیرد (با مشخصاتِ مشتری و خطا) و کانفیگ‌هایِ تحویل‌نشده را مستقیم برایِ خودش
می‌گیرد تا دستی به مشتری برساند.
"""
from __future__ import annotations

import html as _html

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)


def _who(buyer) -> str:
    name = _html.escape((getattr(buyer, "full_name", "") or "—")[:60])
    tg = getattr(buyer, "telegram_id", "—")
    un = getattr(buyer, "username", None)
    return f"{name} · <code>{tg}</code>" + (f" · @{_html.escape(un)}" if un else "")


async def alert_admin_delivery_failed(bot, admin_chat_id: int, what: str, buyer, err: Exception,
                                      cfgs: list | None = None) -> None:
    """به ادمینِ تأییدکننده خبر می‌دهد که «تأیید ثبت شد ولی ارسال به مشتری ناموفق بود»؛ کانفیگ‌ها را برایِ ادمین می‌فرستد."""
    reason = _html.escape(f"{type(err).__name__}: {str(err)[:160]}")
    try:
        await bot.send_message(
            admin_chat_id,
            "⚠️ <b>تأیید ثبت شد ولی ارسال به مشتری ناموفق بود</b>\n\n"
            f"{_html.escape(what)}\n👤 مشتری: {_who(buyer)}\n❗ خطا: <code>{reason}</code>\n\n"
            "پرداخت و سفارش «تأییدشده» است؛ لطفاً دستی برایِ مشتری بفرست"
            + (" (کانفیگ‌ها همین‌جا برایِ تو فرستاده می‌شود):" if cfgs else "."),
            parse_mode="HTML")
    except Exception:
        logger.exception("delivery_guard: هشدار به ادمین هم ارسال نشد")
        return
    if cfgs:
        try:
            from tenant import send_config_to_user
            for cfg in cfgs:
                await send_config_to_user(bot, admin_chat_id, cfg)
        except Exception:
            logger.exception("delivery_guard: ارسالِ کانفیگ به ادمین ناموفق بود")
