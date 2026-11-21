"""join_dedupe.py — جلوگیری از دوبار خوش‌آمد/کپچا برای یک عضو (=== PATCH_JOIN_CHAT_MEMBER_V1 ===).

گزارش ویردنت: «به یه نفر خوش‌آمد می‌گه به صد نفر نمی‌گه» — کسانی که از لینک دعوت («joined the group via …») وارد شدند، پیامِ
سرویسِ new_chat_members به ربات نمی‌رسید و فقط آپدیتِ chat_member می‌آمد. حالا هر دو مسیر عضو را پردازش می‌کنند و این
ماژول تضمین می‌کند هر ورودِ واقعی فقط یک‌بار (هر کدام اول رسید) پردازش شود.
"""
from __future__ import annotations

import time

_TTL = 90.0
_SEEN: dict[tuple, float] = {}


def claim(bot_id: int, chat_id: int, user_id: int, ttl: float = _TTL) -> bool:
    """True اگر این ورود هنوز پردازش نشده (و حالا ثبت می‌شود)؛ False اگر در ttl ثانیهٔ اخیر پردازش شده."""
    now = time.monotonic()
    key = (bot_id, chat_id, user_id)
    last = _SEEN.get(key)
    if last is not None and now - last < ttl:
        return False
    if len(_SEEN) > 5000:
        for k in [k for k, v in _SEEN.items() if now - v > ttl]:
            _SEEN.pop(k, None)
    _SEEN[key] = now
    return True


def is_join(old_status: str, new_status: str, new_is_member: bool | None = None) -> bool:
    """تغییرِ chat_member یک «ورود» است؟ (از left/kicked به عضو)."""
    if old_status not in ("left", "kicked"):
        return False
    if new_status in ("member", "administrator", "creator"):
        return True
    return new_status == "restricted" and bool(new_is_member)
