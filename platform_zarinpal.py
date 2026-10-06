"""platform_zarinpal.py — کلاینتِ سبکِ درگاهِ آنلاینِ ریالیِ زرین‌پال برایِ کیف‌پولِ پلتفرم.

پیاده‌سازی روی REST JSON API نسخهٔ v4 (نه GraphQL) چون رسمی‌ترین و
پایدارترینِ روشِ زرین‌پال برایِ سرور-به-سرور است و بدونِ نیاز به کلاینتِ
GraphQL جداگانه کار می‌کند. مستندات:
  - درخواستِ پرداخت / پرداختِ زرین‌پال:
    https://www.zarinpal.com/docs/paymentGateway/connectToGateway.html
  - مرجعِ کاملِ API (شاملِ معادلِ GraphQL):
    https://www.zarinpal.com/docs/apiDocs/

جریانِ کار:
  ۱) request_payment(...)  → گرفتنِ Authority
  ۲) startpay_url(authority) → لینکی که کاربر باید باز کند و پرداخت کند
  ۳) بعدِ بازگشتِ کاربر به callback_url (با Authority و Status در query)،
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

REQUEST_URL = "https://payment.zarinpal.com/pg/v4/payment/request.json"
VERIFY_URL = "https://payment.zarinpal.com/pg/v4/payment/verify.json"
STARTPAY_URL = "https://payment.zarinpal.com/pg/StartPay/{authority}"

# === PATCH_ZARINPAL_FEE_V1 ===
# زرین‌پال گزینهٔ «کارمزد از خریدار» را از پنل برداشت و گفت به‌جایش از
# API استعلامِ کارمزد استفاده کنید: مبلغ را می‌فرستی، کارمزد را می‌گوید،
# و تو همان را به مبلغِ تراکنش اضافه می‌کنی. این‌طوری فروشنده دقیقاً همان
# مبلغی که می‌خواهد به دستش می‌رسد و کارمزد را پرداخت‌کننده می‌دهد.
FEE_URL = "https://payment.zarinpal.com/pg/v4/payment/feeCalculation.json"


async def calc_fee(merchant_id: str, amount_toman: int) -> tuple[bool, int]:
    """کارمزدِ این مبلغ. برگشتی: (موفق؟, کارمزد به تومان).

    اگر استعلام به هر دلیلی نشد، (False, 0) برمی‌گردد و صداکننده باید
    بدونِ کارمزد ادامه دهد — هیچ‌وقت نباید خریدِ کاربر به‌خاطرِ این
    استعلام متوقف شود.

    === PATCH_ZARINPAL_FEE_RIAL_BUG_V1 ===
    باگِ گزارش‌شده: کارمزدِ نمایش‌داده‌شده ۱۰ برابرِ واقعی بود (۷۵۰۰ به‌جای
    ۷۵۰ تومان). علت: برخلافِ payment/request.json، اندپوینتِ
    feeCalculation.json پارامترِ currency را برایِ خروجیِ fee کامل رعایت
    نمی‌کند و طبقِ مستندِ رسمیِ زرین‌پال (که نمونه‌اش را با currency=IRR
    زده) همیشه fee را به «ریال» برمی‌گرداند. کد قبلاً این عدد را مستقیم
    به‌عنوانِ تومان برمی‌گرداند؛ الان بر ۱۰ تقسیم می‌شود.
    """
    if aiohttp is None:
        return False, 0
    payload = {"merchant_id": merchant_id, "amount": int(amount_toman),
               "currency": "IRT"}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(FEE_URL, json=payload) as resp:
                data = await resp.json(content_type=None)
    except Exception:
        logger.exception("استعلامِ کارمزدِ زرین‌پال ناموفق بود")
        return False, 0
    inner = (data or {}).get("data") or {}
    if inner.get("code") in (100,):
        fee_rial = inner.get("fee")
        try:
            fee_rial = max(0, int(fee_rial))
        except (TypeError, ValueError):
            return False, 0
        # fee همیشه به ریال برمی‌گردد (حتی وقتی currency=IRT فرستاده باشیم)
        fee_toman = round(fee_rial / 10)
        return True, fee_toman
    logger.warning("استعلامِ کارمزد پاسخِ نامنتظره داد: %s", str(data)[:200])
    return False, 0


# کدهایِ موفقیتِ زرین‌پال: ۱۰۰ = تازه تأیید شد، ۱۰۱ = قبلاً تأیید شده بود
_REQUEST_SUCCESS_CODES = (100,)
_VERIFY_SUCCESS_CODES = (100, 101)


# === PATCH_ZARINPAL_API_LINK_V1 ===
# پاسخِ request.jsonِ زرین‌پال (نسخهٔ ۴) در کنارِ authority یک فیلدِ «link» هم
# برمی‌گرداند — آدرسِ دقیقِ صفحهٔ پرداختی که خودِ زرین‌پال برایِ همین مرچنت
# می‌خواهد. اگر برایِ مرچنتی دامنه/مسیرِ دیگری لازم باشد (مثلاً زیردامنهٔ
# اختصاصی)، فقط این فیلد آن را می‌داند؛ ساختنِ دستیِ آدرس حدس است. پس اگر
# link آمد و همان authority را داشت، همان را استفاده می‌کنیم.
_API_LINKS: dict[str, str] = {}
_API_LINKS_MAX = 5000


def _remember_link(authority: str, link) -> None:
    try:
        link = str(link or "").strip()
        if not link.startswith("https://") or str(authority) not in link:
            return
        if len(_API_LINKS) >= _API_LINKS_MAX:
            _API_LINKS.pop(next(iter(_API_LINKS)))
        _API_LINKS[str(authority)] = link
        default = STARTPAY_URL.format(authority=authority)
        if link != default:
            logger.info("zarinpal: لینکِ پرداختِ اختصاصی از API: %s", link.split("/StartPay")[0])
    except Exception:
        logger.exception("zarinpal: ذخیرهٔ لینکِ API ناموفق بود")


def startpay_url(authority: str) -> str:
    return _API_LINKS.get(str(authority)) or STARTPAY_URL.format(authority=authority)


async def request_payment(
    merchant_id: str,
    amount_toman: int,
    description: str,
    callback_url: str,
    mobile: str = "",
    email: str = "",
) -> tuple[bool, str]:
    """درخواستِ ساختِ تراکنش. برگشتی: (موفق؟, authority یا متنِ خطا)."""
    if aiohttp is None:
        return False, "کتابخانهٔ aiohttp روی سرور نصب نیست."
    payload = {
        "merchant_id": merchant_id,
        "amount": int(amount_toman),
        "currency": "IRT",  # تومان — هم‌راستا با بقیهٔ مبالغِ داخلِ ربات
        "description": description or "شارژ کیف‌پول بات‌ساز",
        "callback_url": callback_url,
    }
    metadata = {}
    if mobile:
        metadata["mobile"] = mobile
    if email:
        metadata["email"] = email
    if metadata:
        payload["metadata"] = metadata

    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(REQUEST_URL, json=payload) as resp:
                data = await resp.json(content_type=None)
    except Exception:
        logger.exception("خطای شبکه در درخواستِ پرداختِ زرین‌پال")
        return False, "خطا در اتصال به زرین‌پال. دوباره امتحان کن."

    inner = (data or {}).get("data") or {}
    errors = (data or {}).get("errors") or {}
    code = inner.get("code")
    if code in _REQUEST_SUCCESS_CODES and inner.get("authority"):
        _remember_link(inner["authority"], inner.get("link") or inner.get("payment_url"))  # PATCH_ZARINPAL_API_LINK_V1
        return True, inner["authority"]

    # خطاها می‌تونن dict یا list باشن بسته به نوعِ خطا
    if isinstance(errors, dict):
        msg = errors.get("message") or str(errors) or "خطای نامشخص از زرین‌پال."
    elif isinstance(errors, list) and errors:
        msg = str(errors[0])
    else:
        msg = f"خطای نامشخص از زرین‌پال (کد: {code})."
    return False, msg


# === PATCH_ZP_CARD_PAN_V1 ===
# آخرین شمارهٔ کارتِ هر authority (حافظهای و کوتاهعمر؛ فقط بلافاصله بعد
# از verify خوانده می‌شود).
_LAST_CARD_PAN: dict = {}


def pop_card_pan(authority: str) -> str:
    """شماره کارتِ همان تراکنش را برمی‌دارد و پاک می‌کند."""
    try:
        return _LAST_CARD_PAN.pop(str(authority), "") or ""
    except Exception:
        return ""


async def verify_payment(
    merchant_id: str, amount_toman: int, authority: str,
) -> tuple[bool, str]:
    """استعلام/تأییدِ نهاییِ تراکنش. برگشتی: (موفق؟, ref_id یا متنِ خطا)."""
    if aiohttp is None:
        return False, "کتابخانهٔ aiohttp روی سرور نصب نیست."
    payload = {
        "merchant_id": merchant_id,
        "amount": int(amount_toman),
        "currency": "IRT",
        "authority": authority,
    }
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(VERIFY_URL, json=payload) as resp:
                data = await resp.json(content_type=None)
    except Exception:
        logger.exception("خطای شبکه در استعلامِ پرداختِ زرین‌پال")
        return False, "خطا در اتصال به زرین‌پال برای استعلام. بعداً دوباره امتحان کن."

    inner = (data or {}).get("data") or {}
    errors = (data or {}).get("errors") or {}
    code = inner.get("code")
    if code in _VERIFY_SUCCESS_CODES:
        ref_id = inner.get("ref_id") or inner.get("ref_id ") or ""
        # === PATCH_ZP_CARD_PAN_V1 ===
        # زرین‌پال شماره کارت پرداختکننده را هم میدهد (ماسکشده). قبلاً
        # دور ریخته میشد؛ حالا برای کانال لاگ و قفل کارتِ سطح ۱ نگهش
        # می‌داریم. امضای تابع عوض نشده تا فراخوانیهای موجود نشکنند —
        # کارت جداگانه در _LAST_CARD_PAN مینشیند.
        try:
            _LAST_CARD_PAN[str(authority)] = str(
                inner.get("card_pan") or inner.get("card_hash") or "")[:32]
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return True, str(ref_id)

    if isinstance(errors, dict):
        msg = errors.get("message") or str(errors) or "پرداخت تأیید نشد."
    elif isinstance(errors, list) and errors:
        msg = str(errors[0])
    else:
        msg = f"پرداخت تأیید نشد (کد: {code})."
    return False, msg
