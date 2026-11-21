"""PATCH_VPN_INVOICE_TPL_V1 — قالبِ دلخواهِ «پیش فاکتور» در فروشِ VPN (فقط پلنِ حرفه‌ای).

ادمین کلِ متنِ پیش‌فاکتور را با متغیرهایِ {server} {plan} {category} {price} ... می‌نویسد.
دکمه‌هایِ پرداخت دست‌نخورده می‌مانند. قالبِ خالی ⇒ متنِ پیش‌فرض.
"""
from __future__ import annotations

import html
import re

SETTING_KEY = "vpn_invoice_tpl"
MAX_LEN = 2500

# نام → توضیح (برایِ راهنمایِ ادمین)
VARS: dict[str, str] = {
    "server": "نامِ سرور/پنل (همان نامی که برایِ پنل گذاشته‌ای)",
    "plan": "نامِ پلن (برایِ خریدِ دلخواه: تیترِ صفحه)",
    "category": "نامِ دسته‌ای که کاربر خریده",
    "volume": "حجم (مثلاً ۱۰۰ گیگ)",
    "days": "مدت (مثلاً ۷۰ روز)",
    "count": "تعداد",
    "label": "نامِ دلخواهِ کانفیگ (اگر کاربر گذاشته)",
    "price": "قیمتِ اصلی (تومان)",
    "discount": "مبلغِ تخفیف (تومان)",
    "total": "مبلغِ پرداختی نهایی (تومان)",
    "balance": "موجودیِ کیف پولِ کاربر",
    "buyer": "نامِ خریدار",
    "username": "یوزرنیمِ خریدار با @ (اگر نداشت «—»)",
    "user_id": "آیدیِ عددیِ خریدار",
    "code": "کدِ تخفیفِ اعمال‌شده (اگر نبود «—»)",
    "note": "توضیحِ پلن/پنل (اگر ثبت کرده‌ای)",
    "warn": "هشدارِ «موجودی کافی نیست» (فقط وقتی لازم است)",
}

_PAT = re.compile(r"\{(\w+)\}")


def help_text() -> str:
    lines = ["🧾 <b>قالبِ پیش فاکتور</b>", "",
             "کلِ متنِ پیش‌فاکتور را خودت بنویس. جاگذاری‌هایِ خودکار:"]
    for k, d in VARS.items():
        lines.append(f"<code>{{{k}}}</code> → {d}")
    lines += ["", "مثال:",
              "<code>🧾 پیش فاکتور\n\n🛰 سرور: {server}\n📂 دسته: {category}\n"
              "📦 حجم: {volume} | 📅 مدت: {days}\n💰 مبلغ: {total} تومان\n"
              "👛 موجودی: {balance} {warn}</code>", "",
              "برایِ برگشت به متنِ پیش‌فرض: /default"]
    return "\n".join(lines)


def render(template: str, values: dict) -> str:
    """مقدارها (متنِ کاربر/ادمین) escape می‌شوند؛ متغیرِ ناشناس عیناً می‌ماند."""
    def _sub(m: re.Match) -> str:
        k = m.group(1)
        if k not in VARS:
            return m.group(0)
        v = values.get(k, "")
        if k in ("warn",):
            return str(v)
        return html.escape(str(v)) if k not in ("note",) else str(v)
    return _PAT.sub(_sub, template)


def sample_values() -> dict:
    return {"server": "آلمان ۱", "plan": "پلن ۱۰۰ گیگ", "category": "ویژه", "volume": "100 گیگ",
            "days": "70 روز", "count": "1", "label": "—", "price": "400,000", "discount": "0",
            "total": "400,000", "balance": "1,000,000", "buyer": "علی", "username": "@ali",
            "user_id": "123456789", "code": "—", "note": "", "warn": ""}


async def get_template(bot_id: int, plan: str | None) -> str:
    """قالبِ فعال یا "" — فقط برایِ پلنِ حرفه‌ای."""
    try:
        from plans import has_feature, Feature
        if not has_feature(plan, Feature.VPN_INVOICE_TPL):
            return ""
        from tenant import get_setting
        return ((await get_setting(bot_id, SETTING_KEY, "")) or "").strip()
    except Exception:
        return ""
