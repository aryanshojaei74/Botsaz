"""log_throttle.py — PATCH_LOG_THROTTLE_PERSIST_V1

«این هشدار را حداکثر هر N ثانیه یک‌بار ثبت کن» که با ری‌استارت صفر نشود.

لاگِ سرور (۱۵ مهر): ۳۳هزار WARNING و ۷۶ ERROR در یک روز، بیشترش تکراری. هشدارهایِ
«کلاینت رویِ پنل نیست» (روزی یک‌بار)، «کلیدِ OzVinoo غلط است» (هر ۶ ساعت) و «همگام‌سازیِ
پنل ناموفق» (فقط بارِ اول) زمانِ آخرین ثبت را فقط در حافظه نگه می‌داشتند؛ هر ری‌استارت
(هر اعمالِ زیپ) و هر پروسهٔ جدا دوباره از صفر لاگ می‌کرد — یک حساب ۲۷ بار در روز.

حالا زمانِ آخرین ثبت در یک فایلِ کوچکِ JSON کنارِ error.log هم ذخیره می‌شود و همهٔ
پروسه‌ها/ری‌استارت‌ها آن را می‌بینند. هر خطایی در خواندن/نوشتنِ فایل نادیده گرفته می‌شود
(حداکثر یک لاگِ اضافه) — این ماژول هرگز نباید خودِ کار را بشکند.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".log_throttle.json")
_MAX_KEYS = 20_000
_cache: dict[str, float] | None = None
_cache_mtime = 0.0


def _load() -> dict[str, float]:
    global _cache, _cache_mtime
    try:
        mt = os.path.getmtime(_PATH)
    except OSError:
        mt = 0.0
    if _cache is None or mt > _cache_mtime:
        try:
            with open(_PATH, encoding="utf-8") as f:
                data = json.load(f)
            _cache = {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = _cache or {}
        _cache_mtime = mt
    return _cache


def _save(data: dict[str, float]) -> None:
    global _cache_mtime
    now = time.time()
    if len(data) > _MAX_KEYS:   # کلیدهایِ قدیمی‌تر از یک هفته دور ریخته می‌شوند
        for k in [k for k, v in data.items() if now - v > 7 * 86400]:
            data.pop(k, None)
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_PATH), prefix=".log_throttle.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _PATH)
        _cache_mtime = os.path.getmtime(_PATH)
    except OSError:
        return   # نوشتن ممکن نشد → در بدترین حالت همان رفتارِ قبلی (فقط حافظه)


def should_log(key: str, every_seconds: float) -> bool:
    """True یعنی الان ثبت کن (و زمانش ذخیره می‌شود)؛ False یعنی در این بازه قبلاً ثبت شده."""
    now = time.time()
    data = _load()
    last = data.get(key)
    if last is not None and now - last < every_seconds:
        return False
    data[key] = now
    _save(data)
    return True
