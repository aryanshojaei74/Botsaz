"""
vpn_titles.py — تیترِ قابل‌ویرایشِ صفحه‌هایِ اصلیِ رباتِ VPN.

=== PATCH_VPN_EDITABLE_TITLES_V1 ===
گزارشِ ادمین (ایلیا): «امکانِ ویرایشِ متنِ بالایِ دکمه‌هایِ هر بخش؛ مثلاً در بخشِ
خریدِ سرویس الان نوشته "خرید VPN - حجم/روز دلخواه"، امکانِ تغییرِ این متن باشد».

«متنِ دکمه» (header) از قبل قابل‌تنظیم است و *زیرِ* تیترِ صفحه درج می‌شود؛ ولی خودِ
تیتر (خطِ اولِ صفحه) در کد ثابت بود. این ماژول فقط تیترِ چند صفحهٔ اصلیِ بخشِ
خرید/سرویس‌ها را قابل‌ویرایش می‌کند. این‌جا هیچ router یا handlerی نیست؛ فقط
منطقِ خواندن/نوشتن (ویرایشِ تعاملی در vpn_admin.py است).

اصولِ ایمنی:
  • پیش‌فرض‌ها دقیقاً همان متن‌هایِ قبلی‌اند؛ تا ادمین چیزی ثبت نکند خروجی عوض نمی‌شود.
  • متنِ ادمین همیشه HTML-escape می‌شود (تیتر داخلِ <b> در پیامِ HTML می‌رود).
  • تیترِ لاتین‌محور جهتِ کلِ خط را چپ‌به‌راست نکند: اگر اولین حرفِ «جهت‌دار» لاتین
    باشد، یک RLM جلویش گذاشته می‌شود (رجوع کنید به PATCH_RTL_CLEAN_TITLE_V1).
"""
from __future__ import annotations

import html as _html
import unicodedata

# key → (متنِ پیش‌فرض، توضیحِ فارسی برایِ منویِ ادمین)
# متنِ پیش‌فرض عیناً همان چیزی است که قبلاً در کد ثابت بود.
TITLES: dict[str, tuple[str, str]] = {
    "plans":    ("خرید کانفینگ از پنل ها به صورت خودکار", "صفحهٔ لیستِ پلن‌ها (خرید)"),
    "vc":       ("خرید VPN — حجم/روز دلخواه",             "صفحهٔ خریدِ حجم/روزِ دلخواه (➕/➖)"),
    "all_cats": ("همهٔ دسته‌ها",                            "صفحهٔ «همهٔ دسته‌ها»"),
    "all_svcs": ("همهٔ سرویس‌ها",                           "صفحهٔ «همهٔ سرویس‌ها»"),
    "my_svcs":  ("سرویس‌ها و کانفیگ‌های من",                "صفحهٔ «سرویس‌های من»"),
}

MAX_LEN = 60
_RLM = "\u200f"


def _setting_key(key: str) -> str:
    return f"vpn_title:{key}"


def clean_input(raw: str) -> str:
    """ورودیِ ادمین را برایِ ذخیره پاک می‌کند: بدونِ خطِ جدید، فاصله‌هایِ اضافه حذف."""
    return " ".join((raw or "").replace("\r", " ").replace("\n", " ").split())[:MAX_LEN]


def _rtl_safe(text: str) -> str:
    """اگر اولین حرفِ جهت‌دارِ متن لاتین باشد، RLM جلویش می‌گذارد تا کلِ خط چپ‌چین نشود."""
    for ch in text:
        d = unicodedata.bidirectional(ch)
        if d in ("R", "AL"):
            return text
        if d == "L":
            return _RLM + text
    return text


def render(text: str) -> str:
    """متنِ خام را آمادهٔ درج داخلِ پیامِ HTML می‌کند (escape + محافظِ جهت)."""
    return _html.escape(_rtl_safe(text or ""), quote=False)


async def get_raw(bot_id: int, key: str) -> str:
    """مقدارِ سفارشیِ ذخیره‌شده (خام) یا رشتهٔ خالی اگر ادمین چیزی ثبت نکرده."""
    from tenant import get_setting
    return (await get_setting(bot_id, _setting_key(key), "") or "").strip()


async def get_title(bot_id: int, key: str) -> str:
    """تیترِ آمادهٔ درج در HTML: سفارشیِ ادمین اگر بود، وگرنه پیش‌فرض. هرگز exception نمی‌دهد —
    اگر هر چیزی خراب شود، همان پیش‌فرضِ قبلی برمی‌گردد تا صفحه هیچ‌وقت نشکند."""
    default = TITLES[key][0]
    try:
        custom = await get_raw(bot_id, key)
    except Exception:
        return render(default)
    return render(custom or default)


async def set_title(bot_id: int, key: str, value: str) -> None:
    """value خالی = بازگشت به پیش‌فرض."""
    from tenant import set_setting
    if key not in TITLES:
        raise KeyError(key)
    await set_setting(bot_id, _setting_key(key), clean_input(value))
