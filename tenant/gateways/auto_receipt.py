"""
تایید خودکار رسیدهای کارت‌به‌کارت — PATCH_AUTO_RECEIPT_V3 (فقط Arvan/هوش‌مصنوعیِ خودمختار)
──────────────────────────────────────────────────────────────────────────────
این ماژول مسئولِ «خواندن، مقایسه، و تصمیم‌گیری» دربارهٔ رسید است. **کل کار**
(خواندنِ عکس/متن + مقایسه با اطلاعاتِ سفارش + تصمیمِ نهایی) در یک تماسِ
واحد به سرویسِ هوش مصنوعیِ آروان (ARVAN_AI_BASE_URL، مدلِ قابل‌تنظیم — روی این
دیپلوی GPT-4.1 با پرامپتِ اختصاصی) سپرده می‌شود؛ خودِ مدل رسید را می‌خواند،
با مبلغ/شماره‌کارت/تاریخِ سفارش مقایسه می‌کند، و «تایید» یا «ارجاع به بررسیِ
دستی» را با دلیل برمی‌گرداند.

نکتهٔ تاریخی: نسخه‌های قبلیِ این فایل چند موتورِ OCR جدا (Arvan OCR ساده،
Gemini مستقیم، OCR.space، Tesseract محلی) را به‌ترتیب امتحان می‌کردند و بعد
با رگکس/منطقِ پایتونی مبلغ و شماره‌کارت را استخراج و مقایسه می‌کردند. آن مسیر
به‌طور کامل حذف شده (طبق تصمیمِ صریحِ صاحبِ پروژه، چون نتیجهٔ مسیرِ خودمختار
با پرامپتِ اختصاصی قابل‌اعتمادتر بود) — اگر لازم شد به آن روش برگردی، نسخهٔ
قبلی در تاریخچهٔ گیت/بکاپ‌ها موجود است.

اگر ARVAN_AI_BASE_URL تنظیم نشده باشد، رسیدها همیشه به بررسیِ دستیِ ادمین
ارجاع داده می‌شوند (fail-safe — هیچ‌وقت به‌خاطرِ نبودِ تنظیمات، رسید «تایید»
نمی‌شود).
"""
import html   # === PATCH_AUTO_RECEIPT_ESCAPE_ERR_BODY_V1 === (escape قبل از ورود به پیامِ HTML-mode)
import random  # === PATCH_ARVAN_MULTI_KEY_ROTATION_V1 ===
import re
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except Exception:
    _AIOHTTP_AVAILABLE = False

try:
    from config import ARVAN_AI_BASE_URL, ARVAN_AI_MODEL, ARVAN_AI_API_KEY, ARVAN_AI_API_KEYS
except Exception:
    ARVAN_AI_BASE_URL = ""
    ARVAN_AI_MODEL = "Gemini-2.5-Flash-lite"
    ARVAN_AI_API_KEY = ""
    ARVAN_AI_API_KEYS = []

import asyncio   # PATCH_AUTO_RECEIPT_TIMEOUT_V1 (برایِ asyncio.TimeoutError)

# === PATCH_AUTO_RECEIPT_TIMEOUT_V1 ===
# ۳۰ ثانیه برایِ رسیدهایِ *عکسی* کم بود: عکسِ گوشی چند مگابایت است، به
# base64 حدودِ ۳۳٪ بزرگ‌تر هم می‌شود، و مدل باید کلِ آن را بخواند. نتیجه‌اش
# «خطای اتصال به آروان: TimeoutError» بود در حالی که خودِ سرویس سالم بود.
# با ARVAN_AI_TIMEOUT قابلِ تنظیم است.
import os as _os_ar
try:
    ARVAN_AI_TIMEOUT_SECONDS = int(_os_ar.getenv("ARVAN_AI_TIMEOUT", "60"))
except ValueError:
    ARVAN_AI_TIMEOUT_SECONDS = 60

try:
    import jdatetime
    _JDATE_AVAILABLE = True
except Exception:
    _JDATE_AVAILABLE = False

_FA_TRANS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


@dataclass
class ReceiptCheck:
    ok: bool
    reason: str
    raw_text: str | None = None
    amount: int | None = None
    card_last4: str | None = None
    date_jalali: str | None = None
    sender_card4: str | None = None
    tracking_no: str | None = None   # شمارهٔ پیگیری (اثرِ انگشتِ یکتای رسید)
    time_str: str | None = None      # ساعتِ تراکنش روی رسید


def _norm(text: str) -> str:
    return text.translate(_FA_TRANS).replace(",", "").replace("٬", "").replace("،", "")


# PATCH_AUTO_RECEIPT_RIAL_TOMAN_AND_FAIL_NOTICE
def extract_ai_toman_amount(text: str) -> int | None:
    """مبلغ را از خروجیِ ساختاریافتهٔ هوش مصنوعی می‌خواند: عددِ خام («مبلغ_عدد:»)
    و واحد («مبلغ_واحد:»). سپس خودِ کد تبدیلِ ریال→تومان را انجام می‌دهد (÷۱۰)،
    چون محاسبه در کد قطعی است ولی مدل‌های سبک گاهی در تقسیم خطا می‌کنند.
    خروجی: مبلغِ نهایی به تومان یا None (اگر برچسب‌ها نبودند)."""
    if not text:
        return None
    t = _norm(text)
    m = re.search(r'مبلغ[_\s]?عدد\s*[:：]?\s*(\d{3,12})', t)
    if not m:
        # سازگاری با قالبِ قبلی که مستقیم تومان می‌داد
        m2 = re.search(r'مبلغ[_\s]?تومان\s*[:：]?\s*(\d{3,12})', t)
        if m2:
            try:
                return int(m2.group(1))
            except Exception:
                return None
        return None
    try:
        num = int(m.group(1))
    except Exception:
        return None
    # واحد را از برچسبِ مدل یا از خودِ متنِ رسید تشخیص بده
    unit_m = re.search(r'مبلغ[_\s]?واحد\s*[:：]?\s*(ریال|تومان|تومن)', t)
    unit = None
    if unit_m:
        unit = 'تومان' if unit_m.group(1) in ('تومان', 'تومن') else 'ریال'
    if unit is None:
        # برچسبِ واحد نبود → از کلماتِ نزدیکِ عدد در متنِ خام حدس بزن
        near = t[max(0, m.start() - 40):min(len(t), m.end() + 50)]
        if 'ریال' in near:
            unit = 'ریال'
        elif 'تومان' in near or 'تومن' in near:
            unit = 'تومان'
    if unit is None:
        # هنوز نامشخص → کلِ متن را نگاه کن؛ اگر «ریال» هرجا بود، ریال فرض کن
        if 'ریال' in t:
            unit = 'ریال'
        elif 'تومان' in t or 'تومن' in t:
            unit = 'تومان'
        else:
            unit = 'ریال'  # پیش‌فرضِ رسیدِ بانکی
    if unit == 'ریال':
        # ریال → تومان
        return num // 10
    return num  # تومان


# PATCH_CARD_REGEX_IGNORE_IBAN
def _blank_iban_spans(t: str) -> str:
    """بازه‌های متنیِ شبا (IRxx... یا برچسبِ «شبا»/«حساب») را با فاصله
    جایگزین می‌کند تا رگکسِ شماره‌کارت اشتباهاً با گروه‌های رقمیِ شبا
    مچ نشود؛ طولِ متن (و افست‌های بقیهٔ استخراج‌کننده‌ها) دست‌نخورده می‌ماند."""
    spans = []
    for m in re.finditer(r'IR\s*[\d\s\-]{20,32}', t, re.IGNORECASE):
        spans.append(m.span())
    for m in re.finditer(r'شبا\D{0,10}\d[\d\s\-]{15,30}\d', t):
        spans.append(m.span())
    for m in re.finditer(r'(?:شماره\s*)?حساب\D{0,10}\d[\d\s\-]{4,24}\d', t):
        spans.append(m.span())
    if not spans:
        return t
    chars = list(t)
    for start, end in spans:
        for i in range(start, end):
            if chars[i] not in ('\n',):
                chars[i] = ' '
    return ''.join(chars)


# PATCH_SENDER_CARD_CHECK
def extract_card_last4_candidates(text: str) -> list[str]:
    """همهٔ ۴رقمی‌های آخرِ شماره‌کارت‌های محتمل را از متنِ رسید استخراج می‌کند
    (چون رسید معمولاً هم شماره‌کارتِ مبدأ/ارسال‌کننده و هم مقصد/گیرنده را دارد).
    قبلش بازه‌های شبا/حساب حذف می‌شوند تا با گروه‌های رقمیِ آن‌ها قاطی نشود
    — PATCH_CARD_REGEX_IGNORE_IBAN."""
    if not text:
        return []
    t = _blank_iban_spans(_norm(text))
    group = r'(?:\d{4}|\*{2,4})'
    result: list[str] = []
    for m in re.finditer(rf'{group}[\s\-]?{group}[\s\-]?{group}[\s\-]?(\d{{4}})(?!\d)', t):
        v = m.group(1)
        if v not in result:
            result.append(v)
    for m in re.finditer(r'\b(\d{16})\b', t):
        v = m.group(1)[-4:]
        if v not in result:
            result.append(v)
    return result


def extract_tracking_no(text: str) -> str | None:
    """شمارهٔ پیگیری/مرجعِ تراکنش را از رسید استخراج می‌کند (یکتاترین شناسه).
    اول از برچسبِ ساختاریافتهٔ هوش مصنوعی، بعد از الگوهای متنِ خام."""
    if not text:
        return None
    t = _norm(text)
    m = re.search(r'شماره[_\s]?پیگیری\s*[:：]?\s*(\d{6,20})', t)
    if m:
        return m.group(1)
    # الگوهای رایج در متنِ رسید
    m = re.search(r'(?:پیگیری|مرجع|رهگیری|reference|trace|rrn)\D{0,12}(\d{6,20})', t, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def extract_receipt_time(text: str) -> str | None:
    """ساعتِ تراکنش را از رسید استخراج می‌کند (مثل ۲۱:۱۹:۰۳ یا ۲۱:۱۹)."""
    if not text:
        return None
    t = _norm(text)
    m = re.search(r'ساعت\s*[:：]?\s*(\d{1,2}:\d{2}(?::\d{2})?)', t)
    if m:
        return m.group(1)
    m = re.search(r'\b(\d{1,2}:\d{2}:\d{2})\b', t)
    if m:
        return m.group(1)
    return None


def extract_date_jalali(text: str):
    """پیدا کردنِ تاریخِ شمسی روی رسید (فرمت‌های رایج: ۱۴۰۳/۰۵/۱۲ یا ۱۴۰۳-۰۵-۱۲)."""
    if not text or not _JDATE_AVAILABLE:
        return None
    t = _norm(text)
    m = re.search(r'\b(1[34]\d{2})[/\-](\d{1,2})[/\-](\d{1,2})\b', t)
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return jdatetime.date(y, mo, d)
    except Exception:
        return None


def _receipt_ai_autonomous_prompt(expected_amount_toman: int,
                                   expected_card_last4: str | None,
                                   expected_sheba_last4: str | None,
                                   expected_account_last4: str | None,
                                   expected_sender_last4: str | None,
                                   expected_sender_id_label: str,
                                   order_date_jalali: str | None) -> str:
    """پرامپتِ اختصاصیِ حالتِ خودمختار: اطلاعاتِ سفارش هم به مدل داده می‌شود
    تا خودش مقایسه و تصمیم‌گیری (تایید/ارجاع) را انجام دهد، نه فقط OCR.

    === PATCH_AUTO_APPROVE_MULTI_DEST_ID_V1 ===
    قبلاً فقط «شماره کارتِ مقصد» به مدل داده می‌شد. حالا شبا و شماره‌حسابِ
    مقصد هم (اگر ادمین موقعِ فعال‌سازی وارد کرده باشد) داده می‌شود، چون
    رسیدِ کارت‌به‌کارت گاهی فقط شبا یا فقط شماره‌حساب را نشان می‌دهد، نه
    شماره‌کارت — و قبلاً در آن حالت، هوش مصنوعی نمی‌توانست مقصد را تایید
    کند حتی اگر واقعاً درست بود.
    """
    base = (
        "تو یک متخصص خواندن و راستی‌آزمایی رسیدهای بانکی ایران هستی. این تصویر "
        "(یا متن) یک رسید پرداخت/کارت‌به‌کارت/انتقال وجه است. وظیفه تو فقط خواندن "
        "نیست؛ باید با اطلاعات سفارش که پایین همین پیام آمده مقایسه کنی و خودت "
        "تصمیم بگیری.\n\n"

        "انواع رسیدهایی که ممکن است ببینی: بانک ملی، ملت، صادرات، تجارت، سپه، "
        "پاسارگاد، پارسیان، سامان، اقتصاد نوین، کارآفرین، سینا، شهر، دی، آینده، "
        "رسالت، مهر ایران، انصار، حکمت، گردشگری، کشاورزی، مسکن، رفاه، پست‌بانک، "
        "توسعه تعاون، و اپ‌ها/درگاه‌ها مثل همراه‌بانک‌ها، بلوبانک، بانکینو، ویپاد، "
        "آپ، سکه، تاپ، 724، ایوا، سداد، شاپرک، و رسیدهای شتابی/پایا/ساتنا/پل.\n\n"

        "-- اطلاعات سفارش (برای مقایسه) --\n"
        f"مبلغ مورد انتظار: {expected_amount_toman:,} تومان\n"
    )
    dest_lines = []
    if expected_card_last4:
        dest_lines.append(f"4 رقم آخر شماره‌کارتِ مقصد: {expected_card_last4}")
    if expected_sheba_last4:
        dest_lines.append(f"4 رقم آخر شمای (IBAN) مقصد: {expected_sheba_last4}")
    if expected_account_last4:
        dest_lines.append(f"4 رقم آخر شماره‌حسابِ مقصد: {expected_account_last4}")
    if dest_lines:
        base += (
            "شناسه‌های احتمالیِ مقصد (گیرنده) که فروشنده ثبت کرده — رسید ممکن است "
            "فقط یکی از این سه را نشان دهد (کارت یا شبا یا حساب)، همین که رسید با "
            "**یکی** از این‌ها مطابقت داشته باشد کافی است، لازم نیست هر سه باشد:\n"
            + "\n".join(dest_lines) + "\n"
        )
    if expected_sender_last4:
        base += (f"4 رقم آخر {expected_sender_id_label} مبدأ (فرستنده) که کاربر خودش اعلام "
                 f"کرده و باید روی رسید باشد: {expected_sender_last4}\n")
    if order_date_jalali:
        base += f"تاریخ ثبت سفارش: {order_date_jalali} (رسید باید حداکثر 1 روز اختلاف داشته باشد)\n"

    base += (
        "\n-- قوانین دقیق تصمیم‌گیری --\n"
        "1) مبلغ: اگر رسید به «ریال» است، بر 10 تقسیم کن تا تومان شود (بانک‌های "
        "ایرانی معمولاً ریال می‌نویسند). اگر واحدی نوشته نشده، ریال فرض کن. مبلغ "
        "نهایی (تومان) باید دقیقاً با «مبلغ مورد انتظار» یکی باشد (رواداری حداکثر "
        "1 تومان برای گِردکردن مجاز است). کارمزد/موجودی/مانده را با مبلغ اصلی "
        "اشتباه نگیر.\n"
        "2) مقصد: اگر شناسه‌های مقصد بالا داده شده، رسید باید با حداقلِ یکی از "
        "آن‌ها (کارت یا شبا یا حساب، هرکدام که در رسید دیده شد) دقیقاً مطابقت "
        "داشته باشد.\n"
        "3) شناسه مبدأ: اگر «4 رقم آخر ... مبدأ» بالا داده شده، باید دقیقاً همان "
        "4 رقم روی رسید در همان نوع شناسه (کارت/شبا/حساب) دیده شود.\n"
        "4) تاریخ: اگر «تاریخ ثبت سفارش» داده شده و تاریخ روی رسید خوانده شد، "
        "اختلاف نباید بیش از 1 روز باشد.\n"
        "5) اگر تصویر اصلاً رسید بانکی نیست یا جعلی/دستکاری‌شده به‌نظر می‌رسد، "
        "ارجاع بده.\n"
        "6) فقط وقتی «تایید» بده که همه موارد بالا (که مقدار برایشان داده شده) با "
        "اطمینان کامل مطابقت داشته باشند. اگر حتی یک مورد نامشخص/غیرقابل خواندن/"
        "مغایر بود، حتماً «ارجاع» بده -- هیچ‌وقت به‌خاطر نامشخص بودن تایید نکن "
        "(fail-safe).\n\n"

        "خروجی را دقیقاً به این قالب بده (هر مورد در یک خط، بدون توضیح اضافه):\n"
        "مبلغ_عدد: <عدد مبلغ اصلی رسید، همان‌طور که روی رسید نوشته، فقط رقم>\n"
        "مبلغ_واحد: <ریال یا تومان>\n"
        "کارت_مقصد: <4 رقم آخر کارت گیرنده، اگر بود>\n"
        "شبا_مقصد: <4 رقم آخر شبای گیرنده، اگر بود>\n"
        "حساب_مقصد: <4 رقم آخر شماره‌حساب گیرنده، اگر بود>\n"
        "کارت_مبدأ: <4 رقم آخر کارت فرستنده، اگر بود>\n"
        "شبا_مبدأ: <4 رقم آخر شبای فرستنده، اگر بود>\n"
        "حساب_مبدأ: <4 رقم آخر حساب فرستنده، اگر بود>\n"
        "تاریخ: <تاریخ شمسی رسید مثل 1404/04/19، اگر بود>\n"
        "ساعت: <ساعت تراکنش، اگر بود>\n"
        "شماره_پیگیری: <شماره پیگیری/مرجع/رهگیری، اگر بود>\n"
        "تصمیم: <تایید یا ارجاع>\n"
        "دلیل: <یک جمله کوتاه فارسی روشن برای ادمین؛ اگر تایید است بنویس «همه "
        "موارد مطابقت داشت»، اگر ارجاع است دقیقاً بگو کدام مورد و چرا>\n"
    )
    return base


# === PATCH_AUTO_RECEIPT_IMAGE_SHRINK_V1 ===
_ARVAN_MAX_IMG_BYTES = 900_000     # حدودِ ۱٫۲ مگابایت بعد از base64


def _shrink_receipt_image(raw: bytes, max_w: int = 1600, max_bytes: int = _ARVAN_MAX_IMG_BYTES) -> bytes:
    """عکسِ رسید را تا حدِ خوانا کوچک می‌کند.

    عکسِ خامِ گوشی گاهی ۵ مگابایت است؛ فرستادنش هم کند است هم گران، و
    بیشترِ تایم‌اوت‌ها از همین می‌آمد. متنِ رسید در عرضِ ۱۶۰۰ پیکسل کاملاً
    خواناست. اگر Pillow نصب نباشد یا تبدیل شکست بخورد، همان عکسِ اصلی
    فرستاده می‌شود (هیچ‌وقت نباید جلویِ بررسیِ رسید را بگیرد).
    """
    if not raw or len(raw) <= max_bytes:
        return raw
    try:
        import io as _io
        from PIL import Image
        im = Image.open(_io.BytesIO(raw))
        im = im.convert("RGB")
        if im.width > max_w:
            ratio = max_w / im.width
            im = im.resize((max_w, max(1, int(im.height * ratio))))
        for quality in (80, 65, 50):
            buf = _io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            out = buf.getvalue()
            if len(out) <= max_bytes:
                return out
        return out
    except Exception:
        logger.info("کوچک‌کردنِ عکسِ رسید ممکن نشد؛ عکسِ اصلی فرستاده می‌شود.")
        return raw


async def _arvan_autonomous_call(prompt: str, image_bytes: bytes | None,
                                 creds: dict | None = None) -> tuple[str | None, str | None]:
    """تماس با آروان (سازگار با OpenAI /chat/completions) برای حالتِ خودمختار
    (با یا بدونِ عکس). مدل از طریقِ ARVAN_AI_MODEL قابل‌تنظیم است (روی این
    دیپلوی GPT-4.1)."""
    # === PATCH_RECEIPT_AI_OWN_KEY_V1 === کلیدِ اختصاصیِ رباتِ مستأجر (در صورتِ وجود)
    _base_url = (creds or {}).get("url") or ARVAN_AI_BASE_URL
    _own_key = (creds or {}).get("key") or ""
    _keys = [_own_key] if _own_key else list(ARVAN_AI_API_KEYS)
    _model = ARVAN_AI_MODEL
    if creds and creds.get("url"):
        _mm = re.search(r"/models/([^/]+)/", creds["url"])   # نامِ مدل از خودِ آدرسِ آروان
        if _mm:
            _model = _mm.group(1)
    if not _base_url:
        return None, "آدرس endpoint آروان (ARVAN_AI_BASE_URL) تنظیم نشده."
    if not _AIOHTTP_AVAILABLE:
        return None, "کتابخانه aiohttp روی سرور نصب نیست."
    import base64
    content = [{"type": "text", "text": prompt}]
    _raw_image = image_bytes
    if image_bytes:
        image_bytes = _shrink_receipt_image(image_bytes)   # PATCH_..._IMAGE_SHRINK_V1
        b64 = base64.b64encode(image_bytes).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    url = _base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": _model,
        "temperature": 0,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": content}],
    }
    # === PATCH_ARVAN_MAX_TOKENS_CAP_V1 ===
    # بدونِ max_tokens، Groq یه پیش‌فرضِ بزرگ (۲۰۴۸) رو برایِ محاسبه‌ی
    # سهمیه رزرو می‌کنه — حتی اگه جوابِ واقعی کوتاه باشه. رویِ اکانت‌هایی
    # که سقفِ OTPM (خروجی در دقیقه) پایینه (مثلاً ۱۰۰۰)، همین رزروِ الکی
    # باعث می‌شد هر درخواست همون اول با ۴۲۹ رد بشه — مهم نیست چندتا کلید
    # داری یا چقدر رندم می‌چرخونیش. ۵۰۰ برایِ جوابِ JSONِ استخراجِ رسید
    # کاملاً کافیه.
    # === PATCH_ARVAN_REASONING_EFFORT_NONE_V1 ===
    # مدل‌هایِ reasoning روی Groq (qwen3.6 و gpt-oss) قبل از جوابِ نهایی یه
    # بلوکِ <think> طولانی تولید می‌کنن که هم کند بود (چند ثانیه فقط برایِ
    # فکرکردنِ الکی رویِ سوال‌هایِ سادهٔ رسید) هم مصرفِ توکن رو بی‌دلیل بالا
    # می‌برد. طبقِ مستنداتِ Groq، پارامترِ reasoning_effort این فکرکردن رو
    # کاملاً خاموش می‌کنه: برایِ Qwen مقدارِ "none"، برایِ GPT-OSS مقدارِ
    # "low". این پارامتر فقط وقتی اضافه می‌شه که مدل واقعاً یکی از این
    # خانواده‌ها باشه — برایِ مدل‌هایِ ناشناخته (مثلِ GPT-4.1 که این فایل
    # دیگه صداش نمی‌زنه، ولی محتاط باشیم) دست‌نخورده می‌مونه.
    _model_l = (_model or "").lower()
    if "qwen" in _model_l:
        payload["reasoning_effort"] = "none"
    elif "gpt-oss" in _model_l:
        payload["reasoning_effort"] = "low"
    try:
        # === PATCH_ARVAN_TIMEOUT_RETRY_V1 === (MidnightSoul: «آروان در ۶۰ ثانیه جواب نداد» بارها)
        # قبلاً یک تایم‌اوت (asyncio.TimeoutError) از حلقهٔ تلاش‌ها می‌پرید بیرون و بدونِ امتحانِ کلیدِ دیگر/تلاشِ دوباره
        # شکستِ نهایی بود، پس یک پاسخِ کندِ لحظه‌ای کافی بود رسید به تاییدِ دستی برود. حالا هر تلاش سقفِ کوتاه‌تری دارد
        # (نصفِ ARVAN_AI_TIMEOUT، حداقل ۲۰ ثانیه)، تایم‌اوت هم مثلِ خطایِ موقتِ سرور تلاشِ بعدی را (با کلیدِ دیگر و
        # عکسِ کوچک‌ترِ ۱۱۰۰ پیکسلی) امتحان می‌کند.
        _attempt_secs = max(20, ARVAN_AI_TIMEOUT_SECONDS // 2)
        timeout = aiohttp.ClientTimeout(total=_attempt_secs)
        last_err = None
        # === PATCH_ARVAN_MULTI_KEY_ROTATION_V1 ===
        # با چند کلید، تعدادِ تلاش‌ها رو به تعدادِ کلیدها (حداقل ۳) می‌رسونیم
        # تا واقعاً فرصتِ رفتن سراغِ هر کلید رو داشته باشیم.
        max_attempts = max(3, len(_keys))
        for attempt in range(max_attempts):
            # === PATCH_ARVAN_MULTI_KEY_ROTATION_V1 ===
            # هر تلاش یه کلید رندوم از بینِ کلیدها انتخاب می‌کنه (چند اکانتِ
            # رایگان)، تا اگه یکی سهمیه‌اش تموم شده بود (۴۲۹)، تلاشِ بعدی با
            # یه کلیدِ دیگه انجام بشه، نه لزوماً همون کلیدِ خالی.
            headers = {"Content-Type": "application/json"}
            key = random.choice(_keys) if _keys else ARVAN_AI_API_KEY
            if key:
                headers["Authorization"] = (key if key.lower().startswith(("apikey ", "bearer "))
                                            else f"Bearer {key}")
            # === PATCH_ARVAN_TRANSIENT_NETWORK_RETRY_V1 ===
            # گزارشِ ادمین (سفارش #8707): «ClientPayloadError: ...
            # TransferEncodingError: 400, Not enough data for satisfy
            # transfer length header» — این خطا وسطِ *خوندنِ* پاسخ اتفاق
            # می‌افته (نه رویِ status)، پس قبلاً از تلاشِ اول می‌رفت مستقیم
            # به except بیرونیِ کلِ تابع و بقیه‌یِ max_attempts اصلاً
            # امتحان نمی‌شدن — دقیقاً همون مشکلی که برایِ ۵۰۰/۵۰۲/۵۰۳/۵۰۴/
            # ۵۲۹ (PATCH_AUTO_RECEIPT_504_RETRY_V1) قبلاً حل شده بود، ولی
            # برایِ خطاهایِ سطحِ‌اتصال (نه سطحِ HTTP status) نه. حالا این
            # دسته هم دقیقاً مثلِ خطایِ موقتِ سرور رفتار می‌کنه: تلاشِ بعدی
            # رو امتحان می‌کنه، نه شکستِ فوری.
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            break
                        body = (await resp.text())[:200]
                        if resp.status == 429:
                            # === PATCH_ARVAN_MULTI_KEY_ROTATION_V1 ===
                            # با چند کلید، ۴۲۹ (سهمیه‌ی این کلیدِ خاص تموم شده)
                            # دیگه دلیل نمی‌شه کلاً تسلیم بشیم — تلاشِ بعدی یه
                            # کلیدِ دیگه رندوم انتخاب می‌کنه. فقط اگه یک کلید
                            # بیشتر نداریم (یا تلاش‌ها تموم شد)، واقعاً شکست بخوریم.
                            if len(_keys) > 1:
                                last_err = "سهمیه/اعتبار این کلید تمام شده (429)؛ تلاش با کلیدِ دیگر..."
                                continue
                            return None, "سهمیه/اعتبار تمام شده (429) — کیف‌پول/کلید را شارژ یا عوض کن."
                        # === PATCH_AUTO_RECEIPT_504_RETRY_V1 ===
                        # ۵۰۴ (Gateway Timeout) قبلاً جزو کدهای قابل‌ری‌ترای نبود؛
                        # با شلوغیِ لحظه‌ایِ گیت‌وی آروان (که در عمل رایج‌تر از
                        # ۵۰۰/۵۰۲/۵۰۳ بود) بلافاصله شکستِ سخت می‌خورد.
                        if resp.status in (500, 502, 503, 504, 529):
                            last_err = f"آروان خطای موقت سرور {resp.status}"
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        # === PATCH_AUTO_RECEIPT_DIAGNOSTIC_LOGGING_V1 ===
                        # اسمِ مدل رو هم تویِ خطا میاریم: رایج‌ترین علتِ شکستِ
                        # رسیدهای *عکسی* اینه که مدلِ تنظیم‌شده اصلاً ورودیِ
                        # تصویری پشتیبانی نمی‌کنه (مثلاً یه مدلِ فقط-متنی) — بدونِ
                        # دیدنِ اسمِ مدل، این حالت اصلاً قابلِ‌تشخیص نبود.
                        # === PATCH_AUTO_RECEIPT_ESCAPE_ERR_BODY_V1 ===
                        # این متنِ `err` بعداً بدونِ تغییر داخلِ پیامی با
                        # parse_mode="HTML" به کانال/DM ادمین فرستاده می‌شه
                        # (adm_caption در card_purchase.py). اگه سرورِ بالادستی
                        # به‌جایِ JSON یه صفحه‌ی HTML ارور برگردونه (مثلاً ۵۰۴ از
                        # Cloudflare)، تگ‌هایِ خامش پارسِ HTML تلگرام رو می‌شکنه و
                        # کلِ پیام (هم ارسال به کانال، هم fallback به DM) بی‌صدا
                        # شکست می‌خوره — یعنی ادمین اصلاً از رسید خبردار نمی‌شه.
                        # escape کردنِ اینجا (سرچشمه) تضمین می‌کنه هر جایی که این
                        # متن بعداً استفاده بشه، امن باشه.
                        _img_note = " [این درخواست شاملِ عکس بود]" if image_bytes else ""
                        return None, (f"آروان خطای HTTP {resp.status} (مدل: {_model})"
                                      f"{_img_note}: {html.escape(body)}")
            except asyncio.TimeoutError:
                last_err = f"آروان در {_attempt_secs} ثانیه جواب نداد (تلاش {attempt + 1})"
                logger.warning("auto_receipt: %s", last_err)
                if _raw_image and attempt == 0:      # تلاشِ بعدی با عکسِ کوچک‌ترِ خوانا
                    try:
                        _small = _shrink_receipt_image(_raw_image, max_w=1100, max_bytes=350_000)
                        _b64s = base64.b64encode(_small).decode("ascii")
                        payload["messages"] = [{"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64s}"}}]}]
                    except Exception:
                        logger.info("auto_receipt: کوچک‌ترکردنِ عکس برایِ تلاشِ دوم ممکن نشد")
                continue
            except (aiohttp.ClientError, ConnectionError) as _net_e:
                last_err = f"خطایِ موقتِ اتصال به آروان: {type(_net_e).__name__}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
        else:
            return None, (last_err or "آروان پس از چند تلاش پاسخ نداد") + " (شلوغی موقت)."
        choices = data.get("choices") or []
        if not choices:
            return None, "آروان هیچ نتیجه‌ای برنگرداند."
        msg = choices[0].get("message") or {}
        text = (msg.get("content") or "").strip()
        # === PATCH_ARVAN_REASONING_MODEL_SAFETY_V1 ===
        # بعضی مدل‌ها (مثلِ مدل‌هایِ reasoning روی Groq مثلِ qwen3.6) قبل از
        # جوابِ نهایی یه بلوکِ <think>...</think> (فکرکردنِ خام) برمی‌گردونن.
        # پارسر منتظرِ خطوطِ دقیقِ «تصمیم:»/«دلیل:»ه، نه این فکرکردن — پس
        # قبل از parse، هر بلوکِ <think> رو حذف می‌کنیم. برایِ مدل‌هایی که
        # اصلاً همچین بلوکی ندارن (مثلِ GPT-4.1 روی آروان)، این regex هیچ
        # تطبیقی پیدا نمی‌کنه و متن دست‌نخورده می‌مونه — کاملاً بی‌خطر.
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
        if not text:
            return None, "آروان متنی تشخیص نداد."
        return text, None
    except asyncio.TimeoutError:
        # === PATCH_AUTO_RECEIPT_TIMEOUT_V1 ===
        return None, (f"آروان در {ARVAN_AI_TIMEOUT_SECONDS} ثانیه جواب نداد "
                      "(شلوغیِ موقت یا عکسِ خیلی بزرگ). رسید به تاییدِ دستی رفت.")   # فقط اگر تایم‌اوتِ بیرونی
    except Exception as e:
        # === PATCH_AUTO_RECEIPT_ESCAPE_ERR_BODY_V1 ===
        return None, f"خطای اتصال به آروان: {type(e).__name__}: {html.escape(str(e)[:200])}"


def _parse_autonomous_response(text: str, expected_sender_last4: str | None,
                               expected_card_last4: str | None,
                               expected_amount: int | None = None) -> "ReceiptCheck":
    """پاسخ ساختاریافته حالت خودمختار را به ReceiptCheck تبدیل می‌کند. خط
    «تصمیم» مرجع اصلی ok است؛ اگر پیدا نشد یا مبهم بود، fail-safe: ok=False
    (تایید دستی)."""
    t = _norm(text)

    decision_line = None
    reason_line = None
    for line in t.splitlines():
        s = line.strip()
        if s.startswith("تصمیم"):
            decision_line = s.split(":", 1)[-1].strip() if ":" in s else ""
        elif s.startswith("دلیل"):
            reason_line = s.split(":", 1)[-1].strip() if ":" in s else ""

    ok = bool(decision_line) and ("تایید" in decision_line) and ("ارجاع" not in decision_line)

    amount = extract_ai_toman_amount(text)

    # === PATCH_AI_AMOUNT_SELF_CONTRADICTION_FIX_V1 ===
    # باگِ واقعی: مدل خودش عددِ خام + واحد رو درست تشخیص می‌ده (مثلاً
    # «۳٬۰۰۰٬۰۰۰ ریال»)، ولی موقعِ تصمیمِ نهایی گاهی یادش می‌ره تقسیم‌بر‌ده
    # رو انجام بده و همون رقمِ خامِ ریالی رو به‌جایِ تومان با مبلغِ موردِ
    # انتظار مقایسه می‌کنه — نتیجه: رسیدِ کاملاً درست، فقط به‌خاطرِ یه
    # اشتباهِ محاسباتیِ مدل، رد می‌شه. چون extract_ai_toman_amount() از
    # همون فیلدهایِ خامِ خودِ مدل (که استخراجشون درست بوده) با یه تقسیمِ
    # قطعی در کد حساب می‌شه، اگه مدل رد کرده باشه *و* این مقدارِ
    # مستقل‌محاسبه‌شده دقیقاً با مبلغِ موردِ انتظار یکی باشه، یعنی خودِ رد
    # از رویِ همون اشتباهِ محاسباتی بوده — تصمیم رو اصلاح می‌کنیم، نه اینکه
    # کورکورانه به «تصمیم»ِ متنیِ مدل اعتماد کنیم.
    amount_override = False
    if (not ok and amount is not None and expected_amount is not None
            and amount == int(expected_amount) and reason_line
            and "مبلغ" in reason_line
            # === PATCH_AI_AMOUNT_SELF_CONTRADICTION_FIX_V1 ===
            # فقط وقتی override کن که ردِ مدل واقعاً *فقط* دربارهٔ مبلغ
            # بوده — اگه کارت/شبا/حساب هم تو دلیل مشکوک بودن، یعنی رد به
            # یه دلیلِ دیگه هم هست و نباید کورکورانه تاییدش کنیم.
            and not any(kw in reason_line for kw in ("کارت", "شبا", "حساب", "پیگیری", "تاریخ"))):
        ok = True
        amount_override = True

    # === PATCH_AI_APPROVE_AMOUNT_GUARD_V1 ===
    # باگِ واقعی (گزارشِ ادمینِ VPN): سفارشی ۲۵۰٬۰۰۰ تومانی با رسیدِ ۱٬۸۰۰٬۰۰۰ ریال (= ۱۸۰٬۰۰۰
    # تومان) «تایید خودمختار — همه موارد مطابقت داشت» خورد. علت: بالا فقط یک نگهبانِ یک‌طرفه
    # بود (ردِ مدل → تاییدِ کد وقتی مبلغ برابر است)، ولی هیچ نگهبانی برایِ حالتِ برعکس نبود: اگر
    # مدل خودش عددِ خامِ درست را می‌نوشت (۱٬۸۰۰٬۰۰۰ ریال) ولی در «تصمیم» خطا می‌کرد و می‌نوشت
    # «تایید»، کد کورکورانه به همان اعتماد می‌کرد. مبلغ یک عددِ قطعی است و نباید به قضاوتِ مدل
    # سپرده شود: هر «تایید» فقط وقتی معتبر است که مبلغِ خوانده‌شده (که کد خودش از فیلدهایِ خامِ
    # مدل حساب می‌کند) با مبلغِ سفارش برابر باشد — با رواداریِ ۱ تومان که خودِ پرامپت هم می‌گوید.
    # اگر مبلغ اصلاً خوانده نشد هم تایید نمی‌شود (fail-safe): بدونِ عدد چیزی برایِ راستی‌آزمایی نیست.
    amount_guard_reason = None
    if ok and expected_amount is not None:
        if amount is None:
            ok = False
            amount_guard_reason = ("مبلغِ رسید از خروجیِ هوش مصنوعی خوانده نشد؛ بدونِ مبلغ "
                                   "نمی‌شود رسید را تایید کرد.")
        elif abs(int(amount) - int(expected_amount)) > 1:
            ok = False
            amount_guard_reason = (f"مبلغِ خوانده‌شده از رسید ({int(amount):,} تومان) با مبلغِ "
                                   f"سفارش ({int(expected_amount):,} تومان) مطابقت ندارد.")
        if amount_guard_reason:
            logger.warning("[AI_RECEIPT] تاییدِ مدل توسطِ نگهبانِ مبلغ رد شد — %s | تصمیمِ مدل: %s | دلیلِ مدل: %s",
                           amount_guard_reason, decision_line, reason_line)

    card4_candidates = extract_card_last4_candidates(text)
    card_last4 = (expected_card_last4 if (expected_card_last4 and expected_card_last4 in card4_candidates)
                 else (card4_candidates[0] if card4_candidates else None))
    date_jalali_obj = extract_date_jalali(text)
    date_str = date_jalali_obj.strftime("%Y/%m/%d") if date_jalali_obj else None
    tracking_no = extract_tracking_no(text)
    time_str = extract_receipt_time(text)

    if ok:
        if amount_override:
            reason = ("\U0001F916 تایید خودکار (اصلاح‌شده): مدل تویِ تبدیلِ ریال↔تومان اشتباه "
                     "کرده بود، ولی با محاسبه‌ی قطعیِ خودِ کد، مبلغ دقیقاً برابرِ موردِ انتظار است.")
        else:
            reason = "\U0001F916 تایید خودمختار توسط هوش مصنوعی" + (f" -- {reason_line}" if reason_line else "")
    elif amount_guard_reason:
        # === PATCH_AI_APPROVE_AMOUNT_GUARD_V1 === ادمین باید ببیند *چرا* تایید خودکار نشد
        reason = (f"\U0001F916 ⛔ تایید خودکار متوقف شد: {amount_guard_reason} "
                  "(مدل اشتباهاً «تایید» گفته بود) — بررسی دستی لازم است.")
    else:
        reason = (f"\U0001F916 {reason_line}" if reason_line
                 else "\U0001F916 هوش مصنوعی رسید را تاییدپذیر تشخیص نداد و به بررسی دستی ارجاع داد.")

    return ReceiptCheck(ok=ok, reason=reason, raw_text=text, amount=amount, card_last4=card_last4,
                        date_jalali=date_str, sender_card4=expected_sender_last4,
                        tracking_no=tracking_no, time_str=time_str)


async def evaluate_receipt(bot: Bot, receipt_text: str | None, file_id: str | None,
                           expected_amount, expected_card_number: str | None,
                           order_created_at: datetime | None,
                           expected_sender_last4: str | None = None,
                           expected_sender_id_type: str | None = "card",
                           expected_sheba_number: str | None = None,
                           expected_account_number: str | None = None,
                           bot_id: int | None = None) -> ReceiptCheck:
    """رسید (متنی یا عکسی) + اطلاعاتِ سفارش، یک‌جا به هوش مصنوعیِ آروان داده
    می‌شود تا خودش بخواند، مقایسه کند، و تصمیمِ نهایی (تایید/ارجاع) را با
    دلیل برگرداند. اگر آروان تنظیم نشده باشد یا پاسخ ندهد، fail-safe:
    ok=False (تایید دستی) — هیچ‌وقت رسید بدونِ تاییدِ هوش مصنوعی «تایید»
    نمی‌شود.

    expected_sender_last4: ۴ رقمِ آخرِ کارتی/شبایی/حسابی که کاربر خودش هنگامِ
    ثبتِ درخواستِ پرداخت اعلام کرده (مبدأ/ارسال‌کننده) — برای کراس‌چک.
    expected_sheba_number/expected_account_number: شبا/شماره‌حسابِ مقصدِ
    فروشنده — PATCH_AUTO_APPROVE_MULTI_DEST_ID_V1؛ اختیاری، اگر ادمین در
    ویزاردِ فعال‌سازیِ تاییدِ خودکار وارد کرده باشد.
    """
    # === PATCH_RECEIPT_AI_OWN_KEY_V1 === کلیدِ خودِ رباتِ مستأجر (هزینه با خودش)
    _creds = None
    if bot_id:
        from tenant.gateways.receipt_ai_key import get_creds as _gc, REQUIRE_OWN_KEY as _REQ
        _creds = await _gc(bot_id)
        if _REQ and not _creds:
            return ReceiptCheck(ok=False, reason="کلیدِ هوش مصنوعیِ آروانِ این ربات ثبت نشده؛ "
                                                 "رسید به بررسیِ دستی رفت.")
    if not ARVAN_AI_BASE_URL and not _creds:
        return ReceiptCheck(ok=False, reason="سرویسِ تشخیصِ خودکار (ARVAN_AI_BASE_URL) تنظیم نشده.")

    img_bytes = None
    if not receipt_text and file_id:
        try:
            file = await bot.get_file(file_id)
            buf = await bot.download_file(file.file_path)
            img_bytes = buf.read() if hasattr(buf, "read") else bytes(buf)
        except Exception as e:
            return ReceiptCheck(ok=False,
                                reason=f"خطا در دانلود عکس رسید از تلگرام: {type(e).__name__}: {str(e)[:200]}")
    if not receipt_text and not img_bytes:
        return ReceiptCheck(ok=False, reason="نه متنی و نه عکسی از رسید دریافت نشد.")

    expected_amount = int(expected_amount)

    def _last4(raw: str | None) -> str | None:
        if not raw:
            return None
        # === PATCH_CARD_BOTH_V1 === چند مقدار با «|» (کارتِ اصلی و دوم) → «۱۲۳۴ یا ۵۶۷۸»
        outs = []
        for part in str(raw).split("|"):
            digits = re.sub(r'\D', '', part)
            if len(digits) >= 4 and digits[-4:] not in outs:
                outs.append(digits[-4:])
        return " یا ".join(outs) if outs else None

    expected_card_last4 = _last4(expected_card_number)
    expected_sheba_last4 = _last4(expected_sheba_number)
    expected_account_last4 = _last4(expected_account_number)

    id_labels = {"sheba": "شبا", "account": "حساب", "card": "کارت"}
    sender_label = id_labels.get(expected_sender_id_type or "card", "کارت")

    order_date_jalali = None
    if order_created_at and _JDATE_AVAILABLE:
        try:
            order_date_jalali = jdatetime.date.fromgregorian(date=order_created_at.date()).strftime("%Y/%m/%d")
        except Exception:
            order_date_jalali = None

    prompt = _receipt_ai_autonomous_prompt(expected_amount, expected_card_last4,
                                           expected_sheba_last4, expected_account_last4,
                                           expected_sender_last4, sender_label, order_date_jalali)
    if receipt_text:
        prompt += f"\n-- متن رسید ارسالی کاربر --\n{receipt_text}\n"

    text, err = await _arvan_autonomous_call(prompt, img_bytes, creds=_creds)
    if not text:
        # === PATCH_AUTO_RECEIPT_DIAGNOSTIC_LOGGING_V1 ===
        # قبلاً این شکست فقط به‌صورتِ ReceiptCheck برمی‌گشت و هیچ ردی تویِ
        # error.log نمی‌ذاشت (تنها print موجود، سطحِ INFO داشت که تویِ
        # error.log نمیاد) — برای همین وقتی ادمین‌ها می‌گفتن «تاییدِ خودکار
        # فعاله ولی تایید نمی‌کنه»، هیچ سرنخی برایِ پیداکردنِ علت نبود.
        logger.warning("[AI_RECEIPT] بررسیِ رسید انجام نشد — %s", err or "خطای نامشخص")
        return ReceiptCheck(ok=False, reason=f"\U0001F916 هوش مصنوعی رسید را بررسی نکرد -- {err or 'خطای نامشخص'}")

    print(f"[AI_RECEIPT] پاسخ خام:\n{text}")
    _result = _parse_autonomous_response(text, expected_sender_last4, expected_card_last4,
                                         expected_amount=expected_amount)
    if not _result.ok:
        # === PATCH_AUTO_RECEIPT_DIAGNOSTIC_LOGGING_V1 ===
        # «هوش مصنوعی جواب داد ولی تایید نکرد» هم باید قابلِ‌ردیابی باشه —
        # این رایج‌ترین حالتیه که ادمین ازش شکایت می‌کنه.
        logger.warning("[AI_RECEIPT] رسید تایید نشد — دلیل: %s", _result.reason)
    return _result


async def duplicate_receipt_reason(scope: str, check, file_unique_id: str | None = None) -> str | None:
    """اگر رسید تکراری باشد، پیامِ دلیلِ مناسب برمی‌گرداند؛ وگرنه None.
    برای استفادهٔ یکسان در پلتفرم و ربات‌های مستأجر.

    `file_unique_id`: اگر داده شود، جدا از fingerprintِ متنی (شماره‌پیگیری/
    تاریخ+ساعت+مبلغ)، خودِ عکسِ رسید هم چک می‌شود — یعنی حتی اگر هوش مصنوعی
    نتوانسته باشد شماره‌پیگیری یا تاریخ را از رسید بخواند، ارسالِ دوبارهٔ
    دقیقاً همان عکس هم رسیدِ تکراری تشخیص داده می‌شود."""
    photo_fp = photo_fingerprint(file_unique_id)
    if photo_fp and await is_duplicate_receipt(scope, photo_fp):
        return "همین عکسِ رسید قبلاً برای یک شارژِ دیگر استفاده شده است — رسیدِ تکراری."
    fp = receipt_fingerprint(check)
    if fp and await is_duplicate_receipt(scope, fp):
        if check.tracking_no:
            ident = f"شمارهٔ پیگیری {check.tracking_no}"
        else:
            ident = f"تاریخ/ساعت {check.date_jalali or ''} {check.time_str or ''}".strip()
        return f"این رسید قبلاً استفاده و شارژ شده است ({ident}) — رسیدِ تکراری."
    return None


def photo_fingerprint(file_unique_id: str | None) -> str | None:
    """اثرِ انگشتِ عکسِ رسید بر اساسِ file_unique_id تلگرام (مستقل از OCR/هوش
    مصنوعی) — قابل‌اعتمادترین راه برای تشخیصِ «همین عکس قبلاً ارسال شده»."""
    if not file_unique_id:
        return None
    return f"photo:{file_unique_id}"


def receipt_fingerprint(check: "ReceiptCheck") -> str | None:
    """اثرِ انگشتِ یکتای رسید را از نتیجهٔ بررسی می‌سازد.
    اولویت با شمارهٔ پیگیری است؛ اگر نبود از تاریخ+ساعت+مبلغ ساخته می‌شود.
    اگر هیچ‌کدام نبود None برمی‌گرداند (نمی‌توان تکراری بودن را تشخیص داد)."""
    if check.tracking_no:
        # نرمال‌سازی: حذفِ فاصله/جداکننده و صفرهای ابتداییِ اضافه تا دو
        # نوشتارِ متفاوتِ همان شماره (مثلاً با/بدون صفرهای ابتدایی) یکی شناخته شوند.
        trk = re.sub(r'\D', '', check.tracking_no).lstrip('0') or '0'
        return f"trk:{trk}"
    parts = []
    if check.date_jalali:
        parts.append(check.date_jalali)
    if check.time_str:
        # فقط تا سطحِ دقیقه (بدون ثانیه) تا نوسانِ جزئیِ خواندنِ ثانیه توسط
        # هوش مصنوعی بینِ دو بار خواندنِ همان رسید، فینگرپرینت را عوض نکند.
        parts.append(":".join(check.time_str.split(":")[:2]))
    if check.amount is not None:
        parts.append(str(check.amount))
    # فقط وقتی معتبر است که حداقل تاریخ یا ساعت داشته باشیم (نه صرفاً مبلغ)
    if (check.date_jalali or check.time_str) and parts:
        return "dt:" + "|".join(parts)
    return None


async def is_duplicate_receipt(scope: str, fingerprint: str) -> bool:
    """آیا رسیدی با این اثرِ انگشت قبلاً در این scope تأیید شده؟"""
    if not fingerprint:
        return False
    try:
        from config import Session
        from models import UsedReceipt
        from sqlalchemy import select
        async with Session() as s:
            row = await s.scalar(
                select(UsedReceipt).where(
                    UsedReceipt.scope == scope,
                    UsedReceipt.fingerprint == fingerprint,
                ))
            return row is not None
    except Exception:
        return False  # در صورتِ خطا، سخت‌گیری نکن (تاییدِ دستی جلوی سوءاستفاده را می‌گیرد)


async def claim_receipts(scope: str, fingerprints, amount: int | None = None,
                         user_telegram_id: int | None = None) -> bool:
    """=== PATCH_RECEIPT_CLAIM_ATOMIC_V1 ===
    «چکِ تکراری» و «ثبتِ مصرف» قبلاً دو مرحلهٔ جدا بودند (اولی قبل و دومی بعد
    از شارژ)؛ دو سفارشِ هم‌زمان با یک رسید هر دو از چک رد می‌شدند و یک رسید
    دوبار شارژ/تحویل می‌گرفت. این تابع همهٔ اثرانگشت‌ها را *قبل* از شارژ در یک
    تراکنش ثبت می‌کند؛ UniqueConstraint تضمین می‌کند فقط یکی موفق شود.
    False یعنی رسید تکراری است (یا ثبت ممکن نشد) و باید به تأییدِ دستی برود."""
    fps = [f for f in (fingerprints or []) if f]
    if not fps:
        return True
    from config import Session
    from models import UsedReceipt
    try:
        async with Session() as s:
            for f in dict.fromkeys(fps):
                s.add(UsedReceipt(scope=scope, fingerprint=f, amount=amount,
                                  user_telegram_id=user_telegram_id))
            await s.commit()
        return True
    except Exception:
        logger.info("claim_receipts: رسیدِ تکراری یا خطایِ ثبت (scope=%s)", scope)
        return False


async def release_receipts(scope: str, fingerprints) -> None:
    """آزادسازیِ اثرانگشت‌هایی که claim شدند ولی شارژ انجام نشد."""
    fps = [f for f in (fingerprints or []) if f]
    if not fps:
        return
    try:
        from config import Session
        from models import UsedReceipt
        from sqlalchemy import delete
        async with Session() as s:
            await s.execute(delete(UsedReceipt).where(
                UsedReceipt.scope == scope, UsedReceipt.fingerprint.in_(fps)))
            await s.commit()
    except Exception:
        logger.exception("release_receipts ناموفق بود (scope=%s)", scope)


async def mark_receipt_used(scope: str, fingerprint: str,
                            amount: int | None = None,
                            user_telegram_id: int | None = None) -> None:
    """اثرِ انگشتِ رسید را ثبت می‌کند تا بارِ بعد تکراری شناخته شود."""
    if not fingerprint:
        return
    try:
        from config import Session
        from models import UsedReceipt
        async with Session() as s:
            s.add(UsedReceipt(scope=scope, fingerprint=fingerprint,
                              amount=amount, user_telegram_id=user_telegram_id))
            await s.commit()
    except Exception:
        logger.exception("ثبتِ رسیدِ خودکار ناموفق بود")  # اگر رقابتی/تکراری بود، UniqueConstraint جلوش را می‌گیرد
