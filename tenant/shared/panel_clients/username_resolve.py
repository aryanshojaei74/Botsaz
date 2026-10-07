"""
username_resolve.py — نامِ واقعیِ کاربر روی پنل‌هایِ خانوادهٔ مرزبان (PATCH_PANEL_RAW_USERNAME_V1).

کلاینت‌هایِ مرزبان/یوپنل/مرزنشین قبل از هر درخواست نام را با `_safe_username`
به [a-z0-9_] تبدیل می‌کنند («-»، «.» و «@» → «_»). سرویس‌هایی که خودِ ربات ساخته
با همان نامِ تبدیل‌شده ساخته شده‌اند، پس مشکلی ندارند. ولی سرویسی که از قبل روی پنل
بوده و با «🧷 افزودنِ دستیِ سرویس» به کاربر وصل شده، با نامِ اصلی‌اش (مثلاً
`zahra-12`) روی پنل است؛ ربات `zahra_12` را می‌پرسید و ۴۰۴ می‌گرفت — ساب‌لینک،
مصرفِ زنده، تمدید و حذف هیچ‌کدام کار نمی‌کرد.

این‌جا فقط برایِ «جست‌وجو/تغییرِ» کاربرِ موجود (نه ساختنِ کاربرِ جدید): اول نامِ
تبدیل‌شده (رفتارِ قبلی، برایِ همهٔ سرویس‌هایِ ساختِ ربات) و اگر پنل نشناخت، نامِ
دقیقِ اصلی امتحان می‌شود. نتیجه برایِ هر (پنل، نام) کش می‌شود.
"""
from __future__ import annotations

from urllib.parse import quote

_RESOLVED: dict[tuple[str, str], str] = {}
_MAX_CACHE = 50_000


async def resolve_username(client, email, safe_fn, path_fmt: str = "/api/user/{}") -> str:
    safe = safe_fn(email)
    raw = str(email or "").strip()
    # فقط تفاوتِ حروفِ کوچک/بزرگ: پنل‌هایِ مرزبانی نام را بدونِ حساسیت به حروف پیدا می‌کنند
    if not raw or raw.lower() == safe:
        return safe
    key = (str(getattr(client, "base_url", "")), raw)
    hit = _RESOLVED.get(key)
    if hit:
        return hit
    chosen = safe
    if not await client._request("GET", path_fmt.format(quote(safe, safe=""))):
        if await client._request("GET", path_fmt.format(quote(raw, safe=""))):
            chosen = quote(raw, safe="")
    if len(_RESOLVED) >= _MAX_CACHE:
        _RESOLVED.clear()
    _RESOLVED[key] = chosen
    return chosen
