"""
fa_num.py — PATCH_FA_DAYS_V1

عددِ لاتین وسطِ جملهٔ فارسی ترتیبِ راست‌به‌چپ را به هم می‌ریزد
(«پلن ربات @x 2 روز دیگر…» بد نمایش داده می‌شود). این ماژول عددِ روز را
به حروف برمی‌گرداند و برایِ عددهایِ بزرگتر از ارقامِ فارسی استفاده می‌کند
که خودشان مشکلِ جهت ندارند.
"""
from __future__ import annotations

_WORDS = {
    1: "یک", 2: "دو", 3: "سه", 4: "چهار", 5: "پنج",
    6: "شش", 7: "هفت", 8: "هشت", 9: "نه", 10: "ده",
    11: "یازده", 12: "دوازده", 13: "سیزده", 14: "چهارده", 15: "پانزده",
    16: "شانزده", 17: "هفده", 18: "هجده", 19: "نوزده", 20: "بیست",
}
_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_digits(n) -> str:
    """ارقامِ لاتین را به فارسی تبدیل می‌کند."""
    return str(n).translate(_DIGITS)


def fa_num(n: int) -> str:
    """عدد را به حروف (تا ۲۰) یا ارقامِ فارسی برمی‌گرداند."""
    try:
        n = int(n)
    except Exception:
        return fa_digits(n)
    return _WORDS.get(n, fa_digits(n))


def fa_days(n: int, *, suffix: str = "دیگر") -> str:
    """«امروز» / «فردا» / «دو روز دیگر» / «۲۵ روز دیگر»."""
    try:
        n = int(n)
    except Exception:
        return f"{fa_digits(n)} روز {suffix}".strip()
    if n <= 0:
        return "امروز"
    return f"{fa_num(n)} روز {suffix}".strip()
