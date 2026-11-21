"""platform_zibal.py — کلاینتِ سبکِ درگاهِ آنلاینِ زیبال برایِ کیف‌پولِ ربات‌های مستأجر.

=== PATCH_ZIBAL_GATEWAY_V1 ===
پیاده‌سازی روی APIِ رسمیِ REST/JSONِ زیبال. مستندات (تاییدشده از چند
SDKِ رسمی/جامعه‌ای — Node.js، Python، Go، Rust — همه‌شون رویِ همین سه
اندپوینت توافق دارن):
  - درخواست:  POST https://gateway.zibal.ir/v1/request
  - تاییدیه:  POST https://gateway.zibal.ir/v1/verify
  - پرداخت:   https://gateway.zibal.ir/start/{trackId}
  - کدِ موفقیت: result == 100

⚠️ نکتهٔ مهم: برخلافِ زرین‌پال (که با currency=IRT مستقیم تومان قبول
می‌کنه)، APIِ زیبال مبلغ رو همیشه به **ریال** می‌گیره و برمی‌گردونه.
چون بقیهٔ ربات (کیف‌پول، سفارش‌ها، نمایش به کاربر) همه‌جا تومانه، همین‌جا
تبدیلِ تومان↔ریال (×۱۰ / ÷۱۰) انجام می‌شه تا امضایِ توابع برایِ صداکننده
دقیقاً مثلِ platform_zarinpal.py (ورودی/خروجی به تومان) بمونه.

جریانِ کار:
  ۱) request_payment(...)  → گرفتنِ trackId
  ۲) startpay_url(track_id) → لینکی که کاربر باید باز کند و پرداخت کند
  ۳) بعدِ بازگشتِ کاربر به callback_url (با trackId و success در query)،
     verify_payment(...) صدا زده می‌شود تا پرداخت نهایی/استعلام شود.
"""
from __future__ import annotations

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

try:
    import aiohttp
except Exception:
    aiohttp = None

_TIMEOUT = 15

REQUEST_URL = "https://gateway.zibal.ir/v1/request"
VERIFY_URL = "https://gateway.zibal.ir/v1/verify"
STARTPAY_URL = "https://gateway.zibal.ir/start/{track_id}"

# کدِ موفقیتِ زیبال برایِ هر دو مرحله (درخواست و تاییدیه) همینه.
_SUCCESS_RESULT = 100

# پیام‌هایِ رایجِ کدهایِ نتیجه (طبقِ مستنداتِ رسمی) — برایِ خطایِ خواناتر.
_RESULT_MESSAGES = {
    102: "merchant غیرفعال یا نامعتبر است.",
    103: "amount بایست بزرگ‌تر از ۱۰۰۰ ریال باشد.",
    104: "callbackUrl نامعتبر است.",
    105: "amount نامعتبر است.",
    106: "orderId تکراری است.",
    201: "قبلاً تایید شده است.",
    202: "سفارش پرداخت نشده یا ناموفق بوده است.",
    203: "trackId نامعتبر است.",
}


def startpay_url(track_id) -> str:
    return STARTPAY_URL.format(track_id=track_id)


async def request_payment(
    merchant_id: str,
    amount_toman: int,
    description: str,
    callback_url: str,
    mobile: str = "",
    order_id: str = "",
) -> tuple[bool, str]:
    """درخواستِ ساختِ تراکنش. برگشتی: (موفق؟, trackId یا متنِ خطا)."""
    if aiohttp is None:
        return False, "کتابخانهٔ aiohttp روی سرور نصب نیست."
    payload = {
        "merchant": merchant_id,
        "amount": int(amount_toman) * 10,  # === تومان → ریال ===
        "callbackUrl": callback_url,
        "description": description or "شارژ کیف‌پول",
    }
    if mobile:
        payload["mobile"] = mobile
    if order_id:
        payload["orderId"] = str(order_id)

    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(REQUEST_URL, json=payload) as resp:
                data = await resp.json(content_type=None)
    except Exception:
        logger.exception("خطای شبکه در درخواستِ پرداختِ زیبال")
        return False, "خطا در اتصال به زیبال. دوباره امتحان کن."

    data = data or {}
    result = data.get("result")
    track_id = data.get("trackId")
    if result == _SUCCESS_RESULT and track_id:
        return True, str(track_id)
    msg = _RESULT_MESSAGES.get(result) or data.get("message") or f"خطای نامشخص از زیبال (کد: {result})."
    return False, msg


# === PATCH_ZIBAL_CARD_PAN_V1 ===
# شمارهٔ کارتِ هر trackId (حافظه‌ای و کوتاه‌عمر؛ فقط بلافاصله بعدِ verify خونده می‌شه).
_LAST_CARD_PAN: dict = {}


def pop_card_pan(track_id) -> str:
    """شماره کارتِ همان تراکنش را برمی‌دارد و پاک می‌کند."""
    try:
        return _LAST_CARD_PAN.pop(str(track_id), "") or ""
    except Exception:
        return ""


async def verify_payment(merchant_id: str, track_id) -> tuple[bool, str, int]:
    """استعلام/تأییدِ نهاییِ تراکنش.

    برگشتی: (موفق؟, ref_number یا متنِ خطا, مبلغِ تاییدشده به تومان).
    مبلغ رو هم برمی‌گردونیم (بر خلافِ زرین‌پال) چون صداکننده باید همون
    مبلغِ واقعیِ تاییدشده رو به کیف‌پول اضافه کنه، نه مبلغی که فقط تویِ
    دیتابیسِ خودمون ذخیره کرده بودیم — استعلام از زیبال منبعِ درست‌ه.
    """
    if aiohttp is None:
        return False, "کتابخانهٔ aiohttp روی سرور نصب نیست.", 0
    payload = {"merchant": merchant_id, "trackId": str(track_id)}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(VERIFY_URL, json=payload) as resp:
                data = await resp.json(content_type=None)
    except Exception:
        logger.exception("خطای شبکه در استعلامِ پرداختِ زیبال")
        return False, "خطا در اتصال به زیبال برای استعلام. بعداً دوباره امتحان کن.", 0

    data = data or {}
    result = data.get("result")
    # ۱۰۰ = تازه تایید شد، ۲۰۱ = قبلاً تایید شده بود (هر دو باید موفق حساب بشن،
    # وگرنه در فلوهایِ retry پرداختِ واقعاً موفق «ناموفق» گزارش می‌شه)
    if result in (_SUCCESS_RESULT, 201) and str(data.get("status")) in ("1", "2"):
        try:
            _LAST_CARD_PAN[str(track_id)] = str(data.get("cardNumber") or "")[:32]
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        ref = data.get("refNumber") or track_id
        amount_toman = 0
        try:
            amount_toman = round(int(data.get("amount") or 0) / 10)  # ریال → تومان
        except (TypeError, ValueError):
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return True, str(ref), amount_toman

    msg = _RESULT_MESSAGES.get(result) or data.get("message") or f"پرداخت تأیید نشد (کد: {result})."
    return False, msg, 0
