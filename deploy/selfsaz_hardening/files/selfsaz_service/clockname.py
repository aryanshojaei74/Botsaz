"""کمکی‌های «ساعت کنار نام» و ذخیرهٔ حالت worker — تا ری‌استارت چیزی را به‌هم نریزد.

مشکل قبلی: نام اصلی فقط در حافظه بود. بعد از ری‌استارت، worker نامِ فعلی (که ساعتِ
قبلی داخلش بود) را «نام اصلی» می‌گرفت و ساعت‌ها روی هم انباشته می‌شد
(«علی 12:34 12:35»). اینجا ساعتِ انتهای نام با هر فونتی شناسایی و حذف می‌شود.
"""
from __future__ import annotations

import json
import os
import tempfile
import unicodedata

_EXTRA = {":", "҉"}          # ':' و علامت فونت ۶ (҉)


def _is_clock_char(ch: str) -> bool:
    return ch in _EXTRA or unicodedata.category(ch) == "Nd"


def strip_clock(name: str | None) -> str:
    """ساعت(های) انتهایی مثل «۱۲:۳۴» (در هر یک از ۶ فونت) را از نام برمی‌دارد.

    فقط وقتی حذف می‌کند که دنباله شامل ':' و دست‌کم ۴ رقم باشد؛ پس «Reza 1999» یا
    «Room 12» دست نمی‌خورد.
    """
    s = (name or "").rstrip()
    for _ in range(10):
        j = len(s)
        while j > 0 and _is_clock_char(s[j - 1]):
            j -= 1
        run = s[j:]
        digits = sum(1 for c in run if unicodedata.category(c) == "Nd")
        if ":" in run and digits >= 4:
            s = s[:j].rstrip()
            continue
        break
    return s


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def save_json(path: str, data) -> bool:
    """نوشتن اتمی؛ نیمه‌نوشته ماندن فایل با ری‌استارت وسط کار ممکن نیست."""
    try:
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        return False
