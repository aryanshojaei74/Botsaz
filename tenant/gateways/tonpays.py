"""
tonpays.py — درگاه پرداخت TonPays برای ربات‌های مستأجر (PATCH_TONPAYS_V1).

TonPays یک سرویسِ «کارت‌به‌کارتِ خودکار» است: مبلغ به تومان است و درگاه به
هر فاکتور یک مبلغِ کمی متفاوت (final_amount) می‌دهد تا واریزِ بانکی را خودکار
تشخیص دهد.

API (مطابقِ آخرین مستنداتِ doc.tonpays.online — PATCH_TONPAYS_DOCS_V2):
  POST /api/v1/invoices/create           → invoice_id, final_amount, status,
                                           invoice_url (تلگرام), web_invoice_url (وب)
      بدنه: amount, order_id (حداکثر ۲۰), callback_url?, buyer_chat_id?
      با buyer_chat_id هر دو لینکِ invoice_url و web_invoice_url داده می‌شود.
  GET  /api/v1/invoices/check/{id}       → status, paid  (POST /invoices/check هم هست)
  GET  /health
  احراز هویت: هدر X-API-Key
  وضعیت‌ها: pending, processing, completed, action_required_less, action_required_more, rejected, expired, canceled
  لغو: POST /api/v1/invoices/{id}/cancel (فقط pending)  |  عودت: POST .../{id}/refund  |  رسیدِ مابقی: POST .../{id}/receipt
  فرمتِ خطا: {"detail": {"code": "...", "message": "..."}}
  سقف: ۶۰ ساخت/استعلام در دقیقه برایِ هر کلید، ۱۲۰ درخواست در دقیقه برایِ هر IP

=== PATCH_TONPAYS_CUSTOM_V1 === درگاهِ «کاستوم» (بستر تلگرام)
  کلیدِ مستقل (نوعِ کلیدِ کاستوم-تلگرام) و مسیرهایِ جدا:
  POST /api/custom/v1/invoices/telegram/create  (buyer_chat_id اجباری؛ پاسخ همیشه card_number/card_name دارد)
  GET  /api/custom/v1/invoices/check/{id}
  POST /api/custom/v1/invoices/{id}/receipt     (multipart، فیلدِ file، تصویر تا ۱۰ مگابایت)
  ⚠️ دکمهٔ «ارسال فیش» اجباری است؛ وقتی فاکتور خودکار تأیید نشد (need_action)
  خریدار باید بتواند فیش را بفرستد، وگرنه دسترسیِ درگاه بسته می‌شود.
  نوعِ درگاهِ هر فاکتور از پیشوندِ order_id تشخیص داده می‌شود (tc = کاستوم)، پس
  نیازی به ستونِ جدید/مهاجرتِ دیتابیس نیست.

⚠️ نکتهٔ امنیتیِ مهم و طراحیِ این ماژول:
  کال‌بکِ جدیدِ TonPays هدرهایِ X-TonPays-Signature / -Event / -Delivery-Id هم
  دارد، ولی الگوریتمِ امضا در مستندات نیامده؛ پس نمی‌شود درستی‌اش را راستی‌آزمایی
  کرد. بنابراین رویکردِ امن همان قبلی می‌ماند: بدنه/امضایِ کال‌بک ملاکِ شارژ نیست
  و هر کسی که بتواند درخواست بفرستد می‌تواند «paid: true»ِ جعلی بفرستد.

  برایِ همین در این پیاده‌سازی **بدنهٔ کال‌بک هرگز ملاکِ شارژ نیست**. کال‌بک
  فقط نقشِ «زنگِ بیدارباش» را دارد؛ بعد از آن خودمان مستقیماً از خودِ TonPays
  با /invoices/check وضعیت را می‌پرسیم و تازه اگر آنجا paid=true بود و مبلغ با
  چیزی که در دیتابیسِ خودمان ثبت کرده‌ایم جور بود، شارژ انجام می‌شود.
  دکمهٔ «✅ پرداخت کردم» هم دقیقاً از همین مسیرِ امن استفاده می‌کند.
"""
import asyncio
import secrets

from aiogram.enums import ButtonStyle  # PATCH_TENANT_ADMIN_COLORS_V1
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select, update as sa_update

from config import Session, WEBHOOK_BASE
from logging_setup import get_logger as _get_logger
from models import BotUser, TonPaysCharge, WalletTxn

logger = _get_logger(__name__)

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

API_BASE = "https://tonpays.online"
_TIMEOUT = 20

# === PATCH_TONPAYS_CUSTOM_V1 ===
CUSTOM_CREATE_PATH = "/api/custom/v1/invoices/telegram/create"
CUSTOM_CHECK_PATH = "/api/custom/v1/invoices/check/{id}"
CUSTOM_RECEIPT_PATH = "/api/custom/v1/invoices/{id}/receipt"
CUSTOM_CHANGE_CARD_PATH = "/api/custom/v1/invoices/{id}/change-card"   # PATCH_TONPAYS_CHANGE_CARD_V1
CHANGE_CARD_COOLDOWN = 60            # ثانیه؛ طبقِ مستندات (سمتِ درگاه هم اعمال می‌شود)
RECEIPT_MAX_BYTES = 10 * 1024 * 1024          # سقفِ مستندات: ۱۰ مگابایت
# پیشوندِ order_id: tc/pc = کاستوم (مستأجر/بات‌ساز)، tp/pt = معمولی
CUSTOM_ORDER_PREFIXES = ("tc", "pc")


def is_custom_order(order_id: str) -> bool:
    return (order_id or "").startswith(CUSTOM_ORDER_PREFIXES)

# === PATCH_TONPAYS_MIN_AMOUNT_V2 ===
# حداقلِ مبلغِ فاکتور طبقِ خودِ درگاه (خطایِ AMOUNT_TOO_LOW). API راهی برایِ
# خواندنش ندارد، پس این‌جا نگه داشته می‌شود.
#
# قبلاً یک ثابتِ هاردکد بود و وقتی درگاه سقف را از ۳۰ به ۲۰ هزار آورد،
# همهٔ ربات‌ها تا دیپلویِ بعدی گیر کردند. حالا پیش‌فرض است، نه قانون:
# مالکِ ربات می‌تواند از پنل عوضش کند، پس دفعهٔ بعد که درگاه تغییرش داد
# نیازی به به‌روزرسانیِ کد نیست.
MIN_AMOUNT = 20_000


async def get_min_amount(bot_id: int) -> int:
    """حداقلِ مبلغِ این ربات (تومان). صفر/نامعتبر → پیش‌فرض."""
    from tenant import get_setting
    try:
        n = int(float(await get_setting(bot_id, "tonpays_min_amount", "") or 0))
        return n if n > 0 else MIN_AMOUNT
    except (TypeError, ValueError):
        return MIN_AMOUNT

tonpays_router = Router(name="tonpays")


# ⚠️ سلبِ مسئولیت — طبقِ تصمیمِ صریحِ بات‌ساز، هرجا این درگاه به فروشنده
# نمایش داده می‌شود باید همین متن هم دیده شود.
DISCLAIMER = (
    "⚠️ <b>نکتهٔ امنیتی که باید بدانی:</b> کال‌بکِ این درگاه یک هدرِ امضا هم "
    "دارد، ولی روشِ راستی‌آزماییِ آن در مستنداتِ درگاه منتشر نشده. برایِ همین "
    "ربات هیچ‌وقت به کال‌بک اعتماد نمی‌کند و هر پرداخت را مستقیم از خودِ "
    "TonPays دوباره استعلام می‌گیرد، بعد شارژ می‌کند. کلیدت را جایی منتشر "
    "نکن؛ همان کلید اجازهٔ ساخت و خواندنِ فاکتورهایت را می‌دهد.\n\n"
    "📌 <b>سلبِ مسئولیت:</b> این درگاه <b>به درخواستِ خودِ کاربران</b> به "
    "بات‌ساز اضافه شده و متعلق به بات‌ساز نیست و توسطِ بات‌ساز اداره یا تایید "
    "نمی‌شود. اگر این درگاه از نظرِ امنیتی یا مالی مشکلی داشته باشد — از جمله "
    "لو رفتنِ اطلاعات، پرداختِ تاییدنشده، تسویه‌نشدنِ وجه، یا تعطیل‌شدنِ سرویس — "
    "<b>بات‌ساز هیچ‌گونه مسئولیتی نمی‌پذیرد</b> و امکانِ پیگیری یا بازگرداندنِ "
    "وجه از طریقِ بات‌ساز وجود ندارد. استفاده از آن کاملاً اختیاری و با "
    "مسئولیت و ریسکِ خودِ شماست؛ اگر این شرایط را می‌پذیرید از آن استفاده کنید."
)


# === PATCH_TONPAYS_DISPLAY_NAME_V1 ===
# گزارشِ پشتیبانی: کاربرِ نهایی «تون‌پیز» را با نامِ یک بانک اشتباه گرفته
# بود. دقیقاً مثلِ آبان گیت‌وی، فروشنده باید بتواند برچسبِ نمایشی را عوض
# کند؛ خودِ اتصال/API دست‌نخورده می‌ماند.
DEFAULT_DISPLAY_NAME = "تون‌پیز"


async def get_display_name(bot_id: int) -> str:
    """اسمی که به‌جایِ «تون‌پیز» به کاربر نشان داده می‌شود (کیفِ پول و
    پرداختِ محصول). خالی یعنی همان پیش‌فرض."""
    from tenant import get_setting
    v = (await get_setting(bot_id, "tonpays_display_name", "") or "").strip()
    return v[:40] if v else DEFAULT_DISPLAY_NAME


# === PATCH_TONPAYS_USER_NOTE_V1 ===
# پیشنهادِ #126: متنی که به کاربرِ نهایی موقعِ شارژ نشان داده می‌شود («این
# درگاه متعلق به این ربات یا بات‌ساز نیست…») قابلِ تغییر باشد. سلبِ مسئولیتِ
# سمتِ فروشنده (DISCLAIMER) دست نمی‌خورد و هنوز پیش از فعال‌سازی پذیرفته
# می‌شود؛ فقط متنِ سمتِ مشتری در اختیارِ فروشنده است.
# === PATCH_TONPAYS_NO_DEFAULT_NOTE_V1 === به‌درخواستِ فروشنده‌ها: متنِ پیش‌فرضِ «شخصِ ثالث…» حذف شد.
# فروشنده هنوز می‌تواند متنِ دلخواه بگذارد (دکمهٔ «📝 متنِ توضیحی»).
DEFAULT_USER_NOTE = ""
USER_NOTE_MAX = 600


async def get_user_note(bot_id: int) -> str:
    """متنِ آماده برایِ HTML؛ رشتهٔ خالی یعنی «نمایش نده»."""
    import html as _html
    from tenant import get_setting
    v = (await get_setting(bot_id, "tonpays_user_note", "") or "").strip()
    if v == "off":
        return ""
    if v:
        return _html.escape(v)
    return DEFAULT_USER_NOTE


class TonPaysFSM(StatesGroup):
    api_key = State()   # ادمین: واردکردنِ کلید
    amount = State()    # کاربر: مبلغِ شارژ
    confirm = State()   # === PATCH_EXACT_AMOUNT_ALERT_V3 === کاربر: تأییدِ «ساخت فاکتور» (با آلارمِ مبلغِ دقیق)
    card = State()      # ادمین: شمارهٔ کارتِ مقصد (دستی)
    holder = State()    # ادمین: نامِ صاحبِ کارت
    display_name = State()  # === PATCH_TONPAYS_DISPLAY_NAME_V1 ===
    min_amount = State()    # === PATCH_TONPAYS_MIN_AMOUNT_V2 ===
    user_note = State()     # === PATCH_TONPAYS_USER_NOTE_V1 ===
    receipt = State()       # === PATCH_TONPAYS_CUSTOM_V1 === کاربر: ارسالِ تصویرِ فیش
    refund_card = State()   # === PATCH_TONPAYS_PENDING_V1 === کاربر: شمارهٔ کارتِ عودتِ مبلغِ بیشتر
    refund_holder = State() # کاربر: نامِ صاحبِ کارتِ عودت


# ═══════════════════════ کلاینتِ API ═══════════════════════

async def _request(method: str, path: str, api_key: str,
                   json_body: dict | None = None) -> tuple[int, dict]:
    if aiohttp is None:
        raise RuntimeError("aiohttp نصب نیست.")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    url = API_BASE + path
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.request(method, url, headers=headers, json=json_body) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {"raw": (await r.text())[:300]}
            return r.status, (data if isinstance(data, dict) else {"data": data})


# === PATCH_TONPAYS_DOCS_V2 ===
class TonPaysError(RuntimeError):
    """خطایِ درگاه با کدِ ماشینی (مثلاً RATE_LIMIT_EXCEEDED). زیرکلاسِ
    RuntimeError است تا همهٔ except/RuntimeErrorهایِ قبلی بی‌تغییر کار کنند."""

    def __init__(self, message: str, code: str = "", status: int = 0, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.data = data or {}


# پیامِ فارسیِ کدهایِ خطایِ مستندات (فقط برایِ نمایش به کاربر/مالک)
ERROR_FA = {
    "MISSING_API_KEY": "کلید API ارسال نشده است.",
    "INVALID_API_KEY": "کلید API معتبر نیست. از پنل/رباتِ TonPays کلیدِ تازه بگیر.",
    "INACTIVE_API_KEY": "این کلید API غیرفعال شده است.",
    "WRONG_API_KEY_KIND": "نوعِ کلید اشتباه است؛ کلیدِ متناسب با نوعِ درگاه (معمولی / کاستوم-تلگرام) را وارد کن.",
    # === PATCH_TONPAYS_CUSTOM_V1 ===
    "GATEWAY_NOT_APPROVED": "درگاهِ کاستومِ تو هنوز توسطِ TonPays تأیید نشده است.",
    "DOMAIN_NOT_APPROVED": "دامنهٔ وب تأیید نشده است.",
    "ORIGIN_DENIED": "Origin مجاز نیست.",
    "INVALID_RECEIPT_TYPE": "فایلِ ارسالی تصویرِ معتبر نیست؛ فیش را به‌صورتِ عکس (JPG/PNG) بفرست.",
    "RECEIPT_TOO_LARGE": "حجمِ فیش بیشتر از ۱۰ مگابایت است؛ عکسِ کم‌حجم‌تری بفرست.",
    "ACCOUNT_NOT_VERIFIED": "حسابِ TonPays هنوز تأیید نشده است.",
    "ACCOUNT_SUSPENDED": "حسابِ TonPays تعلیق شده است.",
    "STORE_INACTIVE": "فروشگاهِ TonPays غیرفعال است.",
    "DUPLICATE_ORDER_ID": "شناسهٔ سفارش تکراری است؛ دوباره تلاش کن.",
    "AMOUNT_TOO_HIGH": "مبلغ بیشتر از سقفِ مجازِ درگاه است.",
    "INVALID_BUYER_CHAT_ID": "شناسهٔ تلگرامِ خریدار نامعتبر است.",
    # === PATCH_TONPAYS_MERCHANT_BUYER_V1 ===
    "BUYER_IS_MERCHANT": ("درگاه اجازه نمی‌دهد صاحبِ حسابِ تون‌پیز (خودِ فروشنده) برایِ خودش فاکتور بسازد. "
                          "برایِ تست از یک اکانتِ تلگرامیِ دیگر استفاده کن؛ مشتری‌هایِ عادی این مشکل را ندارند."),
    "INVALID_CALLBACK_URL": "آدرسِ وب‌هوک نامعتبر است.",
    "RATE_LIMIT_EXCEEDED": "تعدادِ درخواست‌ها به درگاه زیاد شد؛ چند لحظه بعد دوباره تلاش کن.",
    "INVOICE_NOT_FOUND": "فاکتور در درگاه پیدا نشد.",
    "ACCESS_DENIED": "دسترسی به این فاکتور ندارید.",
    # === PATCH_TONPAYS_PENDING_V1 === آپدیتِ ۱۸ اکتبرِ ۲۰۲۶: هر خریدار هم‌زمان فقط یک فاکتورِ باز
    "BUYER_HAS_PENDING_INVOICE": "یک فاکتورِ پرداخت‌نشده در درگاه داری؛ باید اول لغوش کنی.",
    "BUYER_HAS_PROCESSING_INVOICE": ("پرداختِ قبلیِ تو در انتظارِ تأییدِ درگاه است؛ تا نتیجه مشخص نشود "
                                     "فاکتورِ جدید ساخته نمی‌شود. چند دقیقه بعد دوباره امتحان کن."),
    "BUYER_HAS_NEED_ACTION_LESS": ("پرداختِ قبلیِ تو کمتر از مبلغِ فاکتور بوده. اول مابقی را واریز کن "
                                   "(با «✅ پرداخت کردم» جزئیات را ببین)، بعد فاکتورِ جدید بساز."),
    "BUYER_HAS_NEED_ACTION_MORE": ("پرداختِ قبلیِ تو بیشتر از مبلغِ فاکتور بوده. اول کارتِ عودتِ مبلغِ اضافه را "
                                   "ثبت کن، بعد فاکتورِ جدید بساز."),
    "BUYER_INVOICE_BUSY": "ساختِ فاکتور برایِ تو در جریان است؛ چند ثانیه بعد دوباره امتحان کن.",
    "CANCEL_NOT_ALLOWED_PROCESSING": "این فاکتور در انتظارِ تأیید است و لغو نمی‌شود.",
    "CANCEL_NOT_ALLOWED_NEED_ACTION_LESS": "این فاکتور (مبلغِ کمتر) لغو نمی‌شود؛ مابقی را واریز کن.",
    "CANCEL_NOT_ALLOWED_NEED_ACTION_MORE": "این فاکتور (مبلغِ بیشتر) لغو نمی‌شود؛ کارتِ عودت را ثبت کن.",
    "INVOICE_ALREADY_CANCELED": "این فاکتور قبلاً لغو شده است.",
    "INVOICE_EXPIRED": "این فاکتور منقضی شده است.",
    "REFUND_NOT_ALLOWED": "عودت فقط برایِ پرداختِ «مبلغِ بیشتر» ممکن است.",
    "REFUND_AMOUNT_UNKNOWN": "مبلغِ مازاد هنوز در درگاه ثبت نشده؛ کمی بعد دوباره امتحان کن.",
    "INVALID_REFUND_CARD": "شمارهٔ کارت نامعتبر است.",
    "INVALID_REFUND_HOLDER": "نامِ صاحبِ کارت لازم است.",
    "INVOICE_ID_MISMATCH": "شناسهٔ فاکتور ارسالی با آدرس درخواست یکی نیست.",   # PATCH_TONPAYS_DOCS_AUDIT_V1
    "REFUND_ALREADY_OPEN": "درخواستِ عودت برایِ این فاکتور قبلاً ثبت شده است.",
    "NOT_PENDING": "رسید فقط وقتی پذیرفته می‌شود که فاکتورِ مابقی در انتظارِ پرداخت باشد.",
    "REMAINDER_INVOICE_MISSING": "فاکتورِ مابقی برایِ این پرداخت باز نیست.",
    "EMPTY_RECEIPT": "فایلِ رسید خالی است.",
}
BUYER_BLOCK_CODES = ("BUYER_HAS_PENDING_INVOICE", "BUYER_HAS_PROCESSING_INVOICE",
                     "BUYER_HAS_NEED_ACTION_LESS", "BUYER_HAS_NEED_ACTION_MORE", "BUYER_INVOICE_BUSY")

# وضعیت‌هایِ نهاییِ ناموفق طبقِ مستندات
TERMINAL_FAIL_STATUSES = {"rejected": "rejected", "expired": "expired", "canceled": "cancelled"}
STATUS_FA = {
    "pending": "در انتظارِ پرداخت",
    "processing": "واریز دیده شد و در انتظارِ تأیید است",
    "completed": "تأیید شد",
    "need_action": "نیاز به اقدام دارد (به پشتیبانیِ درگاه پیام بده)",
    "action_required_less": "مبلغِ کمتر واریز شده؛ مابقی را واریز کن",
    "action_required_more": "مبلغِ بیشتر واریز شده؛ کارتِ عودت را ثبت کن",
    "rejected": "ردّ شد",
    "expired": "منقضی شد",
    "canceled": "لغو شد",
}


def _err_code(data: dict) -> str:
    """کدِ خطا را از هر دو قالبِ قدیم و جدید بیرون می‌کشد."""
    d = data.get("detail")
    if isinstance(d, dict) and d.get("code"):
        return str(d["code"])
    e = data.get("error")
    if isinstance(e, dict) and e.get("code"):
        return str(e["code"])
    return ""


def _is_merchant_buyer_err(data) -> bool:
    """«buyer_chat_id cannot be the merchant Telegram id» — خریدار همان صاحبِ حسابِ درگاه است."""
    try:
        import json as _j
        return "merchant telegram id" in _j.dumps(data, ensure_ascii=False).lower()
    except Exception:
        return False


def _err_text(data: dict, status: int, min_amount: int | None = None) -> str:
    """پیامِ خطایِ خوانا از پاسخِ درگاه بیرون می‌کشد.

    قالبِ خطایِ TonPays: {"success": false, "error": {"code": ..., "message": ...}}
    قبلاً کلِ دیکشنری خام به کاربر نشان داده می‌شد که برایِ کاربرِ نهایی
    بی‌معنی بود.
    """
    # === PATCH_TONPAYS_DOCS_V2 === قالبِ جدید: {"detail": {"code", "message"}}
    _d = data.get("detail")
    if isinstance(_d, dict):
        _code = _d.get("code") or ""
        if _code == "AMOUNT_TOO_LOW":
            return ("مبلغ کمتر از حداقلِ مجازِ درگاه است "
                    f"({(min_amount or MIN_AMOUNT):,} تومان).")
        if _code in ERROR_FA:
            return ERROR_FA[_code]
        if _d.get("message"):
            return str(_d["message"])
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code")
        if msg:
            code = err.get("code") or ""
            if code == "AMOUNT_TOO_LOW":
                # === PATCH_TONPAYS_MIN_AMOUNT_V2 ===
                # این تابع سنکرون است و bot_id ندارد، پس عدد از بیرون
                # تزریق می‌شود؛ اگر ندادند، پیش‌فرضِ ماژول.
                return ("مبلغ کمتر از حداقلِ مجازِ درگاه است "
                        f"({(min_amount or MIN_AMOUNT):,} تومان).")
            return str(msg)
    detail = data.get("detail")
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict) and first.get("msg"):
            return str(first["msg"])
    if isinstance(detail, str):
        return detail
    return f"خطای HTTP {status}"


# === PATCH_TONPAYS_PENDING_V1 === (آپدیتِ ۱۸ اکتبرِ ۲۰۲۶)
# از این پس هر خریدار هم‌زمان فقط یک فاکتورِ «باز» دارد؛ ساختِ فاکتورِ دوم با BUYER_HAS_* رد می‌شود.
def _buyer_block_code(data: dict) -> str:
    code = _err_code(data).upper()
    return code if code in BUYER_BLOCK_CODES else ""


async def health() -> bool:
    if aiohttp is None:
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(API_BASE + "/health") as r:
                return r.status == 200
    except Exception:
        return False


async def create_invoice(api_key: str, amount_toman: int, order_id: str,
                         callback_url: str | None = None,
                         buyer_chat_id: int | None = None,
                         custom: bool = False) -> dict:
    """ساختِ فاکتور. amount به تومان و صحیح و بزرگ‌تر از صفر.

    === PATCH_TONPAYS_DOCS_V2 ===
    buyer_chat_id (اختیاری): شناسهٔ تلگرامِ خریدار. با آن، پاسخ هم invoice_url
    (لینکِ ربات) و هم web_invoice_url (صفحهٔ وب) دارد. اگر درگاه شناسه را
    نپذیرفت (INVALID_BUYER_CHAT_ID)، یک‌بار بدونِ آن دوباره تلاش می‌شود تا
    ساختِ فاکتور به‌خاطرِ یک فیلدِ اختیاری نشکند."""
    body: dict = {"amount": int(amount_toman), "order_id": order_id[:20]}
    if callback_url:
        body["callback_url"] = callback_url
    if buyer_chat_id:
        body["buyer_chat_id"] = int(buyer_chat_id)
    # === PATCH_TONPAYS_CUSTOM_V1 === کاستوم-تلگرام: مسیرِ جدا و buyer_chat_id اجباری
    if custom:
        if not buyer_chat_id:
            raise TonPaysError(ERROR_FA["INVALID_BUYER_CHAT_ID"], "INVALID_BUYER_CHAT_ID", 0)
        status, data = await _request("POST", CUSTOM_CREATE_PATH, api_key, body)
        if status in (200, 201):
            return data
        if _is_merchant_buyer_err(data):   # PATCH_TONPAYS_MERCHANT_BUYER_V1
            raise TonPaysError(ERROR_FA["BUYER_IS_MERCHANT"], "BUYER_IS_MERCHANT", status)
        code = _err_code(data)
        _bc = _buyer_block_code(data)   # PATCH_TONPAYS_PENDING_V1
        if _bc:
            raise TonPaysError(ERROR_FA[_bc], _bc, status, data)
        if status in (401, 403) and not code:
            raise TonPaysError(ERROR_FA["INVALID_API_KEY"], "INVALID_API_KEY", status)
        raise TonPaysError(_err_text(data, status), code, status)
    status, data = await _request("POST", "/api/v1/invoices/create", api_key, body)
    if status in (200, 201):
        return data
    code = _err_code(data)
    if buyer_chat_id and (code == "INVALID_BUYER_CHAT_ID" or _is_merchant_buyer_err(data)):
        body.pop("buyer_chat_id", None)
        status, data = await _request("POST", "/api/v1/invoices/create", api_key, body)
        if status in (200, 201):
            return data
        code = _err_code(data)
    _bc = _buyer_block_code(data)   # PATCH_TONPAYS_PENDING_V1
    if _bc:
        raise TonPaysError(ERROR_FA[_bc], _bc, status, data)
    if status in (401, 403) and not code:
        raise TonPaysError("کلید API معتبر نیست (INVALID_API_KEY). از منویِ رباتِ "
                           "TonPays یک کلیدِ تازه بگیر.", "INVALID_API_KEY", status)
    raise TonPaysError(_err_text(data, status), code, status)


async def check_invoice(api_key: str, invoice_id: str, custom: bool = False) -> dict:
    """استعلامِ مستقلِ وضعیتِ فاکتور — تنها منبعِ معتبر برایِ تاییدِ پرداخت."""
    path = (CUSTOM_CHECK_PATH if custom else "/api/v1/invoices/check/{id}").format(id=invoice_id)
    status, data = await _request("GET", path, api_key)
    if status == 200:
        return data
    raise TonPaysError(_err_text(data, status), _err_code(data), status)


def is_paid(info: dict) -> bool:
    """پرداختِ تأییدشده طبقِ مستندات: paid=true یا status=completed."""
    return bool(info.get("paid")) or (info.get("status") == "completed")


# === PATCH_TONPAYS_PENDING_V1 === لغوِ فاکتور در خودِ درگاه (مطابقِ مستنداتِ ۱۸ اکتبرِ ۲۰۲۶)
CANCEL_PATH = "/api/v1/invoices/{id}/cancel"
CUSTOM_CANCEL_PATH = "/api/custom/v1/invoices/{id}/cancel"
REFUND_PATH = "/api/v1/invoices/{id}/refund"
CUSTOM_REFUND_PATH = "/api/custom/v1/invoices/{id}/refund"
STD_RECEIPT_PATH = "/api/v1/invoices/{id}/receipt"        # فقط برایِ رسیدِ مابقی (action_required_less)
NEED_ACTION_STATUSES = ("need_action", "action_required_less", "action_required_more")


async def cancel_invoice(api_key: str, invoice_id: str, custom: bool = False) -> dict:
    """لغوِ فاکتور — بدونِ بدنه؛ فقط فاکتورِ pending لغو می‌شود (بقیه ۴۰۹ با CANCEL_NOT_ALLOWED_*)."""
    path = (CUSTOM_CANCEL_PATH if custom else CANCEL_PATH).format(id=invoice_id)
    status, data = await _request("POST", path, api_key, None)
    if status in (200, 201):
        return data
    code = _err_code(data)
    raise TonPaysError(ERROR_FA.get(code) or _err_text(data, status), code, status, data)


async def cancel_at_gateway(api_key: str, invoice_id: str, custom: bool = False) -> tuple[str, str]:
    """لغوِ امنِ فاکتور: اول استعلام (اگر پرداخت شده، لغو نمی‌شود).
    خروجی: ("paid"|"cancelled"|"blocked"|"retry"|"error", توضیح). «retry» = خطای موقت درگاه/شبکه؛ فاکتور هنوز باز است. «blocked» = درگاه لغو را نپذیرفت چون فاکتور
    در انتظارِ تأیید / مبلغِ کمتر / مبلغِ بیشتر است (کاربر واریز کرده؛ نباید فاکتورِ جدید بسازد)."""
    if not api_key or not invoice_id:
        return "error", "درگاه تنظیم نشده است."
    try:
        info = await check_invoice(api_key, invoice_id, custom=custom)
        if is_paid(info):
            return "paid", ""
        st = info.get("status")
        if st in TERMINAL_FAIL_STATUSES:
            return "cancelled", ""        # قبلاً بسته شده (لغو/منقضی/ردّ)
        if st in ("processing",) + NEED_ACTION_STATUSES:
            return "blocked", ERROR_FA.get("CANCEL_NOT_ALLOWED_" + st.upper().replace("ACTION_REQUIRED_", "NEED_ACTION_"),
                                           "این فاکتور لغو نمی‌شود.")
    except TonPaysError as e:
        if e.code == "INVOICE_NOT_FOUND":
            return "cancelled", ""        # === PATCH_TONPAYS_DOCS_AUDIT_V1 === فاکتوری نیست که باز مانده باشد
        logger.info("tonpays cancel: precheck failed %s: %s", invoice_id, e)
    try:
        await cancel_invoice(api_key, invoice_id, custom)
        return "cancelled", ""
    except TonPaysError as e:
        if e.code in ("INVOICE_ALREADY_CANCELED", "INVOICE_EXPIRED", "INVOICE_NOT_FOUND"):
            return "cancelled", ""
        if (e.code or "").startswith("CANCEL_NOT_ALLOWED"):
            return "blocked", str(e)
        # === PATCH_TONPAYS_DOCS_AUDIT_V1 === خطای موقت (سقف درخواست / مشغول / ۵xx): فاکتور هنوز در درگاه باز است
        # و مستندات می‌گوید تا آن باز است فاکتور جدید ساخته نمی‌شود ⇒ نباید محلی «لغوشده» حساب شود.
        if e.code in ("RATE_LIMIT_EXCEEDED", "BUYER_INVOICE_BUSY") or int(e.status or 0) >= 500:
            return "retry", str(e)
        return "error", str(e)
    except Exception as e:  # noqa: BLE001 — خطای شبکه (فاکتور ممکن است هنوز باز باشد)
        logger.warning("tonpays cancel failed %s: %s", invoice_id, e)
        return "retry", str(e)


async def refund_card(api_key: str, invoice_id: str, card_number: str, card_holder: str,
                      custom: bool = False) -> dict:
    """ثبتِ کارتِ عودتِ مبلغِ بیشتر (فقط action_required_more). مبلغ فرستاده نمی‌شود؛
    با ذخیرهٔ کارت فاکتور completed و paid=true می‌شود."""
    path = (CUSTOM_REFUND_PATH if custom else REFUND_PATH).format(id=invoice_id)
    status, data = await _request("POST", path, api_key, {
        "invoice_id": invoice_id, "card_number": card_number, "card_holder": card_holder})
    if status in (200, 201):
        return data
    code = _err_code(data)
    raise TonPaysError(ERROR_FA.get(code) or _err_text(data, status), code, status, data)


_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def norm_card(text: str) -> str:
    """شمارهٔ کارتِ ۱۶رقمی از متنِ کاربر (ارقامِ فارسی/عربی، فاصله و خط‌تیره حذف می‌شود)؛ نامعتبر ⇒ ''."""
    d = "".join(c for c in (text or "").translate(_FA_DIGITS) if c.isdigit())
    return d if len(d) == 16 else ""


def _amount(info: dict, key: str) -> int:
    try:
        return max(0, int(float(info.get(key) or 0)))
    except (TypeError, ValueError):
        return 0


def needs_action_notice(bot_id: int, order_id: str) -> bool:
    """آیا باید پیامِ «مبلغِ کمتر/بیشتر / نیاز به فیش» برایِ این فاکتور فرستاد؟
    وضعیتِ «need_action» در درگاهِ عادی قبلاً همیشه رد می‌شد؛ ولی اگر درگاه همراهش need_action_kind (less/more) داده باشد
    (واریزِ کمتر/بیشتر) خریدار باید مابقی/عودت را ببیند — نبودنِ این پیام همان گزارشِ «برایِ مشتری فاکتورِ مابقی صادر نشد» بود."""
    key = (bot_id, order_id)
    st = _LAST_STATUS.get(key)
    if st not in NEED_ACTION_STATUSES:
        return False
    if st != "need_action" or is_custom_order(order_id):
        return True
    return (_LAST_INFO.get(key) or {}).get("need_action_kind") in ("less", "more")


def need_action_hint(info: dict) -> str:
    """توضیحِ کوتاهِ وضعیتِ «مبلغِ کمتر/بیشتر» برایِ پیامِ «پرداخت کردم»."""
    st = info.get("status")
    if st == "action_required_less" or info.get("need_action_kind") == "less":
        rem = _amount(info, "remaining_amount")
        return (f" مبلغِ واریزی کمتر از فاکتور بوده؛ مابقی: {rem:,} تومان — "
                "از دکمهٔ «ℹ️ جزئیاتِ مابقی» ببین."
                if rem else " مبلغِ واریزی کمتر از فاکتور بوده.")
    if st == "action_required_more" or info.get("need_action_kind") == "more":
        exc = _amount(info, "refund_amount")
        return (f" مبلغِ واریزی {exc:,} تومان بیشتر از فاکتور بوده؛ برایِ عودتِ مازاد «💳 ثبتِ کارتِ عودت» را بزن."
                if exc else " مبلغِ واریزی بیشتر از فاکتور بوده؛ «💳 ثبتِ کارتِ عودت» را بزن.")
    if st == "rejected":
        rs = [str(x) for x in (info.get("reject_reasons") or []) if x]
        return (" دلیل: " + "؛ ".join(rs)) if rs else ""
    return ""


def action_notice(prefix: str, order_id: str, info: dict, custom: bool) -> tuple[str, InlineKeyboardMarkup]:
    """پیامِ اعلانِ وضعیت‌هایِ «نیاز به اقدام» + دکمه‌ها. prefix: tonp | ptonp."""
    st = info.get("status")
    if st == "action_required_less" or info.get("need_action_kind") == "less":
        rem = _amount(info, "remaining_amount")
        card = str(info.get("remaining_card_number") or "")
        holder = str(info.get("remaining_card_holder") or "")
        lines = [f"⚠️ واریزِ فاکتورِ <code>{order_id}</code> <b>کمتر از مبلغِ فاکتور</b> بوده.", ""]
        if rem:
            lines.append(f"💵 مابقی: <b>{rem * 10:,} ریال</b> (= {rem:,} تومان)")
        if card:
            lines += ["💳 واریز به کارتِ زیر:", f"<code>{card}</code>"]
            if holder:
                lines.append(f"🧑 به نام: <b>{holder}</b>")
        got = bool(info.get("remaining_receipt_received"))    # PATCH_TONPAYS_DOCS_AUDIT_V1 — رسید مابقی قبلا رفته
        lines += ["", ("✅ رسید مابقی را فرستاده‌ای و در انتظار بررسی درگاه است؛ بعد از تأیید، کیف پولت شارژ می‌شود."
                       if got else
                       "بعد از واریز مابقی، پرداخت خودکار تأیید می‌شود. اگر نشد، «📎 ارسال رسید مابقی» را بزن "
                       "و تصویر رسید را بفرست.")]
        rows = []
        copy = []
        if rem:
            copy.append(InlineKeyboardButton(text="📋 کپی مبلغ (ریال)", copy_text=CopyTextButton(text=str(rem * 10))))
        digits = "".join(c for c in card if c.isdigit())
        if digits:
            copy.append(InlineKeyboardButton(text="📋 کپی شماره کارت", copy_text=CopyTextButton(text=digits)))
        if copy:
            rows.append(copy)
        if not got:
            rows.append([InlineKeyboardButton(text="📎 ارسال رسید مابقی", callback_data=f"{prefix}:rcpt:{order_id}")])
        rows.append([InlineKeyboardButton(text="✅ واریز کردم، بررسی کن", callback_data=f"{prefix}:verify:{order_id}")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)
    if st == "action_required_more" or info.get("need_action_kind") == "more":
        exc = _amount(info, "refund_amount")
        txt = (f"⚠️ واریزِ فاکتورِ <code>{order_id}</code> <b>بیشتر از مبلغِ فاکتور</b> بوده.\n\n"
               + (f"💵 مازاد: <b>{exc:,} تومان</b>\n" if exc else "")
               + "برایِ عودتِ مازاد، شمارهٔ کارت و نامِ صاحبِ کارتِ خودت را ثبت کن؛ بعد از ثبت، پرداختت تأیید "
                 "و کیف پول شارژ می‌شود و مازاد به کارتت برمی‌گردد.")
        return txt, InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 ثبتِ کارتِ عودت", callback_data=f"{prefix}:rfnd:{order_id}")]])
    # need_action قدیمی (درگاهِ کاستوم): فیش بخواه
    return (NEED_ACTION_TEXT.format(oid=order_id),
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📎 ارسال فیش واریزی", callback_data=f"{prefix}:rcpt:{order_id}")]]))


def pending_text(amount: int | None = None) -> str:
    amt = f" به مبلغِ <b>{int(amount):,} تومان</b>" if amount else ""
    return (f"⚠️ شما یک پرداختِ ریالیِ نیمه‌کاره{amt} در درگاه دارید که در ربات ثبت شده است. "
            "تا آن باز است، پرداختِ جدید ساخته نمی‌شود.\n\n"
            "اگر به آن واریز کرده‌اید، <b>لغو نکنید</b> و با پشتیبانی در تماس باشید؛ "
            "در غیرِ این صورت «لغو و پرداختِ جدید» را بزنید.")


RETRY_CANCEL_TEXT = ("⚠️ لغو فاکتور در درگاه الان انجام نشد (مشکل موقت ارتباط). فاکتور هنوز باز است؛ "
                     "چند لحظه بعد دوباره «لغو و پرداخت جدید» را بزن.")


def pending_kb(prefix: str, order_id: str, amount: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ واریز کرده‌ام، بررسی کن", callback_data=f"{prefix}:verify:{order_id}")],
        [InlineKeyboardButton(text="❌ لغو و پرداخت جدید", callback_data=f"{prefix}:cnew:{order_id}:{int(amount)}",
                              style=ButtonStyle.DANGER)],
    ])


# === PATCH_TONPAYS_CHANGE_CARD_V1 ===
async def change_card(api_key: str, invoice_id: str) -> dict:
    """تعویضِ کارتِ فاکتور (موتورِ تعویضِ کارتِ TonPays، کول‌داونِ ۶۰ثانیه، حذفِ کارت‌هایِ
    استفاده‌شده). پاسخ: card_number/card_name تازه + show_change_card +
    change_card_cooldown_seconds + change_card_exhausted. فقط درگاهِ کاستوم."""
    status, data = await _request(
        "POST", CUSTOM_CHANGE_CARD_PATH.format(id=invoice_id), api_key, {})
    if status in (200, 201):
        return data
    code = _err_code(data)
    raise TonPaysError(ERROR_FA.get(code) or _err_text(data, status), code, status)


# === PATCH_TONPAYS_CUSTOM_V1 ===
async def upload_receipt(api_key: str, invoice_id: str, data: bytes,
                         filename: str = "receipt.jpg",
                         content_type: str = "image/jpeg", custom: bool = True) -> dict:
    """ارسالِ تصویرِ فیشِ واریزی (multipart، فیلدِ file، حداکثر ۱۰ مگابایت).

    پاسخِ موفق: status=processing و receipt_received=true. خطا → TonPaysError
    با کدهایِ INVALID_RECEIPT_TYPE / RECEIPT_TOO_LARGE / INVOICE_NOT_FOUND / ..."""
    if aiohttp is None:
        raise RuntimeError("aiohttp نصب نیست.")
    if len(data) > RECEIPT_MAX_BYTES:
        raise TonPaysError(ERROR_FA["RECEIPT_TOO_LARGE"], "RECEIPT_TOO_LARGE", 413)
    form = aiohttp.FormData()
    form.add_field("file", data, filename=filename, content_type=content_type)
    # Content-Type را نباید دستی گذاشت؛ aiohttp مرزِ multipart را خودش می‌سازد
    headers = {"X-API-Key": api_key}
    timeout = aiohttp.ClientTimeout(total=60)
    url = API_BASE + (CUSTOM_RECEIPT_PATH if custom else STD_RECEIPT_PATH).format(id=invoice_id)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(url, headers=headers, data=form) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = {"raw": (await r.text())[:300]}
            if not isinstance(body, dict):
                body = {"data": body}
            if r.status in (200, 201):
                return body
            code = _err_code(body)
            if r.status == 413 and not code:
                code = "RECEIPT_TOO_LARGE"
            if r.status in (401, 403) and not code:
                code = "INVALID_API_KEY"
            msg = ERROR_FA.get(code) or _err_text(body, r.status)
            raise TonPaysError(msg, code, r.status)


async def validate_api_key(api_key: str, custom: bool = False) -> tuple[bool, str]:
    """چکِ سریعِ کلید با یک استعلامِ بی‌خطر رویِ فاکتورِ ناموجود.

    اگر کلید بد باشد ۴۰۱/۴۰۳ می‌گیریم؛ اگر کلید خوب باشد ۴۰۴ یا ۲۰۰ می‌گیریم.
    این‌طوری بدونِ ساختِ فاکتورِ الکی، اعتبارِ کلید تایید می‌شود.
    """
    if not await health():
        return False, "سرورِ TonPays در دسترس نیست (/health جواب نداد)."
    try:
        status, data = await _request(
            "GET",
            (CUSTOM_CHECK_PATH if custom else "/api/v1/invoices/check/{id}").format(id="TP-VALIDATION-0"),
            api_key)
    except Exception as e:
        return False, f"خطا در ارتباط: {e}"
    # === PATCH_TONPAYS_DOCS_V2 === تصمیم بر اساسِ «کدِ خطا»، نه فقط HTTP status
    code = _err_code(data)
    if code in ("INVOICE_NOT_FOUND", "ACCESS_DENIED"):
        return True, "کلید معتبر است و درگاه در دسترس است."
    if code in ERROR_FA:
        return False, ERROR_FA[code]
    if status in (401, 403):
        return False, "کلید API پذیرفته نشد (INVALID_API_KEY)."
    if status == 429:
        return False, ERROR_FA["RATE_LIMIT_EXCEEDED"]
    if status in (200, 404, 422):
        return True, "کلید معتبر است و درگاه در دسترس است."
    return False, f"پاسخِ غیرمنتظره از درگاه (HTTP {status})."


def _new_order_id(custom: bool = False) -> str:
    """شناسهٔ یکتا در سقفِ ۲۰ کاراکترِ مجازِ API (tc = کاستوم، tp = معمولی)."""
    return ("tc" if custom else "tp") + secrets.token_hex(6)          # ۱۴ کاراکتر


# === PATCH_TONPAYS_CUSTOM_V1 === نوعِ درگاهِ فعالِ هر ربات + کلیدِ مربوطه
async def is_custom_mode(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, "tonpays_mode", "") or "") == "custom"


async def get_key(bot_id: int, custom: bool) -> str:
    from tenant import get_setting
    return (await get_setting(
        bot_id, "tonpays_custom_key" if custom else "tonpays_api_key", "") or "")


def pick_card(inv: dict, manual_card: str = "", manual_name: str = "") -> tuple[str, str]:
    """شمارهٔ کارت را از پاسخِ درگاه برمی‌دارد؛ اگر ماسک‌شده بود («6037····1234»)
    و مالک کارتِ دستی ثبت کرده، آن را جایگزین می‌کند."""
    card = str(inv.get("card_number") or "")
    name = str(inv.get("card_name") or "")
    digits = "".join(ch for ch in card if ch.isdigit())
    if card and len(digits) < 16 and manual_card:
        return manual_card, (manual_name or name)
    return card, name


def callback_url() -> str | None:
    return f"{WEBHOOK_BASE}/tonpays/callback" if WEBHOOK_BASE else None


# ═══════════════════ هستهٔ امن: تایید و شارژ ═══════════════════

# آخرین وضعیتِ گزارش‌شدهٔ درگاه برایِ هر فاکتور (فقط برایِ اعلانِ «فیش بفرست»).
_LAST_STATUS: dict[tuple[int, str], str] = {}
_LAST_INFO: dict[tuple[int, str], dict] = {}          # PATCH_TONPAYS_PENDING_V1 — آخرین پاسخِ استعلام
_NEED_ACTION_NOTIFIED: set[tuple[int, str]] = set()
_RECEIPT_REMINDED: set[tuple[int, str]] = set()   # PATCH_TONPAYS_RECEIPT_REMINDER_V1


def receipt_kb(order_id: str) -> InlineKeyboardMarkup:
    """دکمهٔ اجباریِ ارسالِ فیش (درگاهِ کاستوم)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 ارسال فیش واریزی",
                              callback_data=f"tonp:rcpt:{order_id}")]])


NEED_ACTION_TEXT = (
    "⚠️ واریزِ فاکتورِ <code>{oid}</code> به‌صورتِ خودکار تأیید نشد.\n"
    "لطفاً <b>تصویرِ فیشِ واریزی</b> را با دکمهٔ زیر برایِ بررسی بفرست.")


async def settle_charge(bot_id: int, order_id: str) -> tuple[bool, str, int]:
    """تاییدِ مستقلِ پرداخت و شارژِ کیف پول.

    تنها نقطه‌ای است که موجودی را زیاد می‌کند. هم کال‌بکِ HTTP و هم دکمهٔ
    «پرداخت کردم» از همین‌جا رد می‌شوند، پس یک منطقِ امن بیشتر وجود ندارد.
    خروجی: (شارژ_شد؟، پیام، مبلغ)
    """
    from tenant import get_setting
    async with Session() as s:
        ch = await s.scalar(select(TonPaysCharge).where(
            TonPaysCharge.bot_id == bot_id, TonPaysCharge.order_id == order_id))
        if not ch:
            return False, "فاکتور پیدا نشد.", 0
        if ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        # فاکتورِ «لغوشده» عمداً مسدود نمی‌شود: اگر کاربر واقعاً واریز کرده و
        # بعد لغو زده، پولش نباید بسوزد — استعلام انجام می‌شود و اگر درگاه
        # پرداخت را تایید کرد، شارژ می‌شود.
        invoice_id, expected = ch.invoice_id, int(ch.request_amount)
        payer_tg = int(ch.user_telegram_id)

    _custom = is_custom_order(order_id)   # PATCH_TONPAYS_CUSTOM_V1
    api_key = await get_key(bot_id, _custom)
    if not api_key or not invoice_id:
        return False, "درگاه تنظیم نشده است.", 0

    # ← منبعِ حقیقت: خودِ TonPays، نه بدنهٔ کال‌بک
    try:
        info = await check_invoice(api_key, invoice_id, custom=_custom)
    except Exception as e:
        logger.warning("tonpays check failed for %s: %s", order_id, e)
        return False, f"استعلام از درگاه ناموفق بود: {e}", 0

    if not is_paid(info):
        st = info.get("status") or "نامشخص"
        _LAST_STATUS[(bot_id, order_id)] = st   # PATCH_TONPAYS_CUSTOM_V1
        _LAST_INFO[(bot_id, order_id)] = info
        # === PATCH_TONPAYS_DOCS_V2 === وضعیت‌هایِ نهاییِ ناموفق: پیگیری متوقف شود
        # (سقفِ ۶۰ استعلام در دقیقه؛ فاکتورِ مرده نباید سهمِ درگاه را بخورد).
        # عمداً فقط status محلی عوض می‌شود؛ دکمهٔ «پرداخت کردم» همچنان استعلام می‌گیرد.
        if st in TERMINAL_FAIL_STATUSES:
            try:
                async with Session() as s:
                    await s.execute(
                        sa_update(TonPaysCharge)
                        .where(TonPaysCharge.bot_id == bot_id,
                               TonPaysCharge.order_id == order_id,
                               TonPaysCharge.status == "waiting")
                        .values(status=TERMINAL_FAIL_STATUSES[st]))
                    await s.commit()
            except Exception:
                logger.exception("tonpays: ثبتِ وضعیتِ نهاییِ %s ناموفق بود", order_id)
        _hint = ""
        if _custom and st in ("pending", "need_action"):
            _hint = " اگر واریز کرده‌ای، «📎 ارسال فیش» را بزن و تصویرِ فیش را بفرست."
        if st in NEED_ACTION_STATUSES or st == "rejected":   # PATCH_TONPAYS_PENDING_V1
            _hint += need_action_hint(info)
        return False, f"هنوز پرداخت تایید نشده (وضعیت: {STATUS_FA.get(st, st)}).{_hint}", 0

    # مبلغ هم باید با چیزی که خودمان ثبت کرده‌ایم بخواند
    reported = int(info.get("request_amount") or 0)
    if reported and reported < expected:
        logger.error("tonpays amount mismatch %s: expected %s got %s",
                     order_id, expected, reported)
        return False, "مبلغِ اعلام‌شده با فاکتور نمی‌خواند؛ با پشتیبانی تماس بگیر.", 0

    credit = expected
    async with Session() as s:
        # قفلِ اتمی: فقط یک بار شارژ شود حتی اگر کال‌بک و دکمه هم‌زمان بیایند
        claim = await s.execute(
            sa_update(TonPaysCharge)
            .where(TonPaysCharge.bot_id == bot_id,
                   TonPaysCharge.order_id == order_id,
                   TonPaysCharge.status != "paid")
            .values(status="paid"))
        if claim.rowcount == 0:
            await s.rollback()
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id,
            BotUser.telegram_id == payer_tg).with_for_update())
        if u:
            u.balance = float(u.balance or 0) + credit
            s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=credit,
                            kind="tonpays_charge", note="شارژ با درگاه TonPays"))
        _kyc_uid = u.id if u else None
        await s.commit()
        _uid = u.telegram_id if u else None
        _bal = float(u.balance) if u else None
    # === PATCH_KYC_GATE_ALL_PATHS_V1 ===
    if _kyc_uid:
        from tenant.shared.kyc import after_charge as _ac
        await _ac(bot_id, _kyc_uid)
    # === PATCH_TX_LOG_CHANNEL_V1 ===
    if _uid:
        try:
            from tenant.gateways.payment_page import _tx_log_tenant
            await _tx_log_tenant(bot_id, _uid, credit, _bal, "ton")
        except Exception:
            logger.exception("tx_log (ton)")
    return True, "پرداخت تایید و کیف پول شارژ شد.", credit


# ═══════════════════════ تنظیماتِ ادمین ═══════════════════════

async def _menu_kb(bot_id: int) -> InlineKeyboardMarkup:
    from tenant import get_setting
    on = await get_setting(bot_id, "tonpays_enabled", "0") == "1"
    _custom = await is_custom_mode(bot_id)   # PATCH_TONPAYS_CUSTOM_V1
    has_key = bool(await get_key(bot_id, _custom))
    in_wallet = await get_setting(bot_id, "tonpays_show_wallet", "1") == "1"
    in_buy = await get_setting(bot_id, "tonpays_show_purchase", "1") == "1"
    rows = [
        [InlineKeyboardButton(
            text="🔀 نوعِ درگاه: " + ("کاستوم (تلگرام)" if _custom else "معمولی"),
            callback_data="tonp:mode")],
        [InlineKeyboardButton(
            text="🔑 ثبت / تغییر کلید API" + (" (کاستوم)" if _custom else ""),
            callback_data="tonp:setkey")],
        [InlineKeyboardButton(text="🧪 تست اتصال", callback_data="tonp:test")],
    ]
    if has_key:
        rows.append([InlineKeyboardButton(
            text=("💠 درگاه: ✅ فعال" if on else "💠 درگاه: ❌ غیرفعال"),
            callback_data="tonp:toggle")])
    else:
        rows.append([InlineKeyboardButton(text="⚠️ اول کلید API را ثبت کن",
                                          callback_data="tonp:setkey")])
    if on:
        # === PATCH_TONPAYS_VISIBILITY_V1 ===
        # دو محلِ نمایش مستقل از هم کنترل می‌شوند: بعضی فروشنده‌ها فقط
        # می‌خواهند کاربر کیف پولش را شارژ کند، بعضی فقط سرِ خرید.
        # === PATCH_TONPAYS_DISPLAY_NAME_V1 ===
        rows.append([InlineKeyboardButton(
            text=f"🏷 تغییرِ اسمِ نمایشی: {await get_display_name(bot_id)}",
            callback_data="tonp:setname")])
        # === PATCH_TONPAYS_MIN_AMOUNT_V2 ===
        rows.append([InlineKeyboardButton(
            text=f"🔢 حداقلِ مبلغ: {await get_min_amount(bot_id):,} تومان",
            callback_data="tonp:setmin")])
        # === PATCH_TONPAYS_USER_NOTE_V1 ===
        _nv = (await get_setting(bot_id, "tonpays_user_note", "") or "").strip()
        rows.append([InlineKeyboardButton(
            text="📝 متنِ توضیحِ کاربر: " + ("❌ خاموش" if _nv == "off" else ("✏️ سفارشی" if _nv else "پیش‌فرض")),
            callback_data="tonp:setnote")])
        rows.append([InlineKeyboardButton(
            text="👛 نمایش در کیف پول: " + ("✅ فعال" if in_wallet else "❌ غیرفعال"),
            callback_data="tonp:vis:wallet")])
        rows.append([InlineKeyboardButton(
            text="🛒 نمایش در خریدها: " + ("✅ فعال" if in_buy else "❌ غیرفعال"),
            callback_data="tonp:vis:purchase")])
        # === PATCH_TONPAYS_CARD_MODE_DETECT_V1 ===
        card_mode = await get_setting(bot_id, "tonpays_card_mode", "")
        card_lbl = {"1": "✅ فعال (داخلِ ربات)",
                    "0": "❌ غیرفعال (کاربر به صفحهٔ درگاه می‌رود)"}.get(
            card_mode, "❓ هنوز مشخص نیست")
        if not _custom:   # در کاستوم شمارهٔ کارت همیشه در پاسخ می‌آید
            rows.append([InlineKeyboardButton(
                text=f"💳 نمایش شمارهٔ کارت: {card_lbl}",
                callback_data="tonp:cardmode")])
        # === PATCH_TONPAYS_MANUAL_CARD_V1 ===
        man = await get_setting(bot_id, "tonpays_card_number", "")
        rows.append([InlineKeyboardButton(
            text=("🏦 کارتِ مقصد (دستی): " + (man if man else "ثبت نشده")),
            callback_data="tonp:setcard")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# === PATCH_TONPAYS_MIN_AMOUNT_V2 ===
@tonpays_router.callback_query(F.data == "tonp:setmin")
async def tonp_setmin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """حداقلِ مبلغِ درگاه — چون خودِ TonPays هر از گاهی عوضش می‌کند و API
    راهی برایِ خواندنش ندارد."""
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_min_amount(row.id)
    await state.set_state(TonPaysFSM.min_amount)
    await cb.message.answer(
        f"🔢 حداقلِ مبلغِ مجازِ این درگاه را به تومان بفرست "
        f"(فعلاً: <b>{cur:,}</b>).\n\n"
        "این عدد باید با چیزی که خودِ TonPays اعلام کرده یکی باشد؛ اگر کمتر "
        "بگذاری، درگاه فاکتور را رد می‌کند.\n"
        f"برایِ بازگشت به پیش‌فرض ({MIN_AMOUNT:,})، کلمهٔ "
        "<code>پیش‌فرض</code> را بفرست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@tonpays_router.message(TonPaysFSM.min_amount, F.text)
async def tonp_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting
    row = await _owner_ok_msg(msg, bot)
    await state.clear()
    if not row:
        return
    raw = (msg.text or "").strip()
    if raw in ("پیش‌فرض", "پیشفرض", "default", "-"):
        await set_setting(row.id, "tonpays_min_amount", "")
        await msg.answer(f"✅ به پیش‌فرض ({MIN_AMOUNT:,} تومان) برگشت.")
    else:
        norm = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                                           "01234567890123456789")).replace(",", "")
        try:
            n = int(float(norm))
        except ValueError:
            await msg.answer("عدد معتبر نیست. دوباره امتحان کن.")
            return
        if n <= 0:
            await msg.answer("عدد باید بزرگ‌تر از صفر باشد.")
            return
        await set_setting(row.id, "tonpays_min_amount", str(n))
        await msg.answer(f"✅ حداقلِ مبلغ رویِ <b>{n:,}</b> تومان تنظیم شد.")
    await msg.answer(
        "برایِ ادامه به تنظیماتِ درگاه برگرد:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💠 بازگشت به درگاه",
                                  callback_data="tonp:menu")]]))


# === PATCH_TONPAYS_DISPLAY_NAME_V1 ===
@tonpays_router.callback_query(F.data == "tonp:setname")
async def tonp_setname(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """تغییرِ برچسبی که کاربرِ نهایی به‌جایِ «تون‌پیز» می‌بیند."""
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_display_name(row.id)
    await state.set_state(TonPaysFSM.display_name)
    await cb.message.answer(
        f"🏷 اسمی که می‌خواهی به‌جایِ «تون‌پیز» به کاربر نشان داده شود را "
        f"بفرست (فعلاً: <b>{cur}</b>؛ حداکثر ۴۰ حرف).\n\n"
        "برایِ بازگشت به اسمِ پیش‌فرض، کلمهٔ <code>پیش‌فرض</code> را بفرست.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@tonpays_router.message(TonPaysFSM.display_name, F.text)
async def tonp_setname_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting
    row = await _owner_ok_msg(msg, bot)
    await state.clear()
    if not row:
        return
    name = (msg.text or "").strip()
    from tenant import is_default_word
    if is_default_word(name):
        await set_setting(row.id, "tonpays_display_name", "")
        await msg.answer(f"✅ اسمِ نمایشی به پیش‌فرض (<b>{DEFAULT_DISPLAY_NAME}</b>) برگشت.",
                         parse_mode="HTML")
    elif not name:
        await msg.answer("اسمِ نامعتبر. دوباره امتحان کن.")
        return
    else:
        name = name[:40]
        await set_setting(row.id, "tonpays_display_name", name)
        await msg.answer(f"✅ اسمِ نمایشی رویِ «{name}» تنظیم شد.")
    await msg.answer(
        "برایِ ادامه به تنظیماتِ درگاه برگرد:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💠 بازگشت به درگاه",
                                  callback_data="tonp:menu")]]))


@tonpays_router.callback_query(F.data == "tonp:cardmode")
async def tonp_cardmode(cb: CallbackQuery, bot: Bot):
    """توضیحِ وضعیتِ «نمایشِ شمارهٔ کارت» و روشِ فعال‌کردنش.

    این گزینه در پنلِ خودِ TonPays تنظیم می‌شود (نه اینجا)، چون شمارهٔ کارت
    مالِ حسابِ آن‌هاست. اینجا فقط وضعیتِ واقعی را نشان می‌دهیم و راه را
    می‌گوییم.
    """
    from tenant import _owner_ok, get_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mode = await get_setting(row.id, "tonpays_card_mode", "")
    if mode == "1":
        head = ("✅ <b>فعال است.</b>\n\nدرگاه شمارهٔ کارت را به ربات می‌دهد، پس "
                "کلِ پرداخت داخلِ همین ربات انجام می‌شود و کاربر جایی نمی‌رود.")
    elif mode == "0":
        head = ("❌ <b>غیرفعال است.</b>\n\nدرگاه شمارهٔ کارت را نمی‌فرستد، پس "
                "کاربر مجبور است برایِ دیدنِ اطلاعاتِ واریز از ربات خارج شود.")
    else:
        head = ("❓ <b>هنوز مشخص نیست.</b>\n\nاین وضعیت وقتی معلوم می‌شود که "
                "اولین فاکتورِ واقعی ساخته شود (API راهی برایِ پرسیدنش ندارد). "
                "یک بار خودت یک شارژ را شروع کن تا اینجا مشخص شود.")
    await cb.message.answer(
        f"💳 <b>نمایش شمارهٔ کارت داخلِ ربات</b>\n\n{head}\n\n"
        "🔧 <b>اگر در رباتِ TonPays چنین گزینه‌ای نبود:</b> APIِ آن‌ها فقط چهار "
        "اندپوینت دارد و هیچ راهی برایِ روشن‌کردنِ این قابلیت از بیرون ندارد؛ "
        "یا باید از پشتیبانیِ TonPays بخواهی برایِ کلیدت فعالش کنند، یا — "
        "ساده‌تر — از گزینهٔ <b>«🏦 کارتِ مقصد (دستی)»</b> در همین صفحه استفاده "
        "کنی و شمارهٔ کارت را یک‌بار خودت ثبت کنی. چون کارتِ مقصد ثابت است و "
        "تشخیصِ پرداخت از رویِ «مبلغِ یکتا» انجام می‌شود، نتیجه دقیقاً یکی است "
        "و کلِ پرداخت داخلِ خودِ ربات انجام می‌شود.",
        parse_mode="HTML")
    await cb.answer()


@tonpays_router.callback_query(F.data == "tonp:setcard")
async def tonp_setcard(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """ثبتِ دستیِ شمارهٔ کارتِ مقصد.

    === PATCH_TONPAYS_MANUAL_CARD_V1 ===
    APIِ TonPays فقط ۴ اندپوینت دارد (create / check ×۲ / health) و هیچ راهی
    برایِ روشن‌کردنِ ارسالِ شمارهٔ کارت ندارد. ولی چون شمارهٔ کارتِ مقصد برایِ
    یک حسابِ TonPays ثابت است و تشخیصِ پرداخت از رویِ «مبلغِ یکتا» انجام
    می‌شود (نه از رویِ کارت)، می‌شود همان کارت را یک‌بار اینجا ثبت کرد و
    داخلِ خودِ ربات به کاربر نشان داد. تاییدِ پرداخت همچنان از خودِ TonPays
    گرفته می‌شود، پس چیزی از امنیت کم نمی‌شود.
    """
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(TonPaysFSM.card)
    await cb.message.answer(
        "🏦 <b>شمارهٔ کارتِ مقصد</b>\n\n"
        "همان شمارهٔ کارتی که TonPays برایِ دریافتِ واریزها به تو داده را "
        "بفرست (۱۶ رقم).\n\n"
        "⚠️ <b>خیلی مهم:</b> باید <b>دقیقاً</b> همان کارتی باشد که TonPays "
        "رصد می‌کند. اگر کارتِ دیگری بزنی، پولِ کاربر به جایی می‌رود که درگاه "
        "آن را نمی‌بیند و پرداخت هیچ‌وقت تایید نمی‌شود. اگر مطمئن نیستی، "
        "یک بار خودت یک فاکتور بساز و از صفحهٔ پرداختِ TonPays کارت را ببین.\n\n"
        "برایِ پاک‌کردن، عدد <code>0</code> را بفرست.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@tonpays_router.message(TonPaysFSM.card, F.text)
async def tonp_setcard_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting, smart_cancel_kb
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw == "0":
        await set_setting(row.id, "tonpays_card_number", "")
        await set_setting(row.id, "tonpays_card_holder", "")
        await state.clear()
        await msg.answer("✅ کارتِ دستی پاک شد.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="💠 بازگشت به درگاه",
                                                   callback_data="tonp:menu")]]))
        return
    digits = "".join(ch for ch in raw.translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")) if ch.isdigit())
    if len(digits) != 16:
        await msg.answer("شمارهٔ کارت باید دقیقاً ۱۶ رقم باشد. دوباره بفرست:")
        return
    await set_setting(row.id, "tonpays_card_number", digits)
    await state.set_state(TonPaysFSM.holder)
    await msg.answer("🧑 حالا نامِ صاحبِ کارت را بفرست (یا <code>-</code> برایِ رد کردن):",
                     reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")


@tonpays_router.message(TonPaysFSM.holder, F.text)
async def tonp_setholder_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    name = (msg.text or "").strip()
    await set_setting(row.id, "tonpays_card_holder", "" if name == "-" else name[:60])
    await state.clear()
    await msg.answer(
        "✅ ذخیره شد. از این به بعد شمارهٔ کارت و مبلغِ دقیق داخلِ خودِ ربات به "
        "کاربر نشان داده می‌شود و لازم نیست جایی برود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💠 بازگشت به درگاه", callback_data="tonp:menu")]]))


@tonpays_router.callback_query(F.data.startswith("tonp:vis:"))
async def tonp_visibility(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """روشن/خاموش‌کردنِ نمایشِ درگاه در «کیف پول» یا «صفحهٔ خرید»."""
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    which = cb.data.split(":")[2]
    key = "tonpays_show_wallet" if which == "wallet" else "tonpays_show_purchase"
    cur = await get_setting(row.id, key, "1")
    await set_setting(row.id, key, "0" if cur == "1" else "1")
    await set_tonpays_start(cb, bot, state)


async def set_tonpays_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """ورودیِ «💠 درگاه تون‌پیز» از منویِ مالی."""
    from tenant import _owner_ok, get_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    _custom = await is_custom_mode(row.id)   # PATCH_TONPAYS_CUSTOM_V1
    key = await get_key(row.id, _custom)
    on = await get_setting(row.id, "tonpays_enabled", "0") == "1"
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 10 else ("ثبت نشده" if not key else "ثبت شده")
    txt = (
        "💠 <b>درگاه TonPays</b>\n\n"
        "درگاهِ کارت‌به‌کارتِ خودکار (مبلغ به تومان). کاربر مبلغ را واریز می‌کند و "
        "درگاه با «مبلغِ یکتا» واریز را خودکار تشخیص می‌دهد.\n\n"
        f"نوعِ درگاه: <b>{'کاستوم (بستر تلگرام)' if _custom else 'معمولی'}</b>\n"
        f"کلید API: <code>{masked}</code>\n"
        f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n"
        f"نمایش در کیف پول: "
        f"{'✅' if await get_setting(row.id, 'tonpays_show_wallet', '1') == '1' else '❌'}"
        f"   |   نمایش در خریدها: "
        f"{'✅' if await get_setting(row.id, 'tonpays_show_purchase', '1') == '1' else '❌'}\n"
        "\n"
        + DISCLAIMER
    )
    kb = await _menu_kb(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    try:
        await cb.answer()
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")


@tonpays_router.callback_query(F.data.in_({"set:tonpays", "tonp:menu"}))
async def tonp_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await set_tonpays_start(cb, bot, state)


# === PATCH_TONPAYS_CUSTOM_V1 ===
@tonpays_router.callback_query(F.data == "tonp:mode")
async def tonp_mode(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """جابه‌جایی بینِ درگاهِ معمولی و کاستوم (بستر تلگرام). هر نوع کلیدِ خودش را
    دارد؛ فاکتورهایِ بازِ نوعِ قبلی تا پایان با کلیدِ خودشان پیگیری می‌شوند."""
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await is_custom_mode(row.id)
    await set_setting(row.id, "tonpays_mode", "" if cur else "custom")
    await set_tonpays_start(cb, bot, state)


@tonpays_router.callback_query(F.data == "tonp:setkey")
async def tonp_setkey(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(TonPaysFSM.api_key)
    if await is_custom_mode(row.id):   # PATCH_TONPAYS_CUSTOM_V1
        _t = ("🔑 <b>کلیدِ بستر تلگرامِ درگاه کاستوم</b> را از «پنل فروشندگان ← تنظیمات ← "
              "درگاه کاستوم» بگیر و همین‌جا بفرست (فقط خودِ کلید).\n\n"
              "⚠️ هر بخش کلیدِ اختصاصی دارد؛ کلیدِ بستر <b>وب‌سایت</b> اینجا کار نمی‌کند.")
    else:
        _t = ("🔑 کلید API را از منویِ «مستندات API» در رباتِ TonPays کپی کن و همین‌جا "
              "بفرست (فقط خودِ کلید).")
    await cb.message.answer(_t, reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@tonpays_router.message(TonPaysFSM.api_key, F.text)
async def tonp_setkey_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    key = (msg.text or "").strip()
    if len(key) < 8:
        await msg.answer("کلید خیلی کوتاه است. دوباره بفرست:")
        return
    await msg.answer("⏳ در حال بررسی کلید…")
    _custom = await is_custom_mode(row.id)   # PATCH_TONPAYS_CUSTOM_V1
    ok, note = await validate_api_key(key, custom=_custom)
    if not ok:
        await msg.answer(f"❌ {note}\n\nدوباره امتحان کن یا لغو کن.")
        return
    await set_setting(row.id, "tonpays_custom_key" if _custom else "tonpays_api_key", key)
    await state.clear()
    await msg.answer(
        f"✅ {note}\nکلید ذخیره شد. حالا از همان صفحه درگاه را «فعال» کن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💠 بازگشت به درگاه", callback_data="tonp:menu")]]))


@tonpays_router.callback_query(F.data == "tonp:test")
async def tonp_test(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _custom = await is_custom_mode(row.id)   # PATCH_TONPAYS_CUSTOM_V1
    key = await get_key(row.id, _custom)
    if not key:
        await cb.answer("اول کلید API را ثبت کن.", show_alert=True)
        return
    await cb.answer("در حال تست…")
    ok, note = await validate_api_key(key, custom=_custom)
    await cb.message.answer(("✅ " if ok else "❌ ") + note)


@tonpays_router.callback_query(F.data == "tonp:toggle")
async def tonp_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await get_key(row.id, await is_custom_mode(row.id)):   # PATCH_TONPAYS_CUSTOM_V1
        await cb.answer("اول کلید API را ثبت کن.", show_alert=True)
        return
    cur = await get_setting(row.id, "tonpays_enabled", "0")
    if cur == "1":
        await set_setting(row.id, "tonpays_enabled", "0")
        await set_tonpays_start(cb, bot, state)
        return
    # اولین‌بار که می‌خواهد فعال کند، باید سلبِ مسئولیت را صریحاً بپذیرد
    if await get_setting(row.id, "tonpays_accepted", "0") != "1":
        await cb.message.answer(
            "📌 <b>قبل از فعال‌سازی، این را بخوان و تایید کن</b>\n\n" + DISCLAIMER,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ می‌پذیرم و فعال کن",
                                      callback_data="tonp:accept")],
                [InlineKeyboardButton(text="❌ نمی‌پذیرم", callback_data="tonp:menu")],
            ]))
        await cb.answer()
        return
    await _enable(cb, row.id)
    await set_tonpays_start(cb, bot, state)


async def _enable(cb: CallbackQuery, bot_id: int):
    from tenant import set_setting
    await set_setting(bot_id, "tonpays_enabled", "1")
    if not callback_url():
        await cb.answer(
            "فعال شد.\n\nتوجه: WEBHOOK_BASE روی سرور تنظیم نشده، پس کال‌بکِ "
            "خودکار کار نمی‌کند و کاربر باید دکمهٔ «پرداخت کردم» را بزند.",
            show_alert=True)


@tonpays_router.callback_query(F.data == "tonp:accept")
async def tonp_accept(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """ثبتِ پذیرشِ سلبِ مسئولیت و فعال‌سازی."""
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await set_setting(row.id, "tonpays_accepted", "1")
    await _enable(cb, row.id)
    await set_tonpays_start(cb, bot, state)


# ═══════════════════════ جریانِ کاربر ═══════════════════════

async def is_available(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, "tonpays_enabled", "0") == "1"
            and bool(await get_key(bot_id, await is_custom_mode(bot_id))))


async def wallet_tonpays_text(msg: Message, bot: Bot, state: FSMContext):
    """شروعِ شارژ با تون‌پیز — نسخه‌ای که از یک Message کار می‌کند.

    === PATCH_TONPAYS_KEYBOARD_MODE_V1 ===
    ربات دو حالتِ منو دارد: شیشه‌ای (اینلاین، با callback_data) و کیبوردِ پایین
    (که فقط متنِ برچسبِ دکمه را می‌فرستد). قبلاً فقط هندلرِ callback نوشته شده
    بود، پس در حالتِ کیبوردی زدنِ دکمه هیچ واکنشی نداشت. حالا منطق در همین
    تابع است و هر دو حالت آن را صدا می‌زنند.
    """
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row or not await is_available(row.id):
        await msg.answer("این روش پرداخت در حال حاضر در دسترس نیست.")
        return
    await state.set_state(TonPaysFSM.amount)
    _note = await get_user_note(row.id)  # PATCH_TONPAYS_USER_NOTE_V1
    await msg.answer(
        "💠 <b>شارژ کیف پول با TonPays</b>\n\n"
        + (f"{_note}\n\n" if _note else "")
        + "مبلغِ موردنظر را به <b>تومان</b> بفرست "
        f"(حداقل <b>{await get_min_amount(row.id):,} تومان</b> — مثلاً 50000):",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")


@tonpays_router.callback_query(F.data == "w:tonpays")
async def w_tonpays(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """نسخهٔ منویِ شیشه‌ای — همان منطقِ بالا را اجرا می‌کند."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or not await is_available(row.id):
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    await cb.answer()
    await wallet_tonpays_text(cb.message, bot, state)


# === PATCH_TONPAYS_CHANGE_CARD_V1 ===
def build_invoice_view(prefix: str, order_id: str, amount: int, final_amount: int,
                       card: str, card_name: str, custom: bool,
                       invoice_url: str = "", web_invoice_url: str = "",
                       rial_fn=None, can_change: bool = True):
    """متن و کیبوردِ پیامِ فاکتور — هم برایِ ساختِ فاکتور و هم برایِ بازسازیِ پیام بعد از
    «تعویضِ کارت» (تا هر دو دقیقاً یک شکل باشند). prefix: «tonp» (مستأجر) یا «ptonp» (بات‌ساز)."""
    if rial_fn is None:
        rial_fn = lambda n: f"{int(n):,} تومان"
    # === PATCH_TONPAYS_EXACT_AMOUNT_FIRST_V1 ===
    # درخواستِ مشتری‌ها: بالایِ فاکتور همان «مبلغِ دقیقی که باید واریز کنم» (ریال) نوشته شود،
    # نه مبلغِ شارژ؛ چون مبلغِ شارژ (مثلاً ۲۰٬۰۰۰) با مبلغِ واریزی (۲۱٬۲۴۵ تومان) فرق دارد
    # و کاربر عددِ بالا را واریز می‌کرد.
    lines = [
        "💠 <b>فاکتور شارژ کیف پول</b>\n",
        f"💵 <b>مبلغِ واریزی: {final_amount * 10:,} ریال</b>",
        f"(= {final_amount:,} تومان)",
        # === PATCH_TONPAYS_NO_TARGET_LINE_V1 === خطِ «مبلغی که به کیف پولت اضافه می‌شود» برداشته شد
        # (مشتری‌ها همان را واریز می‌کردند و مبلغِ دقیق را نمی‌زدند).
        f"کدِ پیگیری: <code>{order_id}</code>\n",
    ]
    if card:
        lines += [
            "💳 <b>شمارهٔ کارت</b> (برایِ کپی، روی عدد بزن):",
            f"<code>{card}</code>",
        ]
        if card_name:
            lines.append(f"🧑 به نام: <b>{card_name}</b>")
        lines += [
            # === PATCH_TONPAYS_RIAL_AMOUNT_V1 ===
            # باگِ واقعی: عددِ کپی‌شونده «تومان» بود ولی اپِ بانک «ریال» می‌گیرد؛ کاربر
            # ۵۱٬۸۵۵ را به‌عنوانِ ریال واریز کرد (≈ ۵ هزار تومان) به‌جایِ ۵۱٬۸۵۵ تومان.
            # حالا مبلغِ دقیق به «ریال» (چیزی که در اپِ بانک زده می‌شود) اولین و
            # قابلِ‌کپی است، و معادلِ تومانی فقط توضیح است.
            "\n📋 <b>مبلغِ دقیقِ واریزی به ریال</b> (روی عدد بزن تا کپی شود):",
            f"🌐 <code>{final_amount * 10}</code> ریال",
            "⚠️ در اپِ بانک <b>مبلغ را به «ریال»</b> وارد کن؛ اگر عددِ تومانی را به‌عنوانِ ریال بزنی "
            "۱۰ برابر کمتر واریز می‌شود و شارژ نمی‌شود.",
            "\n⚠️ حتماً <b>دقیقاً همین عدد</b> را واریز کن. مبلغ عمداً چند تومان "
            "با مبلغِ شارژ فرق دارد تا واریزِ تو خودکار شناسایی شود؛ اگر عددِ "
            "گرد یا متفاوتی بفرستی، پرداخت تشخیص داده نمی‌شود.",
            # === PATCH_TONPAYS_WARNINGS_V1 === هشدارهایِ هم‌سبکِ خودِ تون‌پیز
            "\n⚠️ <b>انتقال وجه از طریق روش‌هایِ پایا و پل برایِ این فاکتور پذیرفته نمی‌شود.</b>",
            "\n🔄 بعد از واریز، همین‌جا منتظر بمان — وضعیت خودکار بررسی می‌شود و "
            "تا چند دقیقه نتیجه را همین‌جا اعلام می‌کنم. اگر عجله داری، دکمهٔ "
            "«پرداخت کردم» را بزن."
            + ("\n📎 <b>در این درگاه، بعد از واریز حتماً «ارسال فیش واریزی» را هم بزن و تصویرِ فیش را بفرست</b> — "
               "بدونِ فیش معمولاً پرداخت تأیید نمی‌شود." if custom else "")
            # === PATCH_EXACT_AMOUNT_ALERT_V1 === (درخواستِ دیار) هشدارِ «فقط کارت‌به‌کارت» در درگاهِ کاستوم
            + ("\n⚠️ <b>فقط با روشِ «کارت به کارت» واریز کن</b> و از روش‌هایِ پایا و پل و … استفاده نکن؛ "
               "مبلغِ رند یا کم‌وزیاد خودکار تأیید نمی‌شود و شارژ معطلِ بررسیِ دستی می‌ماند.\n"
               "اگر با روشی جز کارت‌به‌کارت واریز کنی، پول برمی‌گردد و باعثِ معطلیِ خودت می‌شود." if custom else ""),
        ]
    else:
        lines += [
            f"💵 مبلغِ دقیقِ واریزی: <b>{final_amount * 10:,} ریال</b> (= {final_amount:,} تومان)",
            "\n⚠️ برای دریافت شماره کارت و پرداخت از دکمه اطلاعات پرداخت اقدام کن .",
        ]

    rows = [[InlineKeyboardButton(text="✅ پرداخت کردم، بررسی کن",
                                  callback_data=f"{prefix}:verify:{order_id}")]]
    # === PATCH_TONPAYS_RIAL_AMOUNT_V1 === دکمه‌هایِ کپیِ واقعی (مبلغِ ریالی + شمارهٔ کارت)
    _copy_row = [InlineKeyboardButton(text="📋 کپی مبلغ (ریال)",
                                      copy_text=CopyTextButton(text=str(final_amount * 10)))]
    if card:
        _card_digits = "".join(ch for ch in str(card) if ch.isdigit())
        if _card_digits:
            _copy_row.append(InlineKeyboardButton(text="📋 کپی شماره کارت",
                                                  copy_text=CopyTextButton(text=_card_digits)))
    rows.insert(0, _copy_row)
    if custom:
        # === PATCH_TONPAYS_CUSTOM_V1 === دکمهٔ ارسالِ فیش اجباری است
        lines.append(
            "\n📎 اگر بعد از واریز، پرداخت خودکار تأیید نشد، دکمهٔ «ارسال فیش "
            "واریزی» را بزن و <b>تصویرِ فیش</b> را بفرست تا بررسی شود.")
        rows.insert(0, [InlineKeyboardButton(text="📎 ارسال فیش واریزی",
                                             callback_data=f"{prefix}:rcpt:{order_id}")])
    if not card and invoice_url:
        rows.insert(0, [InlineKeyboardButton(text="🧾 اطلاعات پرداخت", url=invoice_url)])
    # === PATCH_TONPAYS_DOCS_V2 === صفحهٔ وبِ فاکتور (برایِ کسی که تلگرامِ درگاه را نمی‌خواهد)
    if not card and web_invoice_url:
        rows.insert(1 if invoice_url else 0,
                    [InlineKeyboardButton(text="🌐 پرداخت در وب", url=web_invoice_url)])
    if custom and can_change and card:   # PATCH_TONPAYS_WARNINGS_V1
        lines.append("\n⚠️ در صورت مشاهدهٔ محدودیت در شمارهٔ کارتِ فاکتور، از طریقِ دکمهٔ «🔄 تعویض کارت» "
                     "پایینِ همین پیام اقدام به تغییرِ شمارهٔ کارت کن.")
    if custom and can_change:
        # === PATCH_TONPAYS_CHANGE_CARD_V1 === کارتِ فاکتور پر شده/مشکل دارد ⇒ تعویض (کول‌داون ۶۰ثانیه)
        rows.append([InlineKeyboardButton(text="🔄 تعویض کارت (اگر کارت پر شده یا واریز نمی‌شود)",
                                          callback_data=f"{prefix}:chg:{order_id}")])
    rows.append([InlineKeyboardButton(text="❌ لغو این فاکتور",
                                      callback_data=f"{prefix}:cancel:{order_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@tonpays_router.message(TonPaysFSM.amount, F.text)
async def tonpays_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_setting, fa_to_int, rial
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    amount = fa_to_int(msg.text)
    if amount is None or amount <= 0:
        await msg.answer("مبلغ نامعتبر است. یک عدد به تومان بفرست:")
        return
    # === PATCH_WALLET_LIMITS_V1 === حداقل (همان کلیدِ tonpays_min_amount) + حداکثرِ تازه
    from tenant.gateways import wallet_limits as _wl
    _err = await _wl.check(row.id, "tonpays", amount)
    if _err:
        # قبل از تماس با درگاه چک می‌شود تا کاربر خطایِ انگلیسیِ درگاه را نبیند
        await state.set_state(TonPaysFSM.amount)
        await msg.answer(_err + "\nمبلغِ دیگری بفرست:", parse_mode="HTML")
        return
    await state.clear()

    # === PATCH_KYC_CAP_V1 === سقفِ احرازِ هویت
    try:
        from sqlalchemy import select as _sel
        from models import BotUser as _BU
        from tenant.shared.kyc import check_charge as _cc
        async with Session() as _s:
            _u = await _s.scalar(_sel(_BU).where(
                _BU.bot_id == row.id, _BU.telegram_id == msg.from_user.id))
        _ok, _why = await _cc(row.id, _u, amount)
        if not _ok:
            await msg.answer(_why)
            return
    except Exception:
        logger.exception("kyc: بررسیِ سقف ناموفق بود (ton)")

    # === PATCH_EXACT_AMOUNT_ALERT_V3 === (درخواستِ دیار) لحظهٔ «ساخت فاکتور» آلارمِ «دقیقاً مبلغ فاکتور را واریز کنید» بیاید.
    # تلگرام فقط جوابِ کلیکِ دکمه را پاپ‌آپ می‌کند؛ پس بعد از مبلغ یک دکمهٔ «🧾 ساخت فاکتور» می‌آید و همان کلیک آلارم + فاکتور را می‌دهد.
    if (await get_setting(row.id, "pay_exact_alert", "1")) != "0":
        await state.update_data(tonp_amount=int(amount))
        await state.set_state(TonPaysFSM.confirm)
        await msg.answer(
            f"💵 مبلغِ شارژ: <b>{int(amount):,} تومان</b>\n\nبرایِ ساختِ فاکتور دکمهٔ زیر را بزن:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🧾 ساخت فاکتور", callback_data="tonp:mk", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton(text="❌ لغو", callback_data="tonp:mkcancel", style=ButtonStyle.DANGER)]]))
        return
    await _create_invoice(bot, row, msg, msg.from_user.id, amount)


@tonpays_router.message(TonPaysFSM.confirm, F.text)
async def tonpays_amount_again(msg: Message, bot: Bot, state: FSMContext):
    """وسطِ مرحلهٔ تأیید اگر مبلغِ تازه فرستاد، همان مسیرِ مبلغ."""
    await state.set_state(TonPaysFSM.amount)
    await tonpays_amount(msg, bot, state)


@tonpays_router.callback_query(F.data == "tonp:mk")
async def tonp_make_invoice(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    from tenant.gateways.exact_amount_alert import ALERT_TEXT
    row = await get_bot_row(bot)
    data = await state.get_data()
    amount = data.get("tonp_amount")
    if not row or not amount:
        await cb.answer("مبلغ پیدا نشد؛ دوباره از «شارژ» شروع کن.", show_alert=True)
        await state.clear()
        return
    await state.clear()
    await cb.answer(ALERT_TEXT, show_alert=True)       # آلارمِ لحظهٔ ساختِ فاکتور
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("tonp: پاک‌کردنِ دکمهٔ ساخت فاکتور ممکن نشد (بی‌اهمیت)")
    await _create_invoice(bot, row, cb.message, cb.from_user.id, int(amount))


@tonpays_router.callback_query(F.data == "tonp:mkcancel")
async def tonp_make_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("لغو شد")
    try:
        await cb.message.edit_text("❌ لغو شد.")
    except Exception:
        logger.debug("tonp: ویرایشِ پیامِ لغو ممکن نشد (بی‌اهمیت)")


async def _create_invoice(bot: Bot, row, msg: Message, user_id: int, amount: int,
                          after_cancel: bool = False):
    """ساختِ فاکتور و ارسالِ آن. msg فقط برایِ پاسخ دادن (answer/chat)؛ شناسهٔ کاربر جدا می‌آید
    (در مسیرِ کلیک، msg پیامِ خودِ ربات است)."""
    from tenant import get_setting, rial
    _custom = await is_custom_mode(row.id)   # PATCH_TONPAYS_CUSTOM_V1
    api_key = await get_key(row.id, _custom)
    order_id = _new_order_id(_custom)
    await msg.answer("⏳ در حال ساختِ فاکتور…")
    try:
        inv = await create_invoice(api_key, amount, order_id, callback_url(),
                                   buyer_chat_id=user_id,
                                   custom=_custom)  # PATCH_TONPAYS_DOCS_V2
    except TonPaysError as e:
        if e.code in BUYER_BLOCK_CODES:   # PATCH_TONPAYS_PENDING_V1
            await _offer_buyer_block(msg, row.id, user_id, amount, _custom, after_cancel, e.code)
            return
        await msg.answer(f"❌ ساختِ فاکتور ناموفق بود.\n{e}")
        return
    except Exception as e:
        await msg.answer(f"❌ ساختِ فاکتور ناموفق بود.\n{e}")
        return

    invoice_id = inv.get("invoice_id") or ""
    final_amount = int(inv.get("final_amount") or amount)
    invoice_url = inv.get("invoice_url") or ""
    web_invoice_url = inv.get("web_invoice_url") or ""  # === PATCH_TONPAYS_DOCS_V2 ===

    async with Session() as s:
        s.add(TonPaysCharge(
            bot_id=row.id, user_telegram_id=user_id,
            order_id=order_id, invoice_id=invoice_id,
            request_amount=amount, final_amount=final_amount, status="waiting"))
        await s.commit()

    card = inv.get("card_number")
    card_name = inv.get("card_name")
    if _custom:   # PATCH_TONPAYS_CUSTOM_V1 — کارت همیشه در پاسخ است (ممکن است ماسک باشد)
        card, card_name = pick_card(
            inv, await get_setting(row.id, "tonpays_card_number", ""),
            await get_setting(row.id, "tonpays_card_holder", ""))

    # === PATCH_TONPAYS_CARD_MODE_DETECT_V1 ===
    # API راهی برایِ پرسیدنِ «آیا نمایشِ شمارهٔ کارت برایِ این کلید روشن است؟»
    # ندارد؛ تنها راهِ فهمیدنش این است که ببینیم در پاسخِ ساختِ فاکتور آمده یا
    # نه. پس همان‌جا نتیجه را ذخیره می‌کنیم تا صفحهٔ تنظیمات بتواند وضعیتِ
    # واقعی را به مالکِ ربات نشان دهد، بدونِ ساختِ فاکتورِ آزمایشیِ الکی.
    from tenant import set_setting as _set
    if not _custom:
        await _set(row.id, "tonpays_card_mode", "1" if card else "0")

    # === PATCH_TONPAYS_MANUAL_CARD_V1 ===
    # اگر درگاه شمارهٔ کارت را نفرستاد، از کارتی که مالکِ ربات دستی ثبت کرده
    # استفاده می‌کنیم تا کاربر مجبور نشود از ربات خارج شود.
    if not card:
        card = await get_setting(row.id, "tonpays_card_number", "")
        card_name = await get_setting(row.id, "tonpays_card_holder", "")

    # === PATCH_TONPAYS_INBOT_FLOW_V1 ===
    # کلِ عملیات داخلِ خودِ ربات انجام می‌شود: شمارهٔ کارت و مبلغِ دقیق همین‌جا
    # نشان داده می‌شود، وضعیت هم خودکار پیگیری می‌شود. کاربر لازم نیست از ربات
    # خارج شود یا واردِ رباتِ TonPays بشود.
    #
    # نکته: شمارهٔ کارت فقط وقتی در پاسخِ API می‌آید که مالکِ کلید این گزینه را
    # در پنلِ TonPays روشن کرده باشد. اگر روشن نباشد چاره‌ای جز لینکِ صفحهٔ
    # پرداخت نیست، ولی همان را هم با توضیح نشان می‌دهیم تا کاربر سردرگم نشود.
    _text, _kb = build_invoice_view(
        "tonp", order_id, amount, final_amount, card, card_name, _custom,
        invoice_url=invoice_url, web_invoice_url=web_invoice_url, rial_fn=rial)
    await msg.answer(_text, parse_mode="HTML", reply_markup=_kb)

    # پیگیریِ خودکار در پس‌زمینه — کاربر لازم نیست هیچ دکمه‌ای بزند
    asyncio.create_task(_auto_poll(bot, row.id, order_id, msg.chat.id))


# ═══════════════ پیگیریِ خودکارِ وضعیتِ پرداخت ═══════════════

# هر چند ثانیه یک‌بار و تا چند دقیقه، وضعیتِ فاکتور از خودِ درگاه پرسیده
# می‌شود. فاصله‌ها عمداً پلکانی است: اولِ کار تند (چون کاربر معمولاً همان
# دقایقِ اول واریز می‌کند) و بعد کندتر، تا هم تجربه سریع باشد و هم به API
# درگاه فشارِ بی‌مورد نیاید.
_POLL_SCHEDULE = [15, 15, 15, 15, 30, 30, 30, 60, 60, 60, 90, 90, 120, 120, 180, 180]


async def _auto_poll(bot: Bot, bot_id: int, order_id: str, chat_id: int) -> None:
    """پیگیریِ خودکارِ یک فاکتور تا زمانِ تایید یا پایانِ مهلت."""
    from tenant import rial
    for _i, delay in enumerate(_POLL_SCHEDULE):
        await asyncio.sleep(delay)
        try:
            async with Session() as s:
                ch = await s.scalar(select(TonPaysCharge).where(
                    TonPaysCharge.bot_id == bot_id,
                    TonPaysCharge.order_id == order_id))
            if not ch or ch.status != "waiting":
                return          # یا تسویه شده یا لغو شده — کار تمام است
            ok, _note, credit = await settle_charge(bot_id, order_id)
            # === PATCH_TONPAYS_DOCS_V2 === فاکتورِ ردشده/منقضی/لغوشده دیگر پیگیری نمی‌شود
            async with Session() as _s:
                _cur = await _s.scalar(select(TonPaysCharge.status).where(
                    TonPaysCharge.bot_id == bot_id, TonPaysCharge.order_id == order_id))
            if _cur in ("rejected", "expired", "cancelled"):
                _fa = {"rejected": "ردّ شد", "expired": "منقضی شد", "cancelled": "لغو شد"}[_cur]
                await bot.send_message(
                    chat_id,
                    f"⚠️ فاکتورِ <code>{order_id}</code> در درگاه <b>{_fa}</b>.\n"
                    "اگر واریز کرده‌ای با پشتیبانی تماس بگیر؛ وگرنه فاکتورِ جدید بساز.",
                    parse_mode="HTML")
                return
            if ok:
                await bot.send_message(
                    chat_id,
                    "✅ <b>پرداختت تایید شد!</b>\n\n"
                    f"مبلغِ <b>{rial(credit)}</b> به کیف پولت اضافه شد.",
                    parse_mode="HTML")
                return
            # === PATCH_TONPAYS_CUSTOM_V1 === تأییدِ خودکار نشد ⇒ از خریدار فیش بخواه
            if (needs_action_notice(bot_id, order_id)
                    and (bot_id, order_id) not in _NEED_ACTION_NOTIFIED):
                _NEED_ACTION_NOTIFIED.add((bot_id, order_id))
                _t, _k = action_notice("tonp", order_id, _LAST_INFO.get((bot_id, order_id)) or {},
                                       is_custom_order(order_id))
                await bot.send_message(chat_id, _t, parse_mode="HTML", reply_markup=_k)
            # === PATCH_TONPAYS_RECEIPT_REMINDER_V1 ===
            # طبقِ مستنداتِ درگاهِ کاستوم، فاکتور تا وقتی «فیش» نرسد معمولاً pending می‌ماند (تأییدِ بانکی
            # همیشه خودکار نیست). گزارشِ ۷۲ ساعتِ اخیر: همهٔ فاکتورهایِ تسویه‌نشده pending بودند ⇒ کاربر
            # واریز کرده ولی فیش نفرستاده. یک‌بار، حدودِ ۹۰ ثانیه بعد از ساختِ فاکتور، یادآوریِ فیش می‌فرستیم.
            elif (is_custom_order(order_id) and _i >= 4
                    and _LAST_STATUS.get((bot_id, order_id)) == "pending"
                    and (bot_id, order_id) not in _RECEIPT_REMINDED):
                _RECEIPT_REMINDED.add((bot_id, order_id))
                await bot.send_message(
                    chat_id,
                    f"⏳ فاکتورِ <code>{order_id}</code> هنوز تأیید نشده.\n\n"
                    "اگر <b>واریز کرده‌ای</b>، برایِ تأییدِ سریع <b>تصویرِ فیشِ واریزی</b> را با دکمهٔ "
                    "زیر بفرست؛ بدونِ فیش ممکن است پرداختت تأیید نشود. "
                    "اگر هنوز واریز نکرده‌ای، این پیام را نادیده بگیر.",
                    parse_mode="HTML", reply_markup=receipt_kb(order_id))
        except Exception:
            logger.exception("tonpays auto-poll error for %s", order_id)
    # مهلت تمام شد ولی هنوز تایید نشده
    try:
        async with Session() as s:
            ch = await s.scalar(select(TonPaysCharge).where(
                TonPaysCharge.bot_id == bot_id,
                TonPaysCharge.order_id == order_id))
        if ch and ch.status == "waiting":
            await bot.send_message(
                chat_id,
                "⏳ هنوز پرداختی برایِ فاکتورِ "
                f"<code>{order_id}</code> ثبت نشده.\n\n"
                "اگر واریز کرده‌ای، دکمهٔ «پرداخت کردم» را در پیامِ فاکتور بزن "
                "تا دوباره بررسی شود.",
                parse_mode="HTML")
    except Exception:
        logger.exception("tonpays auto-poll final notice")


# === PATCH_TONPAYS_PENDING_V1 ===
async def _offer_buyer_block(msg: Message, bot_id: int, user_id: int, amount: int,
                             custom: bool, after_cancel: bool, code: str) -> None:
    """درگاه گفته کاربر فاکتورِ بازِ دیگری دارد (BUYER_HAS_*) ⇒ پیامِ مناسبِ همان وضعیت:
    pending → «لغو و پرداخت جدید»؛ processing → صبر؛ مبلغِ کمتر/بیشتر → دکمهٔ جزئیات/ثبتِ کارتِ عودت."""
    async with Session() as s:
        ch = await s.scalar(select(TonPaysCharge).where(
            TonPaysCharge.bot_id == bot_id, TonPaysCharge.user_telegram_id == user_id,
            TonPaysCharge.status == "waiting",
            TonPaysCharge.order_id.like("tc%" if custom else "tp%"),
        ).order_by(TonPaysCharge.id.desc()))
    if code == "BUYER_HAS_PENDING_INVOICE" and ch and not after_cancel:
        await msg.answer(pending_text(ch.request_amount), parse_mode="HTML",
                         reply_markup=pending_kb("tonp", ch.order_id, amount))
        return
    kb = None
    if ch and code in ("BUYER_HAS_NEED_ACTION_LESS", "BUYER_HAS_NEED_ACTION_MORE", "BUYER_HAS_PROCESSING_INVOICE"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="✅ بررسیِ وضعیتِ پرداختِ قبلی", callback_data=f"tonp:verify:{ch.order_id}")]])
    txt = ERROR_FA[code]
    if code == "BUYER_HAS_PENDING_INVOICE":
        txt = ("⚠️ در درگاه هنوز یک فاکتورِ پرداخت‌نشده برایِ تو باز است و فعلاً فاکتورِ جدید ساخته نمی‌شود.\n"
               "چند دقیقه بعد دوباره تلاش کن یا با پشتیبانی تماس بگیر.")
    await msg.answer("⚠️ " + txt if not txt.startswith("⚠️") else txt, reply_markup=kb)


@tonpays_router.callback_query(F.data.startswith("tonp:cnew:"))
async def tonp_cancel_and_new(cb: CallbackQuery, bot: Bot):
    """لغوِ فاکتورِ قبلی (در درگاه + محلی) و ساختِ فاکتورِ جدید با همان مبلغ."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    try:
        _, _, order_id, amt = cb.data.split(":")
        amount = int(amt)
    except ValueError:
        await cb.answer("درخواست نامعتبر است.", show_alert=True)
        return
    if not row:
        await cb.answer()
        return
    async with Session() as s:
        ch = await s.scalar(select(TonPaysCharge).where(
            TonPaysCharge.bot_id == row.id, TonPaysCharge.order_id == order_id))
    if not ch or ch.user_telegram_id != cb.from_user.id:
        await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        return
    if ch.status == "paid":
        await cb.answer("این فاکتور قبلاً پرداخت شده است.", show_alert=True)
        return
    await cb.answer("⏳ در حال لغو…")
    _custom = is_custom_order(order_id)
    api_key = await get_key(row.id, _custom)
    res, why = await cancel_at_gateway(api_key, ch.invoice_id, _custom)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("tonp: پاک‌کردنِ دکمه‌هایِ لغو ممکن نشد (بی‌اهمیت)")
    if res == "paid":    # واریز شده بود: لغو نمی‌کنیم، شارژ می‌کنیم
        ok, text, _amt = await settle_charge(row.id, order_id)
        await cb.message.answer("✅ " + text if ok else text)
        return
    if res == "blocked":   # در انتظارِ تأیید / مبلغِ کمتر / مبلغِ بیشتر ⇒ لغو نمی‌شود، فاکتورِ جدید هم نه
        await cb.message.answer("⚠️ " + why + "\nبا «✅ پرداخت کردم» وضعیت را بررسی کن.")
        return
    if res == "retry":     # PATCH_TONPAYS_DOCS_AUDIT_V1 — لغو در درگاه نشد؛ فاکتور همچنان باز و قابل لغو می‌ماند
        await cb.message.answer(RETRY_CANCEL_TEXT, reply_markup=pending_kb("tonp", order_id, amount))
        return
    async with Session() as s:
        await s.execute(
            sa_update(TonPaysCharge)
            .where(TonPaysCharge.bot_id == row.id, TonPaysCharge.order_id == order_id,
                   TonPaysCharge.status != "paid")
            .values(status="cancelled"))
        await s.commit()
    if res == "error":
        logger.warning("tonpays cancel-and-new: gateway cancel failed for %s: %s", order_id, why)
    await _create_invoice(bot, row, cb.message, cb.from_user.id, amount, after_cancel=True)


@tonpays_router.callback_query(F.data.startswith("tonp:cancel:"))
async def tonp_cancel(cb: CallbackQuery, bot: Bot):
    """لغوِ یک فاکتورِ پرداخت‌نشده (فقط سمتِ ما — پیگیریِ خودکار هم متوقف می‌شود)."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    order_id = cb.data.split(":")[2]
    async with Session() as s:
        ch = await s.scalar(select(TonPaysCharge).where(
            TonPaysCharge.bot_id == row.id, TonPaysCharge.order_id == order_id))
        if not ch or ch.user_telegram_id != cb.from_user.id:
            await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
            return
        if ch.status == "paid":
            await cb.answer("این فاکتور قبلاً پرداخت شده و قابلِ لغو نیست.",
                            show_alert=True)
            return
        _inv_id = ch.invoice_id
    # === PATCH_TONPAYS_PENDING_V1 === فاکتور در خودِ درگاه هم لغو شود (وگرنه فاکتورِ بعدی رد می‌شود)
    _custom = is_custom_order(order_id)
    res, _why = await cancel_at_gateway(await get_key(row.id, _custom), _inv_id, _custom)
    _paid_text = ""
    if res == "paid":
        ok, text, _amt = await settle_charge(row.id, order_id)
        _paid_text = ("✅ " + text) if ok else text
    elif res == "blocked":
        await cb.answer(_why, show_alert=True)
        return
    elif res == "retry":   # PATCH_TONPAYS_DOCS_AUDIT_V1
        await cb.answer(RETRY_CANCEL_TEXT, show_alert=True)
        return
    else:
        async with Session() as s:
            await s.execute(
                sa_update(TonPaysCharge)
                .where(TonPaysCharge.bot_id == row.id, TonPaysCharge.order_id == order_id,
                       TonPaysCharge.status != "paid")
                .values(status="cancelled"))
            await s.commit()
    await cb.answer("فاکتور لغو شد." if not _paid_text else "")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")
    await cb.message.answer(
        _paid_text or "❌ فاکتور لغو شد. اگر بعداً خواستی، دوباره از کیف پول شارژ را شروع کن.\n\n"
        "⚠️ اگر قبلِ لغو واریز کرده بودی، با پشتیبانی تماس بگیر.")


@tonpays_router.callback_query(F.data.startswith("tonp:verify:"))
async def tonp_verify(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, rial
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    order_id = cb.data.split(":")[2]
    await cb.answer("در حال استعلام از درگاه…")
    ok, note, credit = await settle_charge(row.id, order_id)
    if ok:
        await cb.message.answer(f"✅ {note}\n\nمبلغِ <b>{rial(credit)}</b> به کیف "
                                "پولت اضافه شد.", parse_mode="HTML")
    else:
        # === PATCH_TONPAYS_CUSTOM_V1 === در کاستوم، کنارِ پیام دکمهٔ ارسالِ فیش هم باشد
        await cb.message.answer(
            f"ℹ️ {note}",
            reply_markup=receipt_kb(order_id) if is_custom_order(order_id) else None)


# ═══════════ تعویضِ کارت (درگاهِ کاستوم) ═══════════
# === PATCH_TONPAYS_CHANGE_CARD_V1 ===
# سقفِ کارت‌هایِ TonPays زود پر می‌شود؛ وقتی کارتِ فاکتور پر شده یا واریز به آن ممکن نیست،
# خریدار با یک دکمه کارتِ دیگری می‌گیرد (کول‌داونِ ۶۰ثانیه، کارت‌هایِ استفاده‌شده حذف می‌شوند).
_CHG_LAST: dict[tuple[int, str], float] = {}


def _cooldown_left(key) -> int:
    import time as _t
    last = _CHG_LAST.get(key)
    return max(0, int(CHANGE_CARD_COOLDOWN - (_t.time() - last))) if last else 0


@tonpays_router.callback_query(F.data.startswith("tonp:chg:"))
async def tonp_change_card(cb: CallbackQuery, bot: Bot):
    import time as _t
    from tenant import get_bot_row, rial
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    order_id = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(TonPaysCharge).where(
            TonPaysCharge.bot_id == row.id, TonPaysCharge.order_id == order_id))
    if not ch or ch.user_telegram_id != cb.from_user.id or not is_custom_order(order_id):
        await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        return
    if ch.status == "paid":
        await cb.answer("این فاکتور قبلاً پرداخت شده است.", show_alert=True)
        return
    key = (row.id, order_id)
    left = _cooldown_left(key)
    if left:
        await cb.answer(f"⏳ برایِ تعویضِ دوباره {left} ثانیهٔ دیگر صبر کن.", show_alert=True)
        return
    api_key = await get_key(row.id, True)
    if not api_key or not ch.invoice_id:
        await cb.answer("درگاه تنظیم نشده است.", show_alert=True)
        return
    try:
        res = await change_card(api_key, ch.invoice_id)
    except TonPaysError as e:
        if e.status == 429 or "COOLDOWN" in (e.code or "").upper():
            _CHG_LAST[key] = _t.time()
            await cb.answer(f"⏳ تعویضِ کارت محدود است؛ حدودِ {CHANGE_CARD_COOLDOWN} ثانیه بعد دوباره بزن.",
                            show_alert=True)
        else:
            await cb.answer(f"❌ {e}", show_alert=True)
        return
    except Exception:
        logger.exception("tonpays change_card failed for %s", order_id)
        await cb.answer("❌ تعویضِ کارت ناموفق بود؛ کمی بعد دوباره امتحان کن.", show_alert=True)
        return
    _CHG_LAST[key] = _t.time()
    card, card_name = pick_card(
        res, await get_setting_safe(row.id, "tonpays_card_number"),
        await get_setting_safe(row.id, "tonpays_card_holder"))
    final_amount = int(res.get("final_amount") or ch.final_amount or ch.request_amount)
    if final_amount != int(ch.final_amount or 0):
        try:
            async with Session() as s:
                await s.execute(sa_update(TonPaysCharge).where(
                    TonPaysCharge.bot_id == row.id, TonPaysCharge.order_id == order_id
                ).values(final_amount=final_amount))
                await s.commit()
        except Exception:
            logger.exception("tonpays: ثبتِ مبلغِ جدیدِ بعد از تعویضِ کارت ناموفق بود")
    exhausted = bool(res.get("change_card_exhausted"))
    can_change = bool(res.get("show_change_card", True)) and not exhausted
    text, kb = build_invoice_view(
        "tonp", order_id, int(ch.request_amount), final_amount, card, card_name, True,
        rial_fn=rial, can_change=can_change)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:      # noqa: BLE001
        if "not modified" not in str(e).lower():
            await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
    if exhausted:
        await cb.answer("✅ کارت عوض شد. این آخرین کارتِ موجود بود؛ دیگر تعویض ممکن نیست.", show_alert=True)
    else:
        await cb.answer("✅ کارتِ جدید نمایش داده شد. اگر باز مشکل داشت، بعد از ۶۰ ثانیه می‌توانی دوباره تعویض کنی.",
                        show_alert=True)


async def get_setting_safe(bot_id: int, key: str) -> str:
    from tenant import get_setting
    return (await get_setting(bot_id, key, "")) or ""


# ═══════════ ارسالِ فیش (درگاهِ کاستوم) ═══════════
# === PATCH_TONPAYS_CUSTOM_V1 ===
# طبقِ مستنداتِ TonPays دکمهٔ ارسالِ فیش اجباری است (وگرنه دسترسی بسته می‌شود):
# وقتی فاکتور خودکار تأیید نمی‌شود، خریدار باید بتواند تصویرِ فیش را بفرستد.

@tonpays_router.callback_query(F.data.startswith("tonp:rcpt:"))
async def tonp_receipt_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    order_id = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(TonPaysCharge).where(
            TonPaysCharge.bot_id == row.id, TonPaysCharge.order_id == order_id))
    if (not ch or ch.user_telegram_id != cb.from_user.id
            or not (is_custom_order(order_id)
                    or _LAST_STATUS.get((row.id, order_id)) == "action_required_less")):
        await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        return
    if ch.status == "paid":
        await cb.answer("این فاکتور قبلاً تأیید و شارژ شده است.", show_alert=True)
        return
    await state.set_state(TonPaysFSM.receipt)
    await state.update_data(tonp_receipt_order=order_id)
    await cb.message.answer(
        f"📎 <b>ارسال فیش واریزی</b> — فاکتورِ <code>{order_id}</code>\n\n"
        "تصویرِ فیش را همین‌جا به‌صورتِ <b>عکس</b> بفرست (حداکثر ۱۰ مگابایت).",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@tonpays_router.message(TonPaysFSM.receipt, F.photo | F.document)
async def tonp_receipt_file(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    data = await state.get_data()
    order_id = data.get("tonp_receipt_order") or ""
    if not row or not order_id:
        await state.clear()
        return

    if msg.photo:
        f = msg.photo[-1]
        fname, ctype = "receipt.jpg", "image/jpeg"
    else:
        f = msg.document
        ctype = (f.mime_type or "").lower()
        if not ctype.startswith("image/"):
            await msg.answer("❌ فقط تصویرِ فیش (JPG/PNG) پذیرفته می‌شود. دوباره بفرست:")
            return
        fname = f.file_name or "receipt.jpg"
    if f.file_size and f.file_size > RECEIPT_MAX_BYTES:
        await msg.answer("❌ " + ERROR_FA["RECEIPT_TOO_LARGE"])
        return

    async with Session() as s:
        ch = await s.scalar(select(TonPaysCharge).where(
            TonPaysCharge.bot_id == row.id, TonPaysCharge.order_id == order_id))
    if not ch or ch.user_telegram_id != msg.from_user.id:
        await state.clear()
        await msg.answer("این فاکتور پیدا نشد.")
        return
    api_key = await get_key(row.id, is_custom_order(order_id))
    if not api_key or not ch.invoice_id:
        await state.clear()
        await msg.answer("درگاه تنظیم نشده است.")
        return

    await msg.answer("⏳ در حال ارسالِ فیش…")
    try:
        buf = await bot.download(f.file_id)
        raw = buf.read() if buf else b""
        if not raw:
            raise RuntimeError("دانلودِ فایل از تلگرام ناموفق بود.")
        res = await upload_receipt(api_key, ch.invoice_id, raw, fname, ctype,
                                   custom=is_custom_order(order_id))
    except TonPaysError as e:
        # خطایِ فایل (نوع/حجم) ⇒ حالتِ انتظار می‌ماند تا دوباره بفرستد
        if e.code in ("INVALID_RECEIPT_TYPE", "RECEIPT_TOO_LARGE"):
            await msg.answer(f"❌ {e}\nدوباره بفرست:")
            return
        await state.clear()
        await msg.answer(f"❌ ارسالِ فیش ناموفق بود.\n{e}", reply_markup=receipt_kb(order_id))
        return
    except Exception as e:
        logger.exception("tonpays receipt upload failed for %s", order_id)
        await state.clear()
        await msg.answer(f"❌ ارسالِ فیش ناموفق بود: {e}", reply_markup=receipt_kb(order_id))
        return

    await state.clear()
    _NEED_ACTION_NOTIFIED.discard((row.id, order_id))
    await msg.answer(
        "✅ <b>فیش دریافت شد.</b>\nبعد از بررسی و تأییدِ درگاه، مبلغ خودکار به "
        "کیف پولت اضافه می‌شود و همین‌جا خبرت می‌کنم.\n\n"
        f"وضعیت: {STATUS_FA.get((res or {}).get('status'), (res or {}).get('status') or 'در حال بررسی')}",
        parse_mode="HTML")
    # پیگیریِ خودکار دوباره (مهلتِ قبلی ممکن است تمام شده باشد)
    asyncio.create_task(_auto_poll(bot, row.id, order_id, msg.chat.id))


# ═══════════ ثبتِ کارتِ عودتِ مبلغِ بیشتر ═══════════
# === PATCH_TONPAYS_PENDING_V1 === action_required_more: مازاد به کارتِ خریدار عودت می‌شود؛
# با ذخیرهٔ کارت فاکتور completed/paid می‌شود و همان مسیرِ امنِ settle_charge کیف پول را شارژ می‌کند.

@tonpays_router.callback_query(F.data.startswith("tonp:rfnd:"))
async def tonp_refund_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    order_id = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(TonPaysCharge).where(
            TonPaysCharge.bot_id == row.id, TonPaysCharge.order_id == order_id))
    if not ch or ch.user_telegram_id != cb.from_user.id:
        await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        return
    if ch.status == "paid":
        await cb.answer("این فاکتور قبلاً تأیید و شارژ شده است.", show_alert=True)
        return
    await state.set_state(TonPaysFSM.refund_card)
    await state.update_data(tonp_rfnd_order=order_id)
    await cb.message.answer(
        "💳 <b>ثبتِ کارتِ عودت</b>\n\nشمارهٔ <b>۱۶ رقمیِ کارتِ خودت</b> را بفرست "
        "(کارتی که مازادِ واریز به آن برگردد):",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@tonpays_router.message(TonPaysFSM.refund_card, F.text)
async def tonp_refund_card(msg: Message, state: FSMContext):
    card = norm_card(msg.text)
    if not card:
        await msg.answer("❌ شمارهٔ کارت باید ۱۶ رقم باشد. دوباره بفرست:")
        return
    await state.update_data(tonp_rfnd_card=card)
    await state.set_state(TonPaysFSM.refund_holder)
    await msg.answer("🧑 حالا <b>نامِ صاحبِ کارت</b> را بفرست:", parse_mode="HTML")


@tonpays_router.message(TonPaysFSM.refund_holder, F.text)
async def tonp_refund_holder(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    data = await state.get_data()
    order_id, card = data.get("tonp_rfnd_order") or "", data.get("tonp_rfnd_card") or ""
    holder = (msg.text or "").strip()[:60]
    if not row or not order_id or not card:
        await state.clear()
        return
    if len(holder) < 3:
        await msg.answer("❌ نامِ صاحبِ کارت را کامل بفرست:")
        return
    async with Session() as s:
        ch = await s.scalar(select(TonPaysCharge).where(
            TonPaysCharge.bot_id == row.id, TonPaysCharge.order_id == order_id))
    if not ch or ch.user_telegram_id != msg.from_user.id:
        await state.clear()
        await msg.answer("این فاکتور پیدا نشد.")
        return
    _custom = is_custom_order(order_id)
    try:
        await refund_card(await get_key(row.id, _custom), ch.invoice_id, card, holder, _custom)
    except TonPaysError as e:
        await state.clear()
        await msg.answer(f"❌ ثبتِ کارتِ عودت ناموفق بود.\n{e}")
        return
    except Exception as e:
        logger.exception("tonpays refund failed for %s", order_id)
        await state.clear()
        await msg.answer(f"❌ ثبتِ کارتِ عودت ناموفق بود: {e}")
        return
    await state.clear()
    _NEED_ACTION_NOTIFIED.discard((row.id, order_id))
    ok, text, _amt = await settle_charge(row.id, order_id)
    await msg.answer(("✅ کارتِ عودت ثبت شد. " + text) if ok else
                     "✅ کارتِ عودت ثبت شد؛ مازادِ واریز به کارتت برمی‌گردد. " + text)


# ═══════════ جابِ دوره‌ای: پیگیریِ فاکتورهایِ باز بعد از ری‌استارت ═══════════

# مهلتی که بعد از آن، فاکتورِ پرداخت‌نشده رها می‌شود (دقیقه).
STALE_AFTER_MIN = 60


async def poll_pending_once() -> None:
    """همهٔ فاکتورهایِ waitingِ تازه را یک دور استعلام و در صورتِ پرداخت تسویه می‌کند.

    === PATCH_TONPAYS_PERSISTENT_POLL_V1 ===
    پیگیریِ درون‌حافظه‌ای (_auto_poll) با ری‌استارتِ سرویس از بین می‌رفت و
    فاکتورهایِ باز بی‌صاحب می‌شدند. این جاب مستقل از حافظه، از رویِ دیتابیس
    کار می‌کند، پس بعد از هر ری‌استارت هم پیگیری ادامه پیدا می‌کند.
    """
    from datetime import datetime, timedelta
    from models import Bot as BotRow
    from tenant import rial

    cutoff = datetime.utcnow() - timedelta(minutes=STALE_AFTER_MIN)
    async with Session() as s:
        pend = (await s.scalars(select(TonPaysCharge).where(
            TonPaysCharge.status == "waiting",
            TonPaysCharge.created_at >= cutoff))).all()
        items = [(c.bot_id, c.order_id, c.user_telegram_id) for c in pend]

    for bot_id, order_id, tg_id in items:
        try:
            ok, _note, credit = await settle_charge(bot_id, order_id)
        except Exception:
            logger.exception("tonpays sweeper: settle failed for %s", order_id)
            continue
        # === PATCH_TONPAYS_DOCS_V2 === سقفِ ۶۰ استعلام/دقیقه: با اولین ۴۲۹ دور را رها کن
        if (not ok) and "تعدادِ درخواست" in (_note or ""):
            logger.warning("tonpays sweeper: RATE_LIMIT_EXCEEDED — ادامه در دورِ بعد")
            break
        await asyncio.sleep(1.1)  # حدودِ ۵۵ استعلام در دقیقه، زیرِ سقفِ ۶۰
        # === PATCH_TONPAYS_CUSTOM_V1 === need_action ⇒ یک‌بار از خریدار فیش بخواه
        if (not ok and needs_action_notice(bot_id, order_id)
                and (bot_id, order_id) not in _NEED_ACTION_NOTIFIED):
            _NEED_ACTION_NOTIFIED.add((bot_id, order_id))
            try:
                async with Session() as s:
                    brow = await s.get(BotRow, bot_id)
                if brow:
                    from aiogram import Bot as _Bot
                    from aiogram.client.default import DefaultBotProperties
                    from aiogram.enums import ParseMode
                    tbot = _Bot(token=brow.token,
                                default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                    try:
                        _t, _k = action_notice("tonp", order_id, _LAST_INFO.get((bot_id, order_id)) or {},
                                               is_custom_order(order_id))
                        await tbot.send_message(tg_id, _t, reply_markup=_k)
                    finally:
                        await tbot.session.close()
            except Exception:
                logger.exception("tonpays sweeper: need_action notify failed for %s", order_id)
        if not ok or credit <= 0:
            continue
        try:
            async with Session() as s:
                brow = await s.get(BotRow, bot_id)
            if not brow:
                continue
            from aiogram import Bot as _Bot
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode
            tbot = _Bot(token=brow.token,
                        default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                await tbot.send_message(
                    tg_id,
                    "✅ <b>پرداختت تایید شد!</b>\n\n"
                    f"مبلغِ <b>{rial(credit)}</b> به کیف پولت اضافه شد.")
            finally:
                await tbot.session.close()
        except Exception:
            logger.exception("tonpays sweeper: notify failed for %s", order_id)


# === PATCH_TONPAYS_LATE_SWEEP_V1 ===
# گزارشِ مشتری (nitroteeam): کاربر با TonPays پرداخت کرد ولی کیف پول خودکار شارژ نشد. دو حفره:
#  ۱) جاب‌ِ بالا فقط فاکتورهایِ <۶۰ دقیقه را می‌دید؛ پرداختِ دیرهنگام (یا تأییدِ دستیِ درگاه) هرگز شارژ نمی‌شد.
#  ۲) فاکتورِ «expired/rejected/cancelled» برایِ همیشه رها می‌شد، حتی اگر درگاه بعداً آن را تأیید می‌کرد.
# حالا هر ~۱۰ دقیقه فاکتورهایِ ۲۴ ساعتِ اخیرِ تسویه‌نشده (بالای ۶۰ دقیقه یا ناموفق) یک‌بار دیگر
# از خودِ درگاه استعلام می‌شوند؛ فقط اگر درگاه paid بگوید و مبلغ بخواند، شارژ می‌شود (همان settle_charge امن).
LATE_WINDOW_H = 24
LATE_EVERY_SEC = 600
LATE_MAX_PER_RUN = 40
_LATE_LAST = 0.0


async def poll_late_once() -> None:
    import time as _t
    from datetime import datetime, timedelta
    from models import Bot as BotRow
    from tenant import rial
    global _LATE_LAST
    if _t.time() - _LATE_LAST < LATE_EVERY_SEC:
        return
    _LATE_LAST = _t.time()
    now = datetime.utcnow()
    async with Session() as s:
        rows = (await s.scalars(select(TonPaysCharge).where(
            TonPaysCharge.status != "paid",
            TonPaysCharge.created_at >= now - timedelta(hours=LATE_WINDOW_H),
            ((TonPaysCharge.created_at < now - timedelta(minutes=STALE_AFTER_MIN))
             | (TonPaysCharge.status != "waiting"))
        ).order_by(TonPaysCharge.id.desc()).limit(LATE_MAX_PER_RUN))).all()
        items = [(c.bot_id, c.order_id, c.user_telegram_id) for c in rows]
    for bot_id, order_id, tg_id in items:
        try:
            ok, _note, credit = await settle_charge(bot_id, order_id)
        except Exception:
            logger.exception("tonpays late-sweep: settle failed for %s", order_id)
            continue
        if (not ok) and "تعدادِ درخواست" in (_note or ""):
            break
        await asyncio.sleep(1.1)
        if not ok or credit <= 0:
            continue
        logger.warning("tonpays late-sweep: فاکتورِ دیرهنگام تسویه شد bot=%s order=%s", bot_id, order_id)
        try:
            async with Session() as s:
                brow = await s.get(BotRow, bot_id)
            if brow:
                from aiogram import Bot as _Bot
                from aiogram.client.default import DefaultBotProperties
                from aiogram.enums import ParseMode
                tbot = _Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                try:
                    await tbot.send_message(
                        tg_id, "✅ <b>پرداختت تایید شد!</b>\n\n"
                               f"مبلغِ <b>{rial(credit)}</b> به کیف پولت اضافه شد.")
                finally:
                    await tbot.session.close()
        except Exception:
            logger.exception("tonpays late-sweep: notify failed for %s", order_id)


# === PATCH_TONPAYS_USER_NOTE_V1 ===
@tonpays_router.callback_query(F.data == "tonp:setnote")
async def tonp_setnote(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_user_note(row.id)
    await state.set_state(TonPaysFSM.user_note)
    await cb.message.answer(
        "📝 <b>متنِ توضیحی که کاربر موقعِ شارژ با این درگاه می‌بیند</b>\n\n"
        "متنِ فعلی:\n" + (f"<blockquote>{cur}</blockquote>" if cur else "— (خاموش)") + "\n\n"
        f"متنِ جدید را بفرست (حداکثر {USER_NOTE_MAX} حرف).\n"
        "• برای برگشت به متنِ پیش‌فرض: <code>پیش‌فرض</code>\n"
        "• برای اینکه هیچ متنی نشان داده نشود: <code>خاموش</code>",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@tonpays_router.message(TonPaysFSM.user_note, F.text)
async def tonp_setnote_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg, set_setting
    row = await _owner_ok_msg(msg, bot)
    await state.clear()
    if not row:
        return
    text = (msg.text or "").strip()
    if text in ("پیش‌فرض", "پیشفرض", "default"):
        await set_setting(row.id, "tonpays_user_note", "")
        await msg.answer("✅ متنِ توضیح به پیش‌فرض برگشت.")
    elif text in ("خاموش", "off", "-"):
        await set_setting(row.id, "tonpays_user_note", "off")
        await msg.answer("✅ از این به بعد متنِ توضیحی به کاربر نشان داده نمی‌شود.")
    elif not text:
        await msg.answer("متنِ نامعتبر. دوباره امتحان کن.")
        return
    else:
        await set_setting(row.id, "tonpays_user_note", text[:USER_NOTE_MAX])
        await msg.answer("✅ متنِ توضیح ذخیره شد.")
    await msg.answer(
        "برایِ ادامه به تنظیماتِ درگاه برگرد:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💠 بازگشت به درگاه", callback_data="tonp:menu")]]))
