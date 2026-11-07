"""platform_idpay.py — کلاینتِ سبکِ درگاهِ آنلاینِ آیدی‌پی (IDPay) برایِ
شارژِ کیف‌پولِ خودِ پلتفرمِ بات‌ساز (نه ربات‌هایِ مستأجر).

=== PATCH_PLATFORM_IDPAY_GATEWAY_V1 ===
پیاده‌سازی روی APIِ رسمیِ IDPay (تاییدشده از چند SDKِ مستقلِ متن‌باز —
PHP، Node، Rust، CodeIgniter — همه‌شون رویِ همین دو اندپوینت توافق دارن):
  - ساختِ پرداخت: POST https://api.idpay.ir/v1.1/payment
  - تاییدیه:      POST https://api.idpay.ir/v1.1/payment/verify
  - احرازِ هویت با هدرِ X-API-KEY (نه تویِ بدنه، برخلافِ زرین‌پال/زیبال)
  - کدهایِ موفقیتِ تاییدیه: 100 (تازه تایید شد)، 101 (قبلاً تایید شده
    بود)، 200 (به دریافت‌کننده واریز شد) — هر سه یعنی پرداخت معتبره.

⚠️ نکته: مبلغ در APIِ آیدی‌پی همیشه **ریال** است (حداقل ۱۰۰۰ ریال).
چون بقیهٔ پلتفرم همه‌جا تومانه، همین‌جا تبدیلِ تومان↔ریال (×۱۰ / ÷۱۰)
انجام می‌شه تا امضایِ توابع برایِ صداکننده (تومان) ساده بمونه.
"""
from __future__ import annotations

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

try:
    import aiohttp
except Exception:
    aiohttp = None

_TIMEOUT = 15

BASE_URL = "https://api.idpay.ir"
PAYMENT_URL = BASE_URL + "/v1.1/payment"
VERIFY_URL = BASE_URL + "/v1.1/payment/verify"

_VERIFIED_STATUSES = {100, 101, 200}

_ERROR_CODES = {
    11: "کاربر مسدود شده است.",
    12: "API Key نامعتبر است.",
    13: "درخواست از IP یا Referer نامعتبر است.",
    14: "سرویس درگاه فعال نیست.",
    15: "درخواست شما به دریافت‌کننده نمی‌رسد.",
    16: "شناسه کسب‌وکار نامعتبر است.",
    21: "درگاه پرداخت شما فعال نیست.",
    22: "مبلغ پرداخت بایست بیشتر از ۱٬۰۰۰ ریال باشد.",
    23: "مبلغ پرداخت بیشتر از سقفِ مجاز است.",
    32: "callback نامعتبر است.",
    34: "مقدار order_id نباید خالی باشد.",
}


async def request_payment(
    api_key: str,
    order_id: str,
    amount_toman: int,
    description: str,
    callback_url: str,
    name: str = "",
    phone: str = "",
    mail: str = "",
) -> tuple[bool, str, str]:
    """درخواستِ ساختِ تراکنش. برگشتی: (موفق؟, id یا متنِ خطا, لینکِ پرداخت)."""
    if aiohttp is None:
        return False, "کتابخانهٔ aiohttp روی سرور نصب نیست.", ""
    payload = {
        "order_id": str(order_id)[:50],
        "amount": int(amount_toman) * 10,  # === تومان → ریال ===
        "callback": callback_url,
        "desc": (description or "شارژ کیف‌پول")[:255],
    }
    if name:
        payload["name"] = name[:255]
    if phone:
        payload["phone"] = phone[:11]
    if mail:
        payload["mail"] = mail[:255]

    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(PAYMENT_URL, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
    except Exception:
        logger.exception("خطای شبکه در درخواستِ پرداختِ آیدی‌پی")
        return False, "خطا در اتصال به آیدی‌پی. دوباره امتحان کن.", ""

    data = data or {}
    tx_id = data.get("id")
    link = data.get("link")
    if tx_id and link:
        return True, str(tx_id), str(link)
    err = data.get("error_code")
    msg = _ERROR_CODES.get(err) or data.get("error_message") or f"خطای نامشخص از آیدی‌پی (کد: {err})."
    return False, msg, ""


async def verify_payment(api_key: str, tx_id: str, order_id: str) -> tuple[bool, str, int]:
    """تاییدِ نهاییِ تراکنش. برگشتی: (موفق؟, track_id یا متنِ خطا, مبلغ به تومان)."""
    if aiohttp is None:
        return False, "کتابخانهٔ aiohttp روی سرور نصب نیست.", 0
    payload = {"id": str(tx_id), "order_id": str(order_id)}
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(VERIFY_URL, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
    except Exception:
        logger.exception("خطای شبکه در استعلامِ پرداختِ آیدی‌پی")
        return False, "خطا در اتصال به آیدی‌پی برای استعلام. بعداً دوباره امتحان کن.", 0

    data = data or {}
    try:
        status = int(data.get("status") or 0)
    except (TypeError, ValueError):
        status = 0

    if status in _VERIFIED_STATUSES:
        track_id = data.get("track_id") or tx_id
        amount_toman = 0
        try:
            amount_toman = round(int(data.get("amount") or 0) / 10)  # ریال → تومان
        except (TypeError, ValueError):
            logger.exception("تبدیلِ مبلغِ آیدی‌پی ناموفق بود")
        return True, str(track_id), amount_toman

    err = data.get("error_code")
    msg = (_ERROR_CODES.get(err) or data.get("error_message")
          or f"پرداخت تأیید نشد (status: {status}).")
    return False, msg, 0
