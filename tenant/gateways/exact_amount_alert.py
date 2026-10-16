"""exact_amount_alert.py — پاپ‌آپِ «دقیقاً مبلغ فاکتور را واریز کنید!» برایِ درگاه‌هایی که پرداخت را با «مبلغِ یکتا» تشخیص می‌دهند.

درخواستِ مشتری (دیار): همان هشداری که بعضی رباتِ دیگر وقتی مشتری درگاهِ کارت‌به‌کارتِ خودکار را می‌زند نشان می‌دهند
(پنجرهٔ تأیید، نه فقط متنِ فاکتور). این درگاه‌ها واریزِ «رند/کم/زیاد» را خودکار تأیید نمی‌کنند و شارژ معطل می‌ماند.

پیاده‌سازی: یک میان‌افزارِ callback_query. اگر دکمهٔ انتخابِ این درگاه‌ها زده شد، اولین `cb.answer()`ی که هندلر بدونِ متن
بزند (همان بستنِ اسپینر) به یک پاپ‌آپِ هشدار تبدیل می‌شود. اگر هندلر خودش متنی بدهد (مثلاً «درگاه در دسترس نیست»)
دست‌نخورده می‌ماند. کلیدِ خاموش‌کردن: تنظیمِ ربات `pay_exact_alert` = "0".
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ALERT_TEXT = (
    "⚠️ دقیقاً مبلغ فاکتور را واریز کنید!\n\n"
    "مبلغ رند یا کم و زیاد، خودکار تأیید نمی‌شود و شارژ معطل بررسی دستی می‌ماند."
)

# درگاه‌هایی که مبلغِ یکتا/کارتِ اختصاصیِ هر فاکتور دارند (انتخابِ درگاه در منویِ کیف پول و خرید)
_PREFIXES = ("w:tonpays", "w:uniquepay", "w:apay", "w:abangateway")


def is_target(cb_data: str | None) -> bool:
    d = cb_data or ""
    return any(d == p or d.startswith(p + ":") for p in _PREFIXES)


async def exact_amount_alert_mw(handler, event, data):
    if not is_target(getattr(event, "data", None)):
        return await handler(event, data)
    bot = data.get("bot")
    try:
        from tenant import get_bot_row, get_setting
        row = await get_bot_row(bot) if bot is not None else None
        if not row or await get_setting(row.id, "pay_exact_alert", "1") == "0":
            return await handler(event, data)
    except Exception:
        logger.exception("exact_amount_alert: خواندنِ تنظیمات ناموفق بود")
        return await handler(event, data)

    orig = event.answer
    state = {"used": False}

    async def _answer(text=None, show_alert=None, *a, **kw):
        first = not state["used"]
        state["used"] = True
        if first and text is None:
            return await orig(text=ALERT_TEXT, show_alert=True)
        return await orig(text, show_alert, *a, **kw)

    try:
        # model_copy(update={"answer": …}) روی مدل‌هایِ aiogram اثری ندارد (کلید به extra می‌رود و متدِ کلاس برنده می‌شود)؛
        # باید مستقیم رویِ __dict__ِ کپی نشست تا cb.answer در هندلر همین تابع شود.
        wrapped = event.model_copy()
        object.__setattr__(wrapped, "answer", _answer)
    except Exception:
        logger.exception("exact_amount_alert: ساختِ کپیِ رویداد ناموفق بود")
        return await handler(event, data)
    return await handler(wrapped, data)
