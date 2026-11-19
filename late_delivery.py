"""PATCH_LATE_DELIVERY_LOG_V1: «slow update» فقط زمانِ پردازشِ ما را می‌سنجد، نه فاصلهٔ «ارسالِ کاربر تا رسیدنِ آپدیت به سرور».

برایِ پیام‌ها تلگرام زمانِ ارسال (date) را می‌دهد؛ اگر آپدیت چند ثانیه بعد رسید یعنی تأخیر از مسیرِ تلگرام→سرور (یا شبکهٔ خودِ
کاربر) است، نه از کدِ ما. (دکمه‌ها callback هستند و زمانِ ارسال ندارند؛ فقط پیام‌ها اندازه‌گیری می‌شوند.)"""
from __future__ import annotations

import time

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)
LATE_SECS = 6.0


def delivery_lag(data: dict, now: float | None = None) -> float | None:
    if "edited_message" in data:                      # date همان پیامِ اصلی است
        return None
    d = (data.get("message") or {}).get("date")
    if not isinstance(d, (int, float)):
        return None
    return (time.time() if now is None else now) - float(d)


def warn(data: dict, token: str) -> None:
    try:
        lag = delivery_lag(data)
        if lag is not None and lag > LATE_SECS:
            logger.warning(f"late update delivery (token …{token[-6:]}): پیام {lag:.0f} ثانیه بعد از ارسال به سرور رسید")
    except Exception:
        logger.exception("late-delivery check failed")
