"""یادآوری «از کدام صفحه آمدم» برای دکمهٔ بازگشت صفحه‌هایی که از چند جا باز می‌شوند.

=== PATCH_PLATFORM_NAVBACK_V1 ===
مشکل: «بازی کن پول بگیر» هم از دستهٔ سرگرمی باز می‌شد و هم از تنظیمات شرط‌بندی؛ دکمهٔ بازگشتش ثابت
adm:back بود و ادمین را به ریشه/دستهٔ پنل می‌پراند، نه یک صفحه عقب.

قرارداد (callback_data = «پایه» یا «پایه@برچسب»):
  • بدون برچسب  ⇒ ورود از والد پیش‌فرض (مبدأ ریست می‌شود)
  • @keep        ⇒ برگشت از زیرصفحه (مبدأ دست نمی‌خورد)
  • @<tag>       ⇒ ورود از صفحه‌ای که در origins برای این برچسب ثبت شده
حافظه درون‌پروسه‌ای است (مثل _LAST_CAT در platform_admin_menu): اگر خالی باشد دکمه به والد
پیش‌فرض برمی‌گردد و چیزی خراب نمی‌شود.
"""
from __future__ import annotations

_BACK: dict[tuple[int, str], str] = {}


def split(data: str | None) -> tuple[str, str | None]:
    base, _, tag = (data or "").partition("@")
    return base, (tag or None)


def enter(uid: int, screen: str, tag: str | None, origins: dict[str, str]) -> None:
    """ثبت مبدأ هنگام ورود به screen؛ نگاشت tag → callback_data صفحهٔ مبدأ در origins."""
    if tag == "keep":
        return
    target = origins.get(tag) if tag else None
    if target:
        _BACK[(uid, screen)] = target
    else:
        _BACK.pop((uid, screen), None)


def back(uid: int | None, screen: str, default: str) -> str:
    return _BACK.get((uid, screen), default) if uid is not None else default
