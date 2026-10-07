"""
username_resolve.py — نامِ واقعیِ کاربر روی پنل‌هایِ خانوادهٔ مرزبان (PATCH_PANEL_RAW_USERNAME_V1/V2).

کلاینت‌هایِ مرزبان/یوپنل/مرزنشین/پاسارگاد قبل از هر درخواست نام را با `_safe_username`
به [a-z0-9_] تبدیل می‌کنند (حروفِ بزرگ → کوچک؛ «-»، «.» و «@» → «_»). سرویس‌هایی که
خودِ ربات ساخته با همان نامِ تبدیل‌شده ساخته شده‌اند. ولی سرویسی که از قبل روی پنل
بوده و با «🧷 افزودنِ دستیِ سرویس» وصل شده با نامِ اصلی‌اش (مثلاً `Zahra8368` یا
`zahra-12`) روی پنل است — و پنل‌هایِ جدید (PasarGuard / مرزبان رویِ PostgreSQL) به
حروفِ بزرگ/کوچک حساس‌اند؛ پس ربات ۴۰۴ می‌گرفت و ساب‌لینک/مصرف/تمدید/حذف کار نمی‌کرد.

راهِ حل، فقط برایِ کاربرِ موجود (ساختنِ کاربرِ جدید دست نمی‌خورد):
  • خواندن (get_client): اول نامِ تبدیل‌شده (رفتارِ قبلی، بدونِ هیچ درخواستِ اضافه برایِ
    سرویس‌هایِ ربات)، اگر پیدا نشد نامِ دقیقِ اصلی؛ نامِ موفق برایِ هر پنل کش می‌شود.
  • نوشتن (تمدید/حذف/…): اگر نام از قبل کش نشده، یک‌بار با GET معلوم می‌شود کدام نام هست.
"""
from __future__ import annotations

from urllib.parse import quote

_RESOLVED: dict[tuple[str, str], str] = {}
_MAX_CACHE = 50_000


def _key(client, raw: str) -> tuple[str, str]:
    return (str(getattr(client, "base_url", "")), raw)


def name_candidates(client, email, safe_fn) -> list[str]:
    """نام‌هایی که به ترتیب باید امتحان شوند (اولی همیشه کم‌هزینه‌ترین است)."""
    safe = safe_fn(email)
    raw = str(email or "").strip()
    if not raw or raw == safe:
        return [safe]
    hit = _RESOLVED.get(_key(client, raw))
    if hit:
        return [hit]
    return [safe, quote(raw, safe="")]


def remember(client, email, name: str) -> None:
    raw = str(email or "").strip()
    if not raw:
        return
    if len(_RESOLVED) >= _MAX_CACHE:
        _RESOLVED.clear()
    _RESOLVED[_key(client, raw)] = name


async def resolve_username(client, email, safe_fn, path_fmt: str = "/api/user/{}") -> str:
    """برایِ مسیرهایِ نوشتن: نامی که واقعاً روی پنل هست (در صورتِ نیاز با یک GET)."""
    cands = name_candidates(client, email, safe_fn)
    if len(cands) == 1:
        return cands[0]
    for name in cands:
        if await client._request("GET", path_fmt.format(name)):
            remember(client, email, name)
            return name
    return cands[0]
