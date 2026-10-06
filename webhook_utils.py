"""
webhook_utils.py — کمک‌کننده‌های عمومیِ پردازشِ وبهوک که به هیچ‌کدام از
دو سرویس (بات‌ساز/پلتفرم یا بات‌های مستأجر) وابسته نیستند؛ هر دو
main_platform.py و main_tenant.py از همین‌جا استفاده می‌کنند.

=== PATCH_SPLIT_WEBHOOK_UTILS_V1 ===
این توابع قبلاً داخلِ main.py بودند. چون کاملاً عمومی‌اند (فقط bot و data
می‌گیرند، هیچ ارجاعی به platform_bot/tenant_dp/BotRow و غیره ندارند)، به
این فایلِ مستقل منتقل شدند تا کدشان بینِ دو سرویسِ جدا تکرار (کپی‌پیست)
نشود — تکرار یعنی هر باگ/بهبود باید دوجا اعمال شود و به‌مرورِ زمان از هم
واگرا می‌شوند.
"""
from __future__ import annotations

import asyncio
import datetime as _dt

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

# === PATCH_FAKE_JOIN_GUARD_V1 ===
# لیستِ صریحِ allowed_updates برایِ وبهوکِ ربات‌ِ پلتفرم. تلگرام به‌صورتِ
# پیش‌فرض (اگه این پارامتر داده نشه) آپدیت‌هایِ chat_member رو نمی‌فرسته؛
# چون fake_join_guard.py برایِ شناساییِ عضویتِ کانال به همین آپدیت نیاز
# داره، باید صریحاً درخواستش کنیم. بقیه‌ی مقادیر هم اینجان تا هیچ‌کدوم
# از قابلیت‌هایِ فعلی (پیام، دکمه، پرداختِ استارز، و...) قطع نشه.
PLATFORM_ALLOWED_UPDATES = [
    "message", "edited_message", "channel_post", "edited_channel_post",
    "callback_query", "inline_query", "chosen_inline_result",
    "shipping_query", "pre_checkout_query", "poll", "poll_answer",
    "my_chat_member", "chat_member", "chat_join_request",
]

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

_bg_tasks: set = set()


def spawn(coro):
    """اجرای یک کوروتین در پس‌زمینه (مثلاً پردازشِ آپدیت) تا پاسخِ وبهوک فوراً
    به تلگرام برگردد. این کار باعث می‌شود تلگرام منتظرِ پایان پردازش
    (کوئری‌ها/ارسال پیام) نماند؛ در نتیجه پاسخ‌دهی سریع‌تر می‌شود و زیر بار،
    آپدیت‌ها صف/تایم‌اوت نمی‌شوند.

    رفرنسِ تسک در _bg_tasks نگه داشته می‌شود چون طبقِ مستندِ خودِ asyncio،
    تسکی که هیچ‌جا رفرنس نداشته باشد ممکن است وسطِ اجرا توسطِ garbage
    collector بی‌صدا و بدونِ هیچ خطایی نابود شود.
    """
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


async def maintenance_reply(bot: Bot, data: dict, text: str | None = None,
                             reply_markup: "InlineKeyboardMarkup | None" = None) -> None:
    """برای ربات‌های موقتاً خاموش‌شده (paused) یا قفل‌شده توسط ادمین (locked):
    به‌جای پردازشِ کاملِ آپدیت، فقط یک پیامِ کوتاه به کاربر می‌دهد — روی پیامِ
    معمولی (/start یا زدنِ دکمهٔ متنی) با ارسال پیام، و روی دکمهٔ اینلاین با
    یک Alert سبک (بدون نیاز به فرستادنِ پیامِ جدید). اگر reply_markup داده
    شود (مثلاً دکمهٔ راهنمای فعال‌سازی)، چون Alertِ تلگرام دکمه پشتیبانی
    نمی‌کند، به‌جای Alert یک پیامِ معمولی همراه با دکمه فرستاده می‌شود.
    """
    text = text or "🛠 ربات در حال به‌روزرسانی می‌باشد. لطفاً چند دقیقهٔ دیگر دوباره امتحان کنید."
    try:
        if "callback_query" in data:
            cq = data["callback_query"]
            cq_id = cq.get("id")
            chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
            if reply_markup and chat_id:
                if cq_id:
                    try:
                        await bot.answer_callback_query(cq_id)
                    except Exception:
                        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
                await bot.send_message(chat_id, text, reply_markup=reply_markup)
                return
            if cq_id:
                try:
                    await bot.answer_callback_query(cq_id, text=text, show_alert=True)
                    return
                except Exception as e:
                    if _is_expected_telegram_delivery_error(e):
                        logger.info("خطای طبیعی Telegram هنگام پاسخ callback نادیده گرفته شد: %s", e)
                    else:
                        logger.exception("خطای نادیده‌گرفته‌شده در پاسخ callback")
            if chat_id:
                await bot.send_message(chat_id, text)
            return
        msg = data.get("message") or data.get("edited_message")
        if msg:
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id:
                await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        if _is_expected_telegram_delivery_error(e):
            logger.info("خطای طبیعی Telegram هنگام ارسال پیام خطا نادیده گرفته شد: %s", e)
        else:
            logger.exception("خطای نادیده‌گرفته‌شده هنگام ارسال پیام خطا")


def extract_chat_id(data: dict):
    try:
        if "callback_query" in data:
            return ((data["callback_query"].get("message") or {}).get("chat") or {}).get("id")
        msg = data.get("message") or data.get("edited_message")
        if msg:
            return (msg.get("chat") or {}).get("id")
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")
    return None


def extract_update_kind(data: dict) -> str:
    for k in ("message", "callback_query", "my_chat_member", "chat_member",
              "edited_message", "channel_post", "chat_join_request"):
        if k in data:
            return k
    return "?"


def _is_expected_telegram_delivery_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in (
        'query is too old',
        'response timeout expired',
        'query id is invalid',
        'message is not modified',
        'message to edit not found',
        'message to delete not found',
        'chat not found',
        'bot was blocked by the user',
    ))


async def notify_processing_error(bot: Bot, data: dict) -> None:
    """وقتی حین پردازشِ یک آپدیت استثنای مدیریت‌نشده‌ای رخ می‌دهد (مثلاً باگی در
    یک هندلر که پیش‌بینی نشده)، به‌جای سکوتِ کامل، یک پیامِ کوتاهِ خطا به
    فرستنده می‌دهد. این خودِ خطا را رفع نمی‌کند، ولی از سکوتِ گیج‌کننده
    جلوگیری می‌کند و امکانِ ردیابیِ خطای واقعی از لاگ سرور (که همان لحظه
    چاپ می‌شود) را برای دیباگ باقی می‌گذارد.

    یک کدِ خطای کوتاه (HHMMSS) هم داخلِ پیام گذاشته می‌شود تا وقتی کاربر
    خطا رو به پشتیبانی گزارش می‌ده، بشه دقیقاً همون لحظه رو تو لاگ سرور
    پیدا کرد.
    """
    err_code = _dt.datetime.now().strftime("%H%M%S")
    text = (f"⚠️ خطایی پیش‌بینی‌نشده رخ داد. لطفاً دوباره تلاش کن؛ "
            f"در صورت تکرار به پشتیبانی اطلاع بده و این کد رو بفرست: "
            f"<code>ERR-{err_code}</code>")
    logger.error(f"[ERR-{err_code}] update handling failed — "
                 f"see traceback above (chat_id="
                 f"{extract_chat_id(data)}, kind={extract_update_kind(data)})")
    try:
        if "callback_query" in data:
            cq = data["callback_query"]
            cq_id = cq.get("id")
            if cq_id:
                try:
                    await bot.answer_callback_query(
                        cq_id, text=f"⚠️ خطای پیش‌بینی‌نشده. کد: ERR-{err_code}",
                        show_alert=True)
                    return
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
            if chat_id:
                await bot.send_message(chat_id, text, parse_mode="HTML")
            return
        msg = data.get("message") or data.get("edited_message")
        if msg:
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id:
                await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
