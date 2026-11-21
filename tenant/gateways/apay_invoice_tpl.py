"""PATCH_APAY_INVOICE_TPL_V1 — قالبِ دلخواهِ «فاکتورِ شارژِ کیف پول» در درگاهِ آپی (فقط پلنِ حرفه‌ای).

درخواستِ ایلیا: مثلِ قالبِ پیش‌فاکتورِ VPN. مالک کلِ متنِ فاکتور را با متغیرهایِ {amount} {card} … می‌نویسد؛
دکمه‌هایِ پرداخت/کپی/بررسی/لغو دست‌نخورده می‌مانند. قالبِ خالی ⇒ متنِ پیش‌فرض.
"""
from __future__ import annotations

import html
import re

SETTING_KEY = "apay_invoice_tpl"
MAX_LEN = 2500

VARS: dict[str, str] = {
    "title": "اسمِ نمایشیِ درگاه (همان که برایِ کاربر گذاشته‌ای)",
    "amount": "مبلغی که به کیف پول اضافه می‌شود (تومان)",
    "pay_toman": "مبلغِ دقیقِ واریز (تومان)",
    "pay_rial": "مبلغِ دقیقِ واریز (ریال، بدونِ ویرگول؛ برایِ کپی)",
    "fee": "کارمزدِ درگاه که به مبلغ اضافه شده (تومان؛ اگر ندارد ۰)",
    "card": "شمارهٔ کارتِ مقصد (اگر نمایش داده می‌شود)",
    "holder": "نامِ صاحبِ کارت",
    "code": "کدِ پیگیریِ فاکتور",
    "ttl": "مهلتِ فاکتور (مثلاً «۱۵ دقیقه»)",
    "buyer": "نامِ کاربر",
    "username": "یوزرنیمِ کاربر با @ (اگر نداشت «—»)",
    "user_id": "آیدیِ عددیِ کاربر",
    "balance": "موجودیِ فعلیِ کیف پولِ کاربر (تومان)",
}
_PAT = re.compile(r"\{(\w+)\}")
TTL_TEXT = "۱۵ دقیقه"


def help_text() -> str:
    lines = ["🧾 <b>قالبِ فاکتورِ آپی</b>", "",
             "کلِ متنِ فاکتورِ شارژ را خودت بنویس. جاگذاری‌هایِ خودکار:"]
    for k, d in VARS.items():
        lines.append(f"<code>{{{k}}}</code> → {d}")
    lines += ["", "مثال:",
              "<code>🧾 فاکتورِ شارژ — {title}\n\n💰 واریز: {pay_toman} تومان\n🏦 کارت: {card}\n"
              "👤 به نام: {holder}\n🔑 کد: {code}\n⏳ مهلت: {ttl}</code>", "",
              "دکمه‌هایِ پرداخت و کپی همان‌طور می‌مانند. برایِ برگشت به متنِ پیش‌فرض: /default"]
    return "\n".join(lines)


def render(template: str, values: dict) -> str:
    """مقدارها escape می‌شوند؛ متغیرِ ناشناس عیناً می‌ماند."""
    def _sub(m: re.Match) -> str:
        k = m.group(1)
        if k not in VARS:
            return m.group(0)
        return html.escape(str(values.get(k, "")))
    return _PAT.sub(_sub, template)


def sample_values() -> dict:
    return {"title": "کارت به کارت آنی", "amount": "500,000", "pay_toman": "500,037", "pay_rial": "5000370",
            "fee": "0", "card": "6037991234567893", "holder": "علی رضایی", "code": "AP123456",
            "ttl": TTL_TEXT, "buyer": "علی", "username": "@ali", "user_id": "123456789", "balance": "120,000"}


def values_for(*, title, pay_rial, fee_toman, credit_toman, card, holder, order_id,
               buyer="—", username="", user_id="", balance=0) -> dict:
    toman = pay_rial // 10 if pay_rial and pay_rial % 10 == 0 else (pay_rial / 10 if pay_rial else 0)
    return {"title": title, "amount": f"{int(credit_toman):,}", "pay_toman": f"{int(toman):,}",
            "pay_rial": str(pay_rial or ""), "fee": f"{int(fee_toman or 0):,}", "card": card or "—",
            "holder": holder or "—", "code": order_id, "ttl": TTL_TEXT, "buyer": buyer or "—",
            "username": f"@{username}" if username else "—", "user_id": str(user_id or ""),
            "balance": f"{int(balance or 0):,}"}


async def get_template(bot_id: int, plan: str | None) -> str:
    """قالبِ فعال یا "" — فقط برایِ پلنِ حرفه‌ای."""
    try:
        from plans import has_feature, Feature
        if not has_feature(plan, Feature.APAY_INVOICE_TPL):
            return ""
        from tenant import get_setting
        return ((await get_setting(bot_id, SETTING_KEY, "")) or "").strip()
    except Exception:
        return ""
