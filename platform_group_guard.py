"""
platform_group_guard.py — «محافظِ گروه» برایِ گروه/کانالِ خودِ پلتفرم (بات‌ساز).

نسخهٔ اول (هسته): اتصالِ یک گروه، بن/انبن/خطا (با بنِ خودکار بعد از رسیدن
به سقفِ خطا)، و پنلِ مدیریتِ کامل (لیستِ بن‌شده‌ها و خطاکارها، به‌صورتِ
دکمه‌های اینلاینِ تک‌تک/ستونی، با صفحه‌بندی).

قابلیت‌هایی که عمداً برای فازهایِ بعدی گذاشته شدن (طبق تصمیمِ ادمین):
  • فیلترِ محتوایِ ممنوعه (کلمات/لینک/...)
  • دکمهٔ «📚 آموزش» با دستوراتِ قابل‌تنظیمِ دلخواه
  • بخشِ «🤖 هوش مصنوعی» (پرسش‌وجواب دربارهٔ مسیرهایِ فعال‌سازی در ربات‌هایِ
    مستأجر) — نیاز به تنظیمِ کلید API یک مدلِ زبانی دارد.

دستوراتِ داخلِ گروه (فقط ادمین‌های همون گروه، یا مدیرِ اصلیِ پلتفرم):
  /فعال_محافظ  یا  /setguard      — اتصالِ همین گروه (فقط مدیرِ اصلی)
  /غیرفعال_محافظ یا /unsetguard   — خاموش کردنِ محافظ برایِ همین گروه (فقط مدیرِ اصلی)
  /بن یا /ban   (با ریپلای)        — بن‌کردنِ کاربر
  /انبن یا /unban <آیدیِ عددی>    — رفعِ بن (یا با ریپلای)
  /خطا یا /warn (با ریپلای)        — ثبتِ یک خطا؛ بعد از رسیدن به سقف، بنِ خودکار

پنلِ ادمین: دکمهٔ «🛡 محافظ گروه» در پنلِ مدیریتِ اصلی (فقط مدیرِ اصلی).
"""
from __future__ import annotations

import asyncio
import datetime
import html as _html
import re
import time
from collections import deque

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatPermissions, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import PLATFORM_ADMIN_ID, Session
from models import GroupGuardBan, GroupGuardCaptcha, GroupGuardWarn
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

group_guard_router = Router()

GG_PAGE_SIZE = 8
BTN_CANCEL = "❌ لغو"


# سازگاری با کدِ platform_bot.py: کمک‌توابعی که آنجا تعریف شدن رو lazy
# import می‌کنیم تا ایمپورتِ دَوری پیش نیاد (دقیقاً همون الگویِ
# platform_community.py).
async def get_pset(key: str, default: str = "") -> str:
    from platform_bot import get_pset as _fn
    return await _fn(key, default)


async def set_pset(key: str, value: str) -> None:
    from platform_bot import set_pset as _fn
    await _fn(key, value)


def back_to_admin_kb() -> InlineKeyboardMarkup:
    from platform_bot import back_to_admin_kb as _fn
    return _fn()


async def smart_cancel_kb():
    from platform_bot import smart_cancel_kb as _fn
    return await _fn()


class GgFSM(StatesGroup):
    threshold = State()
    # === PATCH_GROUP_GUARD_PHASE2_V1 ===
    banned_words = State()
    tutorial_text = State()
    ai_question = State()
    # === PATCH_GROUP_GUARD_CAPTCHA_V1 ===
    captcha_timeout = State()
    # === PATCH_GROUP_GUARD_FLOOD_V1 ===
    flood_config = State()
    # === PATCH_GROUP_GUARD_TEMPMUTE_V1 ===
    mute_default = State()
    # === PATCH_GROUP_GUARD_WELCOME_V1 ===
    welcome_text = State()
    # === PATCH_GROUP_GUARD_NIGHT_V1 ===
    night_config = State()
    # === PATCH_GROUP_GUARD_CHANNEL_CONNECT_V1 ===
    connect_via_channel = State()
    add_extra_admin = State()
    # === PATCH_GROUP_GUARD_LOG_V1 ===
    log_channel = State()
    # === PATCH_GROUP_GUARD_PUBLIC_AI_V1 ===
    ai_trigger_word = State()
    ai_ratelimit_config = State()
    ai_custom_knowledge = State()


# ─────────────────────── دستوراتِ داخلِ گروه ───────────────────────

_CONNECT_CMDS = {"/فعال_محافظ", "/setguard"}
_DISCONNECT_CMDS = {"/غیرفعال_محافظ", "/unsetguard"}
_BAN_CMDS = {"/بن", "/ban"}
_UNBAN_CMDS = {"/انبن", "/unban"}
_WARN_CMDS = {"/خطا", "/اخطار", "/warn"}  # === PATCH_WARN_SLASH_ALIAS_FIX_V1 ===
# باگ: کلمهٔ فارسیِ درستِ «هشدار» یعنی «اخطار» است، ولی قبلاً فقط شکلِ
# بدونِ‌اسلشِ آن («اخطار») و اسلش‌دارِ «خطا» به‌عنوانِ دستور شناخته می‌شدند —
# «/اخطار» (که خودِ ادمین طبیعتاً همین را می‌زند) اصلاً در این مجموعه
# نبود، پس بی‌سروصدا هیچ کاری نمی‌کرد.
# === PATCH_GROUP_GUARD_TEMPMUTE_V1 ===
_MUTE_CMDS = {"/سکوت", "/mute"}
_UNMUTE_CMDS = {"/رفع‌سکوت", "/رفع_سکوت", "/unmute"}
# === PATCH_GROUP_GUARD_BARE_WORDS_V1 === صفرکردنِ خطاهایِ یک نفر از داخلِ گروه
_UNWARN_CMDS = {"/پاک_اخطار", "/پاک‌اخطار", "/unwarn"}


# === PATCH_GROUP_GUARD_BARE_WORDS_V1 ===
# باگ: ادمین رویِ پیامِ کاربر ریپلای می‌کرد و می‌نوشت «بن» یا «اخطار» و هیچ
# اتفاقی نمی‌افتاد، چون _cmd_token فقط متن‌هایی را دستور می‌شناخت که با «/»
# شروع شوند. ضمناً «اخطار» اصلاً جزوِ کلماتِ دستور نبود (فقط /خطا) و
# «پاک‌کردنِ اخطار» هم در گروه اصلاً وجود نداشت و فقط از پنلِ ادمین ممکن بود.
#
# حالا همان کلمات بدونِ اسلش هم کار می‌کنند — ولی فقط وقتی هر سه شرط برقرار
# باشد: گروهِ متصل و محافظِ روشن، فرستنده ادمین، و ریپلای رویِ پیامِ هدف.
# پس گفت‌وگویِ عادیِ اعضا («بن شدی؟») هیچ‌وقت دستور تلقی نمی‌شود.
_BARE_WORD_CMDS: dict[str, str] = {
    "بن": "/بن", "مسدود": "/بن", "ban": "/بن",
    "انبن": "/انبن", "آنبن": "/انبن", "رفع بن": "/انبن", "رفع‌بن": "/انبن",
    "unban": "/انبن",
    "اخطار": "/خطا", "خطا": "/خطا", "warn": "/خطا",
    "پاک اخطار": "/پاک_اخطار", "پاک کردن اخطار": "/پاک_اخطار",
    "حذف اخطار": "/پاک_اخطار", "رفع اخطار": "/پاک_اخطار",
    "پاک‌کردن اخطار": "/پاک_اخطار", "صفر کردن اخطار": "/پاک_اخطار",
    "unwarn": "/پاک_اخطار",
    "سکوت": "/سکوت", "میوت": "/سکوت", "mute": "/سکوت",
    "رفع سکوت": "/رفع_سکوت", "رفع‌سکوت": "/رفع_سکوت", "آنمیوت": "/رفع_سکوت",
    "unmute": "/رفع_سکوت",
}


def _bare_word_cmd(text: str) -> str | None:
    """کلمهٔ فارسیِ بدونِ اسلش را به دستورِ معادلش تبدیل می‌کند.

    عمداً **تطبیقِ کاملِ کلِ پیام** لازم است، نه فقط کلمهٔ اول: جمله‌ای مثل
    «بن شدی؟» یا «اخطار گرفتی قبلاً» نباید کسی را بن کند. برایِ نوشتنِ دلیل
    یا مدت، شکلِ اسلش‌دار هست: «/سکوت ۳۰ توهین».
    """
    t = " ".join((text or "").split()).strip()
    if not t or t.startswith("/"):
        return None
    return _BARE_WORD_CMDS.get(t.lower())


def _cmd_token(text: str) -> str | None:
    if not text or not text.startswith("/"):
        return None
    first = text.split()[0].split("@")[0]
    return first.lower()


def _is_spam_bot_message(msg: Message) -> bool:
    """PATCH_GROUP_GUARD_SENDER_CHAT_ISBOT_V1 — باگِ ریشه‌ای که باعث می‌شد
    پیام‌هایی که «به‌نمایندگیِ کانالِ لینک‌شده» فرستاده می‌شدن، همه‌جایِ این
    فایل بی‌سروصدا نادیده گرفته بشن: تلگرام برایِ چنین پیام‌هایی، msg.from_user
    رو یه اکانتِ سیستمی (آیدیِ ۷۷۷۰۰۰، «Channel_Bot») می‌ذاره که خودش
    from_user.is_bot=True داره! یعنی همون چکِ معمولیِ «نادیده‌گرفتنِ پیامِ
    بات‌ها» (که برایِ فیلترکردنِ اسپمِ بات‌هایِ دیگه لازمه) اشتباهی همینِ
    پیام‌هایِ کاملاً معتبرِ کانال رو هم قربانی می‌کرد — دقیقاً چیزی که باعث
    شد «/بن» و «بات ساز» و همه‌چیزِ دیگه، وقتی از طریقِ هویتِ کانال تایپ
    می‌شد، کاملاً بی‌صدا هیچ واکنشی نشون ندن. راه‌حل: اگه msg.sender_chat ست
    باشه (یعنی واقعاً یه هویتِ کانال/ادمینِ ناشناسه، نه اسپمِ یه بات)،
    هیچ‌وقت به‌عنوانِ «پیامِ بات» رد نمی‌شه، فارغ از مقدارِ is_bot."""
    if getattr(msg, "sender_chat", None):
        return False
    return not msg.from_user or msg.from_user.is_bot


async def _is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if user_id == PLATFORM_ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


# === PATCH_LINKED_CHANNEL_ADMIN_V1 ===
async def _is_authorized_sender_chat(msg: Message, bot: Bot) -> bool:
    """توی گروهی که به یه کانال «لینک» شده، وقتی ادمینِ کانال از طریقِ خودِ
    کانال (یا با گزینهٔ «ارسال به‌نمایندگیِ کانال») توی گروه پیام می‌ذاره،
    msg.from_user دیگه شخصِ واقعی رو نشون نمی‌ده (یه اکانتِ سیستمیِ تلگرامه،
    Channel Bot) — ولی msg.sender_chat هویتِ واقعیِ فرستنده (خودِ کانال) رو
    داره. اگه این sender_chat دقیقاً همون کانالِ لینک‌شده به همین گروه باشه،
    یعنی فرستنده حتماً ادمینِ اون کاناله (چون تلگرام فقط به ادمین‌هایِ کانال
    اجازه می‌ده به‌نمایندگیِ کانال پست بذارن) — پس مجازش می‌شناسیم.

    === PATCH_GROUP_GUARD_ANON_ADMIN_V1 ===
    باگ: حالتِ «ارسالِ ناشناس»یِ خودِ گروه (Send Anonymously — دکمه‌ای که
    گوشهٔ پایینِ باکسِ تایپ در تلگرام هست و خیلی از ادمین‌ها پیش‌فرض روشنش
    می‌کنند) اصلاً چک نمی‌شد. در آن حالت sender_chat برابرِ خودِ گروه است،
    نه یک کانالِ لینک‌شده؛ پس شرطِ قبلی (فقط linked_id) رد می‌شد و ادمینی که
    ناشناس پیام می‌داد، دستورهایش (بن/اخطار/سکوت) بی‌صدا نادیده گرفته
    می‌شد — دقیقاً چیزی که کاربر گزارش داد. تلگرام فقط به ادمین‌هایی که
    اجازهٔ «remain anonymous» دارند امکانِ پست‌کردن با sender_chat=خودِ گروه
    را می‌دهد، پس این یک نشانهٔ کاملاً معتبر برایِ ادمین‌بودن است.
    """
    sc = getattr(msg, "sender_chat", None)
    if not sc:
        return False
    if sc.id == msg.chat.id:
        return True  # ارسالِ ناشناسِ خودِ گروه — فقط ادمین‌ها می‌توانند
    try:
        chat = await bot.get_chat(msg.chat.id)
        linked_id = getattr(chat, "linked_chat_id", None)
    except Exception:
        return False
    return bool(linked_id) and sc.id == linked_id


async def _is_extra_admin(user_id: int) -> bool:
    """PATCH_GROUP_GUARD_CHANNEL_CONNECT_V1 — لیستِ ادمین‌هایِ اضافه‌ای که
    مستقیم از پنلِ ادمین (نه از لیستِ ادمین‌هایِ واقعیِ گروه) اضافه شدن —
    برایِ وقتی که تشخیصِ ادمینِ گروه به هر دلیلی (تنظیماتِ حریمِ خصوصی،
    ادمینِ ناشناس و...) قابل‌اعتماد نیست."""
    raw = await get_pset("group_guard_extra_admins", "")
    ids = {int(x) for x in raw.split(",") if x.strip().isdigit()}
    return user_id in ids


async def _is_authorized(msg: Message, bot: Bot) -> bool:
    """ترکیبِ همهٔ راه‌هایِ مجازشناسی: خودِ کاربر (مدیرِ اصلی/ادمینِ گروه)،
    پیامی که به‌نمایندگیِ کانالِ لینک‌شده به همین گروه فرستاده شده، یا
    ادمینِ اضافه‌ایِ ثبت‌شده از پنل."""
    if msg.from_user and await _is_group_admin(bot, msg.chat.id, msg.from_user.id):
        return True
    if msg.from_user and await _is_extra_admin(msg.from_user.id):
        return True
    return await _is_authorized_sender_chat(msg, bot)


# === PATCH_GROUP_GUARD_FLOOD_V1 ===
# ردیابیِ فلود در حافظه (مثلِ سیستمِ آنتی‌اسپمِ خودِ پلتفرم که قبلاً بود، ولی
# اینجا سطحِ «چند پیام در گروه»، نه سطحِ فرمان‌های بات). کلید: (chat_id, user_id).
_flood_times: dict[tuple[int, int], deque] = {}
_flood_muted_until: dict[tuple[int, int], float] = {}


async def _check_flood(msg: Message, bot: Bot) -> bool:
    """اگه فلود تشخیص داده شد (و اقدام لازم انجام شد یا کاربر همچنان تویِ
    میوتِ قبلیه)، True برمی‌گردونه — یعنی پردازشِ بعدیِ پیام (فیلترِ محتوا
    و...) لازم نیست ادامه پیدا کنه."""
    if (await get_pset("group_guard_flood_enabled", "off")) != "on":
        return False
    if _is_spam_bot_message(msg):
        return False
    if await _is_authorized(msg, bot):
        return False
    key = (msg.chat.id, msg.from_user.id)
    now = time.monotonic()
    if _flood_muted_until.get(key, 0) > now:
        return True  # همین الان تویِ میوتِ فلوده؛ دیگه پردازش نکن
    max_msgs = int(await get_pset("group_guard_flood_max_msgs", "5") or "5")
    window = int(await get_pset("group_guard_flood_window_sec", "10") or "10")
    mute_sec = int(await get_pset("group_guard_flood_mute_sec", "300") or "300")
    dq = _flood_times.setdefault(key, deque())
    dq.append(now)
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) < max_msgs:
        return False
    dq.clear()
    try:
        until_dt = datetime.datetime.now() + datetime.timedelta(seconds=mute_sec)
        await bot.restrict_chat_member(
            msg.chat.id, msg.from_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_dt)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return False
    _flood_muted_until[key] = now + mute_sec
    mins = max(1, mute_sec // 60)
    try:
        await msg.answer(
            f"🔇 {msg.from_user.full_name} به‌خاطرِ ارسالِ پیامِ زیاد در مدتِ کوتاه، "
            f"{mins} دقیقه سکوت شد.")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await _log_action(
        f"🔇 <b>سکوتِ خودکار (فلود)</b> ({mins} دقیقه)\n"
        f"👤 {_html.escape(msg.from_user.full_name)} (<code>{msg.from_user.id}</code>)")
    return True


# === PATCH_GROUP_GUARD_CONTENT_RESTRICT_V1 ===

# === PATCH_GROUP_GUARD_ONLY_PERSONAL_V1 ===
# درخواستِ مدیر: بشود جلویِ چت‌کردن «به‌نمایندگیِ کانال/گروه» یا «با ربات»
# را گرفت و فقط اکانتِ شخصیِ خودِ آدم اجازهٔ پیام داشته باشد.
#
# تلگرام دو حالتِ غیرشخصی دارد و هر دو این‌جا پوشش داده می‌شوند:
#   • ارسال به‌نمایندگیِ کانال/گروه → msg.sender_chat پر است
#     (در این حالت from_user یک اکانتِ سیستمیِ ۷۷۷۰۰۰ است، نه شخصِ واقعی)
#   • پیامِ ربات‌هایِ دیگر → from_user.is_bot
#
# ادمین‌ها عمداً مستثنا هستند، وگرنه خودِ مدیر که ناشناس پیام می‌دهد هم
# پاک می‌شد و عملاً از گروهِ خودش قفل می‌شد.
async def _check_identity_restriction(msg: Message, bot: Bot) -> bool:
    """True یعنی پیام حذف شد و پردازشِ بعدی لازم نیست."""
    if (await get_pset("group_guard_only_personal", "off")) != "on":
        return False
    sender_chat = getattr(msg, "sender_chat", None)
    is_bot_msg = bool(msg.from_user and msg.from_user.is_bot)
    if not sender_chat and not is_bot_msg:
        return False                      # اکانتِ شخصی — مجاز
    if await _is_authorized(msg, bot):
        return False                      # ادمین‌ها مستثنا
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        logger.exception("حذفِ پیامِ غیرشخصی ناموفق بود")
    if sender_chat:
        who = getattr(sender_chat, "title", None) or "کانال/گروه"
        why = f"«{who}» به‌نمایندگیِ کانال/گروه پیام داد"
    else:
        who = msg.from_user.full_name if msg.from_user else "ربات"
        why = f"«{who}» یک ربات است"
    try:
        await msg.answer(
            f"👤 در این گروه فقط با <b>اکانتِ شخصی</b> می‌شود پیام داد.\n"
            f"پیام حذف شد چون {why}.\n\n"
            "<i>اگر با هویتِ کانال می‌نویسی، از گوشهٔ باکسِ تایپ به اکانتِ "
            "خودت برگرد.</i>")
    except Exception:
        logger.exception("اطلاع‌رسانیِ محدودیتِ هویت ناموفق بود")
    return True


async def _check_content_restrictions(msg: Message, bot: Bot) -> bool:
    """اگه محدودیتِ نوعِ محتوا فعال بود و پیامِ یک عضوِ عادی مطابقتش داشت،
    حذف می‌شه + یک خطا ثبت می‌شه. خروجی True یعنی پردازشِ بعدی لازم نیست."""
    if _is_spam_bot_message(msg):
        return False
    if await _is_authorized(msg, bot):
        return False  # محدودیت‌ها شاملِ ادمین‌ها نمی‌شه
    reasons: list[str] = []
    if (await get_pset("group_guard_restrict_forward", "off")) == "on" and getattr(msg, "forward_origin", None):
        reasons.append("فوروارد کردنِ پیام")
    if (await get_pset("group_guard_restrict_sticker", "off")) == "on" and msg.sticker:
        reasons.append("ارسالِ استیکر")
    if (await get_pset("group_guard_restrict_animation", "off")) == "on" and msg.animation:
        reasons.append("ارسالِ گیف/انیمیشن")
    if (await get_pset("group_guard_restrict_link", "off")) == "on":
        # === PATCH_GROUP_GUARD_MENTION_AS_LINK_V1 ===
        # قبلاً فقط لینک‌هایِ واقعی (url/text_link) گرفته می‌شد؛ منشن‌هایِ
        # @username (که خودشون هم قابل‌کلیک و ناوبری‌پذیرن، دقیقاً مثلِ یه
        # لینک) رد می‌شدن — یعنی یکی می‌تونست به‌جایِ فرستادنِ لینکِ کامل،
        # فقط بنویسه «@channel_name» و از فیلتر رد بشه. الان mention/
        # text_mention هم جزوِ «لینک» حساب می‌شن.
        entities = list(msg.entities or []) + list(msg.caption_entities or [])
        if any(e.type in ("url", "text_link", "mention", "text_mention") for e in entities):
            reasons.append("ارسالِ لینک/منشن")
    if not reasons:
        return False
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    reason_txt = "، ".join(reasons)
    cnt, threshold, auto_banned = await _apply_warn(bot, msg.chat.id, msg.from_user, reason_txt, warned_by=0)
    note = f"⚠️ پیامِ {msg.from_user.full_name} به‌خاطرِ «{reason_txt}» حذف شد ({cnt}/{threshold})."
    if auto_banned:
        note += f"\n🚫 چون به سقفِ {threshold} خطا رسید، به‌صورتِ خودکار بن شد."
    try:
        await msg.answer(note)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return True


# === PATCH_GROUP_GUARD_ANTIPROMO_V1 ===
# آنتی‌فلودِ لینکِ تبلیغاتی — بر خلافِ سوییچِ کلیِ «محدودیتِ لینک» در
# «محدودیتِ نوعِ محتوا» (که هر نوع لینکی رو می‌گیره)، این یکی مشخصاً فقط
# لینک‌هایِ دعوت/تبلیغِ کانال یا گروهِ *دیگر* رو تشخیص می‌ده — یعنی می‌تونی
# اجازهٔ لینک‌هایِ عادی (مقاله، محصول و...) رو بدی ولی تبلیغِ کانال‌هایِ
# دیگه رو نه. تشخیص خودکاره و نیازی به کلمهٔ خاص تو لیستِ محتوایِ ممنوعه نداره.
_TME_INVITE_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)", re.IGNORECASE)
_TME_LINK_RE = re.compile(r"(?:https?://)?t\.me/([A-Za-z][\w]{3,31})", re.IGNORECASE)


async def _check_promo_links(msg: Message, bot: Bot) -> bool:
    if (await get_pset("group_guard_antipromo_enabled", "off")) != "on":
        return False
    if _is_spam_bot_message(msg):
        return False
    if await _is_authorized(msg, bot):
        return False
    text = f"{msg.text or ''} {msg.caption or ''}".strip()
    if not text:
        return False
    matched = bool(_TME_INVITE_RE.search(text))
    own_username = (msg.chat.username or "").lower()
    if not matched:
        for m in _TME_LINK_RE.finditer(text):
            uname = m.group(1).lower()
            if uname != own_username:
                matched = True
                break
    # === PATCH_GROUP_GUARD_MENTION_AS_LINK_V1 ===
    # قبلاً فقط لینکِ کاملِ t.me/... رو تشخیص می‌داد؛ رایج‌ترین روشِ واقعیِ
    # تبلیغِ کانال («فقط بنویس @channel_name»، بدونِ لینکِ کامل) رو اصلاً
    # نمی‌گرفت. حالا منشن‌هایِ @username (از رویِ entityهایِ واقعیِ تلگرام،
    # نه یه رجکسِ دستی) رو هم چک می‌کنه — همون‌طور که برایِ خودِ گروه
    # استثنا قائل می‌شه، برایِ منشن‌هایی که فقط اسمِ ادمین/مالکِ گروه رو تو
    # پیام‌شون گذاشتن استثنا نمی‌ذاریم چون تشخیصش قابل‌اعتماد نیست؛ فقط
    # منشنِ خودِ گروه (اگه یوزرنیم داشته باشه) رو رد می‌کنیم.
    if not matched:
        entities = list(msg.entities or []) + list(msg.caption_entities or [])
        for e in entities:
            if e.type != "mention":
                continue
            uname = text[e.offset: e.offset + e.length].lstrip("@").lower()
            if uname and uname != own_username:
                matched = True
                break
    if not matched:
        return False
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    cnt, threshold, auto_banned = await _apply_warn(
        bot, msg.chat.id, msg.from_user, "ارسالِ لینکِ تبلیغاتیِ کانال/گروهِ دیگر", warned_by=0)
    note = f"⚠️ پیامِ {msg.from_user.full_name} به‌خاطرِ لینکِ تبلیغاتی حذف شد ({cnt}/{threshold})."
    if auto_banned:
        note += f"\n🚫 چون به سقفِ {threshold} خطا رسید، به‌صورتِ خودکار بن شد."
    try:
        await msg.answer(note)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return True


# === PATCH_GROUP_GUARD_LOG_V1 ===
# لاگِ کاملِ اقدامات در یک کانالِ جدا — دقیقاً اختیاری؛ اگه چیزی وصل نشده
# باشه، این تابع بی‌سروصدا هیچ‌کاری نمی‌کنه (و خطاهاش هیچ‌وقت نباید جلویِ
# اقدامِ اصلی رو بگیره، برایِ همین همه‌جا داخلِ try/except صدا زده می‌شه).

async def _log_action(text: str) -> None:
    log_chat_id = await get_pset("group_guard_log_chat_id", "")
    if not log_chat_id:
        return
    try:
        from main_platform import platform_bot as bot
        if not bot:
            return
        await bot.send_message(int(log_chat_id), text)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


_TME_MSG_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/(c/(?P<cid>\d+)|(?P<uname>[A-Za-z][\w]{3,31}))(?:/\d+)?/?$")


@group_guard_router.callback_query(F.data == "gg:log_setup")
async def gg_log_setup_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(GgFSM.log_channel)
    await cb.message.answer(
        "📝 یه پیام از کانالِ لاگِ موردنظر برایم فوروارد کن، یا لینکش رو بفرست "
        "(مثلاً <code>https://t.me/mychannel</code>).\n\n"
        "⚠️ ربات باید تویِ اون کانال ادمین باشه.",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.log_channel)
async def gg_log_setup_recv(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    if (msg.text or "").strip() == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return

    target_chat_id: int | str | None = None
    fwd_origin = getattr(msg, "forward_origin", None)
    if fwd_origin is not None and getattr(fwd_origin, "type", None) == "channel":
        target_chat_id = fwd_origin.chat.id
    elif msg.forward_from_chat and msg.forward_from_chat.type == "channel":
        target_chat_id = msg.forward_from_chat.id
    else:
        raw = (msg.text or "").strip()
        m = _TME_MSG_LINK_RE.search(raw)
        if m:
            if m.group("cid"):
                target_chat_id = int(f"-100{m.group('cid')}")
            elif m.group("uname"):
                target_chat_id = f"@{m.group('uname')}"

    if target_chat_id is None:
        await msg.answer(
            "❌ این نه فورواردِ یک پستِ کانال بود، نه لینکِ معتبر. یکی از پستِ کانال "
            "برایم فوروارد کن یا لینکِ کانال رو بفرست، یا «❌ لغو» را بزن.")
        return
    try:
        chat = await bot.get_chat(target_chat_id)
    except Exception:
        await msg.answer("❌ به این کانال دسترسی ندارم. مطمئن شو ربات اونجا ادمینه.")
        return
    await set_pset("group_guard_log_chat_id", str(chat.id))
    await set_pset("group_guard_log_chat_title", chat.title or "")
    await state.clear()
    await msg.answer(f"✅ کانالِ لاگ وصل شد: <b>{chat.title or chat.id}</b>")
    await _log_action("📝 <b>کانالِ لاگِ محافظ گروه وصل شد.</b>\nاز این به بعد همهٔ اقدامات اینجا ثبت می‌شن.")


# === PATCH_GROUP_GUARD_CHANNEL_CONNECT_V1 ===
# اتصالِ گروه از طریقِ کانالِ لینک‌شده — کاملاً از تویِ دی‌امِ ادمین، بدونِ
# نیاز به تایپِ هیچ دستوری تویِ خودِ گروه. چون فورواردِ یک پستِ *کانال*
# (برخلافِ فورواردِ یک پیامِ گروهِ معمولی) قابل‌اعتماد به بات می‌گه پیام از
# کدوم کانال اومده، و کانال‌هایی که یک «گروهِ گفتگو» (Discussion Group)
# دارن، آیدیِ اون گروه رو تویِ linked_chat_id نگه می‌دارن — با فرستادنِ یک
# پستِ کانال، می‌شه مستقیم گروهِ لینک‌شده رو پیدا و وصل کرد.

@group_guard_router.callback_query(F.data == "gg:connect_via_channel")
async def gg_connect_via_channel_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(GgFSM.connect_via_channel)
    await cb.message.answer(
        "🔗 <b>اتصالِ گروه از طریقِ کانال</b>\n\n"
        "یه پیام از کانالت (همونی که این گروه، «گروهِ گفتگو»/Discussion Group "
        "شماست) برایم فوروارد کن، یا لینکِ کانال رو بفرست.\n\n"
        "⚠️ لازمه:\n"
        "۱. ربات تویِ خودِ کانال هم ادمین باشه\n"
        "۲. ربات تویِ گروهِ گفتگو هم ادمین باشه (با دسترسیِ «محدودسازیِ اعضا»)\n"
        "۳. این کانال از تنظیماتِ خودِ تلگرام به یه گروهِ گفتگو لینک شده باشه",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.connect_via_channel)
async def gg_connect_via_channel_recv(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    if (msg.text or "").strip() == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return

    target_chat_id: int | str | None = None
    fwd_origin = getattr(msg, "forward_origin", None)
    if fwd_origin is not None and getattr(fwd_origin, "type", None) == "channel":
        target_chat_id = fwd_origin.chat.id
    elif msg.forward_from_chat and msg.forward_from_chat.type == "channel":
        target_chat_id = msg.forward_from_chat.id
    else:
        raw = (msg.text or "").strip()
        m = _TME_MSG_LINK_RE.search(raw)
        if m:
            if m.group("cid"):
                target_chat_id = int(f"-100{m.group('cid')}")
            elif m.group("uname"):
                target_chat_id = f"@{m.group('uname')}"

    if target_chat_id is None:
        await msg.answer(
            "❌ این نه فورواردِ یک پستِ کانال بود، نه لینکِ معتبر. یکی از پستِ کانال "
            "برایم فوروارد کن یا لینکِ کانال رو بفرست، یا «❌ لغو» را بزن.")
        return
    try:
        channel = await bot.get_chat(target_chat_id)
    except Exception:
        await msg.answer("❌ به این کانال دسترسی ندارم. مطمئن شو ربات اونجا ادمینه.")
        return
    linked_id = getattr(channel, "linked_chat_id", None)
    if not linked_id:
        await msg.answer(
            "❌ این کانال به هیچ «گروهِ گفتگو»یی لینک نشده (یا ربات نمی‌بینتش).\n"
            "از تنظیماتِ کانال → Discussion → یه گروه بهش وصل کن، بعد دوباره امتحان کن."
        )
        return
    try:
        group = await bot.get_chat(linked_id)
    except Exception:
        await msg.answer(
            "❌ گروهِ گفتگویِ این کانال رو پیدا کردم ولی ربات بهش دسترسی نداره. "
            "مطمئن شو ربات تویِ اون گروه هم ادمینه.")
        return
    await set_pset("group_guard_chat_id", str(group.id))
    await set_pset("group_guard_chat_title", group.title or "")
    await set_pset("group_guard_enabled", "on")
    await state.clear()
    await msg.answer(
        f"✅ وصل شد! گروهِ گفتگویِ کانالِ «{channel.title or channel.id}» — یعنی "
        f"«{group.title or group.id}» — به‌عنوانِ گروهِ محافظت‌شده تنظیم و فعال شد.\n\n"
        "دیگه نیازی به تایپِ چیزی تویِ خودِ گروه نبود؛ از همین‌جا مدیریتش کن.")


# === PATCH_GROUP_GUARD_CHANNEL_CONNECT_V1 ===
# ادمینِ اضافه — برایِ وقتی که تشخیصِ خودکارِ ادمین (چه از طریقِ عضویتِ
# واقعی، چه از طریقِ هویتِ کانال) به هر دلیلی جواب نمی‌ده.

@group_guard_router.callback_query(F.data == "gg:extra_admins")
async def gg_extra_admins_menu(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    raw = await get_pset("group_guard_extra_admins", "")
    ids = [x for x in raw.split(",") if x.strip().isdigit()]
    text = (
        "👤 <b>ادمین‌هایِ اضافه</b>\n\n"
        "این آیدی‌ها، صرفِ‌نظر از اینکه تلگرام آن‌ها را ادمینِ واقعیِ گروه "
        "بشناسد یا نه، برایِ دستوراتِ محافظِ گروه (بن/خطا/سکوت و اتصال/قطع) "
        "مجاز شناخته می‌شوند.\n\n"
        + ("لیستِ فعلی:\n" + "\n".join(f"• <code>{i}</code>" for i in ids) if ids
           else "هنوز کسی اضافه نشده.")
    )
    kb_rows = [[hx_button("➕ افزودنِ آیدی", callback_data="gg:extra_admin_add")]]
    for i in ids:
        kb_rows.append([hx_button(f"🗑 حذفِ {i}", callback_data=f"gg:extra_admin_del:{i}")])
    kb_rows.append([hx_button("⬅️ بازگشت", callback_data="adm:group_guard", style=ButtonStyle.DANGER)])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data == "gg:extra_admin_add")
async def gg_extra_admin_add_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(GgFSM.add_extra_admin)
    await cb.message.answer(
        "🆔 آیدیِ عددیِ تلگرامِ فرد رو بفرست (یا یه پیام ازش برام فوروارد کن).",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.add_extra_admin)
async def gg_extra_admin_add_recv(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    if (msg.text or "").strip() == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    new_id = None
    fwd_origin = getattr(msg, "forward_origin", None)
    if fwd_origin is not None and getattr(fwd_origin, "sender_user", None):
        new_id = fwd_origin.sender_user.id
    elif msg.forward_from:
        new_id = msg.forward_from.id
    else:
        txt = (msg.text or "").strip()
        if txt.isdigit():
            new_id = int(txt)
    if not new_id:
        await msg.answer("❌ آیدیِ عددیِ معتبر بفرست، یا یه پیام از همون فرد فوروارد کن.")
        return
    raw = await get_pset("group_guard_extra_admins", "")
    ids = {x for x in raw.split(",") if x.strip().isdigit()}
    ids.add(str(new_id))
    await set_pset("group_guard_extra_admins", ",".join(sorted(ids)))
    await state.clear()
    await msg.answer(f"✅ آیدیِ <code>{new_id}</code> به ادمین‌هایِ اضافه اضافه شد.")


@group_guard_router.callback_query(F.data.startswith("gg:extra_admin_del:"))
async def gg_extra_admin_del(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    target = cb.data.split(":")[2]
    raw = await get_pset("group_guard_extra_admins", "")
    ids = {x for x in raw.split(",") if x.strip().isdigit()}
    ids.discard(target)
    await set_pset("group_guard_extra_admins", ",".join(sorted(ids)))
    await cb.answer("حذف شد.")
    await gg_extra_admins_menu(cb)


@group_guard_router.message(F.chat.type.in_({"group", "supergroup"}))
async def group_guard_command_router(msg: Message, bot: Bot):
    # === PATCH_GROUP_GUARD_CONTENT_RESTRICT_V1 ===
    # قبلاً این هندلر فقط F.text می‌گرفت. برایِ محدودیتِ نوعِ محتوا (استیکر/
    # گیف/فوروارد) باید پیام‌هایِ بدونِ متن رو هم ببینه — پس فیلتر باز شد.
    # ولی پیام‌هایِ سرویسی (عضوِ جدید/رفتنِ عضو) نباید اینجا مصرف بشن، چون
    # هندلرِ کپچا (gg_new_members) با F.new_chat_members جداگانه باید
    # اجراشون کنه؛ SkipHandler یعنی «من این آپدیت رو نمی‌خوام، برو سراغِ
    # هندلرِ بعدی» — بدونش، چون این هندلر زودتر ثبت شده، کپچا هرگز اجرا نمی‌شد.
    if msg.new_chat_members or msg.left_chat_member:
        raise SkipHandler
    cmd = _cmd_token(msg.text or "")
    if cmd in _CONNECT_CMDS:
        await _cmd_connect(msg, bot)
        return
    if cmd in _DISCONNECT_CMDS:
        await _cmd_disconnect(msg, bot)
        return

    # === PATCH_DICE_GAME_V1 ===
    # این هندلر یک catch-all برایِ *همهٔ* پیام‌هایِ گروه است و روترش زودتر از
    # بقیه ثبت می‌شود، پس هیچ هندلرِ دیگری در گروه به پیام نمی‌رسد — دلیلِ
    # اینکه «بات ساز» (هوش مصنوعی، که همین‌جا هندل می‌شود) در گروه جواب می‌داد
    # ولی نوشتنِ نامِ بازی هیچ واکنشی نداشت. پس بازی هم باید از همین‌جا صدا
    # زده شود، دقیقاً مثلِ هوش مصنوعی.
    if cmd == "/dice":
        from platform_dice import pdg_bind_in_group
        await pdg_bind_in_group(msg, bot)
        return
    if cmd is None and (msg.text or "").strip():
        try:
            from platform_dice import try_play_by_name as _dice_try
            if await _dice_try(msg, bot):
                return
        except Exception as e:
            print(f"[dice_group_trigger] {e}")

    guard_chat_id = await get_pset("group_guard_chat_id", "")
    is_guarded_chat = bool(guard_chat_id) and str(msg.chat.id) == guard_chat_id
    guard_on = (await get_pset("group_guard_enabled", "off")) == "on"

    # === PATCH_GROUP_GUARD_BARE_WORDS_V1 ===
    # «بن» / «اخطار» / «پاک کردن اخطار» و … بدونِ اسلش. عمداً سخت‌گیرانه:
    # فقط در گروهِ متصلِ روشن، فقط از ادمین، و فقط وقتی ریپلای شده باشد.
    if cmd is None and is_guarded_chat and guard_on:
        bare = _bare_word_cmd(msg.text or "")
        if bare and await _is_authorized(msg, bot):
            if msg.reply_to_message:
                cmd = bare
            else:
                await msg.reply(
                    "برایِ این کار رویِ پیامِ همون شخص ریپلای کن و دوباره "
                    f"«{(msg.text or '').strip().split()[0]}» رو بفرست.")
                return

    if cmd is not None:
        if cmd not in (_BAN_CMDS | _UNBAN_CMDS | _WARN_CMDS | _MUTE_CMDS
                       | _UNMUTE_CMDS | _UNWARN_CMDS):
            return
        if not (is_guarded_chat and guard_on):
            return  # این گروه محافظت‌شده/روشن نیست — بی‌سروصدا نادیده بگیر
        if not await _is_authorized(msg, bot):
            return
        if cmd in _UNWARN_CMDS:
            await _cmd_unwarn(msg, bot)
        elif cmd in _BAN_CMDS:
            await _cmd_ban(msg, bot)
        elif cmd in _UNBAN_CMDS:
            await _cmd_unban(msg, bot)
        elif cmd in _WARN_CMDS:
            await _cmd_warn(msg, bot)
        elif cmd in _MUTE_CMDS:
            await _cmd_mute(msg, bot)
        elif cmd in _UNMUTE_CMDS:
            await _cmd_unmute(msg, bot)
        return

    # === PATCH_GROUP_GUARD_PUBLIC_AI_V1 ===
    # پاسخ‌گوییِ عمومی عمداً مستقل از سوییچِ کلیِ «محافظ» (guard_on) کار
    # می‌کنه — فقط نیاز به گروهِ متصل داره، نه اینکه خودِ حالتِ محافظت هم
    # روشن باشه. قبلاً همین‌جا بودنش زیرِ «if not (is_guarded_chat and
    # guard_on): return» باعث می‌شد اگه ادمین سوییچِ اصلیِ محافظ رو خاموش
    # کرده باشه (که ربطی به آزمایشِ هوش مصنوعی نداره)، پاسخ‌گویی هم کلاً
    # بی‌صدا کار نکنه.
    if is_guarded_chat and (msg.from_user or getattr(msg, 'sender_chat', None)) and not _is_spam_bot_message(msg):
        if await _check_ai_trigger(msg, bot):
            return

    # === PATCH_GROUP_GUARD_PHASE2_V1 — فیلترِ محتوایِ ممنوعه ===
    # اینجا فقط برایِ پیام‌هایِ غیردستوری اجرا می‌شه (چون بالاتر با return
    # از دستورات جدا شد) — تا با ثبتِ چندباره یا تداخل با هندلرِ دستورات
    # مواجه نشیم (هردو رویِ یک هندلرِ واحدن، چون دیسپچرِ aiogram فقط اولین
    # هندلرِ منطبق رو صدا می‌زنه).
    if not (is_guarded_chat and guard_on):
        return
    # === PATCH_GROUP_GUARD_ONLY_PERSONAL_V1 ===
    # عمداً *قبل* از _is_spam_bot_message: همان پیام‌هایی که آن تابع
    # کنار می‌گذارد (کانال/ربات) دقیقاً هدفِ این محدودیت‌اند.
    if await _check_identity_restriction(msg, bot):
        return
    if _is_spam_bot_message(msg):
        return
    # === PATCH_GROUP_GUARD_FLOOD_V1 ===
    if await _check_flood(msg, bot):
        return
    # === PATCH_GROUP_GUARD_CONTENT_RESTRICT_V1 ===
    if await _check_content_restrictions(msg, bot):
        return
    # === PATCH_GROUP_GUARD_ANTIPROMO_V1 ===
    if await _check_promo_links(msg, bot):
        return
    if (await get_pset("group_guard_filter_enabled", "off")) != "on":
        return
    if await _is_authorized(msg, bot):
        return  # فیلتر رویِ ادمین‌ها اعمال نمی‌شه
    words_raw = await get_pset("group_guard_banned_words", "")
    words = [w.strip() for w in words_raw.splitlines() if w.strip()]
    if not words:
        return
    text_l = (msg.text or "").lower()
    matched = next((w for w in words if w.lower() in text_l), None)
    if not matched:
        return
    try:
        await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    cnt, threshold, auto_banned = await _apply_warn(
        bot, msg.chat.id, msg.from_user, f"ارسالِ محتوایِ ممنوعه: «{matched}»", warned_by=0)
    note = f"⚠️ پیامِ {msg.from_user.full_name} به‌خاطرِ محتوایِ ممنوعه حذف شد ({cnt}/{threshold})."
    if auto_banned:
        note += f"\n🚫 چون به سقفِ {threshold} خطا رسید، به‌صورتِ خودکار بن شد."
    try:
        await msg.answer(note)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


async def _cmd_connect(msg: Message, bot: Bot) -> None:
    if not await _is_authorized(msg, bot):
        return
    await set_pset("group_guard_chat_id", str(msg.chat.id))
    await set_pset("group_guard_chat_title", msg.chat.title or "")
    await set_pset("group_guard_enabled", "on")
    threshold = await get_pset("group_guard_warn_threshold", "3")
    await msg.reply(
        "🛡 <b>محافظِ گروه فعال شد.</b>\n\n"
        "دستورات (فقط برایِ ادمین‌هایِ همین گروه):\n"
        "• رویِ پیامِ کسی ریپلای کن و بنویس /بن (یا /ban) — بن می‌شه\n"
        "• /انبن آیدیِ_عددی (یا با ریپلای) — رفعِ بن\n"
        f"• رویِ پیامِ کسی ریپلای کن و بنویس /خطا (یا /warn) — ثبتِ خطا؛ "
        f"بعد از {threshold} خطا خودکار بن می‌شه\n"
        "• /پاک_اخطار — صفرکردنِ خطاهایِ یک نفر (با ریپلای)\n"
        "• /غیرفعال_محافظ — خاموش‌کردنِ محافظ برایِ این گروه\n\n"
        "💡 بدونِ اسلش هم می‌شه: کافیه رویِ پیامِ طرف ریپلای کنی و فقط بنویسی "
        "«بن»، «اخطار»، «پاک کردن اخطار»، «سکوت»، «رفع سکوت» یا «انبن». "
        "(برایِ نوشتنِ دلیل یا مدت از شکلِ اسلش‌دار استفاده کن.)\n\n"
        "مدیریتِ کامل (لیستِ بن‌شده‌ها/خطاکارها) از پنلِ ادمینِ ربات، "
        "بخشِ «🛡 محافظ گروه» در دسترسه.")


async def _cmd_disconnect(msg: Message, bot: Bot) -> None:
    if not await _is_authorized(msg, bot):
        return
    guard_chat_id = await get_pset("group_guard_chat_id", "")
    if guard_chat_id and str(msg.chat.id) != guard_chat_id:
        return
    await set_pset("group_guard_enabled", "off")
    await msg.reply("🔴 محافظِ گروه برایِ این گروه خاموش شد.")


# === PATCH_GROUP_GUARD_BAN_VERIFY_V1 ===
async def _ban_and_verify(bot: Bot, chat_id: int, user_id: int) -> tuple[bool, str | None]:
    """بن را اجرا و از وضعیت واقعی کاربر در Telegram تأیید می‌کند.

    پیام موفقیت و رکورد GroupGuardBan فقط پس از دیدن وضعیت ``kicked`` ثبت
    می‌شود؛ این مانعِ «بن شد» کاذب در صورت خطای مجوز، chat/user اشتباه یا
    تأخیر کوتاه Telegram می‌شود.
    """
    try:
        result = await bot.ban_chat_member(chat_id, user_id)
        if result is False:
            return False, "پاسخ تأیید بن از تلگرام ناموفق بود."
    except Exception as exc:
        logger.exception("group guard ban API failed chat=%s user=%s", chat_id, user_id)
        return False, str(exc)

    last_status = "نامشخص"
    for attempt in range(3):
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            last_status = str(getattr(member, "status", "نامشخص"))
            if last_status == "kicked":
                return True, None
        except Exception as exc:
            last_status = str(exc)
        if attempt < 2:
            await asyncio.sleep(0.5 * (attempt + 1))
    logger.error("group guard ban not verified chat=%s user=%s status=%s", chat_id, user_id, last_status)
    return False, f"وضعیت نهایی کاربر پس از بن تأیید نشد ({last_status})."


# === PATCH_GROUP_GUARD_ANON_ADMIN_V2 ===
# باگِ سوم و سیستمیک: هر شش دستورِ مدیریتی (بن/انبن/سکوت/رفعِ‌سکوت/اخطار/
# پاک‌اخطار) مستقیم به msg.from_user.id یا msg.from_user.full_name تکیه
# می‌کردند تا «چه کسی این کار را کرد» را ثبت/لاگ کنند. وقتی ادمین با
# «ارسالِ ناشناس» می‌فرستد، msg.from_user برابرِ None است — پس همان خط،
# با AttributeError کرش می‌کرد، درست بعد از اینکه دو باگِ قبلی
# (واژهٔ /اخطار و تشخیصِ مجازبودنِ فرستندهٔ ناشناس) رفع شده بودند. همین
# کرش، به‌صورتِ کاملاً بی‌صدا، دستور را متوقف می‌کرد.
def _actor_id(msg: Message) -> int:
    """آیدیِ عددیِ فرستنده؛ اگر ناشناس فرستاده باشد ۰ (همان قراردادِ
    موجودِ «warned_by=0 یعنی خودکار/بدونِ فرستندهٔ مشخص»)."""
    return msg.from_user.id if msg.from_user else 0


def _actor_label(msg: Message) -> str:
    """نامِ نمایشیِ فرستنده برایِ پیام‌ها/لاگ."""
    if msg.from_user:
        return msg.from_user.full_name
    return "ادمینِ ناشناسِ گروه"


async def _cmd_ban(msg: Message, bot: Bot) -> None:
    target = msg.reply_to_message
    if not target or not target.from_user:
        await msg.reply("برایِ بن‌کردن، رویِ پیامِ همون شخص ریپلای کن و /بن رو بفرست.")
        return
    u = target.from_user
    parts = (msg.text or "").split(maxsplit=1)
    reason = parts[1].strip() if len(parts) > 1 else None
    ban_ok, ban_error = await _ban_and_verify(bot, msg.chat.id, u.id)
    if not ban_ok:
        await msg.reply(f"❌ بن تأیید نشد؛ پیام موفقیت ثبت نشد.\n{ban_error}")
        return
    async with Session() as s:
        row = await s.scalar(select(GroupGuardBan).where(
            GroupGuardBan.chat_id == msg.chat.id, GroupGuardBan.user_id == u.id))
        if not row:
            row = GroupGuardBan(chat_id=msg.chat.id, user_id=u.id)
            s.add(row)
        row.full_name = u.full_name
        row.username = u.username
        row.reason = reason
        row.banned_by = _actor_id(msg)  # PATCH_GROUP_GUARD_ANON_ADMIN_V2
        row.banned_at = datetime.datetime.now()
        await s.commit()
    await msg.reply(f"🚫 {u.full_name} بن شد." + (f"\nدلیل: {reason}" if reason else ""))
    await _log_action(
        f"🚫 <b>بن</b>\n👤 {_html.escape(u.full_name)} (<code>{u.id}</code>)\n"
        f"👮 توسط: {_html.escape(_actor_label(msg))}"  # PATCH_GROUP_GUARD_ANON_ADMIN_V2
        + (f"\n📝 دلیل: {_html.escape(reason)}" if reason else ""))


async def _cmd_unban(msg: Message, bot: Bot) -> None:
    target_id = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_id = msg.reply_to_message.from_user.id
    else:
        parts = (msg.text or "").split()
        if len(parts) > 1 and parts[1].lstrip("-").isdigit():
            target_id = int(parts[1])
    if not target_id:
        await msg.reply(
            "برایِ رفعِ بن، آیدیِ عددی رو بعدِ دستور بنویس (یا رویِ پیامش ریپلای کن):\n"
            "/انبن 123456789")
        return
    try:
        await bot.unban_chat_member(msg.chat.id, target_id, only_if_banned=True)
    except Exception as e:
        await msg.reply(f"❌ رفعِ بن ناموفق بود: {e}")
        return
    async with Session() as s:
        row = await s.scalar(select(GroupGuardBan).where(
            GroupGuardBan.chat_id == msg.chat.id, GroupGuardBan.user_id == target_id))
        if row:
            await s.delete(row)
            await s.commit()
    await msg.reply(f"✅ رفعِ بن انجام شد (آیدی {target_id}).")
    await _log_action(
        f"✅ <b>رفعِ بن</b>\n👤 آیدی: <code>{target_id}</code>\n"
        f"👮 توسط: {_html.escape(_actor_label(msg))}")  # PATCH_GROUP_GUARD_ANON_ADMIN_V2


def _full_permissions() -> ChatPermissions:
    """PATCH_GROUP_GUARD_TEMPMUTE_V1 — دسترسیِ کاملِ یک عضوِ عادی؛ برایِ رفعِ
    سکوت (چه دستیِ /رفع‌سکوت، چه کپچایِ موفق) یک‌جا استفاده می‌شه."""
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True)


async def _cmd_mute(msg: Message, bot: Bot) -> None:
    target = msg.reply_to_message
    if not target or not target.from_user:
        await msg.reply(
            "برایِ سکوت‌کردن، رویِ پیامِ همون شخص ریپلای کن و /سکوت رو بفرست.\n"
            "می‌تونی مدت (به دقیقه) و دلیل هم بنویسی، مثلاً: /سکوت 30 توهین")
        return
    u = target.from_user
    parts = (msg.text or "").split(maxsplit=1)
    minutes = int(await get_pset("group_guard_mute_default_min", "10") or "10")
    reason = None
    if len(parts) > 1:
        rest_parts = parts[1].strip().split(maxsplit=1)
        if rest_parts[0].isdigit():
            minutes = int(rest_parts[0])
            reason = rest_parts[1].strip() if len(rest_parts) > 1 else None
        else:
            reason = parts[1].strip()
    try:
        until_dt = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
        await bot.restrict_chat_member(
            msg.chat.id, u.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_dt)
    except Exception as e:
        await msg.reply(f"❌ سکوت‌کردن ناموفق بود: {e}")
        return
    await msg.reply(
        f"🔇 {u.full_name} برایِ {minutes} دقیقه سکوت شد." + (f"\nدلیل: {reason}" if reason else ""))
    await _log_action(
        f"🔇 <b>سکوتِ موقت</b> ({minutes} دقیقه)\n👤 {_html.escape(u.full_name)} (<code>{u.id}</code>)\n"
        f"👮 توسط: {_html.escape(_actor_label(msg))}"  # PATCH_GROUP_GUARD_ANON_ADMIN_V2
        + (f"\n📝 دلیل: {_html.escape(reason)}" if reason else ""))


async def _cmd_unmute(msg: Message, bot: Bot) -> None:
    target_id = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_id = msg.reply_to_message.from_user.id
    else:
        parts = (msg.text or "").split()
        if len(parts) > 1 and parts[1].lstrip("-").isdigit():
            target_id = int(parts[1])
    if not target_id:
        await msg.reply(
            "برایِ رفعِ سکوت، آیدیِ عددی رو بعدِ دستور بنویس (یا رویِ پیامش ریپلای کن):\n"
            "/رفع‌سکوت 123456789")
        return
    try:
        await bot.restrict_chat_member(msg.chat.id, target_id, permissions=_full_permissions())
    except Exception as e:
        await msg.reply(f"❌ رفعِ سکوت ناموفق بود: {e}")
        return
    await msg.reply(f"🔊 رفعِ سکوت انجام شد (آیدی {target_id}).")
    await _log_action(
        f"🔊 <b>رفعِ سکوت</b>\n👤 آیدی: <code>{target_id}</code>\n"
        f"👮 توسط: {_html.escape(_actor_label(msg))}")  # PATCH_GROUP_GUARD_ANON_ADMIN_V2


async def _apply_warn(bot: Bot, chat_id: int, u, reason: str | None, warned_by: int) -> tuple[int, int, bool]:
    """PATCH_GROUP_GUARD_PHASE2_V1 — ثبتِ یک خطا برایِ کاربرِ u در chat_id؛ اگه
    به سقف برسه خودکار بن می‌کنه. هم دستورِ /خطا هم فیلترِ محتوایِ ممنوعه از
    همین تابع استفاده می‌کنن. warned_by=0 یعنی خودکار (توسطِ فیلتر، نه یک ادمین).
    خروجی: (تعدادِ فعلیِ خطا, سقف, آیا خودکار بن شد)."""
    threshold = int(await get_pset("group_guard_warn_threshold", "3") or "3")
    async with Session() as s:
        row = await s.scalar(select(GroupGuardWarn).where(
            GroupGuardWarn.chat_id == chat_id, GroupGuardWarn.user_id == u.id))
        if not row:
            row = GroupGuardWarn(chat_id=chat_id, user_id=u.id, count=0)
            s.add(row)
        row.count = (row.count or 0) + 1
        row.full_name = u.full_name
        row.username = u.username
        row.last_reason = reason
        row.last_warned_at = datetime.datetime.now()
        cnt = row.count
        await s.commit()
    auto_banned = False
    if cnt >= threshold:
        try:
            ban_ok, ban_error = await _ban_and_verify(bot, chat_id, u.id)
            if not ban_ok:
                logger.error("automatic group guard ban failed chat=%s user=%s: %s", chat_id, u.id, ban_error)
            else:
                auto_banned = True
                async with Session() as s2:
                    brow = await s2.scalar(select(GroupGuardBan).where(
                        GroupGuardBan.chat_id == chat_id, GroupGuardBan.user_id == u.id))
                    if not brow:
                        brow = GroupGuardBan(chat_id=chat_id, user_id=u.id)
                        s2.add(brow)
                    brow.full_name = u.full_name
                    brow.username = u.username
                    brow.reason = f"رسیدن به سقفِ {threshold} خطا"
                    brow.banned_by = warned_by
                    brow.banned_at = datetime.datetime.now()
                    await s2.commit()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    by_label = "🤖 خودکار (فیلتر)" if warned_by == 0 else f"<code>{warned_by}</code>"
    log_txt = (
        f"⚠️ <b>خطا</b> ({cnt}/{threshold})\n👤 {_html.escape(u.full_name)} (<code>{u.id}</code>)\n"
        f"👮 ثبت‌کننده: {by_label}"
        + (f"\n📝 دلیل: {_html.escape(reason)}" if reason else "")
        + ("\n🚫 بنِ خودکار به‌خاطرِ رسیدن به سقف" if auto_banned else ""))
    await _log_action(log_txt)
    return cnt, threshold, auto_banned


# === PATCH_GROUP_GUARD_BARE_WORDS_V1 ===
async def _cmd_unwarn(msg: Message, bot: Bot) -> None:
    """صفرکردنِ خطاهایِ یک کاربر از داخلِ گروه (قبلاً فقط از پنلِ ادمین بود)."""
    target = msg.reply_to_message
    if not target or not target.from_user:
        await msg.reply("برایِ پاک‌کردنِ اخطار، رویِ پیامِ همون شخص ریپلای کن.")
        return
    u = target.from_user
    async with Session() as s:
        row = await s.scalar(select(GroupGuardWarn).where(
            GroupGuardWarn.chat_id == msg.chat.id, GroupGuardWarn.user_id == u.id))
        if not row or not (row.count or 0):
            await msg.reply(f"ℹ️ {_html.escape(u.full_name)} خطایِ ثبت‌شده‌ای نداره.")
            return
        had = row.count
        await s.delete(row)
        await s.commit()
    await msg.reply(f"✅ خطاهایِ {_html.escape(u.full_name)} صفر شد (قبلاً {had} تا بود).")
    await _log_action(
        f"🔄 <b>صفرشدنِ خطاها</b>\n👤 {_html.escape(u.full_name)} (<code>{u.id}</code>)\n"
        f"👮 توسطِ: <code>{_actor_id(msg)}</code>")  # PATCH_GROUP_GUARD_ANON_ADMIN_V2


async def _cmd_warn(msg: Message, bot: Bot) -> None:
    target = msg.reply_to_message
    if not target or not target.from_user:
        await msg.reply("برایِ ثبتِ خطا، رویِ پیامِ همون شخص ریپلای کن و /خطا رو بفرست.")
        return
    u = target.from_user
    parts = (msg.text or "").split(maxsplit=1)
    reason = parts[1].strip() if len(parts) > 1 else None
    cnt, threshold, auto_banned = await _apply_warn(bot, msg.chat.id, u, reason, _actor_id(msg))  # PATCH_GROUP_GUARD_ANON_ADMIN_V2
    if auto_banned:
        await msg.reply(
            f"⚠️ خطا برایِ {u.full_name} ثبت شد ({cnt}/{threshold}).\n"
            f"🚫 چون به سقفِ {threshold} خطا رسید، به‌صورتِ خودکار بن شد.")
    else:
        await msg.reply(
            f"⚠️ خطا برایِ {u.full_name} ثبت شد ({cnt}/{threshold})."
            + (f"\nدلیل: {reason}" if reason else ""))


# ─────────────────────── کپچایِ عضوِ جدید ───────────────────────
# === PATCH_GROUP_GUARD_CAPTCHA_V1 / PATCH_GROUP_GUARD_WELCOME_V1 ===

_DEFAULT_WELCOME_TEXT = "👋 {name} عزیز، به {group} خوش اومدی!"


async def _send_welcome(msg: Message, member) -> None:
    template = await get_pset("group_guard_welcome_text", "") or _DEFAULT_WELCOME_TEXT
    name_html = f'<a href="tg://user?id={member.id}">{_html.escape(member.full_name)}</a>'
    text = template.replace("{name}", name_html).replace("{group}", _html.escape(msg.chat.title or "گروه"))
    try:
        await msg.answer(text)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


async def _start_captcha(msg: Message, bot: Bot, member) -> None:
    timeout = int(await get_pset("group_guard_captcha_timeout_sec", "300") or "300")
    try:
        await bot.restrict_chat_member(
            msg.chat.id, member.id,
            permissions=ChatPermissions(can_send_messages=False))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        hx_button("✅ من ربات نیستم", callback_data=f"gg:captcha_ok:{member.id}",
                             style=ButtonStyle.SUCCESS)]])
    mins = max(1, timeout // 60)
    try:
        sent = await msg.answer(
            f"🔰 {member.full_name} عزیز، برایِ اینکه بتونی توی گروه پیام بدی، "
            f"ظرفِ {mins} دقیقه رویِ دکمهٔ زیر بزن؛ وگرنه به‌صورتِ خودکار از گروه حذف می‌شی.",
            reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    expires_at = datetime.datetime.now() + datetime.timedelta(seconds=timeout)
    async with Session() as s:
        row = await s.scalar(select(GroupGuardCaptcha).where(
            GroupGuardCaptcha.chat_id == msg.chat.id, GroupGuardCaptcha.user_id == member.id))
        if not row:
            row = GroupGuardCaptcha(chat_id=msg.chat.id, user_id=member.id)
            s.add(row)
        row.message_id = sent.message_id
        row.full_name = member.full_name
        row.created_at = datetime.datetime.now()
        row.expires_at = expires_at
        await s.commit()


async def _delete_after_delay(bot: Bot, chat_id: int, message_id: int, delay_sec: int = 10) -> None:
    """PATCH_GROUP_GUARD_AUTODELETE_V1 — بعدِ delay_sec ثانیه، پیام رو پاک
    می‌کنه (برایِ تمیزنگه‌داشتنِ گروه از پیام‌هایِ کوتاه‌مدت مثلِ «فلانی
    وارد شد» یا «تأیید شد»). fire-and-forget؛ اگه پیام قبلاً پاک شده
    باشه یا دسترسی نباشه، بی‌سروصدا نادیده می‌گیره."""
    await asyncio.sleep(delay_sec)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@group_guard_router.message(F.chat.type.in_({"group", "supergroup"}), F.new_chat_members)
async def gg_new_members(msg: Message, bot: Bot):
    guard_chat_id = await get_pset("group_guard_chat_id", "")
    if not guard_chat_id or str(msg.chat.id) != guard_chat_id:
        return
    if (await get_pset("group_guard_enabled", "off")) != "on":
        return
    captcha_on = (await get_pset("group_guard_captcha_enabled", "off")) == "on"
    welcome_on = (await get_pset("group_guard_welcome_enabled", "off")) == "on"
    if not captcha_on and not welcome_on:
        return
    # === PATCH_GROUP_GUARD_AUTODELETE_V1 ===
    # پیامِ سرویسیِ خودِ تلگرام («فلانی وارد گروه شد») رو هم بعدِ ۱۰ ثانیه پاک می‌کنیم.
    asyncio.create_task(_delete_after_delay(bot, msg.chat.id, msg.message_id, 10))
    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        # === PATCH_GROUP_GUARD_WELCOME_V1 ===
        # پیامِ خوش‌آمد و کپچا کاملاً مستقل از همدیگه‌ن — می‌تونی هرکدوم رو
        # جدا روشن/خاموش کنی، یا هردو رو با هم داشته باشی.
        if welcome_on:
            await _send_welcome(msg, member)
        if captcha_on:
            await _start_captcha(msg, bot, member)


@group_guard_router.callback_query(F.data.startswith("gg:captcha_ok:"))
async def gg_captcha_ok(cb: CallbackQuery, bot: Bot):
    target_id = int(cb.data.split(":")[2])
    if cb.from_user.id != target_id:
        await cb.answer("این دکمه برایِ تو نیست — فقط خودِ عضوِ جدید می‌تونه بزنه.", show_alert=True)
        return
    async with Session() as s:
        row = await s.scalar(select(GroupGuardCaptcha).where(
            GroupGuardCaptcha.chat_id == cb.message.chat.id, GroupGuardCaptcha.user_id == target_id))
        if not row:
            await cb.answer("این تأیید منقضی شده یا قبلاً انجام شده.", show_alert=True)
            return
        try:
            await bot.restrict_chat_member(
                row.chat_id, row.user_id, permissions=_full_permissions())
        except Exception as e:
            await cb.answer(f"خطا: {e}", show_alert=True)
            return
        await s.delete(row)
        await s.commit()
    try:
        await cb.message.edit_text(f"✅ {cb.from_user.full_name} تأیید شد. خوش اومدی به گروه!")
        # === PATCH_GROUP_GUARD_AUTODELETE_V1 ===
        asyncio.create_task(_delete_after_delay(bot, cb.message.chat.id, cb.message.message_id, 10))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("✅ تأیید شد!")
    await _log_action(
        f"✅ <b>کپچا تأیید شد</b>\n👤 {_html.escape(cb.from_user.full_name)} (<code>{target_id}</code>)")


async def _sweep_expired_captchas() -> None:
    from main_platform import platform_bot as bot
    if not bot:
        return
    now = datetime.datetime.now()
    async with Session() as s:
        rows = (await s.scalars(
            select(GroupGuardCaptcha).where(GroupGuardCaptcha.expires_at <= now))).all()
        for row in rows:
            try:
                await bot.ban_chat_member(row.chat_id, row.user_id)
                await bot.unban_chat_member(row.chat_id, row.user_id, only_if_banned=True)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            try:
                await bot.delete_message(row.chat_id, row.message_id)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            await _log_action(
                f"🔰❌ <b>کیکِ خودکار (کپچایِ منقضی‌شده)</b>\n"
                f"👤 {_html.escape(row.full_name or '')} (<code>{row.user_id}</code>)")
            await s.delete(row)
        if rows:
            await s.commit()


async def group_guard_captcha_sweep_loop() -> None:
    """PATCH_GROUP_GUARD_CAPTCHA_V1 — هر ۳۰ ثانیه چک می‌کنه کدوم کپچاها منقضی
    شدن و بدونِ تأیید مونده‌ن؛ اون‌ها رو کیک (بن+آنبن فوری) می‌کنه. باید در
    main_platform.py هنگامِ بالاآمدنِ سرویس اسپاون بشه."""
    while True:
        try:
            await _sweep_expired_captchas()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await asyncio.sleep(30)


# === PATCH_GROUP_GUARD_NIGHT_V1 ===

async def _night_mode_should_be_active() -> bool:
    if (await get_pset("group_guard_night_enabled", "off")) != "on":
        return False
    start_s = await get_pset("group_guard_night_start", "00:00")
    end_s = await get_pset("group_guard_night_end", "07:00")
    try:
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
    except Exception:
        return False
    from platform_bot import _tehran_now
    now = _tehran_now()
    cur_min = now.hour * 60 + now.minute
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min  # بازه از نیمه‌شب رد می‌شه


async def _apply_night_mode_state() -> None:
    from main_platform import platform_bot as bot
    if not bot:
        return
    chat_id_s = await get_pset("group_guard_chat_id", "")
    if not chat_id_s or (await get_pset("group_guard_enabled", "off")) != "on":
        return
    should_be_active = await _night_mode_should_be_active()
    is_active = (await get_pset("group_guard_night_active", "off")) == "on"
    if should_be_active == is_active:
        return
    chat_id = int(chat_id_s)
    try:
        if should_be_active:
            await bot.set_chat_permissions(chat_id, permissions=ChatPermissions(can_send_messages=False))
            try:
                await bot.send_message(chat_id, "🌙 حالتِ شب فعال شد؛ گروه تا پایانِ بازه بسته می‌مونه.")
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        else:
            await bot.set_chat_permissions(chat_id, permissions=_full_permissions())
            try:
                await bot.send_message(chat_id, "☀️ حالتِ شب تموم شد؛ گروه دوباره باز شد.")
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await set_pset("group_guard_night_active", "on" if should_be_active else "off")
        await _log_action("🌙 <b>حالتِ شب فعال شد</b> (گروه بسته شد)" if should_be_active
                          else "☀️ <b>حالتِ شب تموم شد</b> (گروه باز شد)")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


async def group_guard_night_mode_loop() -> None:
    """PATCH_GROUP_GUARD_NIGHT_V1 — هر ۶۰ ثانیه چک می‌کنه آیا الان باید حالتِ
    شب باز/بسته باشه و در صورتِ نیاز پرمیشنِ گروه رو عوض می‌کنه. باید در
    main_platform.py هنگامِ بالاآمدنِ سرویس اسپاون بشه."""
    while True:
        try:
            await _apply_night_mode_state()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await asyncio.sleep(60)


# ─────────────────────── پنلِ ادمین ───────────────────────

def _require_admin(cb: CallbackQuery) -> bool:
    return cb.from_user.id == PLATFORM_ADMIN_ID


async def _gg_main_text_kb() -> tuple[str, InlineKeyboardMarkup]:
    chat_id = await get_pset("group_guard_chat_id", "")
    title = await get_pset("group_guard_chat_title", "")
    enabled = (await get_pset("group_guard_enabled", "off")) == "on"
    threshold = await get_pset("group_guard_warn_threshold", "3")
    if not chat_id:
        text = (
            "🛡 <b>محافظ گروه</b>\n\n"
            "هنوز گروهی وصل نشده.\n\n"
            "برایِ اتصال:\n"
            "۱. تو @BotFather برو رویِ همین ربات → <b>Bot Settings → Group Privacy → "
            "Turn off</b> (این مرحله اجباریه؛ بدونش ربات اصلاً پیام‌هایِ عادیِ گروه — "
            "شاملِ خطایِ محتوا/فلود/لینکِ تبلیغاتی — رو نمی‌بینه، فقط دستورات رو).\n"
            "۲. ربات رو به گروهت اضافه کن\n"
            "۳. ادمینش کن (حتماً با دسترسیِ «محدودسازیِ اعضا»)\n"
            "۴. توی همون گروه بنویس: <code>/فعال_محافظ</code> (یا اگه به هر دلیل کار "
            "نکرد، انگلیسیش رو امتحان کن: <code>/setguard</code>)\n\n"
            "💡 اگه دستورات تویِ گروه هیچ جوابی نمی‌دن (مثلاً بعدِ رعایتِ همه‌ی نکاتِ "
            "بالا)، به‌جاش از دکمه‌هایِ زیر استفاده کن — کاملاً از همین‌جا و بدونِ "
            "نیاز به تایپِ چیزی تویِ گروه:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [hx_button("🔗 اتصال از طریقِ کانالِ لینک‌شده", callback_data="gg:connect_via_channel")],
            [hx_button("👤 ادمین‌هایِ اضافه", callback_data="gg:extra_admins")],
            [hx_button("⬅️ بازگشت به پنل", callback_data="adm:back", style=ButtonStyle.DANGER)],
        ])
        return text, kb
    async with Session() as s:
        ban_count = await s.scalar(
            select(func.count()).select_from(GroupGuardBan)
            .where(GroupGuardBan.chat_id == int(chat_id))) or 0
        warn_count = await s.scalar(
            select(func.count()).select_from(GroupGuardWarn)
            .where(GroupGuardWarn.chat_id == int(chat_id))) or 0
    status_txt = "🟢 روشن" if enabled else "🔴 خاموش"
    filter_on = (await get_pset("group_guard_filter_enabled", "off")) == "on"
    filter_status = "🟢 روشن" if filter_on else "🔴 خاموش"
    captcha_on = (await get_pset("group_guard_captcha_enabled", "off")) == "on"
    captcha_status = "🟢 روشن" if captcha_on else "🔴 خاموش"
    captcha_timeout = int(await get_pset("group_guard_captcha_timeout_sec", "300") or "300")
    # === PATCH_GROUP_GUARD_ANTIPROMO_V1 ===
    antipromo_on = (await get_pset("group_guard_antipromo_enabled", "off")) == "on"
    # === PATCH_GROUP_REPORT_V1 ===
    report_on = (await get_pset("platform_group_report_enabled", "on")) == "on"
    # === PATCH_GROUP_GUARD_LOG_V1 ===
    log_chat_title = await get_pset("group_guard_log_chat_title", "")
    log_chat_id = await get_pset("group_guard_log_chat_id", "")
    text = (
        f"🛡 <b>محافظ گروه</b>\n\n"
        f"گروهِ متصل: <b>{title or chat_id}</b>\n"
        f"وضعیت: {status_txt}\n"
        f"فیلترِ محتوایِ ممنوعه: {filter_status}\n"
        f"کپچایِ عضوِ جدید: {captcha_status} ({captcha_timeout // 60} دقیقه مهلت)\n"
        f"سقفِ خطا برایِ بنِ خودکار: <b>{threshold}</b>\n\n"
        f"🚫 تعدادِ بن‌شده‌ها: {ban_count}\n"
        f"⚠️ تعدادِ خطاکارها: {warn_count}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button(f"🚫 لیستِ بن‌شده‌ها ({ban_count})", callback_data="gg:bans:0")],
        [hx_button(f"⚠️ لیستِ خطاکارها ({warn_count})", callback_data="gg:warns:0")],
        [hx_button(("🔴 خاموش کردن" if enabled else "🟢 روشن کردن"), callback_data="gg:toggle",
                              style=(ButtonStyle.DANGER if enabled else ButtonStyle.SUCCESS))],
        [hx_button("✏️ تغییرِ سقفِ خطا", callback_data="gg:setthreshold")],
        # === PATCH_GROUP_GUARD_PHASE2_V1 ===
        [hx_button("🚫 محتوایِ ممنوعه", callback_data="gg:filter")],
        [hx_button("📋 راهنمایِ دستورات", callback_data="gg:help")],
        [hx_button("🤖 هوش مصنوعی", callback_data="gg:ai")],
        # === PATCH_GROUP_GUARD_CAPTCHA_V1 ===
        [hx_button(f"🔰 کپچایِ عضوِ جدید: {captcha_status}", callback_data="gg:captcha_toggle")],
        [hx_button("⏱ تنظیمِ مهلتِ کپچا", callback_data="gg:captcha_timeout")],
        # === PATCH_GROUP_GUARD_FLOOD_V1 ===
        [hx_button("🌊 محدودیتِ فلود", callback_data="gg:flood")],
        # === PATCH_GROUP_GUARD_TEMPMUTE_V1 ===
        [hx_button("⏱ مدتِ پیش‌فرضِ سکوتِ دستی", callback_data="gg:mute_default")],
        # === PATCH_GROUP_GUARD_CONTENT_RESTRICT_V1 ===
        [hx_button("🔗 محدودیتِ نوعِ محتوا", callback_data="gg:content_restrict")],
        # === PATCH_GROUP_GUARD_WELCOME_V1 ===
        [hx_button("👋 پیامِ خوش‌آمد", callback_data="gg:welcome")],
        # === PATCH_GROUP_GUARD_NIGHT_V1 ===
        [hx_button("🌙 حالتِ شب", callback_data="gg:night")],
        # === PATCH_GROUP_GUARD_ANTIPROMO_V1 ===
        [hx_button(f"📢 آنتی‌فلودِ لینکِ تبلیغاتی: {'🟢 روشن' if antipromo_on else '🔴 خاموش'}",
                              callback_data="gg:antipromo_toggle")],
        # === PATCH_GROUP_REPORT_V1 ===
        [hx_button(f"📣 گزارش اعضا (ریپلای + «گزارش»): {'🟢 روشن' if report_on else '🔴 خاموش'}",
                              callback_data="gg:report_toggle")],
        # === PATCH_GROUP_GUARD_LOG_V1 ===
        [hx_button(f"📝 لاگِ کامل: {('✅ ' + log_chat_title) if log_chat_id else '🔴 وصل نیست'}",
                              callback_data="gg:log_setup")],
        # === PATCH_GROUP_GUARD_CHANNEL_CONNECT_V1 ===
        [hx_button("👤 ادمین‌هایِ اضافه", callback_data="gg:extra_admins")],
        # === PATCH_GROUP_GUARD_DISCONNECT_V1 ===
        [hx_button("🔌 قطعِ اتصالِ این گروه / اتصال به گروهِ دیگر",
                              callback_data="gg:disconnect_confirm", style=ButtonStyle.DANGER)],
        [hx_button("⬅️ بازگشت به پنل", callback_data="adm:back", style=ButtonStyle.DANGER)],
    ])
    return text, kb


@group_guard_router.callback_query(F.data == "adm:group_guard")
async def gg_menu(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    text, kb = await _gg_main_text_kb()
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data == "gg:toggle")
async def gg_toggle(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_enabled", "off")
    await set_pset("group_guard_enabled", "off" if cur == "on" else "on")
    await gg_menu(cb)


# === PATCH_GROUP_GUARD_DISCONNECT_V1 ===
# قطعِ اتصالِ گروهِ فعلی، مستقیم از پنلِ ادمین (بدونِ نیاز به رفتن تو خودِ
# گروه و تایپِ /غیرفعال_محافظ). بعدش برای وصل‌کردنِ گروهِ جدید، همچنان باید
# ربات رو به اون گروه اضافه و ادمین کرد و توش /فعال_محافظ نوشت — چون
# برخلافِ کانال، فورواردِ یک پیامِ معمولیِ گروه هیچ‌وقت آیدیِ خودِ گروه رو
# به بات نمی‌ده (محدودیتِ خودِ API تلگرامه، نه چیزی که بشه دورش زد)، برایِ
# همین «اتصال با فوروارد» فقط برایِ کانال‌ها (مثلِ کانالِ لاگ) پیاده‌سازی
# شد، نه برایِ گروه. تنظیمات/سقف‌ها/فیلترها و تاریخچهٔ بن/خطا دست‌نخورده
# می‌مونه (اگه دوباره همون گروه وصل بشه برمی‌گردن؛ برایِ گروهِ جدید هم
# طبیعتاً از صفر شروع می‌شن چون بر اساسِ chat_id ذخیره می‌شن).

@group_guard_router.callback_query(F.data == "gg:disconnect_confirm")
async def gg_disconnect_confirm(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    title = await get_pset("group_guard_chat_title", "")
    chat_id = await get_pset("group_guard_chat_id", "")
    if not chat_id:
        await cb.answer("گروهی وصل نیست.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("✅ بله، قطع کن", callback_data="gg:disconnect_do", style=ButtonStyle.DANGER)],
        [hx_button("❌ انصراف", callback_data="adm:group_guard")],
    ])
    text = (
        f"⚠️ مطمئنی می‌خوای اتصالِ گروهِ «{title or chat_id}» قطع بشه؟\n\n"
        "تنظیمات (فیلتر، سقفِ خطا، کپچا و...) و تاریخچهٔ بن/خطا نگه داشته "
        "می‌شه (اگه دوباره همین گروه وصل بشه برمی‌گردن). برایِ وصل‌کردنِ "
        "گروهِ دیگه، بعدِ قطعِ این یکی، باید ربات رو به گروهِ جدید اضافه/ادمین "
        "کنی و توش <code>/فعال_محافظ</code> بنویسی."
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data == "gg:disconnect_do")
async def gg_disconnect_do(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await set_pset("group_guard_chat_id", "")
    await set_pset("group_guard_chat_title", "")
    await set_pset("group_guard_enabled", "off")
    await set_pset("group_guard_night_active", "off")
    await cb.answer("🔌 اتصال قطع شد.")
    await gg_menu(cb)


@group_guard_router.callback_query(F.data == "gg:help")
async def gg_help(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    threshold = await get_pset("group_guard_warn_threshold", "3")
    mute_default = await get_pset("group_guard_mute_default_min", "10")
    custom_note = await get_pset("group_guard_tutorial_text", "")
    text = (
        "📋 <b>راهنمایِ دستوراتِ محافظ گروه</b>\n\n"
        "این دستورات رو خودِ ادمین‌هایِ گروه، مستقیم توی گروهِ متصل‌شده تایپ می‌کنن:\n\n"
        "🚫 <b>بن‌کردن</b>\n"
        "رویِ پیامِ فرد ریپلای کن و بفرست: <code>/بن دلیل (اختیاری)</code>\n\n"
        "✅ <b>رفعِ بن</b>\n"
        "<code>/انبن آیدیِ_عددی</code> — یا رویِ پیامِ قبلیِ فرد ریپلای کن و <code>/انبن</code> بفرست\n\n"
        f"⚠️ <b>ثبتِ خطا</b> (بعد از {threshold} خطا، خودکار بن می‌شه)\n"
        "رویِ پیامِ فرد ریپلای کن و بفرست: <code>/خطا دلیل (اختیاری)</code>\n\n"
        f"🔇 <b>سکوتِ موقت</b> (پیش‌فرض {mute_default} دقیقه)\n"
        "رویِ پیامِ فرد ریپلای کن و بفرست: <code>/سکوت</code> یا <code>/سکوت 30 دلیل</code>\n\n"
        "🔊 <b>رفعِ سکوت</b>\n"
        "<code>/رفع‌سکوت آیدیِ_عددی</code> — یا رویِ پیامِ فرد ریپلای کن و <code>/رفع‌سکوت</code> بفرست\n\n"
        "🔧 <b>اتصال/قطعِ محافظ</b> (فقط مدیرِ اصلی)\n"
        "<code>/فعال_محافظ</code> — اتصالِ همون گروه\n"
        "<code>/غیرفعال_محافظ</code> — خاموش‌کردنِ محافظ برایِ همون گروه\n\n"
        "💡 معادلِ انگلیسیِ همه‌شون هم کار می‌کنه: /ban /unban /warn /mute /unmute /setguard /unsetguard"
    )
    if custom_note:
        text += f"\n\n📝 <b>یادداشتِ ادمین (قوانین/توضیحاتِ اضافه):</b>\n{custom_note}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        # === PATCH_GROUP_GUARD_PHASE2_V1 ===
        [hx_button("✏️ ویرایشِ یادداشتِ ادمین", callback_data="gg:tutorial_edit")],
        [hx_button("⬅️ بازگشت", callback_data="adm:group_guard", style=ButtonStyle.DANGER)]])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data == "gg:tutorial_edit")
async def gg_tutorial_edit_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(GgFSM.tutorial_text)
    await cb.message.answer(
        "📝 متنِ دلخواهت رو بفرست — مثلاً قوانینِ گروه، توضیحِ اینکه چه محتوایی "
        "ممنوعه، یا هر نکتهٔ دیگه‌ای که می‌خوای زیرِ راهنمایِ دستورات دیده بشه.\n"
        "برایِ پاک‌کردنِ یادداشتِ فعلی، بنویس «-».",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.tutorial_text, F.text)
async def gg_tutorial_edit_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    await set_pset("group_guard_tutorial_text", "" if txt == "-" else txt)
    await state.clear()
    await msg.answer("✅ یادداشت ذخیره شد." if txt != "-" else "✅ یادداشت پاک شد.")


# ── محتوایِ ممنوعه ──

@group_guard_router.callback_query(F.data == "gg:filter")
async def gg_filter_menu(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    on = (await get_pset("group_guard_filter_enabled", "off")) == "on"
    words_raw = await get_pset("group_guard_banned_words", "")
    words = [w.strip() for w in words_raw.splitlines() if w.strip()]
    preview = "\n".join(f"• {w}" for w in words[:15]) if words else "(هنوز چیزی اضافه نشده)"
    more = f"\n… و {len(words) - 15} موردِ دیگر" if len(words) > 15 else ""
    text = (
        "🚫 <b>محتوایِ ممنوعه</b>\n\n"
        "هر پیامِ اعضایِ عادیِ گروه (نه ادمین‌ها) که یکی از این عبارت‌ها رو داشته "
        "باشه خودکار حذف می‌شه و یک خطا برایِ فرستنده ثبت می‌شه.\n\n"
        f"وضعیت: {'🟢 روشن' if on else '🔴 خاموش'}\n"
        f"تعدادِ عبارت‌ها: {len(words)}\n\n"
        f"{preview}{more}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button(("🔴 خاموش کردنِ فیلتر" if on else "🟢 روشن کردنِ فیلتر"),
                              callback_data="gg:filter_toggle",
                              style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [hx_button("✏️ ویرایشِ کاملِ لیست", callback_data="gg:filter_edit")],
        [hx_button("⬅️ بازگشت", callback_data="adm:group_guard", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data == "gg:filter_toggle")
async def gg_filter_toggle(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_filter_enabled", "off")
    await set_pset("group_guard_filter_enabled", "off" if cur == "on" else "on")
    await gg_filter_menu(cb)


@group_guard_router.callback_query(F.data == "gg:filter_edit")
async def gg_filter_edit_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(GgFSM.banned_words)
    await cb.message.answer(
        "✏️ کلِ لیستِ عبارت‌هایِ ممنوعه رو بفرست — <b>هرکدوم توی یک خط جدا</b> "
        "(هر پیامی که حاویِ هرکدوم از این کلمه‌ها/عبارت‌ها باشه حذف می‌شه؛ "
        "بزرگی/کوچکیِ حروف مهم نیست). این جایگزینِ کاملِ لیستِ فعلی می‌شه.\n\n"
        "برایِ خالی‌کردنِ کاملِ لیست، فقط «-» بفرست.",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.banned_words, F.text)
async def gg_filter_edit_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if txt == "-":
        await set_pset("group_guard_banned_words", "")
        await state.clear()
        await msg.answer("✅ لیست خالی شد.")
        return
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    await set_pset("group_guard_banned_words", "\n".join(lines))
    await state.clear()
    await msg.answer(f"✅ لیست ذخیره شد ({len(lines)} عبارت).")


# ── هوش مصنوعی ──

# === PATCH_GROUP_AI_REVAMP_V1 === پرامپتِ پایه، بخش‌هایِ تازه، جست‌وجو و پاک‌کنندهٔ فرمت در platform_ai_kb.py
from platform_ai_kb import (ALWAYS as _GG_AI_ALWAYS, TOPICS_2026_10D as _GG_AI_TOPICS_NEW,
                            clean_ai_text as _gg_clean_ai_text, select_relevant_topics as _kb_select,
                            strip_marks as _gg_strip_marks)

# === PATCH_GROUP_GUARD_KB_RETRIEVAL_V1 ===
# هر آیتم: (کلیدواژه‌ها، متنِ کامل همون بخش). فقط بخش‌هایی که با سوالِ
# کاربر هم‌پوشانیِ کلیدواژه دارن انتخاب و به پرامپت اضافه می‌شن — به‌جایِ
# فرستادنِ کلِ ~۴۰هزار کاراکتر تو هر درخواست (که رویِ Groq به سقفِ توکن
# می‌خورد)، فقط چند صد تا چند هزار کاراکترِ مرتبط فرستاده می‌شه.
_GG_AI_TOPICS: list[tuple[tuple[str, ...], str]] = [
    (('VPN', 'X-UI', 'خودکار', 'سرویس', 'محصولات', 'مدیریت', '📦 سرویس\u200cها و محصولات', '🤖 پنل VPN خودکار (X-UI'), '🛰 **VPN خودکار** — پنل مدیریت → «📦 سرویس\u200cها و محصولات» → «🤖 پنل VPN خودکار (X-UI)»:\n  • اتصالِ پنل: دکمهٔ اتصالِ پنلِ جدید، انتخابِ نوعِ پنل (سنایی/مرزبان/هدیفای/پاسارگارد/...)، واردکردنِ آدرس+توکن/یوزرنیم-پسورد\n  • پلن\u200cهایِ فروش (بستهٔ ثابت): پنلِ متصل\u200cشده → «📦 پلن\u200cهای فروش» → «➕ افزودن پلن جدید» (حجم/روز/قیمت/محدودیتِ آی\u200cپی)\n  • تستِ رایگان: از همون بخشِ پلن\u200cها یه پلنِ قیمتِ‌صفر/برچسبِ«تست» بساز، یا «🔒 گیتِ دسترسی» رویِ دکمهٔ تستِ رایگان\n  • فروشِ آنلاینِ دلخواه (کاربر حجم/روز رو با ➕➖ انتخاب می\u200cکنه): «🛒 فروش آنلاین (حجم/روز دلخواه)» — قیمتِ هر گیگ/روز همون\u200cجا\n  • حسابِ همکار (تخفیفِ اختصاصی با آیدیِ عددی): «🤝 حساب\u200cهای همکار (تخفیف)»\n  • تمدید/افزایشِ حجم، اطلاعِ اتمامِ حجم، نام\u200cگذاریِ سرورها، جستجویِ کانفیگ با کد: همه زیرِ همین منو'),
    (('VPN', 'زیرمجموعه', 'سازیِ', 'شرطی', 'گیری', '🎁 جایزهٔ پلکانی', '🔒 گیتِ دسترسی'), '🔒 **شرطی\u200cسازیِ دکمه با زیرمجموعه\u200cگیری** (رویِ هر دکمه‌ای، نه فقط VPN):\nویرایشِ دکمه → شرطی\u200cسازی با زیرمجموعه → دو حالت:\n  • «🎁 جایزهٔ پلکانی»: با N نفر زیرمجموعه یه جایزه (کانفیگ از انبار یا متن)؛ چند سطح قابلِ تعریف\n  • «🔒 گیتِ دسترسی»: عملکردِ دکمه فقط با N نفر باز می‌شه؛ ملاک: زیرمجموعهٔ واقعی، دعوت‌شدگانِ لینکِ عمومی، لینکِ اختصاصیِ دکمه (بعدِ هر استفاده صفر می‌شه)، یا **🏅 امتیازِ رفرال**'),
    (('ارسال', 'ارسالِ', 'مدیریت', 'پست', 'کانال', '📎 افزودنِ دکمه به پستِ موجود', '📝 ساخت پست جدید', '📢 ارسال پست به کانال'), '📢 **ارسالِ پست به کانال** — پنل مدیریت → «📢 ارسال پست به کانال»:\nکانال رو وصل کن (فوروارد یا لینک)، بعد «📝 ساخت پست جدید» (متن+دکمه، خودِ ربات می‌فرسته)، یا برایِ حفظِ ایموجیِ پرمیوم، پست رو خودت تو کانال بذار و «📎 افزودنِ دکمه به پستِ موجود».'),
    (('مالی', 'مدیریت', 'پرداخت', 'پول', 'کدهایِ تخفیف', 'کیف', '💰 مالی و کیف پول', '🧾 کانالِ تاییدِ رسید'), '💳 **مالی و پرداخت** — پنل مدیریت → «💰 مالی و کیف پول»:\nشمارهٔ کارت/شبا، تاییدِ خودکارِ رسید (AI)، «🧾 کانالِ تاییدِ رسید» (پستِ متمرکز به‌جایِ دی‌امِ تک‌تکِ ادمین)، درگاه‌هایِ ارزِ دیجیتال/سواپ‌ولت/استارزِ تلگرام، «کدهایِ تخفیف».'),
    (('بسته', 'دیجیتال/رزرو/تبلیغات', 'رباتِ', 'سرویس', 'شوند', 'ظاهر', 'فروشگاهِ', 'محصولات', 'نوعِ', '📦 سرویس\u200cها و محصولات'), '🛍 **فروشگاهِ دیجیتال/رزرو/تبلیغات**: زیرِ «📦 سرویس‌ها و محصولات»، بسته به نوعِ رباتِ کاربر ظاهر می‌شن.'),
    (('اصلی', 'ترین', 'جواب', 'دکمه', 'سوال', 'صفحهٔ', 'عملکردِ', 'عملکردِ دکمه', 'مسلط', 'مهم', 'هایِ', 'پرتکرارترین', 'کاملاً', '🎛 دکمه\u200cها و تنظیمات', '🏠 مدیریت دکمه\u200cهای صفحه اصلی'), '🏠 **دکمه‌هایِ صفحهٔ اصلی و «عملکردِ دکمه»** (پرتکرارترین سوال — کاملاً مسلط جواب بده):\nمسیر: پنل مدیریت → «🎛 دکمه‌ها و تنظیمات» → «🏠 مدیریت دکمه‌های صفحه اصلی».\nچیدمانِ منویِ ربات: 🟢 فعال، 🔴 غیرفعال، 🗂 زیرمنو، 📜 دارایِ قانون. «➕ افزودن دکمه» می‌سازه، زدن رویِ دکمه وارد تنظیماتش می‌کنه:\n  • «✅ فعال‌سازی/⛔️ غیرفعال‌سازی»\n  • «✏️ نام، متن و ظاهر» — نام، رنگ (قرمز/سبز/آبی)، متنِ نمایشی، ایموجیِ پرمیوم\n  • «💬 پشتیبانیِ این دکمه» — دکمهٔ پشتیبانیِ اختصاصی\n  • «📜 قوانین و دسترسی و تست» — شرطِ ورود و تستش\n  • «🎯 عملکرد دکمه» ← دکمه *چه کاری انجام بده*\n  • «⌨️ کامند سفارشی» — مثلاً /wallet\n  • «↕️ جابه‌جایی» و «🗑 حذف»'),
    (('VPN', 'اول', 'اولش', 'باید', 'بدون', 'بردارد', 'برعکس', 'بستگی', 'بیند', 'تبدیل', 'تبدیل به X', 'تبدیل به …', 'حذفِ', 'داخلِ', 'دهد', 'دکمه', 'دکمهٔ', 'دیده', 'ربات', 'زمینهٔ', 'زمینهٔ ربات', 'ساده', 'ساده / بدون عملکرد', 'شوند', 'ضمناً', 'عملکرد', 'عملکرد دکمه', 'عملکردِ', 'عملکردی', 'فروشگاهی', 'فعلی', 'قلبِ', 'لیستِ', 'مهم', 'نشان', 'نمی', 'نگرفته', 'هایِ', 'هنوز', 'کار', 'گزینهٔ', '↩️', '↩️ حذفِ …', '🎯 عملکرد دکمه'), '🎯 **«عملکرد دکمه» — قلبِ کار.** دکمهٔ تازه اولش «ساده / بدون عملکرد» است، فقط متن نشون می‌ده. برایِ کارکردن، داخلِ «🎯 عملکرد دکمه» یکی از «تبدیل به …» رو بزنه. مهم: «تبدیل به X» فقط وقتی دیده می‌شه که دکمه هنوز عملکرد نگرفته؛ اگه داره، اول با «↩️ حذفِ …» عملکردِ فعلی برداشته بشه. لیستِ گزینه‌ها به «زمینهٔ ربات» بستگی داره (VPN گزینه‌هایِ فروشگاهی رو نمی‌بینه و برعکس).\nعملکردها:\n  • 🗂 زیرمنو — بعدش «🗂 مدیریت زیرمنو»\n  • 🎞 اسلایدشو — بعدش «🎞 مدیریت اسلایدشو»\n  • 💰 کیف پول، 👤 پروفایل من، 🔗 سیستم دعوت، ⭐ امتیازدهی، 💬 نظرات، 🎫 تیکت پشتیبانی، 🏆 نفرات برتر، 🌐 مینی‌اپ\n  • 🎲 بازی کن پول بگیر (تاس می‌اندازه، جایزه به کیف پول؛ تنظیم در «🎛 دکمه‌ها و تنظیمات» → «🎲 بازی کن پول بگیر»)\n  • VPN: 🛒 خرید کانفیگ، ♾️ خرید نامحدود، 🎁 تست رایگان VPN، 📡 سرویس‌های من، 📦 کانفیگ‌های من، 🧩 آموزش اتصال، «خرید از پنل X» (سنایی/مرزبان/مرزنشین/هیدیفای/پاسارگارد/ربکا/علیرضا/یوپنل — چند پنلِ هم‌نوع یعنی «پنل پاسارگاد #2»/#3 هم جدا در لیست)\n  • فروشگاهی: 🛒 نمایش فروشگاه، 🛍 اتصالِ مستقیم به محصول\n  • نوبت‌دهی: 📅 نوبت‌گیری، 🗓 نوبت‌های من\n  • خدماتِ مجازی: افزایشِ ممبر/ری‌اکشن/ویو/لایک/رأی/کامنت، 🛰 خدماتِ SMM'),
    (('VPN', 'از', 'از کجا شروع کنم', 'اندازیِ', 'تازه', 'تازه\u200cکارم', 'دقیقاً', 'راه', 'رباتِ', 'شروع', 'صدِ', 'صفر', 'فروش VPN', 'فروشِ', 'مسیرِ', 'کارم', 'کجا', 'کسی', 'کنم', 'گفت', '💰 مالی و کیف پول', '🛠 پنل مدیریت'), '🧭 **مسیرِ صفر تا صدِ رباتِ فروشِ VPN** (اگر گفت «تازه‌کارم»/«از کجا شروع کنم؟» دقیقاً همین را بده):\n  ۱) بات‌ساز → زمینه‌اش «فروش VPN».\n  ۲) «🛠 پنل مدیریت» → «💰 مالی و کیف پول» → شمارهٔ کارت+نامِ صاحبِ کارت (بدونِ این پرداخت کار نمی‌کند).\n  ۳) «📦 سرویس‌ها و محصولات» → «🤖 پنل VPN خودکار» → وصل کن، «تستِ اتصال».\n  ۴) همون پنل «📦 پلن‌های فروش» → چند پلن (حجم/روز/قیمت).\n  ۵) «🎛 دکمه‌ها و تنظیمات» → «🏠 مدیریت دکمه‌های صفحه اصلی» → «➕ افزودن دکمه» → «🛒 خرید سرویس» → «🎯 عملکرد دکمه» → «خرید کانفیگ از پنل».\n  ۶) دکمهٔ «💰 کیف پول» و «📡 سرویس‌های من» هم بساز.\n  ۷) توصیه‌شده: «🎁 تست رایگان»، «📢 عضویت اجباری در کانال»، «🤖 تایید خودکار رسید».\n  ۸) با اکانتِ دیگر یه خریدِ آزمایشی بزن.'),
    (('QR', 'WireGuard', 'conf', 'اضافه', 'تکیِ', 'وایرگارد', 'کانفیگِ', '📡 سرویس\u200cهای من', '🛡 ارسال کانفیگ تکی وایرگارد'), '🛡 **کانفیگِ تکیِ وایرگارد**:\n«📡 سرویس‌های من» → بازکردنِ سرویس، دکمهٔ «🛡 ارسال کانفیگ تکی وایرگارد» بالایِ QR. اگه پنل برایِ اون سرویس واقعاً اینباند/کلاینتِ WireGuard داره، فایلِ .conf مستقیم فرستاده می‌شه. اگه سرویس اصلاً کانفیگِ وایرگارد نداره، پیامِ «این سرویس کانفیگِ وایرگارد ندارد» می‌آد — خرابی نیست، فقط اون پنل/سرویس وایرگارد نداره.'),
    (('اتصال به پنل قطع شد', 'اضافه', 'خودکارِ', 'قطعیِ', 'هشدارِ'), '⚠️ **هشدارِ خودکارِ قطعیِ پنل**:\nنگهبانِ پس‌زمینه هر ۵ دقیقه اتصالِ پنل‌هایِ فعال رو چک می‌کنه. اگه پنلی قطع بشه (رمز/آدرس عوض، سرور خاموش، فایروال، کارِ یک ادمینِ فرعی)، پیامِ «اتصال به پنل قطع شد!» با میان‌بُر به مدیریتِ همون پنل می‌ره برایِ مالک؛ وصل‌شدنِ دوباره هم «اتصال برقرار شد» می‌گه. فقط لحظهٔ تغییرِ وضعیت، نه هر ۵ دقیقه.'),
    (('VPN', 'اصلاح', 'تست', 'تنظیماتِ', 'درست', 'رایگان', 'مسیرِ', '⏰ یادآوریِ عدمِ استفاده', '🎁 تنظیماتِ تست رایگان', '🎛 دکمه\u200cها و تنظیمات', '🛠 پنل مدیریت'), '🎁 **تنظیماتِ تست رایگان**:\n«🛠 پنل مدیریت» → «🎛 دکمه‌ها و تنظیمات» → «🎁 تنظیماتِ تست رایگان» (فقط ربات‌هایِ VPN). شاملِ روشن/خاموش، منبعِ کانفیگِ تست (انبار دستی یا پنل خودکار)، حجم، مدت، «⏰ یادآوریِ عدمِ استفاده» (اگه تست گرفته و استفاده نکرده، یادآوری بعد حذفِ خودکار). هم inline هم کیبوردی کار می‌کنه. توجه: غیر از «🎁 تست رایگان VPN» (دکمهٔ مصرف‌کنندهٔ صفحهٔ اصلی برایِ مشتری‌ها، ساخته‌شده در «🏠 مدیریت دکمه‌های صفحه اصلی») — اگه قاطی کرد، فرقشون رو روشن کن.'),
    (('اضافه', 'ربات همکار', 'سوالش', 'فروشِ', 'نمایندگیِ', 'همکار'), '🤝 **ربات همکار (نمایندگیِ فروشِ ربات)** — سوالش زیاد می‌شه؛ کامل بلد باش:\nیعنی کاربر خودش نمایندهٔ بات‌ساز می‌شه و با رباتِ خودش به مشتری‌هایش ربات می‌فروشه. سه نقش، قاطی‌شان نکن:'),
    (('ارسال', 'اصلیِ', 'بات', 'بندد', 'بینم', 'تایید', 'تواند', 'توجه', 'خواند', 'خواهد', 'درخواست', 'درخواستش', 'دکمهٔ', 'رباتِ', 'رود', 'زند', 'ساز', 'قوانین', 'مدیر', 'مدیرِ', 'ممکن', 'موقتاً', 'نشده', 'نمی', 'همکار', 'همکاری', 'پذیرشِ', 'پذیرم', 'پشتیبانی', 'پیگیری', 'کسی', 'گفت', '✅ قوانین را می\u200cپذیرم', '✉️', '✉️ ارسال پیام به پشتیبانی', '🤝 درخواست همکاری'), '  ▪️ **کسی که می‌خواد همکار شه**: رباتِ اصلیِ بات‌ساز → «🤝 درخواست همکاری» → قوانین → «✅ قوانین را می‌پذیرم» → درخواست برایِ مدیرِ بات‌ساز می‌ره. تا تاییدنشدن از همون صفحه «✉️ ارسال پیام به پشتیبانی» پیگیری کنه. توجه: این دکمه ممکنه موقتاً خاموش باشه (مدیر پذیرش رو بسته) — اگه گفت دکمه رو نمی‌بینه، همینو بگو.'),
    (('BotFather', 'انبار', 'بعدِ', 'بقیهٔ', 'تایید', 'تاییدشده', 'تخفیفِ', 'توکن', 'خرد', 'خریدِ', 'رباتِ', 'ساخت', 'سازد', 'عمدهٔ', 'فرض', 'مثلِ', 'من', 'موجودی', 'نشان', 'همکار', 'همکارش', 'همکارِ', 'همکاری', 'پیش', '۳۰٪', '🗂 ربات\u200cهای من', '🤝 ساخت ربات همکار'), '  ▪️ **همکارِ تاییدشده**: «🗂 ربات‌های من» → «🤝 ساخت ربات همکار» (توکن از BotFather مثلِ بقیه). بعد انبار می‌خره (خریدِ عمده با تخفیفِ همکاری، پیش‌فرض ۳۰٪)، موجودی همون پنل نشون داده می‌شه.'),
    (('اول', 'ای', 'برمی', 'تست', 'توکنِ', 'ثبت', 'حرفه', 'خریدار', 'خودِ', 'داخلِ', 'درخواست', 'دهد', 'ربات', 'رباتِ', 'رشد', 'روزه', 'زند', 'ساخت', 'فرستد', 'مالکیتِ', 'مشتریِ', 'نامِ', 'همکار', 'پلنِ', 'پول', 'پولِ', 'کارت', 'کیفِ', '🎁 درخواست تست ۲ روزه', '💎 حرفه\u200cای', '🚀 رشد', '🤖 ساخت ربات'), '  ▪️ **مشتریِ همکار**: رباتِ همکار → «🤖 ساخت ربات» → پلنِ «🚀 رشد»/«💎 حرفه‌ای» (یا «🎁 درخواست تست ۲ روزه») → کیفِ پول یا کارت‌به‌کارت → توکنِ ربات خودش. مالکیت از اول به‌نامِ خریدار.\n\n  قواعدِ عددی (دقیق بگو، عدد نساز): تخفیفِ پیش‌فرض ۳۰٪ · حدِ نصابِ فعالِ فروخته‌شده ۱۰ عدد · زیرِ حدِ نصاب ۳۰ روز مهلتِ ارفاقی · رباتِ تست ۲ روزه، از انبار کم نمی‌شه و در حدِ نصاب شمرده نمی‌شه · سقفِ هر سفارشِ عمده ۱۰۰ عدد.'),
    (('VPN', 'آنلاینِ', 'تعرفه', 'حجمی', 'دار', 'فروشِ', 'مدت', 'هایِ', 'پله', '➕ افزودن تعرفهٔ جدید', '📦 سرویس\u200cها و محصولات', '🛒 فروش آنلاین', '🤖 پنل VPN خودکار', '🧾 تعرفه\u200cهای مدت\u200cدار (قفل ماهانه'), '🧾 **تعرفه‌هایِ مدت‌دار و پله‌هایِ حجمی (فروشِ آنلاین)**:\nمسیر: پنل مدیریت → «📦 سرویس‌ها و محصولات» → «🤖 پنل VPN خودکار» → پنلِ متصل → «🛒 فروش آنلاین» → «🧾 تعرفه‌های مدت‌دار (قفل ماهانه)».\n«➕ افزودن تعرفهٔ جدید» برایِ هر بازهٔ روز (۳۰–۳۰ یعنی یک‌ماهه) یه بازهٔ حجم+قیمتِ هرگیگ. می‌تونه برایِ **یک بازهٔ روزِ یکسان چند پلهٔ حجمی** بسازه، و قیمت **تجمعی** حساب می‌شه — ۱۵ گیگ با پله‌هایِ ۵-۱۰=۱۰۰۰ت و ۱۱-۲۰=۲۰۰۰ت می‌شه ۱۰×۱۰۰۰+۵×۲۰۰۰=۲۰,۰۰۰ت، نه ۱۵×۲۰۰۰.\nپله‌ها نباید هم‌پوشانی داشته باشن؛ اگه بعدِ ۵-۱۰ دوباره از ۱۰ شروع کنه، ربات خطا می‌ده (باید از ۱۱ شروع کنه). اگه گفت «چرا فقط یکی از تعرفه‌هام اعمال می‌شه»، همینه.'),
    (('ثبتِ', 'جمعیِ', 'دسته', 'ساب', 'لینک', 'چند', 'کانفیگ', '📚 ثبت دسته\u200cجمعی کانفیگ آماده', '📚 ثبت دسته\u200cجمعی کانفیگ نامحدود', '📦 سرویس\u200cها و محصولات'), '📚 **ثبتِ دسته‌جمعیِ کانفیگ (چند ساب‌لینک با هم)**:\nمسیر: پنل مدیریت → «📦 سرویس‌ها و محصولات» → انبارِ کانفیگ → «📚 ثبت دسته‌جمعی کانفیگ آماده» یا «...نامحدود».\nحجم/روز/تعدادِ کاربر/قیمت/دسته‌بندی **یک‌بار** پر می‌شه (برایِ همه یکسان)، بعد «🔗 لینک‌های ساب» و کلِ ساب‌ها فرستاده می‌شه — هر خط/فاصله یه کانفیگِ جدا، چند پیامِ پشتِ‌سرِهم هم می‌شه. تکراری خودکار حذف. آخرش «✅ ثبت کانفیگ» و مثلاً ۲۰ کانفیگ یکجا وارد.'),
    (('ارتقا', 'ارتقایِ', 'اصلیِ', 'بات', 'تغییر', 'حسابِ', 'خودِ', 'ربات', 'رباتِ', 'ساختِ', 'ساز', 'مستأجر', 'من', 'نوع', 'پلن', '⭐️', '⭐️ ارتقا پلن', '🔒 تغییر نوع ربات', '🗂 ربات\u200cهای من'), '🗂 **ساختِ ربات جدید یا ارتقایِ پلن** (خودِ حسابِ کاربر، نه ربات مستأجر): رباتِ اصلیِ بات‌ساز → «🗂 ربات‌های من» → روی ربات → «⭐️ ارتقا پلن» یا «🔒 تغییر نوع ربات».'),
    (('آگوست', 'امتیازِ هر رفرال', 'امتیازگیری', 'انجام', 'باید', 'تکه', 'رفرال', 'قابلیت', 'مرداد', 'هایِ', 'وگرنه', 'کار نمی\u200cکند', '🎛 دکمه\u200cها و تنظیمات', '🏅 امتیازگیری با \nرفرال'), '🏅 **امتیازگیری با رفرال** — دو تکه، هر دو لازمه وگرنه «کار نمی‌کند»:\n  ۱) روشن‌کردن: «🎛 دکمه‌ها و تنظیمات» → «🏅 امتیازگیری با رفرال» → فعال، «امتیازِ هر رفرال» (پیش‌فرض ۱)، «نوعِ جایزه».\n  ۲) وصل به دکمه: «🏠 مدیریت دکمه‌های صفحه اصلی» → دکمه → «📜 قوانین و دسترسی و تستِ این دکمه» → «🔒 گیتِ دسترسی با زیرمجموعه» → عدد → «🏅 امتیازِ رفرال».\n  کارکرد: هر زیرمجموعه امتیاز می‌ده، امتیازها **جمع می‌شن**؛ هر استفاده به‌اندازهٔ همون عدد کم می‌شه، باقی می‌مونه. آستانهٔ ۳: با ۳ امتیاز یه بار، با ۶ دو بار. (فرقش با «لینکِ اختصاصی»: اون بعدِ هر استفاده صفر می‌شه.)\n  سه نوعِ جایزه:\n   • ⚙️ کانفیگِ خودکار (از پنل): بارِ اول ساخته می‌شه؛ دفعاتِ بعد '),
    (('بالا', 'حجم/روزش', 'رود', 'شارژ', 'قبلی', 'کانفیگِ'), '**همان کانفیگِ قبلی شارژ می‌شه** (حجم/روزش بالا می‌ره)، نه کانفیگِ تازه.'),
    (('آمادهٔ', 'انبار', 'بار', 'تحویل', 'جداگانه', 'کانفیگِ'), '   • 📦 کانفیگِ آمادهٔ انبار: هر بار یه کانفیگِ **جداگانه** — شرطش موجودبودنِ همون حجم/روز در انبار.\n   • 📝 متنِ ثابت.\n  اگه جایزه تحویل نشه (انبار خالی/خطای پنل)، امتیازِ کسرشده برمی‌گرده.'),
    (('VPN', 'مرزبان', 'نوع', 'پاسارگاد', 'پنل پاسارگاد #2', 'چند', '➕ ثبت تنظیمات', '➕ ثبت پنل دیگر از نوع …', '📦 سرویس\u200cها و محصولات', '🤖 پنل VPN \nخودکار'), '🛰 **چند پنل از یک نوع** — قبلاً فقط یه پاسارگاد (یا یه مرزبان...) قابلِ ثبت بود؛ حالا هر تعداد. مسیر: «📦 سرویس‌ها و محصولات» → «🤖 پنل VPN خودکار» → پنلِ متصل → «➕ ثبت پنل دیگر از نوع …» → «➕ ثبت تنظیمات».\nپنلِ دوم با نامِ «پنل پاسارگاد #2» می‌آد، پلن‌ها/قیمت‌گذاری/تمدید/روشن‌خاموش کاملاً جداست. پنل و پلن‌هایِ قبلی دست‌نخورده می‌مونن.'),
    (('انتخابِ', 'جهت', 'رباتِ', 'فروش', 'مالکِ', 'همکار', '⚙️ تنظیمات', '🤖 انتخاب ربات\u200cها جهت فروش'), '🤖 **انتخابِ ربات‌ها جهت فروش (فقط رباتِ همکار)** — مالک تعیین می‌کنه کدوم نوعِ ربات به مشتری‌ها نشون داده بشه. مسیر: پنل همکار → «⚙️ تنظیمات» → «🤖 انتخاب ربات‌ها جهت فروش» → تیکِ نوع‌ها. همه‌جا اثر می‌ذاره: خرید، تغییرِ نوع، ویزاردِ شخصی‌سازیِ دکمه‌ها. بدونِ تیک، همه نمایش داده می‌شن.'),
    (('خرید', 'شارژِ', 'صفحهٔ', 'موجودیِ', 'نمایش در خریدها', 'پول', 'کیف'), '👛 **شارژِ کیف پول در صفحهٔ خرید** — با موجودیِ کم، فقط روش‌هایِ روشن پیشنهاد می‌شن (کارت‌به‌کارت، تون‌پیز، ارز دیجیتال، سواپ‌ولت). کارت‌به‌کارتِ خاموش دیگه نشون داده نمی‌شه. تون‌پیز یه تیکِ جدا داره («نمایش در خریدها»)، مستقل از نمایشش تو کیف پول. اگه گفت «درگاهم تو خرید نمایش داده نمی‌شه»، اول همینو چک کن.'),
    (('حجمِ', 'سرویسم ۰ گیگ نشان می\u200cدهد', 'سرویس\u200cهای من', 'نامحدود', 'نمایش', 'کانفیگِ', '♾️', '♾️ نامحدود'), '♾️ **کانفیگِ نامحدود** — با «♾️ نامحدود» نمایش داده می‌شه. اگه گفت «سرویسم ۰ گیگ نشون می‌ده»، این باگِ نمایشی رفع شده؛ کافیه ربات به‌روز باشه و «سرویس‌های من» دوباره باز بشه. ۰ گیگ یعنی نامحدود، نه تمام‌شده.'),
    (('Bot', 'BotFather', 'Fragment', 'Payments', 'Settings', 'TON', 'استارزِ', 'استارزی', 'تلگرام', 'سوال', 'پرسیده'), '⭐ **استارزِ تلگرام** — سوالش زیاده: استارزی که مشتری می‌ده به حسابِ استارزِ **خودِ همون ربات** می‌ره، نه بات‌ساز؛ بات‌ساز سهمی برنمی‌داره. برداشت: @BotFather → ربات → Bot Settings → Payments، به TON از طریقِ Fragment، با دورهٔ انتظارِ حدودِ ۲۱ روز. نرخِ تبدیلِ استارز به تومان رو مالک تنظیم می‌کنه: «💰 مالی و کیف پول» → «⭐ پرداخت با استارز تلگرام».'),
    (('QR', 'آپلود', 'بارکد', 'تواند', 'دلخواه', 'عکس', 'عکسِ', 'مالکِ', 'پشتِ', '🎛 دکمه\u200cها و \nتنظیمات', '🖼 عکسِ پشتِ بارکد (QR'), '🖼 **عکسِ دلخواه پشتِ بارکد** — مالک یه عکس آپلود می‌کنه تا همهٔ QRهایِ ربات روش ساخته بشن (بارکد وسطِ یه کارتِ سفیدِ گرد می‌شینه که حتماً اسکن بشه). مسیر: پنل مدیریت → «🎛 دکمه‌ها و تنظیمات» → «🖼 عکسِ پشتِ بارکد (QR)». بعدِ آپلود پیش‌نمایش + دکمهٔ حذف. رویِ همهٔ بارکدها اثر داره: کانفیگِ پنلِ خودکار، انبار، ساب‌لینک، «سرویس‌های من». عکسِ مربعی بهترین نتیجه رو می‌ده؛ غیرمربع از وسط برش می‌خوره.'),
    (('اتمامِ', 'حجم', 'حجمت رو به اتمام است', 'سرویسِ', 'قبلاً', 'هشدارِ', 'هوشمند', 'کوچک', 'گیگ'), '🔔 **هشدارِ اتمامِ حجم هوشمند شد** — قبلاً سرویسِ کوچک (مثلاً ۲ گیگ) از همون لحظهٔ خرید با صفر مصرف پیامِ «حجمت رو به اتمام است» می‌گرفت. حالا تا وقتی مصرفی نباشه هشداری نمی‌ره، و آستانه برایِ بسته‌هایِ کوچک نسبیه (۲۵٪ حجمِ بسته): بستهٔ ۲گیگ در ۰.۵گیگ، بستهٔ ۵۰گیگ همون ۲گیگِ تنظیمِ مالک. اگه از هشدارِ زودهنگام شکایت کرد، بگو در نسخهٔ جدید رفع شده.'),
    (('/سکوت ۳۰ توهین', 'اخطار', 'ادمینِ', 'اسلش', 'انبن', 'بدونِ', 'بن', 'بن شدی', 'تواند', 'دستورهایِ', 'رفع سکوت', 'سکوت', 'شخص', 'پاک کردن اخطار', 'پیامِ'), '👮 **دستورهایِ گروه بدونِ اسلش** — ادمین رویِ پیامِ شخص ریپلای می‌کنه و فقط می‌نویسه: «بن»، «انبن»، «اخطار»، «پاک کردن اخطار»، «سکوت»، «رفع سکوت». باید تنها همون کلمه باشه (تا «بن شدی؟» کسی رو بن نکنه)؛ برایِ دلیل/مدت شکلِ اسلش‌دار: «/سکوت ۳۰ توهین».'),
    (('/start', 'start', 'بات', 'جواب', 'دهد', 'رباتِ', 'رباتْ تو گروه استارت نمی\u200cخوره', 'ساز', 'عمداً', 'منویِ', 'نمی'), '👥 **رباتِ بات‌ساز در گروه جواب نمی‌ده** — «/start» و منوی بات‌ساز عمداً فقط تو چتِ خصوصی کار می‌کنن تا گروه شلوغ نشه. ولی بازی‌هایِ گروهی (تاس، دارت، بولینگ، بسکتبال)، قرعه‌کشی، نگهبانِ گروه سرِ جاشونن. اگه گفت «رباتْ تو گروه استارت نمی‌خوره»، بگو عمدیه و برایِ پنل باید بره خصوصیِ ربات.'),
    (('VPN', 'تازهٔ', 'ساختِ ربات', 'فروشِ', 'فروشگاهِ', 'مدیریتِ', 'نوعِ', 'کنارِ', '🛡 مدیریتِ \nگروه و کانال'), '🛡 **نوعِ تازهٔ ربات: مدیریتِ گروه و کانال** — کنارِ فروشِ VPN/فروشگاهِ دیجیتال/ربات تبلیغاتی، موقعِ «ساختِ ربات» می‌شه «🛡 مدیریتِ گروه و کانال» رو انتخاب کرد. مالک این ربات رو ادمینِ گروه/کانالِ خودش می‌کنه و از پی‌ویِ همون ربات («📦 سرویس‌ها و محصولات → 🛡 مدیریت گروه و کانال») تنظیم می‌کنه — «مشتری»ای در کار نیست، مستقیم برایِ خودِ مالک. قابلیت‌ها: 🚫 ضداسپم (حذفِ لینک/فوروارد/پیامِ تکراری، واکنشِ قابلِ‌تنظیم: فقط حذف/حذف+اخطار/حذف+سکوت)، 👋 خوشامدگویی ({نام}/{گروه})، 🧹 فیلترِ کلمات، 👮 مدیریتِ اعضا (بن/اخطار/سکوت با ریپلای، مثلِ محافظِ گروهِ بات‌ساز)، 📅 زمان‌بندیِ پست، 📋 نظرسنجی. ضداسپم/خوشامد/فیلتر/مدیریتِ اعضا از پلنِ '),
    (('اند', 'بندیِ', 'رایگان', 'ربات کار نمی\u200cکند', 'رشد/حرفه', 'زمان', 'نظرسنجی', 'پست', 'پلنِ', '❓ راهنمایِ اتصال', '📊 آمار', '🔍 بررسیِ \nدسترسیِ ربات'), '**رایگان** هم فعالن؛ فقط زمان‌بندیِ پست و نظرسنجی پلنِ رشد/حرفه‌ای می‌خوان. تو پنلِ همون ربات یه «❓ راهنمایِ اتصال» و «🔍 بررسیِ دسترسیِ ربات» هست که زنده از تلگرام می‌پرسه ربات واقعاً ادمین هست و چه دسترسی‌هایی داره — اگه گفت «ربات کار نمی‌کند»، اول همینو بگو بزنه. «📊 آمار» هم هست: چند پیام حذف شد، چند نفر بن/اخطار شد.'),
    (('ترکیبی', 'خرید', 'خریدِ', 'دسته/پنل', 'دکمهٔ', 'قبلاً', 'نامحدود', 'چند'), '🧩 **دکمهٔ خریدِ ترکیبی (چند دسته/پنل با هم)** — قبلاً هر دکمهٔ خرید فقط به یه پنل/دستهٔ انبار محدود بود. حالا می‌شه چند دستهٔ پنلِ خودکار (مثلاً یوپنل+پاسارگاد) و یه/چند دستهٔ انبارِ کانفیگِ آماده (مثلاً «نامحدود») رو با هم زیرِ **یه دکمه** جمع کرد. مسیر: دکمهٔ بی‌عملکرد → «🎯 عملکرد دکمه» → «🧩 خرید ترکیبی (چند دسته/پنل با هم)» → یکی از دسته‌هایِ ترکیبیِ موجود، یا اگه نداره «➕ ساختنِ دستهٔ تازه» (می‌ره به صفحهٔ دسته‌بندیِ مینی‌اپ؛ دسته بساز، پلن‌ها/دسته‌هایِ انبار زیرش بذار، برگرد و از «🔁 تغییرِ دستهٔ ترکیبی» انتخابش کن). برایِ مشتری: دستهٔ تک‌منبعی مستقیم لیستِ پلن‌ها رو باز می‌کنه؛ چندمنبعی اول می‌پرسه کدوم منبع.'),
    (('SMM', 'بازنویسیِ', 'خدماتِ', 'خیلی', 'سوالش', 'فروشِ سرویسِ SMM', 'مجازی', 'پنلِ', '📦 سرویس\u200cها و محصولات', '🛰 پنل SMM'), '⭐️ **پنلِ خدماتِ مجازی (SMM)** — سوالش خیلی زیاده: دیگه فقط «فروشِ سرویسِ SMM» نیست؛ «📦 سرویس‌ها و محصولات» → «🛰 پنل SMM» چند محصولِ جدا داره که هرکدوم تنظیماتِ خودش رو داره:'),
    (('انتخاب', 'اول', 'تلگرام', 'فروشِ', 'گیفت', 'گیفتِ', '🎁 فروشِ گیفتِ تلگرام'), '  ▪️ **گیفتِ تلگرام** — «🎁 فروشِ گیفتِ تلگرام». مشتری اول گیفت رو انتخاب می‌کنه، بعد (برایِ گیفتِ رسمی/استارزی، نه سفارشی) یه صفحهٔ '),
    (('انتخابِ', 'برایِ خودم', 'برایِ کاربرِ دیگر', 'بیند', 'تعداد', 'دقیقاً', 'رقبا', 'مثلِ', 'نمونه', 'هایِ', '🎨 \nدکمه\u200cهایِ رنگی', '🔻/🔺'), '**انتخابِ تعداد** با 🔻/🔺 (مثلِ نمونه‌هایِ رقبا)، بعد «برایِ خودم» یا «برایِ کاربرِ دیگر». اگه «🎨 دکمه‌هایِ رنگی» روشن باشه، دکمه‌هایِ کاتالوگِ گیفت سبز می‌شن.'),
    (('تلگرام', 'فروشِ', 'پرمیومِ', 'پلنِ', '۳/۶/۱۲ماهه', '🌟 فروشِ پرمیومِ تلگرام'), '  ▪️ **پرمیومِ تلگرام** — «🌟 فروشِ پرمیومِ تلگرام». سه پلنِ ۳/۶/۱۲ماهه همیشه رنگی‌اند (آبی).'),
    (('API', 'استارزِ', 'بوده', 'تلگرام', 'خام', 'دستی', 'فروشِ', 'همیشه', '⭐ فروشِ استارزِ تلگرام'), '  ▪️ **استارزِ خام** — «⭐ فروشِ استارزِ تلگرام». همیشه دستی (تلگرام APIای برایِ فرستادنِ مستقیمِ N استارز نداره).'),
    (('استارزیِ', 'اضافه', 'اکشنِ', 'پست', '⭐️', '⭐️ ری\u200cاکشنِ استارزیِ \nپست'), '  ▪️ **ری‌اکشنِ استارزیِ پست** — «⭐️ ری‌اکشنِ استارزیِ پست». فروشِ استارز برایِ ری‌اکشن‌گذاشتنِ رویِ پستِ کانال/گروه، نه فرستادن به شخص. مشتری تعداد (حداقلِ قابلِ‌تنظیم، پیش‌فرض ۵) و لینکِ پست می‌ده؛ کانال باید عمومی باشه و ری‌اکشنِ ⭐️ روش فعال باشه. همیشه دستی — ادمین ری‌اکشن‌ها رو خودش ثبت و تو پنل تایید می‌کنه. تنظیماتِ نرخ/حداقل زیرِ همین دکمه. برایِ دیدنِ مشتری‌ها، مثلِ هر دکمه از «🏠 مدیریت دکمه‌های صفحه اصلی» بسازش و «🎯 عملکرد دکمه»اش رو همین بذار.'),
    (('تحویلِ', 'تحویلِ \nدستی وقتِ نبودِ انبار', 'حالتِ', 'شمارهٔ', 'مجازی', 'مدیریتِ', 'موجود نیست', '📱 مدیریتِ شمارهٔ مجازی'), '  ▪️ **شمارهٔ مجازی** — «📱 مدیریتِ شمارهٔ مجازی». یه حالتِ «تحویلِ دستی وقتِ نبودِ انبار» هم داره: اگه برایِ یه کشور تو انبار شماره نمونده، مشتری بازم می‌تونه بخره و ادمین دستی شماره می‌فرسته (به‌جایِ «موجود نیست»).'),
    (('الگویِ', 'تحویلِ', 'تحویلِ دستی', 'حالتِ تحویلِ دستی', 'دستی', 'شمارهٔ', 'عنوانِ', 'مشترک', 'پرمیوم', 'گیفت', '✅ انجام \nشد / ❌ رد'), '  ▪️ **«تحویلِ دستی» به‌عنوانِ الگویِ مشترک** — گیفت، پرمیوم، شمارهٔ مجازی هرکدوم یه سوییچِ جدا («حالتِ تحویلِ دستی») دارن: روشن باشه، سفارش بعدِ پرداخت می‌ره پیشِ ادمین («✅ انجام شد / ❌ رد») به‌جایِ تحویلِ خودکار با استارزِ ربات. دلیلش معمولاً کم‌بودنِ موجودیِ استارزِ رباته. اگه گفت «حالتِ دستی رو روشن کردم ولی بازم می‌گه موجود نیست»، چک کن نسخهٔ ربات به‌روز باشه — قبلاً باگی بود که حتی با دستیِ روشن، فروشگاه بر اساسِ موجودیِ استارزِ ربات فیلتر می‌کرد.'),
    (('SMM', 'حالتِ قیمت\u200cگذاریِ \nدستی', 'سرویس', 'قیمت', 'محور', 'هایِ', 'گذاریِ', '💵 قیمتِ \nدستی', '🗂 سرویس\u200cهای فعال برای \nفروش'), '  ▪️ **قیمت‌گذاریِ سرویس‌هایِ SMM (پنل‌محور)** — «🗂 سرویس‌های فعال برای فروش». هر سرویس (پنلی یا دستی) یه دکمهٔ «💵 قیمتِ دستی» جداگانه داره — دیگه لازم نیست کلِ ربات رو رویِ «حالتِ قیمت‌گذاریِ دستی» بذاری برایِ یه محصول؛ هر سرویس مستقل تصمیم می‌گیره. بدونِ قیمتِ دستی، خودکار از «قیمتِ پنل × درصدِ سود» حساب می‌شه. کنارش «📝 توضیحات» هم هست، قبل از خرید به مشتری نشون داده می‌شه.'),
    (('آخرِ', 'رقمِ', 'فرستادنِ', 'موقعِ', 'کارت', 'کارتِ', 'گیفت/استارز'), '  ▪️ **کارت‌به‌کارتِ گیفت/استارز — ۴ رقمِ آخرِ کارت** — موقعِ فرستادنِ عکسِ رسید، باید ۴ رقمِ آخرِ کارتِ پرداخت‌کننده رو هم تو کپشنِ همون عکس بنویسه؛ بدونش ربات دوباره می‌خواد. فقط مالِ کارت‌به‌کارته، کیف‌پول کاری نداره.'),
    (('استارزِ', 'خام', 'خریدِ', 'محاسبه', 'محاسبه با موجودیِ من', 'من', 'موجودیِ'), '  ▪️ **«محاسبه با موجودیِ من»** — هم تو خریدِ استارزِ خام، هم ری‌اکشنِ استارزی، یه دکمه هست که با موجودیِ فعلیِ کیف‌پولِ مشتری حساب می‌کنه تقریباً چند واحد می‌تونه بخره.'),
    (('آمدگوییِ', 'تایپ', 'تجربهٔ', 'حرفه', 'خوش', 'شونده', 'قبلاً', 'قوانین', 'ورودِ', '🎓 تجربهٔ ورودِ \nحرفه\u200cای', '🎛 دکمه\u200cها و تنظیمات'), '🎓 **تجربهٔ ورودِ حرفه‌ای (قوانین + خوش‌آمدگوییِ تایپ‌شونده)** — قبلاً فقط مخصوصِ رباتِ خدماتِ مجازی بود، حالا برایِ هر نوع رباتی. مسیر: پنل مدیریت → «🎛 دکمه‌ها و تنظیمات» → «🎓 تجربهٔ ورودِ حرفه‌ای». روشن که باشه، اول کاربرِ تازه متنِ قوانین می‌بینه و تاییدش می‌کنه (پین هم می‌شه)، بعد خوش‌آمدگوییِ دلخواه کلمه‌به‌کلمه «تایپ می‌شه». هر دو متن و روشن/خاموشیِ جلوهٔ تایپ‌شونده قابلِ تنظیمه.'),
    (('امتیازگیری', 'دکمه\u200cها و \nتنظیمات', 'رباتِ', 'رفرال', 'قبلاً', 'همکار', '🏠 بازگشت به خانه'), '🏅 **امتیازگیری با رفرال، حالا در رباتِ همکار هم هست** — قبلاً پلنِ همکار این رو نداشت. الان اضافه شده: پنلِ ربات همکار → «🏠 بازگشت به خانه» → پنلِ اصلیِ ادمین → «🎛 دکمه‌ها و تنظیمات» → «🏅 امتیازگیری با رفرال».'),
    (('دیده', 'ساختِ', 'قابلیتِ', 'نظرسنجی', 'همکار', 'پنلِ', '📢 ارسال به کانال و \nگروه'), '📊 **نظرسنجی، حالا در پنلِ ربات همکار هم هست** — ساختِ نظرسنجی و ارسالش به گروه، الان یه دکمهٔ مستقیم تو پنلِ ربات همکار هم داره، کنارِ «📢 ارسال به کانال و گروه».'),
    (('دقیقاً', 'پلن'), '# 💳 پلن‌ها — دقیقاً چه چیزی در هر کدام است\nاین سوال زیاد پرسیده می‌شه؛ دقیق و کامل جواب بده، نه فقط یه جمله.'),
    (('QR', 'رایگان', 'مدیریتِ گروه و کانال', 'پایه', 'پول', 'کارت', 'کیبوردی/اینلاین', 'کیف'), '🆓 **رایگان (پایه)** — دکمه‌های کیبوردی/اینلاین، کیف پول، کارت‌به‌کارت، انبارِ دستیِ کانفیگ، خریدِ +/-، پنلِ مدیریت، مدیریتِ کاربران، تحویلِ کانفیگ+QR (ساده)، ویرایشِ متن/دکمه‌ها، کانالِ اعتمادسازی، کانفیگِ وایرگارد. رباتِ «مدیریتِ گروه و کانال» هم از همین پلن کار می‌کنه: ضداسپم، خوشامدگویی، فیلترِ کلمات، بن/اخطار همه رایگان.\n❌ ندارد: اتصال به پنلِ VPN خودکار (مهم‌ترین محدودیت — رایگان فقط با انبارِ دستی می‌فروشه، نه اتصالِ زنده به مرزبان/پاسارگاد/…)، امتیازگیری با رفرال، عکسِ پشتِ بارکد، خریدِ ترکیبی، چند پنلِ هم‌نوع.'),
    (('VPN', 'اتصال', 'خودکار', 'خودکارِ', 'رایگان', 'رشد', 'ساختِ', 'علاوهٔ', 'همهٔ', 'پنلِ'), '🌱 **رشد** — همهٔ رایگان + اتصال به پنلِ VPN خودکار (ساختِ خودکارِ کانفیگ از مرزبان/پاسارگاد/هیدیفای/…)، دکمه‌هایِ رنگی، کدِ تخفیف، نظرسنجی، زیرمجموعه‌گیریِ معمولی، امتیازگیری با رفرال، پیام‌هایِ خودکار، نظر و امتیازدهی، ارسالِ همگانیِ با مدیا، منویِ سفارشی، ویرایشِ پیامِ خوش‌آمد (و ری‌اکشن رویِ پیامِ استارت، چون وابسته است)، تعویضِ توکن/انتقالِ مالکیت.\n❌ ندارد: چند پنلِ هم‌نوع (فقط یک پاسارگاد، نه دو/سه‌تا)، عکسِ پشتِ بارکد، خریدِ ترکیبی، درگاهِ پرداختِ آنلاین (ارزِ دیجیتال/استارز)، فروشگاهِ محصولاتِ دیجیتال، نوبت‌دهی، آمارِ پیشرفته، برچسب‌گذاریِ کاربران، ارسالِ زمان‌بندی‌شده، ارسالِ پست به کانال با دکمهٔ اینلاین، پرداخت مستقیم ارز، و (در رباتِ VPN) تنظیماتِ گروه.'),
    (('حرفه', 'درگاهِ', 'رشد', 'علاوهٔ', 'نیست', 'همهٔ', 'چیزهایی'), '💎 **حرفه‌ای** — همهٔ رشد + چیزهایی که تو رشد نیست: درگاهِ پرداختِ آنلاین، فروشگاهِ محصولاتِ دیجیتال، نوبت‌دهی، آمارِ پیشرفته (با رتبه‌بندیِ برترینِ مشتریان و تخفیفِ خودکار براشون)، برچسب‌گذاریِ کاربران، ارسالِ زمان‌بندی‌شده، ارسالِ پست به کانال با دکمهٔ اینلاین، **چند پنلِ هم‌نوع** (پاسارگادِ ۱/۲/۳ هم‌زمان)، **عکسِ دلخواه پشتِ بارکد**، **خریدِ ترکیبی**، **پرداخت مستقیم ارز '),
    (('VPN', 'تنظیماتِ', 'تون/ترون/تتر', 'خودِ', 'رباتِ', 'ولتِ'), '(تون/ترون/تتر به ولتِ خودِ مالک)**، و برایِ رباتِ VPN **⚙️ تنظیماتِ گروه** (محافظِ گروه، انتقالِ موجودی، شرط‌بندی — تو رباتِ خدماتِ مجازی این از همهٔ پلن‌ها در دسترسه). یعنی حرفه‌ای همه‌چیزِ رشد رو داره.'),
    (('بندیِ', 'جداست', 'دسته', 'رایگان/رشد/حرفه', 'همکار', 'کاملاً'), '🤝 **ربات همکار** یه دسته‌بندیِ کاملاً جداست (نه رایگان/رشد/حرفه‌ای): کیف پول، کارت‌به‌کارت، درگاهِ پرداخت، پنلِ مدیریت، آمارِ پیشرفته، زیرمجموعه‌گیری، کدِ تخفیف، ارسالِ همگانی، ارسال به کانال، نظرسنجی، امتیازگیری با رفرال، ویرایشِ خوش‌آمد — مخصوصِ کسی که خودش ربات به دیگران می‌فروشه، نه کسی که VPN/محصول می‌فروشه.\n\nاگه پرسید «چرا فلان دکمه رو نمی‌بینم» یا «چرا قفله»، این لیست رو چک کن — احتمالاً پلنش پایین‌تر از نیازِ اون قابلیته؛ بگو کدوم پلن باید بگیره.'),
    (('VPN', 'آگوست', 'ترین', 'ربات', 'ربات همکار', 'شهریور', 'فرق', 'قابلیت', 'قاطی', 'نمایندگیِ', 'نمایندگیِ VPN', 'نکن', 'همکار', 'پنلِ'), '# 🆕 تازه‌ترین قابلیت‌ها (شهریور ۱۴۰۵ / آگوست ۲۰۲۶)\n🏷 **پنلِ نمایندگیِ VPN (فرق دارد با «ربات همکار» — قاطی نکن!)**\n«ربات همکار» یعنی فروشِ *ربات*. «نمایندگیِ VPN» یعنی مالکِ رباتِ VPN به یه کاربرِ خودش تخفیف و پنل می‌ده تا اون کانفیگ بفروشه.\n  ▪️ ساختنِ نماینده (کارِ مالکِ ربات): «📦 سرویس‌ها و محصولات» → «🤖 پنل VPN خودکار» → «🤝 حساب‌های همکار (تخفیف)» → افزودن با آیدیِ عددی، تعیینِ درصد و پنل‌هایِ مجاز.'),
    (('تحتِ', 'نماینده', 'وبِ', 'پنلِ', '🖥 بازکردنِ پنل', '🖥 پنلِ وبِ نماینده'), '  ▪️ **پنلِ تحتِ وبِ نماینده**: همون صفحه → «🖥 پنلِ وبِ نماینده» → ساختِ نام‌کاربری و رمز. رمز فقط یه‌بار نشون داده می‌شه. آدرس تو پیام نوشته نمی‌شه؛ «🖥 بازکردنِ پنل» پنل رو تو خودِ تلگرام باز می‌کنه.'),
    (('دکمهٔ', 'شدنِ', 'محضِ', 'من', 'نمایندگی', '🏷 پنل نمایندگی من', '📱 کانفیگ\u200cهای ساخته\u200cشده'), '  ▪️ **دکمهٔ «🏷 پنل نمایندگی من»**: به‌محضِ فعال‌شدنِ نمایندگی، خودکار تو منویِ همون کاربر ظاهر می‌شه (بقیه نمی‌بینن). داخلش: موجودیِ کیف پول، درصدِ تخفیف، پنل‌هایِ مجاز، سهمیهٔ تست، نام‌کاربریِ پنلِ وب، «📱 کانفیگ‌های ساخته‌شده».'),
    (('آماده', 'آنلاین', 'تحتِ', 'فروشِ', 'پلنِ', 'پنلِ', 'کار'), '  ▪️ تو پنلِ تحتِ وب سه کار می‌کنه: **پلنِ آماده**، **فروشِ آنلاین** '),
    (('تستِ', 'حجم/روزِ', 'دلخواه', 'رایگان'), '(حجم/روزِ دلخواه) و **تستِ رایگان**.'),
    (('ساخته', 'سرویس', 'سرویس\u200cهای من', 'شخصی', 'نمایندگی', 'هایِ', 'کانفیگ', '📱 کانفیگ\u200cهای ساخته\u200cشده'), '  ▪️ **کانفیگ‌هایِ نمایندگی از سرویس‌هایِ شخصی جداست** — ساخته‌هایِ نمایندگی تو «📱 کانفیگ‌های ساخته‌شده» می‌آن، نه «سرویس‌های من».'),
    (('تستِ', 'روزانهٔ', 'سهمیهٔ', 'عددِ', 'مالکِ', 'نماینده', '🎁 سهمیهٔ تستِ روزانه', '🖥 پنلِ وبِ \nنماینده'), '🎁 **سهمیهٔ تستِ روزانهٔ هر نماینده** — مالکِ ربات برایِ هر نماینده عددِ جدا می‌ذاره (یکی روزی ۵، یکی روزی ۱۰). مسیر: همون «🖥 پنلِ وبِ نماینده» → «🎁 سهمیهٔ تستِ روزانه». عددِ ۰ یعنی اجازهٔ تست نداره (پیش‌فرض). سهمیه هر شبانه‌روز از نو پر می‌شه و با حذفِ کانفیگ برنمی‌گرده. مشخصاتِ تست (حجم/مدت) همون تنظیماتِ تستِ رایگانِ رباته.'),
    (('دکمهٔ', 'مخاطبِ', 'منویِ', 'نماینده', 'پلن'), '👤 **مخاطبِ هر پلن (منویِ جدا برایِ نماینده)** — رویِ هر پلن یه دکمهٔ '),
    (('VPN', 'بین', 'حالت', 'مخاطب', 'همه', 'چرخد', '👤 مخاطب', '📦 پلن\u200cهای فروش'), '«👤 مخاطب» هست که بینِ سه حالت می‌چرخه: **همه** / **فقط مشتری** / **فقط نماینده**. مسیر: پنلِ VPN → «📦 پلن‌های فروش» → پلن → «👤 مخاطب». این‌طوری می‌شه برایِ نمایندگان پلن و قیمتِ کاملاً جدا طراحی کرد، به‌جایِ اینکه فقط درصدِ تخفیف رویِ پلن‌هایِ مشتری بخوره. پیش‌فرض «همه».'),
    (('VPN', 'تعرفهٔ', 'مخصوصِ', 'نماینده', 'نمایندگان', 'کسانی', 'گیگی', 'گیگیِ', '🏷 تعرفهٔ \nنمایندگان (گیگ/روز', '🛒 فروش آنلاین'), '🏷 **تعرفهٔ گیگیِ مخصوصِ نمایندگان** — برایِ کسانی که به نماینده گیگی می‌فروشن نه پلنی. مسیر: پنلِ VPN → «🛒 فروش آنلاین» → «🏷 تعرفهٔ نمایندگان (گیگ/روز)». دو عدد با فاصله می‌فرسته. نکتهٔ مهم: این تعرفه '),
    (('تخفیف', 'تخفیفِ', 'جایِ', 'درصدِ', 'دوبار', 'نشیند', 'نمایندگی', 'وگرنه', '۰ ۰'), '**به‌جایِ** درصدِ تخفیفِ نمایندگی می‌شینه، نه رویِ اون — وگرنه تخفیف دوبار حساب می‌شد. با فرستادنِ «۰ ۰» برداشته می‌شه و به حالتِ درصدی برمی‌گرده.'),
    (('خریدار', 'زرین', 'فروشنده', 'پال', 'پرداخت', 'پرداخت\u200cکننده', 'کارمزد', 'کارمزد از خریدار', 'کارمزدِ', 'کننده', 'گزینهٔ', '💰 مالی و کیف پول', '💸 کارمزد', '🔐 درگاه زرین\u200cپال'), '💸 **کارمزدِ زرین‌پال از پرداخت‌کننده** — زرین‌پال گزینهٔ «کارمزد از خریدار» رو از پنلش برداشت. حالا تو ربات: «💰 مالی و کیف پول» → «🔐 درگاه زرین‌پال» → دکمهٔ «💸 کارمزد» که بینِ «فروشنده» و «پرداخت‌کننده» می‌چرخه. رویِ پرداخت‌کننده باشه، کارمزد از زرین‌پال استعلام و به مبلغ اضافه می‌شه؛ کیفِ پولِ کاربر **همون مبلغِ اصلی** شارژ می‌شه، نه با کارمزد.'),
    (('#2', 'VPN', 'جایِ', 'دلخواه', 'سرور آلمان', 'مرزبان', 'نامِ', 'نمایشیِ', 'پنل', 'پنل مرزبان #2', '🏷 نام نمایشی پنل'), '🏷 **نامِ نمایشیِ دلخواه برایِ پنل‌ها** — به‌جایِ «پنل مرزبان #2» می‌شه گذاشت «سرور آلمان». مسیر: پنلِ VPN → پنل → «🏷 نام نمایشی پنل». فقط نامِ نمایشی عوض می‌شه؛ کانفیگ‌ها، ساب‌لینک‌ها، پلن‌ها دست‌نخورده می‌مونن. دکمهٔ بازگشت به نامِ پیش‌فرض هم داره.'),
    (('اصلی', 'بازگشت', 'بازگشت به صفحهٔ اصلی', 'صفحهٔ', 'متنِ', 'مخفی', 'ویرایشِ', '✏️ ویرایشِ متن', '🎛 دکمه\u200cها و \nتنظیمات', '🏠 متنِ بازگشت به صفحهٔ اصلی'), '🏠 **مخفی‌کردن و ویرایشِ متنِ «بازگشت به صفحهٔ اصلی»** — «🎛 دکمه‌ها و تنظیمات» → «🏠 متنِ بازگشت به صفحهٔ اصلی». هم «✏️ ویرایشِ متن» داره هم '),
    (('سازی', 'شیشه', 'مخفی', 'منویِ', 'مهم', 'نکتهٔ', 'پیام', 'کردنِ', '🙈 مخفی\u200cکردنِ این پیام'), '«🙈 مخفی‌کردنِ این پیام». نکتهٔ مهم: تو منویِ **شیشه‌ای** مخفی‌سازی کامل کار می‌کنه (دکمه‌ها رویِ پیامِ موجود می‌شینن)، ولی تو منویِ **کیبوردی** یه پیامِ بدونِ متن باقی می‌مونه — چون تلگرام کیبوردِ پایین رو فقط همراهِ یه پیام عوض می‌کنه. محدودیتِ تلگرامه، نه باگ.'),
    (('ثبتِ', 'مالی', 'پول', 'کانالِ', 'کیف', '💰 مالی و کیف پول', '📥 کانالِ ثبتِ \nشماره\u200cها'), '📥 **کانالِ ثبتِ شماره‌ها** — «💰 مالی و کیف پول» → «📥 کانالِ ثبتِ شماره‌ها». آیدیِ عددیِ کانالِ خصوصی رو می‌ده و هر بار کاربری شماره‌اش رو تایید کنه، نام/یوزرنیم/آیدی/شماره/تاریخ/موجودی تو اون کانال ثبت می‌شه. ربات باید اونجا ادمین باشه (موقعِ تنظیم یه پیامِ آزمایشی می‌فرسته). «تاییدِ شماره پیش از پرداخت» حالا رویِ همهٔ مسیرهایِ خرید اعمال می‌شه، نه فقط شارژِ کیف پول.'),
    (('افزودن', 'بات', 'روز', 'ساز', 'ساعت', 'مدیریتِ', 'پنلِ', '⚡️ سرور فعال\u200cسازی', '🤖 \nربات\u200cهای فعال'), '⏱ **افزودن ساعت (نه فقط روز)** — تو پنلِ مدیریتِ بات‌ساز، هم «🤖 ربات‌های فعال» هم «⚡️ سرور فعال‌سازی»، کنارِ دکمه‌هایِ روز، دکمه‌هایِ افزودن/کسرِ **ساعت** هم هست؛ برایِ تمدیدهایِ کوتاه و تست.'),
    (('آمار', 'انبارِ', 'تاریخچهٔ', 'همکار', 'پنلِ', 'کلی', '📊 آمار کلی', '📜 \nتاریخچهٔ انبار'), '📜 **تاریخچهٔ انبارِ ربات همکار** — پنلِ همکار → «📊 آمار کلی» → «📜 تاریخچهٔ انبار». هر کم‌وزیادِ موجودی با تاریخ و دلیلش (خرید، فروش، ارتقا، تنظیمِ مدیر). نکتهٔ پرتکرار: وقتی مشتریِ همکار رباتِ **رشد** رو به '),
    (('ارتقا', 'انبارم بی\u200cدلیل کم شد', 'حرفه', 'دهد', 'رشد', 'مصرف', 'واحد'), '**حرفه‌ای** ارتقا می‌ده، یه واحد حرفه‌ای مصرف می‌شه و یه واحد رشد به انبار برمی‌گرده. اگه گفت «انبارم بی‌دلیل کم شد»، اول همین تاریخچه رو ببینه. حدِ نصابِ ربات‌هایِ فعال دیگه عددِ ثابتِ ۱۰ نیست، مدیرِ بات‌ساز می‌تونه عوضش کنه — عددِ فعلی رو از خودِ پنل ببینه.'),
    (('تحویلِ', 'جلوگیری', 'درخواستِ قبلی\u200cات در حالِ پردازش است', 'دو تا کانفیگ گرفتم', 'دوتایی', 'سرِهم', 'پرداخت', 'پشتِ'), '🛒 **جلوگیری از تحویلِ دوتایی** — کاربر پشتِ‌سرِهم رویِ «پرداخت» بزنه، فقط یه سرویس ساخته می‌شه و پیامِ «درخواستِ قبلی‌ات در حالِ پردازش است» می‌گیره. اگه گفت «دو تا کانفیگ گرفتم»، بگو در نسخهٔ جدید رفع شده.'),
    (('بازگشت', 'بازگشتِ', 'خرید', 'دکمهٔ', 'فهرستِ', 'پلن', 'پله'), '⬅️ **بازگشتِ یک‌پله‌ای در خرید** — تو فهرستِ پلن‌ها، دکمهٔ بازگشت دیگه تا منویِ اصلی نمی‌پره؛ به دستهٔ خودش برمی‌گرده. اگه شکایت کرد، بگو رفع شده.'),
    (('انقضا', 'روز', 'روزشان', 'صفر', 'پلن', '۰ روز', '♾️ بی\u200cانقضا'), '♾️ **«۰ روز» یعنی بی‌انقضا** — پلن‌هایی که روزشون صفره حالا همه‌جا «♾️ بی‌انقضا» نشون داده می‌شن، نه «۰ روز».'),
    (('TON', 'ارز', 'تتر', 'ترون', 'ترین', 'تون', 'حرفه', 'سپتامبر', 'شهریور', 'قابلیت', 'مستقیم', 'ویژهٔ', 'پرداخت', 'پرداخت مستقیم TON', 'پلنِ'), '# 🆕 تازه‌ترین قابلیت‌ها (شهریور ۱۴۰۵ / سپتامبر ۲۰۲۶)\n💎 **پرداخت مستقیم ارز (تون / ترون / تتر) — ویژهٔ پلنِ حرفه‌ای**\nقبلاً فقط TON بود («پرداخت مستقیم TON»)؛ حالا سه ارز داره. این **درگاه نیست**: پولِ مشتری مستقیم به ولتِ خودِ مالک می‌ره و ربات واریز رو از رویِ خودِ بلاک‌چین تشخیص می‌ده — بدونِ واسطه، بدونِ کارمزدِ درگاه.\n  مسیر: پنل مدیریت → «💰 مالی و کیف پول» → «💎 پرداخت مستقیم ارز (تون/ترون/تتر)» → ارز → آدرسِ ولت رو ثبت و تایید کن → فعالش کن.\n  ▪️ هر ارز اسلاتِ جدا داره: آدرس، فعال/غیرفعال، درصدِ کارمزد، نرخِ دستی، حداقل/حداکثرِ مبلغِ شارژ، لیستِ فاکتورها، تیکِ «نمایش در کیف پول» — همه جدا.'),
    (('EQ', 'TRC20', 'TRON', 'TRX', 'UQ', 'آدرس', 'آدرسِ', 'تتر', 'ترون', 'تواند', 'تون', 'شروع', 'شوند', 'کاراکترند', 'گیرد'), '  ▪️ آدرسِ **تون** با UQ یا EQ شروع می‌شه. آدرسِ **ترون** و **تتر TRC20** هر دو با **T** شروع و ۳۴ کاراکترند (یه آدرسِ TRON هم TRX می‌گیره هم تتر TRC20، پس می‌تونه برایِ هر دو یه آدرس بزنه).'),
    (('BEP20', 'ERC20', 'Receive', 'TRC20', 'TRON', 'TRON/TRC20', 'آدرسش', 'آدرسِ', 'اتریوم', 'اصلاً', 'بایننس', 'بگذارد', 'تتر', 'تتر TRC20', 'تترم', 'تترِ', 'ترون', 'ثبت', 'ردش', 'رفت', 'سوال', 'شبکه', 'شبکهٔ', 'شروع', 'عمداً', 'قبول', 'موقعِ', 'ندارد', 'نمی', 'ولتش', 'پرتکرارترین', 'پولِ', 'چرا', 'چرا آدرسِ تترم را قبول نمی\u200cکند', 'کار'), '  ▪️ **پرتکرارترین سوال:** «چرا آدرسِ تترم رو قبول نمی‌کنه؟» — تتر شبکهٔ خودش رو نداره. «تتر TRC20» یعنی تتر رویِ شبکهٔ ترون، پس آدرسش یه آدرسِ ترونه. آدرسِ کاربر با **0x** شروع بشه، اون تترِ ERC20 (اتریوم) یا BEP20 (بایننس) هست و این‌جا کار نمی‌کنه — ربات عمداً ردش می‌کنه، چون اگه ثبت می‌شد پولِ مشتری‌ها رویِ شبکه‌ای می‌رفت که ربات چک نمی‌کنه. بگو تو ولتش موقعِ Receive شبکه رو رویِ TRON/TRC20 بذاره.\n  ▪️ ربات آدرسِ ولت رو تا مالک صریحاً تایید نکنه ذخیره نمی‌کنه، و هشدار می‌ده **آدرسِ دپوزیتِ صرافی نذاره** (چون بینِ هزاران کاربر مشترکه و پول گم می‌شه). حتماً ولتِ شخصی.\n  ▪️ برایِ مشتری: مبلغ رو به تومان می‌ده → ربات فاکتوری با آدرس و **مبلغِ دقیق و یکتا** می‌سازه (چند رقمِ آخرِ اعشار تصادفیه) → بعدِ واریز، تا کمتر از یه دقیقه کیف پول خودکار شارژ می‌شه. دکمهٔ «🔄 بررسی وضعیت پرداخت» هم هست، فوری زنجیره رو چک می‌کنه.'),
    (('TRC20', 'memo', 'آیا', 'اصلاً', 'اعتبار', 'اعلام', 'انتقالِ', 'بپرس', 'تشخیص', 'دقیقاً', 'دقیقه', 'شارژ', 'عددِ', 'فاکتور', 'فرستاده', 'فیلدِ', 'مبلغِ', 'ندارد', 'نرخِ لحظه\u200cای در دسترس نیست', 'نشد', 'نمی', 'واریز', 'واریز کردم ولی شارژ نشد', 'ولی', 'کردم', 'کسی', 'گفت', 'گِردشده', 'یکتاست'), '  ▪️ «واریز کردم ولی شارژ نشد»: بپرس دقیقاً همون مبلغِ اعلام‌شده رو فرستاده؟ انتقالِ TRC20 اصلاً فیلدِ memo نداره، تشخیص فقط از رویِ همون عددِ یکتاست؛ مبلغِ گِردشده تشخیص داده نمی‌شه. هر فاکتور ۲۰ دقیقه اعتبار داره.\n  ▪️ «نرخِ لحظه‌ای در دسترس نیست» رو دید، از همون منو «💵 ثبت نرخِ دستی» رو بزنه تا تا برگشتنِ نرخِ زنده کار راه بیفته.'),
    (('VPN', 'تنظیماتِ', 'رباتِ', 'مستأجر', 'نگهبانِ گروهِ خودِ بات\u200cساز', 'پرسوال', '🛡 مدیریت گروه و کانال'), '⚙️ **تنظیماتِ گروه در رباتِ مستأجر — بخشِ تازه و پرسوال**\nفرق داره با «نگهبانِ گروهِ خودِ بات‌ساز» و با نوعِ رباتِ «🛡 مدیریت گروه و کانال»: این‌جا مالکِ یه رباتِ فروش (VPN یا خدماتِ مجازی) ربات رو تو گروهِ خودش ادمین می‌کنه تا هم گروه مدیریت بشه، هم کاربرانِ داخلِ گروه با کیفِ پولِ همون ربات کار کنن.\n  مسیر: پنل مدیریت → «🎛 دکمه‌ها و تنظیمات» → «⚙️ تنظیماتِ گروه».'),
    (('VPN', 'اول', 'این', 'این بخش را ندارم', 'بیند', 'حرفه', 'خدماتِ', 'رباتِ', 'فروشِ', 'مجازی', 'ندارم', 'ولی', 'پلنِ', 'پلنی', 'کسی', 'گفت'), '  ⚠️ **چه کسی این دکمه را می\u200cبیند:** رباتِ **خدماتِ مجازی** در هر پلنی، ولی رباتِ **فروشِ VPN فقط در پلنِ حرفه\u200cای**. اگر کسی گفت «این بخش را ندارم»، اول همین را چک کن.\n  زیرِ این منو چهار چیز هست:'),
    (('حذف پیام', 'دستورات', 'محافظ', 'محدودکردنِ اعضا', 'گزارش'), '  🛡 **محافظ گروه، دستورات و گزارش‌ها**\n  فعال‌سازی سه شرط دارد و هر سه لازم است:\n   ۱) ربات در گروه ادمین باشد با مجوزِ «حذف پیام» و «محدودکردنِ اعضا».\n   ۲) خودِ شخصی که دستور را می‌فرستد هم ادمینِ همان گروه باشد.\n   ۳) داخلِ **خودِ گروه** بفرستد: /فعال_محافظ  (نه در پی‌وی ربات).\n  بعدش از همین پنل تنظیمش می‌کند. برایِ خاموش‌کردن در همان گروه: /غیرفعال_محافظ\n  تنظیماتِ داخلش: 📢 کانالِ گزارش‌ها · 👋 پیامِ خوش‌آمد (با {name} نامِ عضو جایگذاری می‌شود، «-» یعنی خاموش) · 🌙 حالتِ شب (بازه را مثلِ 23:00-07:00 می‌دهد، 0 یعنی خاموش) · 🔇 سکوتِ پیش‌فرض به دقیقه · 🌊 محدودیتِ فلود (سه عدد با اسلش: تعداد/بازه‌ثانیه/مدت‌سکوت — مثلاً 10/10/5 یعنی بیش از ۱۰ پیام در ۱۰ ثانیه، ۵ دقیقه سکوت؛ 0 یعنی خاموش) · 🧩 کپچای عضوِ جدید و ⏱ مهلتش به دقیقه · 🚫 ضدلینک و ضدتبلیغ · ⚠️ سقفِ اخطار · 🚫 لیستِ بن‌شده‌ها و ⚠️ لیستِ اخطارها.'),
    # === PATCH_GROUP_GUARD_STATUS_TITLE_KB_V1 ===
    (('اخطار روی نام', 'برچسب', 'بن با تاخیر', 'تیتر ادمین', 'روی اسم', 'سکوت روی نام', 'عنوان', 'عنوان ادمین', 'عنوان سفارشی', 'کنار اسم', 'کنار نام'), '  🏷 **نمایشِ وضعیتِ اخطار/سکوت کنارِ نامِ عضو در گروه** — تلگرام فقط برایِ ادمین‌ها اجازه می‌دهد کنارِ نام یک عنوانِ سفارشی نشان داده شود؛ محافظِ گروه دقیقاً همین را استفاده می‌کند: وقتی کاربری اخطار می‌گیرد یا ساکت می‌شود، محافظ او را با تنها مجوزِ بی‌خطرِ «مشاهدهٔ لاگِ چت» (بدونِ هیچ اختیارِ واقعیِ بن/حذف‌پیام/محدودکردن/دعوت/پین/ادمین‌کردنِ دیگران) ادمین می‌کند و رویِ همان یک عنوان می‌گذارد، مثلاً «اخطار ۲» یا «سکوت».\n  این عنوان **ماندگار** است — تا وقتی ادمینِ اصلی با /رفع_اخطار (یا /حذف_اخطار) اخطارها را صفر نکند یا سکوت با /رفع_سکوت یا پایانِ خودکارِ زمانش تمام نشود، همان‌جا کنارِ نام باقی می‌ماند؛ بعدش کاربر واقعاً دی‌پروموت می‌شود (دیگر اصلاً ادمین نیست) و عنوان هم خودکار حذف می‌شود.\n  پیش‌نیاز: برایِ اینکه این برچسب کار کند، ربات باید در همان گروه علاوه‌بر «حذف پیام» و «محدودکردنِ اعضا»، مجوزِ «افزودنِ ادمینِ جدید» را هم داشته باشد. اگر این مجوز را نداشته باشد، خودِ اخطار/سکوت طبیعی ثبت می‌شود، فقط برچسبِ کنارِ نام نمایش داده نمی‌شود (خطا فقط در لاگ می‌ماند، نه پیامی به کاربر).'),
    (('/اخطار', '/انبن', '/بن', '/حذف_اخطار', '/رفع_اخطار', '/رفع_سکوت', '/سکوت', 'ادمین', 'حسابِ', 'داخلِ', 'دستور کار نمی\u200cکند', 'دستورهایِ', 'دقیقه', 'ریپلای', 'پیامِ'), '  **دستورهایِ داخلِ گروه، همه با ریپلای رویِ پیامِ همان کاربر و فقط از حسابِ ادمین:** /بن · /انبن · /اخطار · /رفع_اخطار (یا /حذف_اخطار) · /سکوت ۱۰ (یعنی ۱۰ دقیقه) · /رفع_سکوت.\n  اگر کسی گفت «دستور کار نمی\u200cکند»: یا ریپلای نکرده، یا ادمین نیست، یا محافظ برایِ آن گروه روشن نیست.'),
    (('/فعال_کردن_دستیار', 'آزمایش', 'اتصال', 'ارائه', 'اول', 'برایش', 'بفرستد', 'بپرسد', 'بگذارد', 'تنظیماتِ', 'ثبت', 'داخلِ', 'دستیار', 'دستیارِ', 'دهنده', 'سوالش', 'مصنوعی', 'نام', 'نامِ', 'هوش', 'هوشِ', 'کلید', 'گروه', '🔗 اتصال هوش مصنوعی به گروه'), '  🤖 **دستیارِ هوشِ مصنوعی در گروه** — اول از تنظیماتِ هوشِ مصنوعی ارائه\u200cدهنده و کلید را ثبت و دستیار را فعال کند و برایش یک نام بگذارد؛ بعد از همین صفحه «🔗 اتصال هوش مصنوعی به گروه» را بزند، یا داخلِ گروه بفرستد /فعال_کردن_دستیار. برایِ آزمایش نامِ دستیار را در گروه بفرستد و بعد سوالش را بپرسد.'),
    (('انتقال', 'انتقال ۱۰۰۰۰', 'انتقالِ', 'ببیند', 'بینِ', 'تعیین', 'حداقل/حداکثرِ', 'دهد', 'روشن/خاموش', 'ریپلای', 'طرفِ', 'فرستنده', 'مقابل', 'موجودی', 'نویسد', 'پیامِ', 'کارمزد', 'کارمزدِ', 'گیرد', 'گیرنده', '۱۰٪', '۱۰۰٬۰۰۰', '۱۰۰۰۰', '۹۰٬۰۰۰'), '  💸 **انتقالِ موجودی بینِ کاربران در گروه** — کاربر در گروه «موجودی» می\u200cنویسد تا موجودی\u200cاش را ببیند، و برایِ انتقال رویِ پیامِ طرفِ مقابل ریپلای می\u200cکند و می\u200cنویسد «انتقال ۱۰۰۰۰». مالک از همین صفحه کارمزد (روشن/خاموش + درصد) و حداقل/حداکثرِ انتقال را تعیین می\u200cکند. پیش\u200cفرض کارمزد از **خودِ مبلغ** کم می\u200cشود (با ۱۰٪، فرستنده ۱۰۰٬۰۰۰ می\u200cدهد و گیرنده ۹۰٬۰۰۰ می\u200cگیرد)؛ با دکمهٔ «👤 کارمزد از: حسابِ فرستنده» گیرنده کاملِ ۱۰۰٬۰۰۰ را می\u200cگیرد و ۱۱۰٬۰۰۰ از فرستنده کم می\u200cشود (جزئیات در بخشِ ۱۰C).'),
    (('انتظار', 'بازی', 'بازی ۱۰۰۰۰', 'بندی', 'بندیِ', 'ثانیه', 'حداقل', 'حداکثرِ', 'خودکار', 'درصدِ', 'روشن/خاموش', 'ریپلای', 'زمانِ', 'شرط', 'شرط بندی ۱۰۰۰۰', 'فرض', 'قبول', 'مسترد', 'مقابل', 'مهلت', 'نشود', 'نفرِ', 'نویسد', 'پایانِ', 'پذیرفته', 'پیامِ', 'پیش', 'کارمزد', 'گروهی', '۱۰۰۰۰', '🎲 تنظیمات شرط\u200cبندی'), '  🎲 **شرط\u200cبندیِ گروهی** — «🎲 تنظیمات شرط\u200cبندی»: روشن/خاموش، درصدِ کارمزد، حداقل و حداکثرِ مبلغ، و زمانِ انتظار (پیش\u200cفرض ۳۰۰ ثانیه). در گروه، کاربر رویِ پیامِ نفرِ مقابل ریپلای می\u200cکند و می\u200cنویسد «بازی ۱۰۰۰۰» («شرط بندی ۱۰۰۰۰» هم قبول است). اگر تا پایانِ مهلت پذیرفته نشود، مبلغ خودکار مسترد می\u200cشود.'),
    (('ادمین', 'ارسالِ', 'انتقالِ', 'باید', 'بندی', 'تنظیمش', 'داخلِ', 'داشته', 'شرط', 'مجوزِ', 'محافظ', 'موجودی', 'پست', 'کار', 'کانالِ', 'گزارش'), '  📢 **کانالِ گزارش** یکی است و برایِ هر سه\u200cتا کار می\u200cکند (محافظ، انتقالِ موجودی، شرط\u200cبندی). ربات باید در آن کانال ادمین باشد و مجوزِ ارسالِ پست داشته باشد. تنظیمش فقط از پنل است، نه از داخلِ گروه.'),
    (('آمار', 'ادمین', 'ارسال', 'تنظیمات', 'همگانی', 'پشتیبانی', 'گزارشات', '🎛 دکمه\u200cها و تنظیمات', '👮 ادمین\u200cها و پشتیبانی', '📊 آمار و گزارشات', '📣 ارسال پیام همگانی'), '📣 **ارسال پیام همگانی** — در «👮 ادمین\u200cها و پشتیبانی» است، **نه** در «🎛 دکمه\u200cها و تنظیمات» و **نه** در «📊 آمار و گزارشات».\nمسیرِ درست: پنل مدیریت → «👮 ادمین\u200cها و پشتیبانی» → «📣 ارسال پیام همگانی». (پلنِ رشد یا حرفه\u200cای لازم دارد.)'),
    (('ادمین', 'نظرسنجی', 'ها', 'پشتیبانی', '👮 ادمین\u200cها و پشتیبانی', '📋 نظرسنجی\u200cها'), '📋 **نظرسنجی\u200cها** — همان\u200cجا: «👮 ادمین\u200cها و پشتیبانی» → «📋 نظرسنجی\u200cها».'),
    (('ادمین', 'تنظیماتش', 'جاست', 'خودکار', 'داخلش', 'طبقِ پله', 'مدیریت', 'نمایندگی', 'همهٔ', 'پشتیبانی', '✏️ تعریف/ویرایش پله\u200cها', '🏷 تخفیفِ جدا برایِ هر پنل', '🏷 نمایندگی خودکار', '👮 ادمین\u200cها و پشتیبانی', '🔀 مبنا'), '🏷 **نمایندگی (همهٔ تنظیماتش یک\u200cجاست)** — پنل مدیریت → «👮 ادمین\u200cها و پشتیبانی» → «🏷 نمایندگی خودکار». داخلش:\n  • «✏️ تعریف/ویرایش پله\u200cها» و «🔀 مبنا» برایِ نمایندگیِ خودکار\n  • «🏷 تخفیفِ جدا برایِ هر پنل» — مثلاً پاسارگاد ۱۰٪ و یوپنل ۲۵٪. پنل\u200cهایِ بدونِ عدد، «طبقِ پله» می\u200cمانند.\n  • «💵 تعرفهٔ گیگیِ نمایندگان» — برایِ کسانی که به نماینده گیگی می\u200cفروشند نه پلنی. این تعرفه **جایگزینِ** درصدِ تخفیف می\u200cشود، نه اضافهٔ آن.\n  • «👤 نمایندگانِ دستی» — ثبتِ آیدیِ عددیِ یک نفر با درصدِ دلخواه.\n  ⚠️ تعرفهٔ نمایندگان قبلاً زیرِ «فروش آنلاین»ِ هر پنل بود و منتقل شد؛ اگر کسی آنجا دنبالش گشت، مسیرِ بالا را بده. صفحهٔ «فروش آنلاین»ِ پنل فقط قیمتِ کاربرانِ عادی است.'),
    (('ارسال', 'ارسال همگانی', 'تنظیمات', 'فرق', 'همگانی', 'پست', 'کاربرانِ', 'کانال/گروه', 'گروه', '🎛 دکمه\u200cها و تنظیمات', '📢 ارسال پیام به کانال و گروه'), '📢 **ارسال پست به کانال و گروه** — این با «ارسال همگانی» فرق دارد و در «🎛 دکمه\u200cها و تنظیمات» است: → «📢 ارسال پیام به کانال و گروه». (همگانی برایِ کاربرانِ ربات است، این یکی برایِ کانال/گروه.)'),
    (('API', 'QR', 'VPN', 'آدرس', 'آمدگویی', 'آموزشِ', 'آنتی', 'آپِ', 'اتصال', 'ارسالِ', 'استارت', 'استیکر', 'اسپم', 'اشتباه', 'اصلی', 'اضافه', 'افزودنِ', 'امتیازگیری', 'اپِ', 'اکشن', 'ای/کیبوردی', 'ایموجیِ', 'اینلاین', 'بارکد', 'بازگشت', 'بازی', 'بگیر', 'تجربهٔ', 'تنظیمات', 'تنظیماتِ', 'تومان', 'جایزه', 'حرفه', 'خدماتِ', 'خرید', 'خوش', 'دعوت', 'دلخواهِ', 'دکمه', 'دکمه\u200cها و تنظیمات', 'رباتِ', 'رفرال', 'رنگِ', 'روز', 'روشن/خاموش', 'شیشه', 'صفحهٔ', 'عمومیِ', 'عکسِ', 'فروشگاهی', 'فهرستِ', 'متنِ', 'مجازی', 'مدیریتِ', 'موردِ', 'مینی', 'نامِ', 'نامِ دلخواهِ کانفیگ', 'ندهی', 'نظرسنجیِ', 'نوعِ', 'ورودِ', 'ویرایشِ', 'پاداشِ', 'پرمیوم', 'پشتِ', 'پیامِ', 'چیزهایی', 'کاملِ', 'کانفیگ', 'کردنِ'), '🎛 **در «دکمه\u200cها و تنظیمات» چه چیزهایی هست** (فهرستِ کامل و به\u200cروز — تا اشتباه آدرس ندهی): تنظیماتِ عمومیِ ربات، 🏠 مدیریتِ دکمه\u200cهای صفحهٔ اصلی، 🎨 رنگِ دکمه\u200cهای اینلاین، ✏️ متنِ خوش\u200cآمدگویی، 😀 ری\u200cاکشن رویِ پیامِ استارت، 🎓 تجربهٔ ورودِ حرفه\u200cای، 🎁 پاداشِ دعوت (تومان)، 🏅 امتیازگیری با رفرال، ⭐ نظرسنجیِ خرید، 🎲 بازی کن و جایزه بگیر، 🖼 عکسِ پشتِ بارکد (QR)، ⭐ ایموجیِ پرمیوم، 🎟 افزودنِ استیکر (استارت و دکمه\u200cها)، 🏠 متنِ بازگشت به صفحهٔ اصلی، 🔑 API ربات (فقط رباتِ خدماتِ مجازی)، 💾 بک\u200cآپِ کاملِ ربات، ⚙️ تنظیماتِ گروه، 🛍 مینی\u200cاپِ فروشگاهی، 📢 ارسالِ پیام به کانال و گروه، 🎛 نوعِ منو (شیشه\u200cای/کیبوردی)، 🤖 روشن/خاموش\u200cکردنِ ربات، و 🛡 آنتی\u200cاسپم. در رباتِ VPN دو موردِ اضافه هم هست: ✏️ ویرایشِ متنِ آموزشِ اتصال و متنِ «نامِ دلخواهِ کانفیگ».'),
    (('NOWPayme', 'NOWPayments', 'احرازِ', 'ارز', 'ارزِ', 'استارزِ', 'امروز', 'تاییدِ', 'تخفیف', 'تراکنش', 'تلگرام', 'تون', 'ثبتِ', 'خریدهای', 'خودکارِ', 'درگاهِ', 'دکمهٔ', 'دیجیتال', 'رسید', 'رسیدها', 'زرین', 'سفارش', 'سواپ', 'شارژ/کسرِ', 'شمارهٔ', 'لاگِ', 'مالی', 'مالی و کیف پول', 'مستقیم', 'همگانیِ', 'هویتِ', 'واریز', 'ولت', 'پال', 'پرداخت', 'پول', 'پیامِ', 'پیز', 'پیش', 'چیزهایی', 'کارت', 'کانالِ', 'کدهایِ', 'کیف', 'گزارشِ', 'یونیک'), '💰 **در «مالی و کیف پول» چه چیزهایی هست** (این هم کامل بلد باش): سفارش\u200cها و رسیدها، گزارشِ خریدهای امروز، 💳 شمارهٔ کارت و کارت\u200cبه\u200cکارت، 🤖 تاییدِ خودکارِ رسید، 🧾 کانالِ تاییدِ رسیدها، 📊 کانالِ لاگِ تراکنش\u200cها، 🛡 احرازِ هویتِ کاربران، 💳 درگاهِ یونیک\u200cپی، 🔐 درگاهِ زرین\u200cپال، 🪙 درگاهِ ارزِ دیجیتال (NOWPayments)، 🟦 سواپ\u200cولت، 💠 تون\u200cپیز، ⭐ استارزِ تلگرام، 💎 پرداخت مستقیم ارز، 💸 شارژ/کسرِ همگانیِ کیف پول، 🔘 دکمهٔ زیرِ پیامِ تاییدِ واریز، 📥 کانالِ ثبتِ شماره\u200cها، 📱 تاییدِ شماره پیش از پرداخت، و 🏷 کدهایِ تخفیف.'),
    (('VPN', 'تخفیف', 'تستِ', 'خودکار', 'روزانه', 'سرویس', 'سهمیهٔ', 'محصولات', 'نماینده', 'همکار', 'وبش', 'وبِ', 'پنلِ', '🎁 سهمیهٔ تستِ روزانه', '📦 سرویس\u200cها و محصولات', '🖥 پنلِ وبِ نماینده', '🤖 پنل VPN خودکار', '🤝 حساب\u200cهای همکار (تخفیف'), '🎁 **سهمیهٔ تستِ نماینده و پنلِ وبش** — این دو زیرِ «📦 سرویس\u200cها و محصولات» → «🤖 پنل VPN خودکار» → «🤝 حساب\u200cهای همکار (تخفیف)» → رویِ همان همکار هستند: «🖥 پنلِ وبِ نماینده» و «🎁 سهمیهٔ تستِ روزانه».'),
    # === PATCH_GG_AI_KB_FILE_LINK_V1 ===
    (('آپلود', 'تبدیل فایل', 'تبدیلِ فایل', 'حجم فایل', 'دانلود', 'دیسک', 'ذخیره\u200cسازی', 'سهمیهٔ ذخیره', 'فایل به لینک', 'فایل↔لینک', 'لینک به فایل', 'لینکِ دانلود', 'وبهوک', 'ظرفیت', '🔗 تبدیل فایل ↔ لینک'), '🔗 **رباتِ «تبدیلِ فایل ↔ لینک»** — این نوعِ رباتی است که هنگامِ ساخت انتخاب می\u200cشود، نه یک دکمهٔ اضافه رویِ سایرِ ربات\u200cها؛ کاملاً خودکار کار می\u200cکند و پنلِ مدیریتِ محصولِ جدایی ندارد:\n  • کاربر هر فایلی (سند/عکس/ویدیو/صدا/گیف) برایِ ربات بفرستد، رویِ سرور ذخیره می\u200cشود و بلافاصله یک لینکِ دانلودِ مستقیمِ HTTP (بازشدنی با هر مرورگری، بدونِ نیاز به تلگرام) برمی\u200cگردد.\n  • هر لینکی (چه لینکِ همین ربات، چه هر لینکِ دیگری رویِ اینترنت) هم که کاربر بفرستد، دانلود و به\u200cصورتِ فایل توی همان چت تحویل داده می\u200cشود.\n  • سقفِ حجمِ هر فایلِ تکی مستقل از پلن است. سهمیهٔ کلِ فضایِ ذخیره\u200cسازیِ هر ربات به پلن بستگی دارد (رایگان کمترین، رشد بیشتر، حرفه\u200cای/همکار بیشترین)؛ اگر فضا پر شود، باید فایلِ قدیمی حذف شود یا پلن ارتقا یابد.\n  • اگر گفت «لینک نمی\u200cسازه» یا «فایل رو ذخیره کرد ولی لینک نداد»: علتِ رایج این است که آدرسِ عمومیِ سرور (WEBHOOK_BASE) تنظیم نشده — این یک تنظیمِ سمتِ سرورِ بات\u200cساز است، نه چیزی که خودِ مالکِ ربات از پنل عوض کند؛ باید به پشتیبانیِ بات\u200cساز اطلاع بدهد.'),
    # === PATCH_GG_AI_KB_PANEL_YAR_V1 ===
    (('اکسپورت پنل', 'انتقالِ کاربران بینِ پنل', 'بکاپِ پنل', 'پنل\u200cیار', 'کاربرانِ پنل با حجم', 'مدیریتِ پنل\u200cهایِ VPN', 'مهاجرت پنل', '🗂 پنل\u200cیار'), '🗂 **رباتِ «پنل\u200cیار» (مدیریتِ پنل\u200cهایِ VPN)** — این نوعِ ربات چیزی به مشتریِ نهایی نمی\u200cفروشد؛ ابزارِ خودِ مالک/فروشنده برایِ مدیریتِ پنل\u200cهایِ سرورِ VPN است (مرزبان/مرزنشین/هیدیفای/سنایی/ربکا/پاسارگاد و…)، کاملاً مستقل از پنل\u200cهایی که یک رباتِ فروشِ VPN ممکن است داشته باشد.\n  فعلاً فقط بخشِ «👥 نمایشِ کاربرانِ هر پنل با حجم و انقضایِ باقی\u200cمانده» کامل و فعال است. دو قابلیتِ «💾 بکاپِ JSON از کاربرانِ یک پنل» و «🔁 انتقال/کپیِ کاربران بینِ دو پنل» هنوز آماده نیستند (🚧 در دستِ ساخت)؛ اگر کسی این دو را خواست، صادقانه بگو فعلاً در دسترس نیست، نه اینکه مسیرِ ساختگی بدهی.\n  مسیر: پنل مدیریت → «🗂 پنل\u200cیار» → افزودنِ پنلِ جدید (نوع، آدرس، توکن/یوزرنیم-پسورد) → از لیستِ پنل\u200cها، «👥 کاربران» را باز کن.'),
    # === PATCH_GG_AI_KB_GENERAL_SUPPORT_V1 ===
    (('پشتیبانی هلپ\u200cدسک', 'ربات پشتیبانی', 'ربات عمومی', 'رباتِ عمومی', 'زمینهٔ عمومی', 'منویِ آزاد', 'هلپ\u200cدسک', '🎧 هلپ\u200cدسک', '🤖 ربات عمومی'), '🤖 **رباتِ «عمومی (منویِ آزاد)» و «هلپ\u200cدسک/پشتیبانی»** — این دو زمینه کاتالوگِ محصول یا سرویسِ ثابتی ندارند؛ ساختارِ منو کاملاً دستیِ خودِ مالکِ ربات است، از همان بخشِ «🎛 دکمه\u200cها و تنظیمات» (مدیریتِ دکمه\u200cهای صفحهٔ اصلی). امکاناتِ عمومیِ هر ربات (تیکت، ادمینِ پشتیبان، عضویتِ اجباری، ارسالِ همگانی، محافظِ گروه و…) طبقِ همان بخش\u200cهایِ استانداردِ «👮 ادمین\u200cها و پشتیبانی» رویِ این دو زمینه هم کار می\u200cکنند؛ اگر کسی پرسید «چطور تیکت/پشتیبانی راه بندازم؟»، مسیرِ همان بخشِ عمومی را بده، نه چیزِ اختصاصیِ دیگری.'),
]



def _gg_normalize_for_match(s: str) -> str:
    """نرمال‌سازیِ ساده برایِ تطبیقِ کلیدواژه: حذفِ نیم‌فاصله و چند نویسه‌ی
    رایج، بدونِ نیاز به کتابخانه‌ی NLP جدا."""
    return (s.replace("\u200c", " ").replace("«", " ").replace("»", " ")
             .replace("ي", "ی").replace("ك", "ک").lower())


# === PATCH_GROUP_GUARD_KB_GATEWAYS_2026_09_V1 ===
# بخش‌هایِ تازه: درگاه‌ها و قابلیت‌هایی که در دانشِ قبلی نبودند یا قدیمی
# شده بودند. به ته لیست اضافه می‌شوند تا ساختار و بقیهٔ بخش‌ها دست نخورند.
_GG_AI_TOPICS.extend([
    (("درگاه", "پلن", "رشد", "حرفه", "حرفه‌ای", "زرین‌پال", "زیبال", "آبان", "یونیک‌پی", "استارز", "قفل"),
     "🆕 **کدوم درگاه تو کدوم پلن هست** (این بخش جدیدتره؛ اگه با لیستِ پلن‌هایِ بالا فرق داشت، این درسته):\n"
     "  • رایگان: فقط کارت‌به‌کارت؛ هیچ درگاهی نمی‌بینه.\n"
     "  • رشد: زرین‌پال، آبان گیت‌وی، یونیک‌پی، استارزِ تلگرام، سواپ‌ولت، تون‌پیز (و حساب‌پی تو رباتِ خدماتِ مجازی).\n"
     "  • فقط حرفه‌ای: زیبال، درگاهِ ارز دیجیتال (NOWPayments)، پرداختِ مستقیمِ ارز (تون/ترون/تتر).\n"
     "  • ربات همکار: زرین‌پال، آبان، یونیک‌پی، استارز، ارز دیجیتال.\n"
     "همهٔ درگاه‌ها از: پنل مدیریت ← «💰 مالی و کیف پول». اگه درگاهی رو نمی‌بینه، اول پلنش رو بپرس."),

    (("زرین‌پال", "زرین پال", "زرینپال", "مرچنت", "callback", "کالبک", "دامنه", "دسترسی از این دامنه", "مجاز نمی", "صفحهٔ واسط", "صفحه واسط", "Referrer", "ارجاع"),
     "🔐 **درگاه زرین‌پال** (پلنِ رشد به بالا) — «💰 مالی و کیف پول» ← «🔐 درگاه زرین‌پالِ ربات»:\n"
     "  ۱) «✏️ ثبت / ویرایش مرچنت‌ایدی» (از پنلِ زرین‌پال).\n"
     "  ۲) «🌐 ثبت/ویرایشِ دامنهٔ callback» — **بدونِ دامنه درگاه کار نمی‌کنه**؛ باید همون دامنه‌ای باشه که تو زرین‌پال ثبت شده و به همین ربات وصل باشه.\n"
     "  ۳) «🟢 فعال کن»، بعد «🧪 تست اتصال زرین‌پال». تست یه دکمهٔ «🔍 بازکردنِ صفحهٔ پرداختِ آزمایشی» هم می‌ده — حتماً بازش کنه (ولی پرداخت نکنه).\n"
     "  ▪️ «💸 کارمزد»: فروشنده/پرداخت‌کننده (بخشِ جدا بالاتر توضیح داده شده).\n"
     "  ▪️ **پرتکرارترین خطا:** «دسترسی از این دامنه مجاز نمی‌باشد، لطفاً از دامنه‌ی اصلی اقدام نمایید» — قانونِ جدیدِ شاپرک: زرین‌پال و زیبال پرداختی که مستقیم از تلگرام باز بشه (بدونِ ارجاع‌دهنده) رد می‌کنن؛ تست اتصال موفقه ولی صفحهٔ پرداخت باز نمی‌شه. راه‌حل: تو صفحهٔ همون درگاه دکمهٔ «🔗 لینک ریدایرکت» (هر درگاه جدا):\n"
     "     • «⚡️ خودکار (دامنهٔ callback)» — اگه دامنهٔ callbackش همون دامنهٔ ثبت‌شده تو درگاهه و به ربات وصله، فقط همین رو بزنه؛ هیچ فایلی لازم نیست.\n"
     "     • «✏️ ثبتِ لینکِ دلخواه» — اگه صفحهٔ ریدایرکت رو سایتِ خودش داره، آدرسِ پایه رو بفرسته (مثلاً https://example.com/payment → لینکِ نهایی …/payment/start/کدِ فاکتور). اگه صفحه نداره، همون‌جا دکمهٔ «⬇️ دانلودِ فایلِ آمادهٔ pay.html» هست؛ دانلود کنه، رویِ هاستِ خودش تو public_html آپلود کنه، بعد آدرسِ https://دامنه‌اش/pay.html رو تو همین «لینک ریدایرکت» ثبت کنه.\n"
     "     بعدش «🧪 تست اتصال» و دکمهٔ «🔍 بازکردنِ صفحهٔ پرداختِ آزمایشی».\n"
     "  ▪️ «زیردامنهٔ اختصاصیِ» خودِ پنلِ زرین‌پال (pay.x.ir) صفحهٔ StartPay نداره و وارد کردنش به‌عنوانِ صفحهٔ واسط ۴۰۴ می‌ده.\n"
     "  ▪️ «محدودیتِ IP» تو پنلِ زرین‌پال ربطی به خطایِ بالا نداره. اگه می‌خواد فعالش کنه، آیِ‌پی رو **فقط از پشتیبانیِ بات‌ساز** بپرسه؛ تو هیچ آیِ‌پی‌ای اعلام نکن. زرین‌پال آیِ‌پیِ ثبت‌شده رو دیگه پاک نمی‌کنه."),

    (("زیبال", "zibal", "trackId"),
     "🔐 **درگاه زیبال** (فقط پلنِ حرفه‌ای) — «💰 مالی و کیف پول» ← «🔐 درگاه زیبالِ ربات»:\n"
     "مرچنت‌ایدی ← دامنهٔ callback (بدونِ اون کار نمی‌کنه) ← «🔗 لینک ریدایرکت» (مثلِ زرین‌پال؛ خطایِ دامنه/ارجاع‌دهنده رو حل می‌کنه) ← فعال کن ← «🧪 تست اتصال زیبال». «📖 راهنمای اتصال زیبال» هم هست.\n"
     "زیبال گزینهٔ «کارمزد از پرداخت‌کننده» نداره (برخلافِ زرین‌پال). بعدِ پرداخت کیف پول خودکار شارژ می‌شه؛ اگه کاربر وسطِ کار برنگشت، حلقهٔ پس‌زمینه فاکتورهایِ باز رو خودش چک می‌کنه."),

    (("آبان", "آبان گیت", "abangateway", "Abangw", "پیامک", "بلوبانک", "بلو", "منقضی", "کارت‌به‌کارتِ خودکار", "ag_live", "ag_test"),
     "💳 **درگاهِ آبان گیت‌وی** (پلنِ رشد به بالا و ربات همکار) — «💰 مالی و کیف پول» ← «💳 درگاهِ آبان گیت‌وی»:\n"
     "کارت‌به‌کارتِ خودکار: کاربر به کارتی که فاکتور نشون می‌ده **دقیقاً همون مبلغِ یکتا** رو واریز می‌کنه؛ آبان پیامکِ بانک رو می‌خونه و ربات تو چند ثانیه کیف پول رو شارژ می‌کنه.\n"
     "  ▪️ توکن از پنلِ abangateway.ir یا ربات @Abangw_bot: پیشوندِ ag_live_ واقعی، ag_test_ آزمایشی. ← «🔑 ثبت / تغییرِ توکن» ← «✅ فعال‌کردن».\n"
     "  ▪️ تنظیماتِ دیگه: اسمِ نمایشی، حداقل/حداکثرِ مبلغ، «نمایش در کیف پول» و «نمایش موقعِ پرداختِ محصول» (جدا از هم). همه‌جا تومانه؛ تبدیل به ریال خودکاره.\n"
     "  ▪️ **پیامکِ بانک دیر رسید و فاکتور منقضی شد** (مثلاً اختلالِ پیامکِ بلوبانک): مالک فاکتور رو تو پنلِ آبان دستی تأیید کنه. ربات فاکتورهایِ منقضیِ ۲۴ ساعتِ اخیر رو هر حدودِ ۱۵ دقیقه دوباره چک می‌کنه و خودکار شارژ می‌کنه؛ کاربر هم می‌تونه «🔍 بررسی پرداخت» بزنه. اگه تأیید بیرون از ربات انجام شده باشه، برایِ مالک هشدار با دکمهٔ «✅ تأیید و شارژ دستی» میاد. فاکتورهایِ قدیمی‌تر از ۲۴ ساعت رو باید دستی از «💸 شارژ/کسر همگانی کیف پول» یا پروفایلِ کاربر شارژ کنه.\n"
     "  ▪️ اگه کاربر «بررسی پرداخت» زد و چند دقیقه تأیید نیومد، برایِ مالک هشدارِ «تأیید/رد» میاد تا اگه رسید رو دیده دستی تأیید کنه."),

    (("یونیک‌پی", "یونیک پی", "uniquepay"),
     "💳 **درگاهِ یونیک‌پی** (رشد به بالا و همکار) — «💰 مالی و کیف پول» ← «💳 درگاهِ یونیک‌پی»: کارت‌به‌کارتِ خودکار با لینکِ پرداخت؛ ربات همیشه خودش از درگاه استعلام می‌گیره و بعد شارژ می‌کنه. مبلغ‌ها تومانیه."),

    # === PATCH_APAY_GATEWAY_V1 ===
    (("آپی", "APay", "apay", "apaygate", "کارت به کارت آنی", "کارت‌به‌کارت آنی"),
     "💳 **درگاهِ آپی (کارت‌به‌کارتِ آنی — apaygate.shop)** (رشد به بالا و همکار) — «💰 مالی و کیف پول» ← «💳 درگاهِ آپی (کارت‌به‌کارت آنی)»:\n"
     "پول مستقیم به کارتِ خودِ مالک میاد؛ کاربر مبلغ رو وارد می‌کنه، تو صفحهٔ پرداختِ آپی کارت و **مبلغِ دقیق** (چند تومانِ یکتا اضافه داره) رو می‌بینه و کارت‌به‌کارت می‌کنه؛ با رسیدنِ پیامکِ بانک به گوشیِ مالک، کیف پول خودکار شارژ می‌شه (به اندازهٔ مبلغی که کاربر وارد کرده).\n"
     "  ▪️ راه‌اندازی: تو رباتِ آپی ثبت‌نام، کارت + فرستندهٔ پیامکِ بانک رو اضافه کنه، اپِ اندرویدِ آپی رو رو گوشیِ دارندهٔ سیم‌کارتِ بانکی نصب کنه، «توکنِ API» (apay_…) رو بگیره و تو ربات «🔑 ثبت / تغییرِ توکن» ← «✅ فعال‌کردن».\n"
     "  ▪️ اختیاری برایِ شارژِ فوری: آدرسِ وب‌هوکی که تو همون صفحه نشون داده می‌شه رو تو آپی ثبت کنه و «🔐 کلیدِ وب‌هوک» رو بزنه. بدونِ وب‌هوک هم ربات هر دقیقه خودش استعلام می‌گیره.\n"
     "  ▪️ کارمزد: هر پرداختِ موفق ۱٬۵۰۰ تومان از کیف پولِ حسابِ آپیِ مالک کم می‌شه (نه از مشتری)؛ اگه موجودیش تموم بشه فاکتور ساخته نمی‌شه. «🧪 تستِ اتصال و موجودی» موجودی و تعدادِ کارت‌ها رو نشون می‌ده.\n"
     "  ▪️ خطاها: «هیچ کارتِ فعالی نداری» = تو آپی کارت اضافه/تأیید نشده. «موجودی کافی نیست» = کیف پولِ آپی رو شارژ کنه. فاکتور ۱۵ دقیقه اعتبار داره و پیامکِ دیرهنگام تا ۱۰ دقیقه بعد هم قبوله؛ اگه کاربر لغو زده ولی واریز کرده بود، باز هم خودکار شارژ می‌شه."),

    (("حساب‌پی", "حساب پی", "hesabpay", "افغانی", "افغانستان", "AFN"),
     "🇦🇫 **درگاهِ حساب‌پی (پرداختِ افغانی)** — فقط رباتِ «خدماتِ مجازی»، پلنِ رشد/حرفه‌ای — «💰 مالی و کیف پول» ← «🇦🇫 درگاه حساب‌پی (پرداخت افغانی)»:\n"
     "کلیدِ API + نرخِ ثابتِ «هر ۱ افغانی = چند تومان» رو ثبت و بعد فعالش کنه. کیف پول تومانی می‌مونه؛ کاربر به افغانی پرداخت می‌کنه. شارژ فقط بعد از تأییدِ امنِ خودکارِ درگاه انجام می‌شه. برایِ کاربر دکمهٔ «🇦🇫 شارژ با افغانی (حساب‌پی)» تو کیف پول ظاهر می‌شه."),

    (("دامنهٔ اختصاصی", "دامنه اختصاصی", "TXT", "CNAME", "رکورد", "DNS", "_botsaz-verify", "resolve"),
     "🌐 **دامنهٔ اختصاصی**: برایِ اینکه لینک‌هایِ پرداخت/وب‌اپ/API ربات زیرِ دامنهٔ خودِ مالک ساخته بشه. از «🪙 درگاه ارز دیجیتال» ← «🌐 دامنهٔ اختصاصیِ لینکِ پرداخت» یا از «🔑 API ربات» واردش می‌شه.\n"
     "دو رکورد لازمه: TXT با نامِ _botsaz-verify.دامنه و مقداری که ربات می‌ده (اثباتِ مالکیت)، و A/CNAME به سمتِ سرور. بعد «🔄 بررسیِ دوباره». انتشارِ DNS چند دقیقه تا چند ساعت طول می‌کشه.\n"
     "⚠️ آدرسِ سرور/آیِ‌پی رو **هرگز خودت اعلام نکن** — بگو برایِ رکوردِ A/CNAME به پشتیبانیِ بات‌ساز پیام بده. این قابلیت برایِ کارکردِ ربات لازم نیست و فقط برایِ برنده."),

    (("پنل تحت وب", "پنل وب", "/panel", "مرورگر", "کامپیوتر", "وب"),
     "🖥 **پنلِ تحت وب** (فقط پلنِ حرفه‌ای): مالک یا ادمین تو رباتِ خودش دستورِ /panel رو می‌زنه؛ یه لینکِ یک‌بارمصرفِ ۵ دقیقه‌ای فقط تو همون چت میاد. با بازکردنش ۱۲ ساعت واردِ پنل می‌مونه. رمز نداره (مالکیت با تلگرام ثابت می‌شه). این پنل برایِ دیدنِ آمار/سفارش‌ها/کاربران و چند کارِ کم‌خطره — توکنِ ربات و کلیدهایِ پنلِ VPN رو نشون نمی‌ده و تغییر نمی‌ده. اگه لینک باز نشد: احتمالاً ۵ دقیقه گذشته یا یه‌بار بازش کرده؛ دوباره /panel."),

    (("API", "کلید API", "X-API-Key", "🔑 API ربات", "وب‌سرویس", "اتصال سایت"),
     "🔑 **API ربات** — فقط رباتِ خدماتِ مجازی: «🎛 دکمه‌ها و تنظیمات» ← «🔑 API ربات» (فقط مالک).\n"
     "دو نوع کلید: کلیدِ کلِ ربات (دسترسیِ کامل به همهٔ سفارش‌ها و ثبتِ سفارش برایِ هر مشتری) و کلیدِ شخصیِ هر مشتری که ادمین از پروفایلِ همون کاربر «دسترسیِ API» رو براش روشن می‌کنه (فقط دیتایِ خودش؛ اگه براش تخفیفِ اختصاصی گذاشته باشه، خودکار رویِ سفارش‌هاش اعمال می‌شه). احرازِ هویت با هدرِ X-API-Key."),

    (("بک‌آپ", "بکاپ", "backup", ".botsaz", "انتقال ربات", "بازگردانی", "ایمپورت"),
     "💾 **بک‌آپِ کاملِ ربات** (رشد به بالا و همکار) — «🎛 دکمه‌ها و تنظیمات» ← «💾 بک‌آپِ کاملِ ربات»: یه فایلِ .botsaz شاملِ تنظیمات، دکمه‌هایِ صفحهٔ اصلی (با زیرمنوها) و برایِ رباتِ خدماتِ مجازی پنل‌ها/دسته‌ها/سرویس‌ها.\n"
     "⚠️ ایمپورت همهٔ این‌ها رو تو رباتِ مقصد **پاک و از رویِ فایل بازسازی** می‌کنه. فایل کلیدهایِ API پنل‌ها رو هم داره — به هیچ‌کس نده."),

    (("NFT", "ان‌اف‌تی", "گیفت", "یوزرنیم", "مینی‌اپ ۲", "Business", "بیزینس", "اجاره"),
     "🎁 **مارکتِ گیفتِ NFT و یوزرنیم (مینی‌اپ ۲)** — فقط رباتِ خدماتِ مجازی + پلنِ حرفه‌ای: خرید و اجارهٔ گیفت/یوزرنیم؛ موجودی مالِ خودِ ادمینه و از تبِ «مدیریت» داخلِ همون مینی‌اپ ثبت و قیمت‌گذاری می‌شه.\n"
     "تحویل دو حالت داره: خودکار (ادمین حسابش رو به‌صورتِ Business به ربات وصل کرده و اجازهٔ انتقالِ گیفت داده) یا دستی (ربات به ادمین خبر می‌ده، ادمین منتقل می‌کنه و «تحویل شد» می‌زنه). اگه خودکار شکست بخوره، سفارش خودش می‌ره رویِ دستی — خطا نمی‌ده."),

    (("کانال اعتماد", "اعتمادسازی", "🎖", "پست خودکار سفارش"),
     "🎖 **کانالِ اعتمادسازی** — «📊 آمار و گزارشات» ← «🎖 کانال اعتمادسازی»: یه کانال که ربات توش ادمینه وصل می‌شه و بعد از هر سفارشِ موفق (کیف پول، کارت‌به‌کارت، فروشگاه و…) یه پستِ عمومیِ مرتب اون‌جا می‌ره تا مشتری‌هایِ جدید ببینن فروش واقعیه."),

    (("ایموجی پرمیوم", "ایموجی", "premium emoji", "متحرک"),
     "⭐ **ایموجیِ پرمیوم** (حرفه‌ای و همکار) — «🎛 دکمه‌ها و تنظیمات» ← «⭐ افزودن ایموجی پرمیوم»: اول ایموجیِ معمولی (مثلاً ❤️) رو می‌فرسته، بعد ایموجیِ پرمیومِ معادل رو (با اکانتِ پرمیومِ خودش). از اون به بعد تو متن‌هایِ ربات جایگزین می‌شه. برایِ یه دکمهٔ خاص هم از ویرایشِ همون دکمه «😎 افزودن ایموجی پرمیوم» هست."),

    (("نیاز ارتقای پلن", "نیازِ ارتقای پلن", "این بخش هنوز چیزی نداره", "تعیین نوع ربات", "نوع ربات", "سرویس‌ها و محصولات خالی"),
     "🧭 **«📦 سرویس‌ها و محصولات» فقط «🔒 این بخش هنوز چیزی نداره (نیاز ارتقای پلن)» نشون می‌ده ولی پلنش رشد/حرفه‌ایه**: پلن مشکلی نداره؛ «نوعِ ربات» ثبت نشده (معمولاً رباتی که از رباتِ همکار خریده شده و ویزاردِ چیدمان کامل نشده). همون‌جا دکمهٔ «🧭 تعیین نوع ربات» رو بزنه و نوع رو انتخاب کنه (فروش VPN / خدمات مجازی / تبلیغاتی) — فقط یک‌بار. اگه دکمه رو نمی‌بینه، به پشتیبانی بگه."),
])


# === PATCH_GROUP_GUARD_KB_2026_10_V1 ===
# آپدیتِ بزرگِ مهرِ ۱۴۰۵ / اکتبر ۲۰۲۶: همهٔ قابلیت‌هایِ تازه که تا اینجا در دانشِ دستیار نبود.
# مثلِ بخش‌هایِ قبل به ته لیست اضافه می‌شود؛ اگر با متنِ قدیمی‌تر فرق داشت، این‌ها درست‌اند.
_GG_AI_TOPICS.extend([
    (("تون‌پیز", "تون پیز", "tonpays", "کاستوم", "فیش", "ارسال فیش", "تعویض کارت", "کارت پر", "سقف کارت", "کول‌داون", "ریال", "مبلغ دقیق", "212", "درگاه تلگرام", "کلید وب"),
     "🪙 **درگاه تون‌پیز + حالتِ «کاستوم» (تلگرامی)** — «💰 مالی و کیف پول» ← تون‌پیز:\n"
     "  ▪️ دو حالت دارد: **استاندارد** و **کاستوم**. در کاستوم برایِ هر بخش یک کلیدِ جدا لازمه (کلیدِ «تلگرام» برایِ رباتِ تلگرامی؛ کلیدِ «وب» برایِ سایت) — کلیدِ وب رو تو ربات نمی‌ذاره، باید کلیدِ تلگرام باشه. حالت با دکمهٔ تغییرِ حالت عوض می‌شه.\n"
     "  ▪️ **فاکتورِ کاستوم**: بالایِ فاکتور «💵 مبلغِ واریزی: ۲۱۲٬۴۵۰ ریال (= ۲۱٬۲۴۵ تومان)» نوشته می‌شه — **همین مبلغِ دقیق** رو باید به ریال تو اپِ بانک بزنه (عمداً چند تومان با مبلغِ شارژ فرق داره تا واریز شناسایی بشه). زیرش «مبلغی که به کیف پول اضافه می‌شه» هست. دکمه‌هایِ «📋 کپی مبلغ (ریال)» و «📋 کپی شماره کارت» هم هست. اگه عددِ تومانی رو به‌عنوانِ ریال بزنه، ۱۰ برابر کمتر واریز می‌شه و شارژ نمی‌شه.\n"
     "  ▪️ **«📎 ارسال فیش واریزی»** در حالتِ کاستوم اجباریه: اگه واریز خودکار تأیید نشد، عکسِ فیش رو همین‌جا می‌فرسته تا درگاه بررسی کنه.\n"
     "  ▪️ **«🔄 تعویض کارت»** (جدید): اگه کارتِ نمایش‌داده‌شده سقفش پر شده یا واریز نمی‌خوره، همین دکمه زیرِ فاکتور رو بزنه؛ کارت و مبلغِ جدید می‌گیره (مبلغِ دقیق ممکنه عوض بشه — همونی که تو پیامِ جدیده رو بزنه). بینِ دو بار تعویض **۶۰ ثانیه کول‌داون** هست. وقتی کارتِ دیگه‌ای نمونده دکمه حذف می‌شه.\n"
     "  ▪️ بعد از واریز، ربات خودش وضعیت رو چک می‌کنه؛ بدنهٔ کال‌بک رو باور نمی‌کنه و همیشه از خودِ درگاه استعلام می‌گیره (ایمن)."),

    (("کیف پول", "پرداخت از کیف پول", "کیف پول در خرید", "خاموش", "روشن", "wallet_pay", "موجودی کافی", "خرید محصولات"),
     "👛 **«پرداخت از کیف پول در خرید محصولات»** (کلیدِ روشن/خاموش) — «💰 مالی و کیف پول» ← «👛 پرداخت از کیف پول در خرید محصولات: ✅ فعال/❌ غیرفعال»: اگه خاموشش کنه، تو صفحهٔ پیش‌فاکتور دکمهٔ «پرداخت از کیف پول» نشون داده نمی‌شه و کاربر فقط از روش‌هایِ دیگه (کارت‌به‌کارت، درگاه‌ها) می‌پردازه. کنارش «🔋 شارژ کیف پول در صفحه خرید» و «📱 تایید شماره پیش از پرداخت» هم تیکِ جداست."),

    (("نام درگاه", "ترتیب درگاه", "اسم دکمه", "زرین‌پال", "جابه‌جا", "ترتیب دکمه", "gwl", "تغییر نام درگاه", "نام و ترتیب"),
     "🎛 **«نام و ترتیبِ دکمه‌هایِ پرداخت»** — «💰 مالی و کیف پول» ← «🎛 نام و ترتیبِ دکمه‌هایِ پرداخت»: اسمِ هر درگاه رو (مثلاً «زرین‌پال» → «پرداخت آنلاین») عوض کنه و ترتیبِ نمایششون رو جابه‌جا کنه. هم تو صفحهٔ کیف پول اعمال می‌شه هم تو دکمه‌هایِ شارژِ صفحهٔ خرید."),

    (("تایید شماره", "تاییدِ شماره", "شماره موبایل", "شماره پیش از پرداخت", "شمارهٔ همراه", "کارت‌به‌کارت شماره", "phone"),
     "📱 **تاییدِ شماره پیش از پرداخت** — «💰 مالی و کیف پول» ← «📱 تایید شماره پیش از پرداخت»: وقتی روشن باشه، قبل از هر پرداختِ کارت‌به‌کارت (چه شارژ کیف پول، چه خریدِ مستقیمِ سرویس/کانفیگ) از کاربر شمارهٔ ایرانی (با دکمهٔ اشتراکِ مخاطبِ تلگرام) می‌گیره. این تیک جدا از «تاییدِ شمارهٔ ورود» (ضد رفرالِ فیک) است. اگه روشنه ولی ربات شماره نمی‌گیره، ربات رو آپدیت کنه؛ این باگ رفع شده."),

    (("تخفیف", "اولین خرید", "اولین‌خرید", "پیش‌نمایش", "قیمت پیش‌نمایش", "پنج نفر برتر", "رتبه", "نفرات برتر", "SMM تخفیف"),
     "🏷 **تخفیفِ خودکار در سفارشِ SMM**: تخفیفِ «اولین خرید» و «رتبهٔ نفراتِ برترِ مشتری‌ها» همون‌جا که پیش‌نمایشِ سفارش نشون داده می‌شه (مبلغِ نهایی با خط‌خورده/تخفیف) اعمال می‌شه و **همون عدد** موقعِ ثبت از کیف پول کم می‌شه — دیگه بینِ پیش‌نمایش و سفارش اختلاف نیست. کدِ تخفیفِ دستی اولویتِ بالاتر داره؛ اگه کد نذاره، تخفیفِ خودکار (بهترینِ یکی) اعمال می‌شه."),

    (("TGTools", "تی‌جی‌تولز", "استارز خودکار", "اتصال خودکار", "ارسال خودکار استارز", "tgtools", "ولت استارز"),
     "🤖 **TGTools (ارسالِ خودکارِ استارز)** — رباتِ خدماتِ مجازی: «📦 سرویس‌ها و محصولات» ← «SMM» ← «پنل‌های متصل» (بخشِ «اتصالِ خودکار»، نه دستی) ← «🤖 اتصال به TGTools». توکنِ TGTools رو بده؛ برایِ شارژِ خودکار باید موجودیِ ولتِ TGTools (آدرسی که ربات می‌ده) شارژ باشه. بعدِ اتصال، سفارش‌هایِ استارز خودکار ارسال می‌شن و دکمهٔ «🤖 ارسال خودکار با TGTools» هم رویِ سفارش‌هایِ دستی هست."),

    (("Tonnel", "تونل", "Portals", "پورتالز", "authData", "توکن مارکت", "tma", "منبع", "منابع مارکت", "توکن منقضی"),
     "🔌 **منابعِ مارکت (Tonnel / Portals)** — مینی‌اپ ۲ ← «⚙️ مدیریت» ← «🔌 منابع»: توکنِ مارکت رو (فقط خودِ توکن، یا کلِ متنِ «tma …» یا لینکِ دارایِ authData) بچسبونه؛ ربات خودش تمیزش می‌کنه (پیشوندِ tma، فاصله و ... رو درست می‌کنه). توکنِ این مارکت‌ها هر ۲ تا ۳ روز منقضی می‌شه — طبیعیه؛ ربات هشدار می‌ده و باید توکنِ تازه بذاره. «درصدِ سود» رویِ قیمتِ مبدأ اعمال می‌شه."),

    (("لاگ", "کانال تراکنش", "تون", "ترون", "تتر", "پرداخت مستقیم ارز", "txlog", "کانال لاگ", "گزارش شارژ", "TON direct", "ton_direct", "tron"),
     "📣 **لاگِ شارژها و پرداخت‌هایِ مستقیمِ ارز (تون/ترون/تتر) تو کانال** — «💰 مالی و کیف پول» ← «📊 کانالِ لاگ تراکنش‌ها» (بسته به پلن): وقتی کانال وصل باشه، هر شارژِ موفق (از جمله پرداختِ مستقیمِ TON و TRON/تتر) با مبلغ، موجودیِ قبل/بعد و درگاه تو کانال ثبت می‌شه. اگه کانالِ لاگ وصل نباشه، به خودِ مالک خبر می‌ره."),

    (("جدولی", "حالت نمایش", "جدول پلن", "لیستی", "تک‌کاربره", "چندکاربره", "تعداد کاربر", "حالتِ نمایشِ پلن", "نمایش جدولی", "پلن‌ها جدول", "نامحدود جدول"),
     "📊 **نمایشِ جدولیِ پلن‌ها** (فقط پلنِ حرفه‌ای) — پنلِ VPN ← انتخابِ پنل ← «📦 پلن‌هایِ فروش» ← «🖼 حالتِ نمایشِ پلن‌ها: لیستی/جدولی»: پلن‌ها به‌صورتِ جدولِ سه‌ستونه [قیمت | زمان | حجم] نشون داده می‌شن؛ عنوان‌ها سبزن و زیرِ تیترِ هر دسته میان. زیرش دکمهٔ «👥 نمایشِ تعدادِ کاربر در جدول: ✅/⛔️» هست (پیش‌فرض روشن): روشن = «تک‌کاربره · 30GB»، خاموش = فقط «30GB»؛ برایِ نامحدود «♾ نامحدود». هر پنل جدا تنظیم می‌شه. جدولی هم تو صفحهٔ خودِ پنل کار می‌کنه هم تو مسیرِ «خرید سرویس ← دسته». اگه کاربر لیستی می‌بینه، یعنی حالتِ جدولی رویِ **همهٔ** پنل‌هایِ اون دسته روشن نیست. «بازگشت» از پیش‌فاکتور هم به همون صفحه‌ای برمی‌گرده که کاربر ازش اومده."),

    (("تیتر", "تیتر صفحه", "ویرایش تیتر", "عنوان صفحه", "خرید VPN حجم/روز دلخواه", "همهٔ دسته‌ها", "سرویس‌های من", "تیتر صفحه‌ها"),
     "✏️ **ویرایشِ تیترِ صفحه‌هایِ خرید و سرویس‌ها** — «🎛 دکمه‌ها و تنظیمات» ← «✏️ تیترِ صفحه‌هایِ خرید و سرویس‌ها» (قبلاً فقط تو رباتِ همکار بود؛ حالا همه‌جا): تیترِ پنج صفحه رو عوض می‌کنه: لیستِ پلن‌ها، حجم/روزِ دلخواه، همهٔ دسته‌ها، همهٔ سرویس‌ها و سرویس‌هایِ من. «/skip» برایِ برگشت به پیش‌فرض. حداکثر ۶۰ کاراکتر."),

    (("ویرایش پیام", "حذف و ارسال", "سریع", "پیام تکراری", "پیام جدید", "ادیت", "دیلیت", "edit"),
     "⚡️ **ناوبری سریع**: تو رباتِ VPN و خدماتِ مجازی، رفتن بینِ صفحه‌هایِ خرید (دسته، پلن، پیش‌فاکتور، سفارش) با **ویرایشِ همون پیام** انجام می‌شه، نه حذف و ارسالِ دوباره؛ پس سریع‌تره و پیام اضافه تو چت نمی‌مونه. اگه تو ربات خودش هنوز پیامِ جدید می‌ده، ربات رو آپدیت کنه."),

    (("پیام همگانی", "برودکست", "گیف", "عکس همگانی", "ویدیو همگانی", "رسانه", "ارسال همگانی", "broadcast"),
     "📣 **ارسالِ پیامِ همگانی با رسانه**: تو «📢 ارسال پیام همگانی» می‌تونه به‌جایِ متن، **عکس، گیف (انیمیشن)، ویدیو یا فایل** هم بفرسته (با کپشن)؛ گیفِ ربات‌هایی که قبلاً خطا می‌داد رفع شد."),

    (("کانال اعتماد", "پیام تکراری", "۱۰ گیگ", "پست اعتماد", "اعتمادسازی"),
     "🎖 **کانالِ اعتمادسازی**: پستِ هر سفارش حالا یک‌بار نوشته می‌شه (مشکلِ تکرارِ عنوانِ پلن مثلِ «۱۰ گیگ ۳۶۵ روزه - ۱۰ گیگ ۳۶۵ روزه» رفع شد). مسیر: «📊 آمار و گزارشات» ← «🎖 کانال اعتمادسازی»."),

    (("عضویت اجباری", "رنگ دکمه", "رنگی", "جوین", "کانال اجباری", "fj", "عضو شدم"),
     "📢 **عضویت اجباری + رنگِ دکمه‌ها**: رنگِ هر کانال (آبی/سبز/قرمز) و دکمهٔ «✅ عضو شدم» فقط تو پلنِ حرفه‌ای کار می‌کنه — «👮 ادمین‌ها و پشتیبانی» ← «📢 عضویت اجباری در کانال» ← انتخابِ کانال ← ردیفِ رنگ. باگی که رنگ فقط گاهی به کاربر می‌رسید رفع شده (حالا همیشه می‌رسه). «✏️ تغییر متن عضویت اجباری» هم فقط حرفه‌ایه."),

    (("مینی‌اپ", "مینی اپ", "miniapp", "اشتراک", "محصولات دستی", "تغییر نام", "ویرایش نام", "سرویس‌های وب", "نام محصول"),
     "🪟 **مینی‌اپِ فروشگاهی** (رباتِ خدماتِ مجازی): تب‌ها «خانه، حساب کاربری، کیف پول، خرید، محصولات». چون رباتِ خدماتِ مجازی «اشتراک» نداره، تب/تایلِ «اشتراک‌ها/اشتراک‌هایِ من» و آمارِ «اشتراکِ فعال» اینجا حذف شده.\n"
     "  ▪️ **ویرایشِ نامِ محصولات از داخلِ مینی‌اپ**: «حساب کاربری» ← پایینِ صفحه «⚙️ مدیریت» (فقط مالک/ادمین) ← روی اسم ✏️ بزنه. شاملِ خدماتِ مجازی (گیفت، پریمیوم، استارز، تون…)، «🌐 سرویس‌هایِ وب و شبکه‌هایِ اجتماعی» (ممبر/ویو/لایک/ریاکشن، فالوور اینستاگرام، پلتفرم و سرویسِ مستقیمِ SMM، شمارهٔ مجازی، سفارش‌هایِ SMM)، دسته‌ها، پلن‌ها و محصولاتِ دستی. همون نام تو خودِ ربات هم عوض می‌شه.\n"
     "  ▪️ **ثبتِ سفارشِ SMM داخلِ مینی‌اپ** (جدید): مینی‌اپ ← «خرید» ← ورودیِ «🛰 خدماتِ شبکه‌هایِ اجتماعی» (یا اسمِ هر دکمهٔ SMMِ صفحهٔ اصلی) ← دسته/پلتفرم ← نوعِ سرویس ← سرویس ← لینک و تعداد (با +/−) ← قیمتِ لحظه‌ایِ نهایی (با تخفیفِ خودکار) ← ثبت. دقیقاً همون مسیر و منطقِ ربات: دسته‌هایِ دستیِ ادمین هم همون‌طور که تو ربات (از دکمه‌هایِ «اتصالِ سریع») دیده می‌شن اینجا هم میان؛ سفارش تو کیف پول کم می‌شه، کدِ پیگیری می‌گیره و ادمین/کانال‌ها مثلِ ربات خبردار می‌شن. سفارشِ گروهی (چند لینک) هنوز فقط تو ربات.\n"
     "  ▪️ خریدِ استارز هم داخلِ مینی‌اپ ثبت می‌شه؛ گیفت/پریمیوم/تون/شمارهٔ مجازی هنوز کاربر رو به چتِ ربات می‌برن."),

    (("مینی‌اپ ۲", "مینی اپ ۲", "miniapp2", "رنگ مینی", "تم مینی", "رنگ دکمه", "رنگ ثانویه", "تم اصلی", "ظاهر", "مارکت رنگ"),
     "🎨 **ظاهرِ مینی‌اپ ۲ (مارکتِ NFT)** — فقط حرفه‌ای: مینی‌اپ ۲ ← «⚙️ مدیریت» ← «🎨 ظاهر»: سه رنگِ مستقل: **رنگِ دکمه‌ها** (دکمه‌ها/تب‌ها/هایلایت)، **تمِ اصلی** (پس‌زمینه؛ روشن هم می‌شه و متن‌ها خودکار تیره می‌شن) و **رنگِ ثانویه** (کارت‌ها). رنگِ آماده یا کدِ هگزِ دلخواه، «♻️ پیش‌فرض» هم هست؛ همون لحظه اعمال و برایِ همهٔ کاربران ذخیره می‌شه. جدا از رنگ‌هایِ مینی‌اپِ فروشگاهیه."),

    (("بازار کاربران", "کاربر به کاربر", "p2p", "فروش گیفت", "اجارهٔ گیفت", "اسکرو", "معامله امن", "برداشت", "کارمزد معامله", "انتقال دادم", "از قبل فروختم", "آگهی", "گیفت خودم", "👤 من"),
     "🤝 **بازارِ کاربران** (مینی‌اپ ۲، حرفه‌ای، رباتِ خدماتِ مجازی) — کاربرها گیفت/یوزرنیمِ خودشون رو می‌فروشن یا اجاره می‌دن و ربات **واسطهٔ امن (اسکرو)** است.\n"
     "  ▪️ **روشن‌کردن (مالک):** مینی‌اپ ۲ ← «⚙️ مدیریت» ← «🤝 کاربران» ← کلیدِ «بازارِ کاربران» (پیش‌فرض خاموشه) و «⚙️ تنظیماتِ کارمزد و مهلت‌ها». کارمزدِ ربات از **فروشنده** گرفته می‌شه: فقط استارز، فقط کیف پول، یا هر دو (فروشنده انتخاب می‌کنه). اگه هر دو کارمزد صفر باشه، بدونِ کارمزد انجام می‌شه. مهلت‌ها: لغوِ خودکار اگه فروشنده اقدام نکرد (پیش‌فرض ۲۴ ساعت)، ارجاع به ادمین اگه خریدار پاسخ نداد (۴۸ ساعت)، حداقلِ برداشت.\n"
     "  ▪️ **فروشنده:** مینی‌اپ ۲ ← «👤 من» ← «➕ آگهی جدید»: نوع (گیفت NFT/یوزرنیم)، فقط **لینکِ گیفت** (https://t.me/nft/…) — عکس/مدل/رنگ/سمبل خودکار پر می‌شه — قیمتِ فروش و/یا اجارهٔ روزانه با بازهٔ روز. وقتی کسی بخره، ربات پیام می‌ده با دو دکمه: «📤 انتقال دادم» (باید کارمزد بده؛ بعدش لغو قفل می‌شه) و «🚫 گیفت را از قبل فروخته‌ام» (معامله لغو و پولِ خریدار برمی‌گرده).\n"
     "  ▪️ **خریدار:** مینی‌اپ ۲ ← «🤝 بازارِ کاربران» ← خرید/اجاره. پولش از کیف پول کم و نزدِ ربات نگه داشته می‌شه. بعدِ «انتقال دادم»ِ فروشنده، ربات می‌پرسه «درسته؟»: «✅ بله» → پول به فروشنده می‌رسه؛ «⚠️ مشکل دارم» → پرونده می‌ره پیشِ ادمین (آزاد به فروشنده یا برگشت به خریدار).\n"
     "  ▪️ **برداشت:** «👤 من» ← «💸 برداشت»: شمارهٔ کارت + نام؛ مبلغ از موجودی کم می‌شه و برایِ ادمین می‌ره؛ ادمین «واریز شد» یا «رد و برگشت». فقط پولِ حاصل از فروش‌هایِ همین بازار قابلِ برداشته.\n"
     "  ▪️ ربات نمی‌تونه مالکیتِ واقعیِ گیفت رو از تلگرام تأیید کنه؛ امنیتِ خریدار از اینه که پولش تا تأییدِ خودش نزدِ ربات می‌مونه."),

    (("اخطار", "اخطار تخلف", "تخلف", "اسکم", "کلاهبرداری", "شکایت", "قطع ربات", "قوانین", "گزارش شکایت", "حذف ربات"),
     "🚨 **اخطارِ تخلف و قوانینِ بات‌ساز**: اگه از رباتی بابتِ اسکم/کلاهبرداری شکایت بشه، مدیریتِ بات‌ساز به مالکش «اخطار تخلف ۱ از ۲» و بعد «۲ از ۲» می‌ده (با دلیل، زیرش دکمهٔ «📜 قوانین»). با **تکرارِ تخلف یا ثبتِ گزارشِ شکایت از طرفِ کاربرانِ ربات**، ربات بدونِ اطلاعِ قبلی قطع و از حسابِ مالک حذف می‌شه و دیگه قابلِ بازیابی نیست؛ هزینهٔ پلن و موجودی‌ها مسترد نمی‌شه. موارد ممنوع: کلاهبرداری و فروشِ سرویسِ جعلی، فیشینگ و سرقتِ اطلاعات، جعلِ هویت، قرعه‌کشیِ ساختگی/طرحِ هرمی، سوءاستفاده از کیف پولِ کاربران، اسپم و محتوایِ غیرقانونی. اگه کسی اخطار گرفته: مشکل رو همون لحظه برطرف کنه، با کاربرانِ شاکی تسویه کنه و قوانین رو بخونه؛ تو **حکمِ پایانی نده**، بگو برایِ اعتراض به پشتیبانیِ بات‌ساز پیام بده."),

    (("ربات اختصاصی", "ربات آماده", "فروش ربات اختصاصی", "تست ربات", "نصب خودکار", "پروژه آماده", "سرور فعال‌سازی", "تست ۲۰ دقیقه", "بعد از تست خاموش"),
     "🛍 **فروشِ رباتِ اختصاصیِ آماده** (تو خودِ بات‌ساز اصلی: «🛍 فروش ربات اختصاصی آماده»): پروژه رو انتخاب می‌کنه، می‌تونه «🧪 تستِ رایگان» (مثلاً ۲۰ دقیقه) بگیره، یا بخره: «📥 دریافتِ فایل» یا «🚀 خرید و فعال‌سازی رویِ سرورِ بات‌ساز» (توکنِ رباتِ خودش و آیدی ادمین رو می‌ده؛ پروژه خودکار نصب می‌شه؛ مدیریتش از «⚡️ سرور فعال‌سازی هوشمند»: روشن/خاموش، لاگ، فایل‌ها، .env). **رباتِ تست بعدِ تمام‌شدنِ مدت خودکار خاموش می‌شه** و پیامِ «تست تمام شد» با دکمهٔ «🛒 خرید پروژه» می‌ره (قبلاً خاموش نمی‌شد؛ رفع شد). هر کاربر برایِ هر پروژه فقط یک تست داره. اگه ربات جواب نداد اول «لاگ» رو ببینه؛ علتِ رایج توکنِ باطل است."),

    (("همکار", "ربات همکار", "انتقال به بات‌ساز اصلی", "جدا کردن از همکار", "دسترسی همکار", "نمایندگی ربات", "فروش همکار"),
     "🤝 **ربات همکار و جداشدن از همکار**: رباتی که از یه رباتِ همکار خریده شده تا وقتی «فروشِ همکار» برایش ثبت باشه، همکار می‌تونه از پنلِ «مشتریان» تمدید/توقف/تعویضِ توکن کنه. اگه مدیرِ بات‌ساز «🔓 انتقالِ ربات به بات‌سازِ اصلی» رو بزنه، ربات مستقیم زیرِ بات‌سازِ اصلی می‌ره و **همکار هیچ دسترسی‌ای بهش نداره** (نه پنلِ مشتریان، نه ویرایشِ دکمه‌ها از رباتِ همکار؛ ادمینِ ثبت‌شده‌اش هم حذف می‌شه). بعدش مالک خودش از «🗂 ربات‌هایِ من» تو بات‌سازِ اصلی ارتقا/تمدید می‌کنه. پلن/سررسید/مالکیت تغییر نمی‌کنه."),

    (("برگشت وجه", "بازگشت وجه", "استرداد", "عودت", "کارمزد برگشت", "ریفاند", "refund"),
     "↩️ **برگشتِ وجه**: خطایِ ساخت/تمدید/افزایشِ حجم → مبلغ خودکار و کامل به کیف پول برمی‌گرده. سفارشِ SMMِ ردشده → عودتِ کامل. تو بازارِ کاربران: لغو/تایم‌اوت/«برگشت به خریدار» → پولِ خریدار کامل برمی‌گرده (کارمزدی که فروشنده پرداخته برنمی‌گرده). **برگشتِ وجهِ سرویسِ ساخته‌شده (با کسرِ کارمزد)** در رباتِ VPN حالا خودکار است (تنظیمات ← «💸 برگشت وجه سرویس»؛ جزئیاتش در بخشِ «برگشت وجه سرویس VPN») — اگه مالک روشنش نکرده باشه، کاربر به مالکِ همون ربات پیام بده."),

    (("امتیاز", "امتیازِ رفرال", "جایزه امتیازی", "کسر امتیاز", "بازکردن این بخش", "ساخت کانفیگ با امتیاز", "refpoint"),
     "🏅 **امتیازگیری با رفرال**: «🎛 دکمه‌ها و تنظیمات» ← «🏅 امتیازگیری با رفرال» (روشن‌کردن، امتیازِ هر رفرال، نوعِ جایزه: انبار/ساختِ خودکار رویِ پنل/متن/محصول/SMM، حجم و مدتِ جایزه، انتخابِ پنل). بعد یه دکمه رو با «شرطی‌سازی با زیرمجموعه ← 🔒 گیتِ دسترسی ← 🏅 امتیازِ رفرال» به امتیاز وصل کن. **نکتهٔ مهم:** اگه رویِ اون دکمه یه «عملکردِ اصلی» (مثلاً منویِ خرید) هم تنظیم شده باشه، با کسرِ امتیاز فقط **همون بخش باز می‌شه** و پیامِ «🔓 N امتیاز برایِ بازکردنِ این بخش کسر شد» میاد — کانفیگِ جایزه ساخته نمی‌شه. برایِ اینکه با امتیاز **کانفیگ ساخته بشه**: دکمه «عملکردِ دیگه‌ای» نداشته باشه، نوعِ جایزه «ساخت خودکار رویِ پنل» باشه و حجم/مدتِ جایزه تنظیم شده باشه. بارِ دوم به بعد همون کانفیگِ قبلیِ کاربر شارژ می‌شه. اگه با این تنظیمات هم نساخت، پیامِ خطایی که ربات داد رو بفرسته."),
])

# === PATCH_GROUP_GUARD_KB_2026_10B_V1 ===
# آپدیتِ دومِ اکتبر ۲۰۲۶: قابلیت‌هایِ تازه‌ای که پس از بخشِ قبل اضافه/اصلاح شد.
# اگر با متنِ قدیمی‌تر فرق داشت، این‌ها درست‌اند (مثلاً «برگشتِ وجهِ سرویس» حالا خودکار دارد).
_GG_AI_TOPICS.extend([
    (("برگشت وجه سرویس", "برگشت وجه", "بازگشت وجه", "استرداد", "کارمزد برگشت", "refund", "vrf"),
     "↩️ **برگشتِ وجهِ سرویسِ VPN (جدید، خودکار)** — مخصوصِ رباتِ فروشِ VPN: مالک از «تنظیمات ← 💸 برگشت وجه سرویس» روشنش می‌کنه و **درصدِ کارمزد** و «محاسبهٔ روزهایِ باقی‌مانده» رو تعیین می‌کنه. مشتری از جزئیاتِ سرویسش «💸 برگشت وجه» رو می‌زنه، مبلغ و کارمزد رو می‌بینه و تأیید می‌کنه؛ سرویس از پنل حذف و مبلغِ خالص به کیف پولش برمی‌گرده. **مشمول نیستند:** تستِ رایگان، سرویسِ هدیه/امتیاز و سرویسی که قبلاً برگشت خورده؛ در سرویس‌هایِ همکارِ ۳۰ روزه روزهای باقی‌مانده هم حساب می‌شه. (این بخش جایگزینِ جملهٔ قدیمیِ «هنوز خودکار نداره» است.)"),

    (("احراز هویت", "KYC", "kyc", "سطح ۱", "سطح ۲", "سقف روزانه", "تغییر کارت", "مدارک", "هویت"),
     "🪪 **احرازِ هویت (KYC)** — فقط پلن‌هایی که قابلیتش رو دارن (رایگان ندارد؛ اگه از پلن افتاده، حتی روشن‌بودنش دیگه کاری نمی‌کنه و کاربر رو بلاک نمی‌کنه): سه سطح 0/1/2. سقفِ مبلغِ روزانه برایِ هر سطح تنظیم می‌شه؛ سطح ۰ که به سقف برسه همون روز بسته می‌شه، سطح ۱ تا تکمیلِ سطح ۲ حساب رو محدود می‌کنه. بازهٔ سقف می‌تونه غلتان (۲۴ ساعت) یا روزِ تقویمیِ تهران باشه. **تغییرِ کارتِ تاییدشده** طبقِ سیاستِ مالک (مثلاً یک‌بار در ۱۵ روز، قابلِ تنظیم) انجام می‌شه. کاربر از کیف پول ← «🪪 احراز هویت» شروع می‌کنه؛ ادمین درخواست‌ها رو تایید/رد می‌کنه."),

    (("نرخ خودکار", "قیمت استارز", "استارز خودکار", "قیمت پایه", "نرخ دلار", "سود استارز", "گیفت استارزی", "ری‌اکشن استارزی", "stars_rate", "۳۹۰۰"),
     "🔄 **نرخِ خودکارِ استارز** (برایِ فروشِ استارز، گیفتِ استارزی و ری‌اکشنِ استارزیِ پست): هر کدوم دکمهٔ «🔄 نرخ خودکار» و «📈 درصدِ سود» دارن. قیمتِ ⭐ = قیمتِ پایهٔ ⭐ به دلار (پیش‌فرض ۰٫۰۱۵) × نرخِ دلار (خودکار از والکس/بایننس) × (۱ + سود). اگه عددِ نهایی با بازار نخوند، از «⚙️ قیمتِ پایهٔ ⭐/نرخِ دلار» می‌شه `قیمت 0.0155` یا `دلار 165000` (دستی) رو داد؛ `دلار 0` یعنی برگشت به نرخِ خودکار. منویِ گیفت «قیمتِ نهاییِ فروش (با سود)» رو هم نشون می‌ده. اگه نرخِ بازار در دسترس نباشه، خودکار روشن نمی‌شه و نرخِ دستی می‌مونه. پرمیوم هم حالا «🔄 نرخ خودکار» داره (بخشِ ۱۰C)."),

    (("انتخاب روش ارسال", "ربات یا پشتیبانی", "اکانت پشتیبانی", "گیفت استارزی", "موجودی استارز ربات", "منتظر موجودی", "ارسال خودکار گیفت", "gift_user_choice"),
     "🎁 **انتخابِ روشِ ارسالِ گیفت توسطِ مشتری**: «گیفت ← 🙋 انتخابِ روشِ ارسال توسطِ مشتری» (روشن/خاموش). روشن باشه، مشتری قبل از فاکتور می‌بینه: «🤖 ارسالِ خودکار توسطِ ربات» یا «👤 اکانتِ پشتیبانی (دستی)». ▪️ پشتیبانی → سفارش مثلِ تحویلِ دستی برایِ ادمین می‌ره (✅ انجام شد / ❌ رد). ▪️ ربات → فوراً ارسال می‌شه؛ اگه موجودیِ ⭐ ربات کم بود پولِ مشتری برنمی‌گرده و سفارش منتظر می‌مونه، به ادمین پیام با «💰 افزایشِ موجودی» و «🔄 ارسال (بعد از شارژ)» و «❌ رد و بازگشتِ وجه» می‌ره. بعد از شارژ ارسال خودکار نیست؛ ادمین «🔄 ارسال» رو می‌زنه. فقط گیفتِ استارزیِ رسمی؛ گیفتِ سفارشی/NFT همیشه دستیه."),

    (("گردونه", "گردونه شانس", "wheel", "چرخش", "جایزه گردونه", "اسلاید گردونه", "مینی اپ گردونه"),
     "🎡 **گردونهٔ شانسِ مینی‌اپ**: «/miniapp ← 🎡 گردونهٔ شانس». روشن/خاموش، تعدادِ اسلایدها (۲ تا ۱۲)، جایزه و «وزنِ شانس»ِ هر اسلاید (پوچ، موجودیِ کیف پول، کدِ تخفیفِ درصدی یا مبلغی)، «چند بار در هر چند ساعت» و سقفِ کلِ دفعاتِ هر کاربر. وقتی روشن باشه در مینی‌اپ هم کاشیِ صفحهٔ اصلی و هم یک دکمهٔ گردِ شناورِ 🎡 (پایین‌چپ) دیده می‌شه. نتیجه رو سرور تعیین می‌کنه؛ موجودی همون لحظه اضافه می‌شه؛ کدِ تخفیف یک‌بار مصرف و ۳۰ روزه است. فعلاً فقط در مینی‌اپِ فروشگاهی است، نه مینی‌اپ ۲."),

    (("راه‌اندازی حرفه‌ای ورود", "قوانین", "تایید قوانین", "خوش‌آمد", "تایپ‌شونده", "جلوه تایپ", "پین قوانین", "vso"),
     "🎓 **راه‌اندازیِ حرفه‌ایِ ورود** («🎛 دکمه‌ها و تنظیمات ← 🎓 راه‌اندازی حرفه‌ای ورود»؛ برایِ همهٔ انواعِ ربات از جمله VPN): قوانینِ اجباری + پیامِ خوش‌آمد. ▪️ با «اجباری‌کردنِ تأییدِ قوانین» تا وقتی کاربر «✅ تأیید قوانین» نزنه **هیچ منو/دکمه‌ای نشون داده نمی‌شه و کار نمی‌کنه** (فقط /start و دکمهٔ تأیید؛ مالک/ادمین معاف‌اند). قوانین دیگه **پین نمی‌شن**. ▪️ بعد از تأیید، خوش‌آمد **فوراً و یک‌جا** می‌آد و منو کنارش (جلوهٔ تایپ‌شونده حذف شده). ▪️ برایِ روشن‌کردنِ قوانین اول باید متنش رو بنویسی."),

    (("تست رایگان", "دستهٔ انبار", "دسته انبار تست", "کانفیگ آماده تست", "انبار دستی تست", "trial_category"),
     "🎁 **تستِ رایگان با دستهٔ انبار**: برایِ هر دکمهٔ تست (حالتِ «📦 کانفیگ آماده») می‌شه «🏷 دستهٔ انبارِ تست» رو انتخاب کرد؛ کاربر فقط از کانفیگ‌هایِ همون دسته (با قیمتِ ۰) تحویل می‌گیره و کنارِ هر دسته تعدادِ موجودی نوشته می‌شه. دسته رو موقعِ «➕ افزودنِ کانفیگ آماده» با «🏷 دسته‌بندی» می‌دی. هم در حالتِ «مقدارِ ثابت» هم «بازه‌ای» هست. دستهٔ خالی → «در این دسته موجود نیست»."),

    (("DNS", "دی‌ان‌اس", "انبار دستی DNS", "فروش DNS"),
     "🌐 **فروشِ DNS از انبارِ دستی**: «انبار کانفیگ‌هایِ دستی ← 🌐 DNS ← ➕ افزودنِ DNS»: فقط «لینک ساب» و «قیمت» لازمه؛ دستهٔ «DNS» و حجم/روزِ نامحدود خودکار پر می‌شه. برایِ فروش یه دکمهٔ «کانفیگ آماده/نامحدود» بساز و دسته‌اش رو DNS بذار (دکمه‌هایِ «همهٔ دسته‌ها» DNS رو هم نشون می‌دن). ثبتِ دسته‌جمعیِ DNS فعلاً نیست."),

    (("کارت دوم", "کارت پشتیبانی", "دو کارت", "جایگزین کارت", "کارت جایگزین"),
     "💳 **کارتِ دوم**: «مالی ← کارت به کارت»: علاوه بر کارتِ اصلی، «کارتِ دوم» (شماره + نام) تنظیم می‌شه؛ می‌شه جاشون رو عوض کرد (🔁) یا کارتِ دوم رو حذف کرد. مشتری می‌تونه به‌عنوانِ کارتِ جایگزین ازش استفاده کنه."),

    (("تون‌پیز", "tonpays", "پایا", "پل", "خریدار", "merchant", "buyer_chat_id", "هشدار"),
     "🪙 **تون‌پیز — نکته‌هایِ تازه**: ▪️ در پیامِ شارژ دیگه خطِ «🎯 مقصد/target» و متنِ پیش‌فرضِ شخصِ ثالث نیست. ▪️ هشدارِ «پایا/پل» و «تغییرِ کارت» اضافه شده. ▪️ اگه خطا بدهد «buyer_chat_id cannot be the merchant Telegram id» یعنی **صاحبِ درگاه نمی‌تونه با همون اکانتِ تلگرامش خریدارِ تست باشه** — با یه اکانتِ دیگه پرداخت رو امتحان کن (ربات پیامِ فارسیِ روشن می‌ده)."),

    (("بن", "مسدود", "بن کردن", "ادمین اصلی", "ادمین ربات", "بن ادمین"),
     "⛔ **بن کردنِ کاربر**: فقط **ادمینِ اصلیِ بات‌ساز** غیرقابلِ بن است؛ هر کاربرِ دیگه‌ای — حتی ادمین‌هایِ فرعیِ ربات و ادمینِ رباتِ همکار — می‌تونه بن بشه. دکمهٔ بن روی ادمینِ اصلی رد می‌شه. مالک‌هایِ ربات «👥 ادمین‌هایِ این ربات» رو (زیرِ «انتقالِ ربات به کاربرِ دیگر» در پنلِ مدیریتِ ربات) برایِ دیدن/حذفِ ادمین‌ها دارن."),

    (("منو", "متن استارت", "شیشه‌ای", "متن خانه", "متن بازگشت", "نقطه", "پیام نقطه", "پیش‌نمایش لینک", "لینک خام", "کارت لینک"),
     "🏠 **منو و متنِ استارت (شیشه‌ای)**: /start متنِ خوش‌آمد و منو رو در **یک پیام** می‌ده (حتی وقتی «مخفی‌کردنِ متنِ بازگشت به خانه» روشنه). با «متنِ استارت روی منو» (set:homemenutext) متنِ خوش‌آمد موقعِ برگشت به خانه هم زیرِ منو می‌آد. پیامِ نامرئیِ «نقطه» برایِ برداشتنِ کیبوردِ قدیمی فقط **یک‌بار برایِ هر کاربر** می‌ره، نه هر /start. کارتِ بزرگِ **پیش‌نمایشِ لینک** در پیام‌هایِ ربات پیش‌فرض **خاموشه** (لینک خام می‌ره)؛ فقط گیفتِ NFT/سفارشی و اسلایدشو پیش‌نمایش دارن."),

    (("مینی‌اپ", "ورود به لینک دلخواه", "تبدیل به مینی‌اپ", "دکمه مینی اپ", "جایگاه دکمه", "کیبورد مینی اپ", "پاداش دعوت مینی اپ"),
     "🛍 **مینی‌اپ — نکته‌هایِ تازه**: ▪️ هر دکمه می‌تونه با «تبدیل به مینی‌اپ» مینی‌اپِ فروشگاهی رو باز کنه (فقط وقتی مینی‌اپ روشن و محلش «داخلِ کیبورد» باشه)؛ گزینهٔ قبلی با اسمِ «ورود به لینک دلخواه (وب‌اپ)» برایِ بازکردنِ هر لینکِ وب‌اپ است. ▪️ جایگاهِ دکمهٔ مینی‌اپ در کیبورد (بالا/پایین) قابلِ جابه‌جایی است. ▪️ «پاداشِ دعوت» در مینی‌اپ دقیقاً طبقِ تنظیماتِ ربات است (ثابت/درصدی، با ورود یا بعد از اولین خرید) و جمعِ پاداش از تراکنش‌هایِ واقعی می‌آد."),

    (("ریفیل", "refill", "کنسل", "لغو سفارش", "سفارش مجدد", "پیگیری", "ردیابی", "کد پیگیری", "SMM"),
     "📈 **سفارش‌هایِ SMM — تازه‌ها**: ▪️ «🔁 سفارش مجدد» پیامِ ثبتِ سفارش رو دست‌نخورده نگه می‌داره و یه پیامِ جدید برایِ لینک/تعدادِ تازه می‌فرسته؛ زیرِ فاکتور فقط «🏠 استارت» می‌مونه. ▪️ «لغو» و «ریفیل» فقط برایِ سرویس‌هایی که پنل پشتیبانی می‌کنه نشون داده می‌شه و پاسخِ پنل به فارسیِ روشن ترجمه می‌شه (کنسلِ درخواست‌شده/قبلاً کنسل‌شده/در حالِ انجام). ▪️ سفارشِ ردشدهٔ استارزی با کیف پول، مبلغ رو برمی‌گردونه. ▪️ مرتب‌سازیِ سرویس‌ها از ارزان به گران با قیمتِ واقعیِ فروش است. ▪️ کدِ سفارش حالا همه‌جا یکی است: متغیرهایِ {{order_id}} و {{tracking_code}} هر دو همان کدِ پیگیریِ لیستِ سفارش‌هایِ پنلِ مدیریت را می‌دهند (بخشِ ۱۰C)."),

    (("شمارهٔ مجازی", "شماره مجازی", "HeroSMS", "SMSPool", "OzVinoo", "sms-activate", "کلید شماره", "hero:"),
     "📱 **شمارهٔ مجازی — ارائه‌دهنده‌ها**: علاوه بر SMSPool و OzVinoo، **HeroSMS** (سازگار با API ِ SMS-Activate) هم پشتیبانی می‌شه؛ کلیدِ HeroSMS رو با پیشوندِ `hero:` یا تشخیصِ خودکار ثبت می‌کنن. در گزارشِ «خرید موفق» اسمِ ارائه‌دهنده نمایش داده **نمی‌شه** (فقط «خودکار»). HeroSMS با حسابِ واقعی هنوز تست نشده؛ اگه خطا داد پیامِ دقیقش رو بفرستن."),

    # === PATCH_EYLAN_PANEL_V1 ===
    (("ایلان", "eylan", "EylanPanel", "پنل ایلان"),
     "🟠 **پنلِ ایلان (EylanPanel — OpenVPN/L2TP/Cisco/WireGuard/sing-box)** — فقط پلنِ حرفه‌ای، رباتِ فروشِ VPN: "
     "«📦 سرویس‌ها و محصولات ← 🤖 پنل VPN خودکار ← ➕ ثبت تنظیمات ← 🟠 پنل ایلان».\n"
     "  ▪️ آدرس: همان آدرسِ پنل (مثلاً https://panel.example.com:port). مرحلهٔ نام‌کاربری «-» است و در مرحلهٔ رمز "
     "**کلیدِ API (X-API-KEY)** را می‌فرستد — کلیدِ ادمینِ اصلی یا نماینده؛ کلیدِ نماینده فقط کاربرانِ خودش را می‌بیند.\n"
     "  ▪️ مرحلهٔ ۵: پروتکل‌هایی که خریدار می‌گیرد با کاما (openvpn، l2tp، cisco، wg1، wg2…، تگِ اینباندِ سینگ‌باکس، node:1) "
     "یا «همه» = OpenVPN + همهٔ وایرگارد/سینگ‌باکس‌هایِ مجاز.\n"
     "  ▪️ ساب‌لینک، کانفیگ‌هایِ سینگ‌باکس و فایل‌هایِ وایرگارد خودکار از پنل گرفته می‌شوند؛ تمدید، افزایشِ حجم، "
     "ریستِ مصرف/لینک، فعال/غیرفعال و حذف پشتیبانی می‌شود. تغییرِ نامِ کاربری را خودِ پنل ندارد."),

    # === PATCH_HEIMDALL_PANEL_V1 ===
    (("HEIMDALL", "هایمدال", "هیمدال", "heimdall", "mygharehshop"),
     "🛡 **پنل HEIMDALL** (نسلِ جدیدِ ۳X-UI؛ وایرگارد/V2Ray) — فقط پلنِ حرفه‌ای، رباتِ فروشِ VPN: «📦 سرویس‌ها و محصولات ← 🤖 پنل VPN خودکار ← 🛡 پنل HEIMDALL»؛ آدرسِ پنل **با مسیرِ مخفی** (مثلاً `https://host:port/<secret>` — بدونِ `/panel/admins`)، یوزرنیم و رمزِ ادمین (2FA خاموش)، و (اختیاری) آدرسِ ساب؛ بعد «🔌 تست اتصال». ساخت/تمدید/حجم/ریست/حذف/لینکِ ساب پشتیبانی می‌شود؛ برایِ inboundِ وایرگارد فایلِ .conf از رویِ کلیدهایِ کلاینت ساخته می‌شود. ادمینِ بدونِ مجوزِ inbounds.view پیامِ راهنما می‌گیرد. دو پنل = دومی با «چند پنلِ هم‌نوع»."),

    (("DEGERIS", "دگریس", "دیگریس", "degeris", "ثنایی جدید", "پنل ثنایی جدید", "degerist"),
     "🟣 **پنل DEGERIS (ثنایی جدید)** — فقط پلنِ حرفه‌ای، رباتِ فروشِ VPN: «📦 سرویس‌ها و محصولات ← 🤖 پنل VPN خودکار ← 🟣 پنل DEGERIS»؛ آدرسِ پنل **با مسیرِ مخفی** (مثلاً `https://host:2087/<secret>`)، یوزرنیم و رمزِ ادمین، و (اختیاری) آدرسِ ساب؛ بعد «🔌 تست اتصال» و فعال‌سازیِ فروش. همان API ِ ۳X-UIِ نسلِ جدید (clients) است: ساخت/تمدید/حجم/ریست/حذف/لینکِ ساب؛ برایِ فروش یه دکمه با «خرید از پنل DEGERIS» بساز. اگه ورود خطا داد: آدرس شاملِ مسیرِ مخفی باشه و 2FA روی پنل خاموش باشه (فعلاً کدِ دومرحله‌ای پشتیبانی نمی‌شه)."),

    (("نودکس", "Nodex", "GoGuard", "پنل نودکس", "خرید از پنل"),
     "🛰 **پنلِ Nodex (GoGuard v2)**: به لیستِ پنل‌هایِ VPN اضافه شده (ساخت/تمدید/حجم/لینکِ ساب) و در «خرید از پنل» و ربات همکار هم هست. اگه بعد از اتصال تو لیست نبود، رباتِ جدید رو نگه دارن و پنل رو دوباره ذخیره کنن."),

    (("قالب صفحه خرید", "متن بالای خرید", "placeholder", "{gb}", "صفحه خرید حجم", "متن خرید سرویس جدید", "تیتر خرید"),
     "✏️ **متنِ بالایِ «خرید سرویس جدید» قابلِ ویرایش**: از «✏️ تیترِ صفحه‌هایِ خرید و سرویس‌ها» متنِ صفحهٔ خریدِ حجم/روز رو با جاگذارهایِ `{gb} {days} {count} {price_gb} {price_day} {total} {discount} {balance} {warn} {title}` سفارشی می‌کنن. همچنین سرویسِ **نامحدود** دیگه «۰ گیگ» نشون داده نمی‌شه."),

    (("ثبت و فروش پنل", "فروش پنل", "پنل آماده", "فروش پنل به مشتری", "buy_panel_market", "پنل خریداری"),
     "🏷 **ثبت و فروش پنل** (پلنِ حرفه‌ای، رباتِ فروشِ VPN): «📦 سرویس‌ها و محصولات ← 🏷 ثبت و فروش پنل ← ➕ افزودنِ پنلِ جدید» (نام، دسته‌بندی، آدرس، یوزرنیم، پسورد، قیمت). برایِ **فروش به مشتری** یه دکمه بساز و «عملکردِ دکمه ← فروش ← 🏷 فروش پنل (ثبت و فروش پنل)» رو روش بذار (دیگه از «خرید کانفیگ ← فقط پنل» استفاده نمی‌شه؛ دکمه‌هایِ قدیمی خودکار به همین فروشگاه می‌رن). مشتری پنل‌ها رو به‌تفکیکِ دسته و قیمت می‌بینه، از کیف پول می‌خره و آدرس/یوزرنیم/پسورد با دکمهٔ کپی براش می‌آد؛ «پنل‌هایِ خریداری‌شده» در «کانفیگ‌هایِ من» هست. فعلاً پرداختِ مستقیمِ کارت‌به‌کارت ندارد (اول کیف پول رو شارژ می‌کنه)."),

    (("بازی", "بازی کن", "شرط بازی", "اولین شارژ", "اولین سفارش", "قفل بازی", "dga"),
     "🎲 **بازی «بازی کن و جایزه بگیر»**: برایِ هر بازی «🔒 شرطِ ورود» تنظیم می‌شه: بدونِ شرط / فقط بعد از **اولین شارژ** / فقط بعد از **اولین سفارش**. موجودیِ کیف پولِ گروه هم دکمهٔ آبی «موجودی» دارد."),
])


# === PATCH_GROUP_GUARD_KB_2026_10C_V1 ===
# آپدیتِ سومِ اکتبر ۲۰۲۶ (۱۵ مهر ۱۴۰۵): وب‌سرویس، روبینو، متغیرهایِ متن، استارز/پرمیوم، کانالِ سفارشات و...
# اگر با بخش‌هایِ قدیمی‌تر تناقض داشت، این‌ها درست‌اند.
_GG_AI_TOPICS.extend([
    (("وب‌سرویس", "وبسرویس", "API", "api", "api/v2", "پنل SMM", "اتصال پنل", "کلید API", "نماینده", "ریسلر", "services", "action=add"),
     "🔌 **وب‌سرویس (API) رباتِ خدماتِ مجازی** — مشتری‌ها می‌تونن رباتت رو مثلِ یه «پنلِ SMM» به پنل/رباتِ خودشون وصل کنن: آدرسِ `/api/v2` + کلید (پارامترِ `key`)، قراردادِ استانداردِ SMM با اکشن‌هایِ `services`، `balance`، `add`، `status`؛ واحدِ پول **تومان**. هر مشتری کلیدش رو از دکمهٔ «🔑 API» داخلِ ربات می‌گیره (🔄 تغییر کلید، 📋 سفارش‌هایِ API، 📘 مستندات). ▪️ لیستِ سرویس‌ها حالا **توضیحات** (desc/description) و دستهٔ همراه با نامِ پلتفرم دارد، مثلاً «تلگرام - ممبر کانال» (تا دسته‌هایِ هم‌نامِ تلگرام/روبیکا قاطی نشن). ▪️ سرویس‌هایِ بی‌دسته دیگه زیرِ «General» نمی‌رن؛ با «پلتفرم - نوع» (مثلاً «اینستاگرام - فالوور») یا «سایر» می‌آن. ▪️ **شمارهٔ مجازی** (خودکار و دستی) عمداً در وب‌سرویس **نیست** چون دریافتِ کد از راهِ API ِ SMM ممکن نیست. ▪️ بعد از هر تغییر، طرفِ مقابل باید سرویس‌ها رو دوباره همگام‌سازی (sync) کنه تا لیستِ تازه بیاد."),

    (("استارز وبسرویس", "پرمیوم وبسرویس", "گیفت وبسرویس", "ری‌اکشن استارزی وبسرویس", "فروش استارز API", "پرمیوم API", "گیفت API", "anonymous"),
     "⭐ **استارز، پرمیوم، گیفت و ری‌اکشنِ استارزی در وب‌سرویس**: هر کدوم که **دکمه‌اش در منویِ اصلیِ ربات فعال باشه** در لیستِ سرویس‌هایِ API با دسته‌هایِ «تلگرام - استارز / پرمیوم / گیفت / ری‌اکشن استارزی» می‌آد، با همان قیمتِ ربات (+ تخفیفِ اختصاصیِ API ِ مشتری). ▪️ `link`: برایِ استارز/پرمیوم/گیفت **یوزرنیمِ گیرنده** (@username)؛ برایِ ری‌اکشن **لینکِ پستِ کانالِ عمومی** (https://t.me/channel/123) و با `anonymous=1` ناشناس. ▪️ پرمیوم quantity همیشه ۱. ▪️ قیمت طبقِ قراردادِ SMM: مبلغ = rate × تعداد ÷ ۱۰۰۰ (فیلدِ price قیمتِ یک عدد). ▪️ سفارش مثلِ سفارشِ خودِ ربات به کانال/ادمین با «✅ انجام شد / ❌ رد» می‌ره و اگه TGTools خودکار روشن باشه خودکار انجام می‌شه؛ رد = برگشتِ وجه. گیفتِ سفارشی/NFT در API نیست."),

    (("اعلان سفارشات وبسرویس", "اعلان وبسرویس", "#وبسرویس", "رسید وبسرویس", "پیام سفارش API", "نوتیف API", "پیام وضعیت API"),
     "🔔 **اعلانِ سفارشاتِ وب‌سرویس**: در صفحهٔ «🔑 API» هر مشتری دکمهٔ «🟢/🔴 اعلان سفارشات وبسرویس» هست (پیش‌فرض **خاموش** تا پیویِ نماینده‌ها با صدها سفارش شلوغ نشه). روشن باشه: با هر سفارشِ API یه رسید می‌آد و بعد هر تغییرِ وضعیت (در حال انجام، تکمیل، نیمه‌تمام، لغو/برگشتِ وجه)؛ اولِ همه‌شون «#وبسرویس». ▪️ رسید **همون متنی است که ادمین در «✍️ متن‌هایِ سفارش» گذاشته** («🚀 ثبت روی پنل» برایِ سفارشِ خودکار، «⏳ ثبت سفارش (منتظرِ تأیید)» برایِ دستی) با همهٔ متغیرها؛ اگه متنی تنظیم نشده، رسیدِ پیش‌فرض (محصول، نوعِ اپلیکیشن، تعداد، قیمتِ هر ۱۰۰۰، لینک، کدِ سفارش، مبلغ، موجودیِ جدید، تاریخ). ▪️ سفارش‌هایِ داخلِ خودِ ربات مثلِ قبل اطلاع داده می‌شن."),

    (("متغیر", "متغیرها", "order_id", "tracking_code", "price_per_1000", "category", "platform", "app", "کد سفارش", "نوع اپلیکیشن", "قیمت هر ۱۰۰۰", "قیمت هر 1000"),
     "🔤 **متغیرهایِ متنِ سفارش (تازه‌ها)**: ▪️ `{{order_id}}` و `{{tracking_code}}` هر دو **همان کدِ پیگیریِ لیستِ سفارشاتِ پنلِ مدیریت** هستند (قبلاً order_id شمارهٔ داخلی بود و با پنل نمی‌خوند). ▪️ `{{price_per_1000}}` نرخِ واقعیِ هر ۱۰۰۰ِ همان سرویس است (قبلاً مبلغِ کل ÷ تعداد بود و با حداقلِ مبلغِ سفارش عددِ غلط می‌داد؛ اگه مشتری تخفیف داشته، نرخِ پرداخت‌شده). ▪️ `{{category}}` = **نامِ دسته** (مثلاً «ری‌اکشن پرمیوم»)؛ برایِ «نوعِ اپلیکیشن» از متغیرِ جدیدِ **`{{platform}}`** (یا `{{app}}`) استفاده کن که «تلگرام/اینستاگرام/روبینو/…» می‌ده. اگه کسی گفت «نوع اپلیکیشن اسمِ دسته رو می‌زنه»، یعنی باید {{category}} رو با {{platform}} عوض کنه."),

    (("روبینو", "rubino", "روبیکا", "دسته روبینو", "دکمه پلتفرم", "پلتفرم SMM مشخص", "چیزی وصل نشده", "سرویسی برای این پلتفرم"),
     "🟣 **روبینو و دسته‌ها**: باگی که «دسته‌هایِ روبینو رو به دکمه وصل می‌کنم، مشتری می‌زنه چیزی وصل نیست» رفع شده. علت: فروشگاه و دکمهٔ «🌐 پلتفرمِ SMM مشخص» پلتفرم رو فقط از **اسمِ سرویس** حدس می‌زد، نه از دسته‌ای که ادمین سرویس رو داخلش گذاشته. حالا هر سرویسی که داخلِ یه دسته باشه، همه‌جا (لیستِ خدمات، دکمهٔ پلتفرم، مینی‌اپ، مدیریتِ محصولات، اتصالِ سریع) **پلتفرمِ همون دسته** رو می‌گیره؛ جابه‌جاکردنِ سرویس بینِ دسته‌ها هم فوراً اعمال می‌شه. اسم‌هایی مثلِ «Rubika Rubino …» هم درست روبینو تشخیص داده می‌شن. اگه هنوز خالی بود: سرویس‌ها فعال باشن و داخلِ دستهٔ روبینو باشن، و یه بار دکمه رو بزنه."),

    (("کانال سفارشات", "کانال سفارش دستی", "سفارشات دستی", "کانال انجام سفارشات", "پیوی شلوغ", "سفارش دستی کانال"),
     "🛰 **کانالِ انجامِ سفارشات** (رباتِ خدماتِ مجازی): «💰 مالی و کیف پول ← 🛰 متصل‌کردنِ کانالِ انجامِ سفارشات» — دو کانالِ جدا: «🤖 سفارشاتِ خودکار (پنل)» و «✋ سفارشاتِ دستی». وقتی کانالِ دستی وصل و فعال باشه، **همهٔ** سفارش‌هایی که کارِ دستی می‌خوان (سرویس‌هایِ دستیِ SMM، سفارش‌هایِ عمده/API/امتیازی، گیفت، استارز، پرمیوم، ری‌اکشنِ استارزی، TGTools برگشت‌خورده، مینی‌اپ) با دکمه‌هایِ «✅ انجام شد / ❌ رد» به کانال می‌رن نه پیوی؛ بعد از رسیدگی، پستِ کانال علامت می‌خوره. اگه کانال وصل نباشه، مثلِ قبل پیوی ادمین‌ها."),

    (("افزایش موجودی استارز ربات", "موجودی استارز ربات", "شارژ استارز ربات", "پرداخت با استارز فعال نشده", "w:botstars", "استارز ربات کم"),
     "💰 **شارژِ موجودیِ استارزِ خودِ ربات** (برایِ ارسالِ خودکارِ گیفت/پرمیوم): دکمهٔ «💰 افزایشِ موجودیِ استارزِ ربات» (در تنظیماتِ گیفت/پرمیوم، لیستِ سفارش‌هایِ گیفت و پیامِ «موجودی کافی نیست») حالا **مسیرِ جدا** دارد: موجودیِ فعلیِ ربات رو نشون می‌ده، ادمین تعداد رو انتخاب می‌کنه (۱۰۰ تا ۵۰۰۰ یا دلخواه)، فاکتورِ استارز می‌آد و بعد از پرداخت استارزها **مستقیم به موجودیِ ربات** اضافه می‌شن؛ کیف‌پولِ تومانی شارژ **نمی‌شه** و لازم نیست «پرداخت با استارز» برایِ مشتری‌ها روشن باشه. (پیامِ قدیمیِ «پرداخت با استارز تلگرام هنوز توسط مدیر ربات فعال نشده» از همین دکمه دیگه نمی‌آد.) بعد از شارژ، سفارشِ منتظر با «🔄 ارسال» فرستاده می‌شه."),

    (("نرخ خودکار پرمیوم", "پرمیوم خودکار", "قیمت پرمیوم", "پرمیوم نرخ", "premium_auto"),
     "🌟 **نرخِ خودکارِ پرمیوم**: «پرمیوم ← 🔄 نرخ خودکار» با هر بار زدن بینِ سه حالت عوض می‌شه: خاموش → **⭐ استارز** (نرخِ بازارِ ⭐ × تعدادِ استارزِ هر پلن) → **💎 فرگمنت** (قیمتِ دلاریِ هر پلن در فرگمنت، پیش‌فرض ۱۱٫۹۹ / ۱۵٫۹۹ / ۲۸٫۹۹ دلار، × نرخِ دلار؛ از دکمهٔ «💵 قیمتِ دلاریِ فرگمنت» قابلِ ویرایش). در هر دو حالت «📈 درصدِ سود»ِ پرمیوم روش می‌آد و نرخِ دلارِ دستی از «⚙️ قیمتِ پایهٔ ⭐/نرخِ دلار» اولویت داره. حالتِ فرگمنت معمولاً ارزان‌تر و نزدیک‌تر به قیمتِ واقعیِ خرید است. «قیمتِ شخصی»ِ هر پلن (۳/۶/۱۲ ماهه) همچنان اولویت داره؛ اگه TGTools ِ پرمیوم روشن باشه قیمت از نرخِ TGTools می‌آد."),

    (("فرگمنت", "fragment", "Fragment", "استارز خودکار فرگمنت", "پرمیوم فرگمنت", "کیف پول TON ربات"),
     "⚠️ **فرگمنت (Fragment) — وضعیتِ فعلی**: بخشِ «استارزِ خودکار — فرگمنت» فعلاً فقط کیف‌پولِ TON ِ جدا می‌سازه، اتصال (کوکی/کلید) و «🧪 تستِ خریدِ استارز» و «💸 برداشتِ TON» داره؛ **سفارش‌هایِ واقعیِ مشتری (استارز و پرمیوم) هنوز خودکار از فرگمنت انجام نمی‌شن** و برایِ ادمین دستی می‌رن (یا با TGTools اگه روشن باشه). پس فعلاً بیشتر از مقدارِ تست، کیف‌پولش رو شارژ نکنن. ارسالِ خودکارِ استارز و پرمیوم از فرگمنت در دستِ ساخت است. اگه تستِ خرید گفت کتابخونهٔ pyfragment نصب نیست، مدیرِ سرور باید نصبش کنه."),

    (("TNumber", "تی نامبر", "tnumber", "OzVinoo", "ازوینو", "شماره مجازی خودکار", "افزودن ارائه دهنده", "لغو شماره", "قابل لغو نیست"),
     "📱 **شمارهٔ مجازیِ خودکار — TNumber و OzVinoo**: در «شمارهٔ مجازیِ خودکار» دو ارائه‌دهنده هست: «➕ افزودن ارائه‌دهنده» (OzVinoo، کلیدِ Bearer از پنلِ OzVinoo؛ آدرسِ API ِ آن api.ozvinoo.xyz است) و **«➕ افزودن TNumber»** (کلیدِ API از b.tnumber.site). فقط یکی هم کافیه. بعد «همگام‌سازی کشورها»، درصدِ سود، روشن‌کردن. کشورهایِ تکراری یکی می‌شن (گران‌ترین قیمت). ▪️ وضعیتِ این سفارش‌ها دیگه با پیگیرِ SMM قاطی نمی‌شه. ▪️ اگه لغو نشد، پیام دلیلِ روشن می‌ده («دیگر قابل لغو نیست» یعنی کد اومده یا مهلت گذشته)."),

    (("کارمزد انتقال", "انتقال موجودی", "انتقال در گروه", "کارمزد از فرستنده", "کارمزد از مبلغ", "انتقال [مبلغ]", "تاپیک"),
     "💸 **کارمزدِ انتقالِ موجودی در گروه**: «🛠 پنل مدیریت ← 🎛 دکمه‌ها و تنظیمات ← ⚙️ تنظیماتِ گروه»: اول «✏️ درصدِ کارمزد» و روشن‌کردنِ کارمزد، بعد دکمهٔ **«👤 کارمزد از: …»** که بینِ دو حالت عوض می‌شه: ▪️ «از خودِ مبلغ» (قدیمی): ۱۰۰٬۰۰۰ منتقل ← گیرنده ۹۵٬۰۰۰. ▪️ «از حسابِ فرستنده»: گیرنده کاملِ ۱۰۰٬۰۰۰ رو می‌گیره و از فرستنده ۱۰۵٬۰۰۰ کم می‌شه؛ اگه موجودی برایِ هر دو کافی نباشه، ربات می‌گه چقدر لازمه. در گروه‌هایِ تاپیک‌دار انتقال فقط با **ریپلای رویِ پیامِ خودِ گیرنده** انجام می‌شه (دیگه پول به سازندهٔ تاپیک نمی‌ره)."),

    (("ادمین فقط افزودن", "فقط اتصال پنل", "ادمین محدود", "دسترسی ادمین", "ادمین پنل جدید", "بکاپ", "پشتیبان‌گیری"),
     "👮 **دسترسیِ «🔌 فقط اتصالِ پنلِ جدید (VPN)»** برایِ ادمین‌هایِ فرعی: در «👮 ادمین‌ها و پشتیبانی» رویِ ادمین ← این گزینه. چنین ادمینی فقط می‌تونه **پنلِ VPN ِ جدید وصل کنه** و به بقیهٔ بخش‌ها (مالی، کاربران، حذف/ویرایشِ پنل‌هایِ قبلی…) دسترسی نداره و پیام‌هایِ سفارش/مالی هم براش نمی‌ره. ▪️ **بکاپ**: پنل‌ها و پلن‌هایِ VPN هم داخلِ پشتیبان هستن؛ گرفتن و بازگرداندنِ بکاپ فقط کارِ **مالکِ ربات** است."),

    (("دامنه اختصاصی", "دامنه اختصاصی API", "مستندات API", "دامنه وبسرویس", "api.", "SSL", "گواهی"),
     "🌐 **دامنهٔ اختصاصی برایِ وب‌سرویس**: در تنظیماتِ API دکمهٔ «🌐 افزودنِ دامنهٔ اختصاصی» هست. ادمین دامنه/زیردامنه (مثلاً api.domain.ir) رو ثبت می‌کنه و ربات دو رکورد می‌ده: **TXT** با نامِ `_botsaz-verify.<دامنه>` برایِ اثباتِ مالکیت، و **A** (یا برایِ زیردامنه CNAME) به سرورِ بات‌ساز — مقدارِ دقیقِ هر دو در همون صفحه نوشته شده. بعد از تأیید، سرور به‌صورتِ خودکار (هر ۱۰ دقیقه) گواهیِ SSL می‌گیره و آدرسِ API و صفحهٔ مستندات رویِ همون دامنه بالا می‌آد. اگه بعدِ ۱۵-۲۰ دقیقه باز نشد: رکوردِ DNS درست باشه و پروکسیِ کلودفلر (ابرِ نارنجی) خاموش باشه."),
])

# === PATCH_GROUP_AI_REVAMP_V1 === بخش‌هایِ تازه (کانفیگ دستی، تحویل دستی، بازی‌هایِ شرط‌بندی، گزارش، …)
_GG_AI_TOPICS.extend(_GG_AI_TOPICS_NEW)


def _gg_select_relevant_topics(question: str, char_budget: int = 11000) -> str:
    """PATCH_GROUP_AI_REVAMP_V1 — انتخابِ بخش‌هایِ مرتبط با سوال؛ جست‌وجویِ وزن‌دار (IDF + مرزِ کلمه + عبارت)
    در platform_ai_kb. قبلاً کلمه‌هایِ عمومی («ای»، «بات»، «نمی») بخش‌هایِ بی‌ربط را بالا می‌آوردند."""
    return _kb_select(question, _GG_AI_TOPICS, char_budget)


async def _build_ai_system_prompt(question: str = "") -> str:
    """PATCH_GROUP_GUARD_CUSTOM_KNOWLEDGE_V1 — پرامپتِ پایه رو با «دانشِ سفارشیِ»
    ادمین (که هر وقت لازم شد از پنل آپدیت می‌کنه، بدونِ نیاز به تغییرِ کد یا
    ری‌استارتِ سرور) ترکیب می‌کنه؛ همیشه در لحظهٔ پرسیدن خونده می‌شه، پس همیشه
    آخرین نسخه‌ست.
    PATCH_GROUP_GUARD_KB_RETRIEVAL_V1 — فقط بخش‌هایِ مرتبط با سوال از میانِ
    دانشِ بزرگِ موضوعی انتخاب می‌شن (نه کلِ متن)، تا رویِ ارائه‌دهنده‌هایی با
    سقفِ توکنِ محدود (مثلِ تیرِ رایگانِ Groq) هم جواب بده.
    PATCH_GROUP_GUARD_FULL_PROMPT_RESTORE_V1 — وقتی ارائه‌دهنده Groq نباشه
    (مثلِ آروان که سقفِ توکنِ تنگ نداره)، دیگه نیازی به این محدودسازی
    نیست — کلِ دانشِ کامل (همون چیزی که قبل از این بهینه‌سازی فرستاده
    می‌شد) رو می‌فرستیم، تا هیچ جزئیاتی احتمالاً جا نیفته."""
    extra = await get_pset("group_guard_ai_custom_knowledge", "")
    from config import GROUP_GUARD_AI_BASE_URL as ARVAN_AI_BASE_URL  # === PATCH_GROUP_GUARD_FULL_PROMPT_RESTORE_V1 ===
    is_groq = "groq.com" in (ARVAN_AI_BASE_URL or "").lower()
    # === PATCH_GROUP_GUARD_AI_COST_V1 ===
    # هزینه: برایِ غیرِ Groq هر سوال کلِ ~۴۰هزار کاراکترِ دانش (≈۱۵هزار توکن) می‌فرستاد. حالا پیش‌فرض «هوشمند» است:
    # فقط بخش‌هایِ مرتبط با سوال (تا سقفِ بودجه). حالتِ قبلی با تنظیمِ group_guard_ai_prompt_mode=full برمی‌گردد.
    _mode = (await get_pset("group_guard_ai_prompt_mode", "smart")).strip().lower()
    if _mode != "full" and question:
        from ai_router import get_router as _gr
        _budget = 9000 if _gr().configured() else (11000 if is_groq else 16000)   # سهمیهٔ توکنیِ رایگان کم است
        relevant = _gg_select_relevant_topics(question, _budget)
        topics_block = (
            "# بخش‌هایِ مرتبط با سوالِ کاربر (از میانِ دانشِ کاملِ ربات‌هایِ مستأجر)\n"
            + relevant) if relevant else ""
    else:
        topics_block = (
            "# دانشِ کاملِ ربات‌هایِ مستأجر (همهٔ بخش‌ها)\n"
            + "\n\n".join(text for _kw, text in _GG_AI_TOPICS))
    parts = [_GG_AI_ALWAYS]
    if topics_block:
        parts.append(topics_block)
    if extra.strip():
        parts.append(
            "# نکاتِ اضافه/آپدیت‌شده توسطِ ادمین (اگه با بالا تناقض داشت، این‌ها ارجحن)\n"
            + extra.strip()
        )
    # === PATCH_GROUP_AI_REVAMP_V1 === مدل سبکِ پرامپت را تقلید می‌کند؛ پس ** و اعرابی که در دانشِ قدیمی هست حذف می‌شود
    return _gg_strip_marks("\n\n".join(parts).replace("**", ""))


# === PATCH_GROUP_GUARD_AI_COST_V1 === کشِ جوابِ سوال‌هایِ تکراری + پاسخِ آماده به سلام/تشکر
_AI_CACHE: dict[str, tuple[float, str]] = {}
_AI_CACHE_TTL = 6 * 3600
_AI_CACHE_MAX = 400
_CANNED = (
    (("سلام", "سلام خوبی", "سلام وقت بخیر", "درود", "hi", "hello", "های", "سلام بات ساز"),
     "🤖 سلام! 👋 سوالت دربارهٔ بات‌ساز رو بپرس."),
    (("ممنون", "مرسی", "تشکر", "دمت گرم", "خیلی ممنون", "ممنون از شما", "مرسی از شما", "تشکر میکنم",
      "thanks", "thx", "thank you"),
     "🤖 خواهش می‌کنم 🌹 اگه سوالِ دیگه‌ای داشتی بپرس."),
    (("خوبی", "چطوری", "حالت چطوره", "خوبی؟"), "🤖 ممنون، آماده‌ام کمکت کنم! سوالت چیه؟"),
)


def _ai_cache_key(question: str) -> str:
    return _gg_normalize_for_match(question)[:600]


def _ai_cache_get(question: str) -> str | None:
    hit = _AI_CACHE.get(_ai_cache_key(question))
    if hit and time.monotonic() - hit[0] < _AI_CACHE_TTL:
        return hit[1]
    return None


def _ai_cache_put(question: str, answer: str) -> None:
    if len(_AI_CACHE) >= _AI_CACHE_MAX:
        for k in sorted(_AI_CACHE, key=lambda k: _AI_CACHE[k][0])[: _AI_CACHE_MAX // 4]:
            _AI_CACHE.pop(k, None)
    _AI_CACHE[_ai_cache_key(question)] = (time.monotonic(), answer)


def _canned_reply(question: str) -> str | None:
    """پاسخِ آماده (بدونِ هوش مصنوعی) فقط به سلام/تشکر/احوال‌پرسیِ «خالص»؛ هر چیزی که سوال به نظر برسد None."""
    t = _gg_normalize_for_match(question).strip(" ؟?!.،,:؛-")
    if not t:
        return None
    for phrases, reply in _CANNED:
        if t in {_gg_normalize_for_match(w).strip(" ؟?!.،,:؛-") for w in phrases}:
            return reply
    return None


async def _ask_ai(question: str) -> str:
    """PATCH_GROUP_GUARD_ARVAN_AI_V1 — به‌جایِ Anthropic، از همون سرویسِ هوش
    مصنوعیِ آروان‌کلاد استفاده می‌کنه که برایِ خواندن/تاییدِ خودکارِ رسیدها
    (tenant/auto_receipt.py) از قبل تنظیم شده — یعنی نیازی به واردکردنِ
    کلیدِ جدا از پنلِ محافظِ گروه نیست؛ فقط کافیه ARVAN_AI_BASE_URL/
    ARVAN_AI_MODEL/ARVAN_AI_API_KEY در تنظیماتِ سرور (.env) ست شده باشن."""
    # === PATCH_GROUP_GUARD_SEPARATE_AI_PROVIDER_V1 ===
    # از متغیرهایِ اختصاصیِ محافظِ گروه استفاده می‌کنیم (نه ARVAN_AI_* که الان
    # مالِ Groq/رسیدهاست)، چون پرامپتِ این فیچر خیلی بزرگ‌تره و باید رو
    # سرویسی بمونه که ظرفیتِ کافی داره (مثلِ آروان).
    from config import GROUP_GUARD_AI_BASE_URL as ARVAN_AI_BASE_URL
    from config import GROUP_GUARD_AI_MODEL as ARVAN_AI_MODEL
    from config import GROUP_GUARD_AI_API_KEY as ARVAN_AI_API_KEY
    from config import GROUP_GUARD_AI_API_KEYS as ARVAN_AI_API_KEYS
    # === PATCH_AI_ROUTER_V1 === اگر مسیرهایِ رایگان تنظیم شده باشد، همه‌چیز از روتر می‌گذرد (فیل‌اوورِ فوری)
    from ai_router import get_router as _get_router
    _router = _get_router()
    if _router.configured():
        system_prompt = await _build_ai_system_prompt(question)
        text, usage, route = await _router.chat(system_prompt, question, max_tokens=600)
        logger.info("group_guard_ai usage: prompt=%s completion=%s total=%s route=%s",
                    usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens"), route)
        import re as _re0
        text = _re0.sub(r"<think>.*?</think>", "", text, flags=_re0.DOTALL | _re0.IGNORECASE).strip()
        return _gg_clean_ai_text(text) or "پاسخی دریافت نشد."
    if not ARVAN_AI_BASE_URL:
        raise RuntimeError(
            "آدرسِ endpointِ هوش‌مصنوعیِ محافظِ گروه (GROUP_GUARD_AI_BASE_URL) "
            "روی سرور تنظیم نشده.")
    import httpx
    import random  # === PATCH_GROUP_GUARD_MULTI_KEY_ROTATION_V1 ===
    url = ARVAN_AI_BASE_URL.rstrip("/") + "/chat/completions"
    system_prompt = await _build_ai_system_prompt(question)  # === PATCH_GROUP_GUARD_KB_RETRIEVAL_V1 ===
    payload = {
        "model": ARVAN_AI_MODEL,
        "temperature": 0,
        "max_tokens": 600,   # PATCH_GROUP_GUARD_AI_COST_V1 (جوابِ راهنما کوتاه است)
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
    }
    # === PATCH_ARVAN_MAX_TOKENS_CAP_V1 ===
    # بدونِ max_tokens، Groq یه پیش‌فرضِ بزرگ (۲۰۴۸) رو برایِ سهمیه رزرو
    # می‌کنه — رویِ اکانت‌هایی با سقفِ OTPM پایین، همین باعثِ ۴۲۹ فوری
    # می‌شد. ۹۰۰ برایِ جوابِ متنیِ راهنمای گروه معمولاً کافیه.
    # === PATCH_ARVAN_REASONING_EFFORT_NONE_V1 ===
    # اگه این فیچر رویِ یه مدلِ reasoning (مثلِ qwen3.6/gpt-oss روی Groq)
    # تنظیم بشه، فکرکردنِ خام رو خاموش می‌کنه (سریع‌تر، بدونِ اتلافِ توکن).
    # برایِ مدل‌هایِ غیرِ reasoning (مثلِ GPT-4.1) اثری نداره.
    _model_l = (ARVAN_AI_MODEL or "").lower()
    if "qwen" in _model_l:
        payload["reasoning_effort"] = "none"
    elif "gpt-oss" in _model_l:
        payload["reasoning_effort"] = "low"
    # آروان گاهی پاسخ 5xx یا chunk ناقص برمی‌گرداند؛ httpx در این gateway
    # پاسخ‌های chunked را پایدارتر از transport قبلی می‌خواند.
    import json
    timeout = httpx.Timeout(35.0, connect=8.0)
    last_err = None
    data = None
    async with httpx.AsyncClient(timeout=timeout, http2=False,
                                 headers={"Accept-Encoding": "identity"}) as client:
        # === PATCH_GROUP_GUARD_MULTI_KEY_ROTATION_V1 ===
        # با چند کلید (مثلاً چند اکانتِ رایگانِ Groq)، هر تلاش یه کلیدِ رندوم
        # انتخاب می‌کنه؛ اگه خالی بود همون تکی (سازگار با قبل).
        max_attempts = max(3, len(ARVAN_AI_API_KEYS))
        for attempt in range(max_attempts):
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            key = random.choice(ARVAN_AI_API_KEYS) if ARVAN_AI_API_KEYS else ARVAN_AI_API_KEY
            if key:
                headers["Authorization"] = f"Bearer {key}"
            try:
                resp = await client.post(url, json=payload, headers=headers)
                raw = resp.content
                body = raw.decode("utf-8", errors="replace")[:200]
                if resp.status_code == 200:
                    try:
                        data = json.loads(raw.decode("utf-8", errors="replace"))
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(f"آروان پاسخ JSON معتبر نداد ({type(exc).__name__}).") from exc
                    break
                if resp.status_code == 429:
                    # === PATCH_GROUP_GUARD_MULTI_KEY_ROTATION_V1 ===
                    if len(ARVAN_AI_API_KEYS) > 1 and attempt < max_attempts - 1:
                        last_err = "سهمیه‌ی این کلید تمام شده (429)؛ تلاش با کلیدِ دیگر..."
                        continue
                    raise RuntimeError("سهمیه/اعتبار تمام شده (429) — کیف‌پول/کلید رو شارژ یا عوض کن.")
                if resp.status_code in (500, 502, 503, 504, 529):
                    last_err = f"آروان خطای موقتِ سرور {resp.status_code}"
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    raise RuntimeError(last_err + "؛ بعداً دوباره تلاش کنید.")
                raise RuntimeError(f"آروان خطایِ HTTP {resp.status_code}: {body}")
            except httpx.TimeoutException as exc:
                last_err = "آروان در مهلت مقرر پاسخ نداد (timeout)"
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(last_err) from exc
            except httpx.HTTPError as exc:
                last_err = f"خطای شبکه یا پاسخ ناقص از آروان ({type(exc).__name__})"
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(last_err) from exc
        if data is None:
            raise RuntimeError((last_err or "آروان پس از چند تلاش پاسخ نداد") + "؛ بعداً دوباره تلاش کنید.")
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("آروان هیچ نتیجه‌ای برنگردوند.")
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    # === PATCH_GROUP_GUARD_AI_COST_V1 === مصرفِ توکن در لاگ ثبت می‌شود تا هزینه قابلِ پایش باشد
    try:
        _u = data.get("usage") or {}
        logger.info("group_guard_ai usage: prompt=%s completion=%s total=%s model=%s",
                    _u.get("prompt_tokens"), _u.get("completion_tokens"), _u.get("total_tokens"), ARVAN_AI_MODEL)
    except Exception:
        logger.debug("group_guard_ai: ثبتِ usage ممکن نشد")
    # === PATCH_GROUP_GUARD_REASONING_MODEL_SAFETY_V1 ===
    # مدل‌هایِ reasoning (مثلِ qwen3.6 روی Groq) قبل از جوابِ نهایی یه بلوکِ
    # <think>...</think> طولانی برمی‌گردونن. اگه این حذف نشه، فکرکردنِ خامِ
    # مدل عیناً توی گروه به کاربر نمایش داده می‌شه که هم بی‌ربطه هم زشت. برایِ
    # مدل‌هایی که این بلوک رو ندارن (GPT-4.1)، این regex بی‌اثره.
    import re as _re
    text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL | _re.IGNORECASE).strip()
    return _gg_clean_ai_text(text) or "پاسخی دریافت نشد."


# === PATCH_GROUP_GUARD_PUBLIC_AI_V1 ===
# پاسخ‌گویی خودکار به سوالِ کاربرها تویِ خودِ گروه، دو حالته:
#   ۱) کلمهٔ فعال‌سازی + همون‌جا سوال هم نوشته شده → بلافاصله جواب می‌ده.
#   ۲) فقط کلمهٔ فعال‌سازی (تنها) → حالتِ «منتظرِ سوال» برایِ همون شخص باز
#      می‌شه؛ اولین پیامِ بعدیِ همون شخص (فارغ از اینکه تریگر توش هست یا نه)
#      به‌عنوانِ سوال جواب داده می‌شه — دقیقاً همونی که خواستی («سوابش فعال بشه»).

_ai_question_times: dict[int, deque] = {}
_ai_awaiting: dict[tuple[int, int], float] = {}  # (chat_id, user_id) -> انقضا (monotonic)
_AI_AWAIT_TIMEOUT_SEC = 300  # ۵ دقیقه مهلت برایِ نوشتنِ سوال بعدِ گفتنِ کلمهٔ فعال‌سازی


async def _check_ai_rate_limit(user_id: int) -> bool:
    """True یعنی مجازه بپرسه (و این سوال رو هم ثبت می‌کنه)."""
    # === PATCH_GROUP_GUARD_AI_COST_V1 === پیش‌فرض: روشن، ۱۰ سوال در ۲۴ ساعت برایِ هر نفر (از پنل قابل‌تغییر)
    if (await get_pset("group_guard_ai_ratelimit_enabled", "on")) != "on":
        return True
    count_limit = int(await get_pset("group_guard_ai_ratelimit_count", "10") or "10")
    hours = int(await get_pset("group_guard_ai_ratelimit_hours", "24") or "24")
    window = hours * 3600
    now = time.monotonic()
    dq = _ai_question_times.setdefault(user_id, deque())
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= count_limit:
        return False
    dq.append(now)
    return True


# === PATCH_AI_REPLY_CONTEXT_V1 ===
# اگر کاربر رویِ یک پیام ریپلای کند و بعد «بات ساز» بگوید، منظورش همان پیام
# است. قبلاً فقط متنِ خودِ کاربر به مدل می‌رفت و مدل نمی‌دانست دربارهٔ چه
# چیزی صحبت می‌شود («این یعنی چی؟» بدونِ «این»).
_REPLY_CTX_MAX = 1500


def _reply_context(msg: Message) -> str:
    r = getattr(msg, "reply_to_message", None)
    if not r:
        return ""
    body = (r.text or r.caption or "").strip()
    if not body:
        # ریپلای رویِ عکس/فایل بدونِ متن — دستِ‌کم نوعش را بگو
        kind = ("عکس" if r.photo else "ویدیو" if r.video else
                "فایل" if r.document else "پیامِ بدونِ متن")
        return f"[کاربر رویِ یک {kind} ریپلای کرده که متنی ندارد.]"
    who = ""
    if r.from_user:
        who = r.from_user.full_name or ""
        if r.from_user.is_bot:
            who += " (ربات)"
    return (f"پیامی که کاربر رویش ریپلای کرده"
            + (f" — از طرفِ {who}" if who else "") + ":\n"
            f"«{body[:_REPLY_CTX_MAX]}»")


async def _answer_ai_question(msg: Message, question: str) -> None:
    """چکِ محدودیتِ نرخ + پرسیدنِ واقعی از آروان + ارسالِ جواب/خطا."""
    # === PATCH_GROUP_GUARD_AI_COST_V1 === سلام/تشکر → پاسخِ آماده بدونِ هزینه
    _trig_word = (await get_pset("group_guard_ai_trigger_word", "بات ساز")).strip()
    _q_only = question
    _tl = _trigger_at_start(question, _trig_word)
    if _tl is not None:
        _q_only = _norm_trigger_text(question).lstrip("@#!.,،؛:-—_ ")[_tl:].strip()
    _canned = _canned_reply(_q_only) if not getattr(msg, "reply_to_message", None) else None
    if _canned:
        try:
            await msg.reply(_canned)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    if not await _check_ai_rate_limit(msg.from_user.id):
        try:
            await msg.reply(
                "⏳ سقفِ تعدادِ سوال از هوش مصنوعی رو برایِ این بازه رد کردی؛ "
                "کمی بعد دوباره امتحان کن.")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    wait_msg = None
    try:
        wait_msg = await msg.reply("⏳ در حالِ بررسی…")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    try:
        # === PATCH_AI_REPLY_CONTEXT_V1 ===
        ctx = _reply_context(msg)
        _full_q = (ctx + "\n\nسوال/حرفِ خودِ کاربر: " + question) if ctx else question
        answer = _ai_cache_get(_full_q)
        if answer is None:
            answer = await _ask_ai(_full_q)
            if answer and not answer.startswith("پاسخی دریافت نشد"):
                _ai_cache_put(_full_q, answer)
    except Exception as e:
        err_text = f"❌ خطا در ارتباط با هوش مصنوعی:\n{e}"
        try:
            if wait_msg:
                await wait_msg.edit_text(err_text)
            else:
                await msg.reply(err_text)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return
    answer = _gg_clean_ai_text(answer) or answer      # PATCH_GROUP_AI_REVAMP_V1 — جوابِ کش‌شدهٔ قدیمی هم تمیز شود
    try:
        if wait_msg:
            await wait_msg.edit_text(f"🤖 {answer}")
        else:
            await msg.reply(f"🤖 {answer}")
    except Exception:
        try:
            await msg.reply(f"🤖 {answer}")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# === PATCH_AI_TRIGGER_AT_START_V1 ===
# کلمهٔ فعال‌سازی فقط وقتی «صدا زدن» حساب می‌شود که پیام با آن *شروع* شده باشد.
# قبلاً هر جایِ متن که می‌آمد کافی بود، برایِ همین جمله‌هایی مثل «من رفتم بات‌ساز
# این کارو کردم» اشتباهی ربات را صدا می‌زد و وسطِ گفتگویِ آدم‌ها می‌پرید.
_ZWNJ = "\u200c"


def _norm_trigger_text(t: str) -> str:
    """یکسان‌سازی برایِ مقایسه: نیم‌فاصله→فاصله، ی/ک عربی→فارسی، فشرده‌سازیِ فاصله‌ها."""
    t = (t or "").replace(_ZWNJ, " ").replace("\u064a", "\u06cc").replace("\u0643", "\u06a9")
    return " ".join(t.split()).lower()


def _trigger_at_start(text: str, trigger: str) -> int | None:
    """اگر پیام با کلمهٔ فعال‌سازی شروع شده باشد، طولِ مصرف‌شده از متنِ نرمال‌شده را
    برمی‌گرداند؛ وگرنه None. علائم/منشن‌هایِ ابتدای پیام نادیده گرفته می‌شوند."""
    if not trigger:
        return None
    norm_text = _norm_trigger_text(text).lstrip("@#!.,،؛:-—_ ")
    norm_trig = _norm_trigger_text(trigger)
    if not norm_trig or not norm_text.startswith(norm_trig):
        return None
    rest = norm_text[len(norm_trig):]
    # نباید وسطِ یک کلمهٔ بزرگ‌تر افتاده باشد (مثلاً «بات‌سازها»)
    if rest and rest[0].isalnum():
        return None
    return len(norm_trig)


# فهرستِ توهین‌هایِ رایج برایِ تذکرِ فوری. هدف کاملْ‌بودن نیست (دورزدنش همیشه
# ممکن است)، هدف این است که پرتکرارترین‌ها بدونِ نیاز به هوش مصنوعی و بدونِ
# مصرفِ سهمیه، همان‌جا یک تذکر بگیرند.
#
# عمداً دو دسته شده:
#  • _ABUSE_CONTAINS: عبارت‌هایِ بلند و بی‌ابهام — هرجایِ کلمه بیایند (حتی با
#    پسوند مثل «احمقی») شمرده می‌شوند.
#  • _ABUSE_EXACT: کلمه‌هایِ کوتاه که داخلِ کلمه‌هایِ کاملاً بی‌ضرر هم پیدا
#    می‌شوند («خر» در «خرید»، «کس» در «عکس») — این‌ها فقط اگر *خودشان* یک کلمهٔ
#    مستقل باشند توهین حساب می‌شوند، وگرنه سوال‌هایِ سالم را بلاک می‌کردند.
_ABUSE_CONTAINS = {
    "مادرجنده", "مادر جنده", "جنده", "حرومزاده", "حروم زاده", "پدرسگ",
    "بیشرف", "بی شرف", "بیناموس", "بی ناموس", "بیشعور", "بی شعور",
    "کسکش", "کس کش", "کسخل", "کس خل", "دیوث", "عوضی", "کثافت", "آشغال",
    "اشغال", "لاشی", "احمق", "خنگ", "اسکل", "گاييد", "گایید", "گاییدم",
    "بگام", "خفه شو", "خفشو", "زر نزن", "زرنزن", "زر مفت", "مزخرف",
    "به درد نخور", "بدردنخور", "بی عرضه", "بیعرضه",
    "fuck", "shit", "idiot", "stupid", "moron", "bullshit",
}
_ABUSE_EXACT = {
    "کیر", "کص", "کس", "کون", "گوه", "گه", "خر", "الاغ", "بیشعور",
}


def _detect_abuse(text: str) -> str | None:
    """اگر در متن توهین دیده شود همان عبارت را برمی‌گرداند، وگرنه None."""
    norm = _norm_trigger_text(text)
    for w in _ABUSE_CONTAINS:
        if _norm_trigger_text(w) in norm:
            return w
    words = set(norm.split())
    for w in _ABUSE_EXACT:
        if _norm_trigger_text(w) in words:
            return w
    return None


async def _warn_if_abusive(msg: Message, text: str) -> bool:
    """اگر پیامی که ربات را صدا زده توهین‌آمیز/مسخره‌کننده باشد، به‌جایِ فرستادن
    به هوش مصنوعی همان‌جا تذکر می‌دهد (و اگر محافظِ گروه روشن است، یک خطایِ
    رسمی هم برایِ کاربر ثبت می‌کند). خروجی True یعنی پیام مصرف شد."""
    if not _detect_abuse(text):
        return False
    note = ("⚠️ اینجا محترمانه صحبت می‌کنیم. اگر سوالی دربارهٔ بات‌ساز داری "
            "بپرس، با کمالِ میل جواب می‌دم — ولی توهین و مسخره‌بازی جواب نمی‌گیره.")
    try:
        guard_chat_id = await get_pset("group_guard_chat_id", "")
        guard_on = (await get_pset("group_guard_enabled", "off")) == "on"
        if guard_on and str(msg.chat.id) == guard_chat_id and msg.from_user \
                and not await _is_authorized(msg, msg.bot):
            cnt, threshold, auto_banned = await _apply_warn(
                msg.bot, msg.chat.id, msg.from_user,
                "توهین/بی‌احترامی در خطاب به ربات", warned_by=0)
            note += f"\n\nیک خطا برات ثبت شد ({cnt}/{threshold})."
            if auto_banned:
                note += f"\n🚫 چون به سقفِ {threshold} خطا رسیدی، بن شدی."
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده هنگام ثبتِ خطایِ توهین")
    try:
        await msg.reply(note)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return True


async def _check_ai_trigger(msg: Message, bot: Bot) -> bool:
    """خروجیِ True یعنی این پیام مصرف شد (جواب داده شد، یا واردِ حالتِ
    «منتظرِ سوال» شدیم، یا محدودیتِ نرخ خورد، یا خطایِ تنظیمات گفته شد) و
    پردازشِ بعدی لازم نیست.

    === PATCH_GROUP_GUARD_AI_DIAGNOSTIC_V1 ===
    قبلاً اگه ARVAN_AI_BASE_URL تنظیم نشده بود، همین اولِ کار بی‌سروصدا
    False برمی‌گشت — یعنی حتی اگه کاربر دقیق کلمهٔ فعال‌سازی رو هم می‌گفت،
    ربات کاملاً ساکت می‌موند و هیچ سرنخی برایِ پیداکردنِ علتِ خرابی نمی‌داد.
    الان اول چک می‌کنیم کلمهٔ فعال‌سازی گفته شده یا نه؛ اگه گفته شده ولی
    آروان تنظیم نیست، یک پیامِ تشخیصیِ روشن برمی‌گردونیم."""
    if _is_spam_bot_message(msg):
        return False
    if (await get_pset("group_guard_ai_public_enabled", "off")) != "on":
        return False
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return False

    key = (msg.chat.id, msg.from_user.id)
    trigger = (await get_pset("group_guard_ai_trigger_word", "بات ساز")).strip()
    trigger_l = trigger.lower()
    text_l = text.lower()
    # فقط «شروعِ پیام» به‌عنوانِ صدا زدن حساب می‌شود — نه هر جایِ متن.
    trig_len = _trigger_at_start(text, trigger)
    has_trigger = trig_len is not None
    was_awaiting = key in _ai_awaiting

    # === PATCH_GROUP_GUARD_AI_REPLY_TO_PROMPT_V1 ===
    # باگ: کاربر «بات ساز» می‌گفت، ربات «🤖 جانم! سوالت رو بپرس» را ریپلای
    # می‌کرد، کاربر رویِ همان پیام ریپلای می‌زد و سوالش را می‌فرستاد —
    # ولی هوش مصنوعی ساکت می‌ماند. علتش این بود که تشخیصِ «منتظرِ سوالیم»
    # فقط رویِ دیکشنری‌یِ درون‌حافظه‌ایِ _ai_awaiting تکیه داشت؛ اگر پروسهٔ
    # ربات بینِ آن دو پیام ری‌استارت شود (دیپلوی، کرش، …) یا اصلاً هر
    # دلیلِ دیگری، آن دیکشنری خالی می‌شود و کلید پیدا نمی‌شود.
    # راهِ مطمئن‌تر که به هیچ حافظه‌ای وابسته نیست: مستقیم چک کن که آیا
    # کاربر رویِ همان پیامِ «جانم بپرس»یِ خودِ ربات ریپلای زده — این‌جوری
    # حتی اگه دیکشنری خالی شده باشه هم درست تشخیص داده می‌شه.
    replying_to_prompt = False
    _rt = getattr(msg, "reply_to_message", None)
    if _rt and _rt.from_user and _rt.from_user.id == bot.id and "جانم" in (_rt.text or ""):
        replying_to_prompt = True

    if not has_trigger and not was_awaiting and not replying_to_prompt:
        return False  # نه تریگر گفته شده، نه قبلاً منتظرِ سوال بودیم، نه ریپلایِ روی «جانم» — کاری نداریم

    from config import ARVAN_AI_BASE_URL
    from ai_router import get_router as _get_router_c
    if not ARVAN_AI_BASE_URL and not _get_router_c().configured():
        try:
            await msg.reply(
                "⚠️ هوش مصنوعی هنوز کامل تنظیم نشده (ARVAN_AI_BASE_URL روی سرور "
                "خالیه). از ادمین بخواه از پنلِ «🛡 محافظ گروه → 🤖 هوش مصنوعی» "
                "وضعیتِ اتصال به آروان رو چک کنه.")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return True

    if has_trigger:
        # چون تریگر حتماً اولِ پیام است، باقی‌ماندهٔ سوال هرچیزیه که بعدش اومده
        norm_text = _norm_trigger_text(text).lstrip("@#!.,،؛:-—_ ")
        remainder = norm_text[trig_len:].strip(" ؟?!.،,:؛-")
        if await _warn_if_abusive(msg, text):
            return True
        if len(remainder) >= 3:
            # همون پیام، هم تریگر داره هم سوال — دیگه لازم نیست منتظر بمونیم
            _ai_awaiting.pop(key, None)
            await _answer_ai_question(msg, text)
            return True
        # === PATCH_AI_REPLY_CONTEXT_V1 ===
        # اگر فقط «بات ساز» گفته ولی رویِ پیامی ریپلای کرده، منظورش روشن است:
        # همان پیام را توضیح بده، به‌جایِ اینکه بپرسی «سوالت رو بپرس».
        if getattr(msg, "reply_to_message", None):
            _ai_awaiting.pop(key, None)
            await _answer_ai_question(
                msg, "دربارهٔ این پیام توضیح بده یا اگر سوالی در آن هست جوابش را بده.")
            return True
        # فقط کلمهٔ فعال‌سازی (تنها) گفته شده — وارد حالتِ «منتظرِ سوال» شو
        _ai_awaiting[key] = time.monotonic() + _AI_AWAIT_TIMEOUT_SEC
        try:
            await msg.reply("🤖 جانم! سوالت رو بپرس تا جواب بدم.")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return True

    # تریگر نبود — یا رویِ «جانم بپرس»یِ خودِ ربات ریپلای زده (مطمئن‌ترین
    # حالت، PATCH_GROUP_GUARD_AI_REPLY_TO_PROMPT_V1)، یا طبقِ دیکشنری هنوز
    # منتظرِ سوالش بودیم.
    if replying_to_prompt:
        _ai_awaiting.pop(key, None)
        if await _warn_if_abusive(msg, text):
            return True
        await _answer_ai_question(msg, text)
        return True

    expiry = _ai_awaiting.get(key)
    if expiry is not None and time.monotonic() < expiry:
        _ai_awaiting.pop(key, None)
        if await _warn_if_abusive(msg, text):
            return True
        await _answer_ai_question(msg, text)
        return True
    if expiry is not None:
        _ai_awaiting.pop(key, None)  # مهلت تموم شده؛ پاکش کن

    return False


@group_guard_router.callback_query(F.data == "gg:ai")
async def gg_ai_menu(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    # === PATCH_GROUP_GUARD_SEPARATE_AI_PROVIDER_V1 ===
    # این منو باید همون endpoint/مدلی رو نشون بده که _ask_ai واقعاً ازش
    # استفاده می‌کنه (GROUP_GUARD_AI_*)، نه ARVAN_AI_* که الان مالِ Groq/رسیدهاست.
    from config import GROUP_GUARD_AI_BASE_URL as ARVAN_AI_BASE_URL
    from config import GROUP_GUARD_AI_MODEL as ARVAN_AI_MODEL
    from ai_router import get_router as _get_router_m   # === PATCH_AI_ROUTER_V1 ===
    _router_m = _get_router_m()
    if not ARVAN_AI_BASE_URL and not _router_m.configured():
        text = (
            "🤖 <b>هوش مصنوعی</b>\n\n"
            "هنوز وصل نیست. یا روترِ رایگان: <code>GROUP_GUARD_AI_ROUTES=groq=KEY,openrouter=KEY,gemini=KEY,…</code>\n"
            "یا این متغیرها رو تویِ تنظیماتِ سرور (.env) ست کن:\n\n"
            "<code>GROUP_GUARD_AI_BASE_URL</code> — آدرسِ endpoint (تا /v1)\n"
            "<code>GROUP_GUARD_AI_MODEL</code> — اسمِ مدل\n"
            "<code>GROUP_GUARD_AI_API_KEY</code> — کلید\n\n"
            "بعدِ ست‌کردن و ری‌استارتِ سرویس، همین‌جا خودکار فعال می‌شه."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [hx_button("⬅️ بازگشت", callback_data="adm:group_guard", style=ButtonStyle.DANGER)],
        ])
    else:
        public_on = (await get_pset("group_guard_ai_public_enabled", "off")) == "on"
        trigger = await get_pset("group_guard_ai_trigger_word", "بات ساز")
        rl_on = (await get_pset("group_guard_ai_ratelimit_enabled", "on")) == "on"   # PATCH_GROUP_GUARD_AI_COST_V1
        rl_count = await get_pset("group_guard_ai_ratelimit_count", "10")
        rl_hours = await get_pset("group_guard_ai_ratelimit_hours", "24")
        rl_txt = f"{rl_count} سوال هر {rl_hours} ساعت" if rl_on else "نامحدود"
        text = (
            "🤖 <b>هوش مصنوعی</b>\n\n"
            + (f"متصل به <b>روترِ رایگان</b> — {len(_router_m.routes)} مسیر (جوابِ ناموفق → فوری مسیرِ بعدی):\n"
               f"<code>{_router_m.status()}</code>\n\n" if _router_m.configured()
               else f"متصل به آروان — مدل: <code>{ARVAN_AI_MODEL}</code>\n\n") +
            "🔹 <b>پرسیدنِ دستی</b>: سوالت رو دربارهٔ محافظ گروه یا مسیرِ "
            "فعال‌سازیِ قابلیت‌ها تو ربات‌هایِ مستأجر بپرس.\n\n"
            f"🔹 <b>پاسخ‌گوییِ عمومی تو گروه</b>: {'🟢 روشن' if public_on else '🔴 خاموش'}\n"
            f"کلمهٔ فعال‌سازی: <code>{trigger}</code>\n"
            f"هر کسی تو پیامش این کلمه رو بگه، بقیهٔ پیام به‌عنوانِ سوال جواب داده می‌شه.\n"
            f"محدودیتِ سوال: {rl_txt}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [hx_button("❓ پرسیدنِ سوال (دستی)", callback_data="gg:ai_ask", style=ButtonStyle.SUCCESS)],
            [hx_button(("🔴 خاموش کردنِ پاسخ‌گوییِ عمومی" if public_on else "🟢 روشن کردنِ پاسخ‌گوییِ عمومی"),
                                  callback_data="gg:ai_public_toggle",
                                  style=(ButtonStyle.DANGER if public_on else ButtonStyle.SUCCESS))],
            [hx_button("✏️ تغییرِ کلمهٔ فعال‌سازی", callback_data="gg:ai_trigger_edit")],
            [hx_button("⏱ محدودیتِ سوال", callback_data="gg:ai_ratelimit")],
            [hx_button("🧠 دانشِ سفارشی/آپدیت‌شده", callback_data="gg:ai_knowledge")],
            [hx_button("💰 تنظیمات کیف‌پول و انتقال گروه", callback_data="gg:wallet_settings")],
            [hx_button("🎲 تنظیمات شرط‌بندی گروه", callback_data="gg:bet_settings")],
            [hx_button("⬅️ بازگشت", callback_data="adm:group_guard", style=ButtonStyle.DANGER)],
        ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data == "gg:ai_public_toggle")
async def gg_ai_public_toggle(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_ai_public_enabled", "off")
    await set_pset("group_guard_ai_public_enabled", "off" if cur == "on" else "on")
    await gg_ai_menu(cb)


@group_guard_router.callback_query(F.data == "gg:ai_trigger_edit")
async def gg_ai_trigger_edit_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_ai_trigger_word", "بات ساز")
    await state.set_state(GgFSM.ai_trigger_word)
    await cb.message.answer(
        f"کلمهٔ فعال‌سازیِ فعلی: <code>{cur}</code>\n"
        "کلمهٔ جدید رو بفرست — هرکس تو پیامش این کلمه رو بگه، هوش مصنوعی جوابش رو می‌ده.",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.ai_trigger_word, F.text)
async def gg_ai_trigger_edit_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if len(txt) < 2:
        await msg.answer("کلمه خیلی کوتاهه؛ یه چیزِ معنادارتر بفرست.")
        return
    await set_pset("group_guard_ai_trigger_word", txt)
    await state.clear()
    await msg.answer(f"✅ کلمهٔ فعال‌سازی روی «{txt}» تنظیم شد.")


@group_guard_router.callback_query(F.data == "gg:ai_ratelimit")
async def gg_ai_ratelimit_menu(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    rl_on = (await get_pset("group_guard_ai_ratelimit_enabled", "off")) == "on"
    rl_count = await get_pset("group_guard_ai_ratelimit_count", "3")
    rl_hours = await get_pset("group_guard_ai_ratelimit_hours", "24")
    text = (
        "⏱ <b>محدودیتِ سوال از هوش مصنوعی</b>\n\n"
        f"وضعیت: {'🔒 محدود' if rl_on else '🔓 نامحدود'}\n"
        + (f"سقف: {rl_count} سوال به ازایِ هر کاربر، در هر {rl_hours} ساعت\n" if rl_on else "")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button(("🔓 نامحدود کردن" if rl_on else "🔒 محدود کردن"), callback_data="gg:ai_ratelimit_toggle",
                              style=(ButtonStyle.SUCCESS if rl_on else ButtonStyle.DANGER))],
        [hx_button("✏️ تغییرِ سقف/بازه", callback_data="gg:ai_ratelimit_edit")],
        [hx_button("⬅️ بازگشت", callback_data="gg:ai", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data == "gg:ai_ratelimit_toggle")
async def gg_ai_ratelimit_toggle(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_ai_ratelimit_enabled", "off")
    await set_pset("group_guard_ai_ratelimit_enabled", "off" if cur == "on" else "on")
    await gg_ai_ratelimit_menu(cb)


@group_guard_router.callback_query(F.data == "gg:ai_ratelimit_edit")
async def gg_ai_ratelimit_edit_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(GgFSM.ai_ratelimit_config)
    await cb.message.answer(
        "دو عدد با فاصله بفرست: «تعدادِ سوالِ مجاز» «بازهٔ زمانی (ساعت)»\n"
        "مثلاً: <code>3 24</code> یعنی «هر کاربر، ۳ سوال در هر ۲۴ ساعت»",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.ai_ratelimit_config, F.text)
async def gg_ai_ratelimit_edit_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    parts = txt.split()
    if len(parts) != 2 or not all(p.isdigit() and int(p) >= 1 for p in parts):
        await msg.answer("دو عددِ صحیحِ مثبت با فاصله بفرست، مثلاً: 3 24")
        return
    count, hours = parts
    await set_pset("group_guard_ai_ratelimit_count", count)
    await set_pset("group_guard_ai_ratelimit_hours", hours)
    await state.clear()
    await msg.answer(f"✅ تنظیم شد: {count} سوال هر {hours} ساعت (به ازایِ هر کاربر).")


# === PATCH_GROUP_GUARD_CUSTOM_KNOWLEDGE_V1 ===
# صادقانه: نمی‌شه به‌صورتِ خودکار و قابل‌اعتماد، هر بار که آپدیت/ری‌استارت
# می‌کنی، خودِ هوش مصنوعی بره کدِ سرور رو بخونه و بفهمه چی عوض شده — این کار
# نیاز به تحلیلِ کامل و دقیقِ کد داره که نه سریعه، نه قابل‌اعتماد (ممکنه
# چیزِ اشتباه بفهمه و به کاربر غلط بگه). راهِ عملی و قابل‌اعتماد: همین‌جا یه
# جعبه‌متنِ آزاد گذاشتم که هروقت قابلیتِ جدیدی اضافه کردی، در عرضِ ۳۰ ثانیه
# خودت با زبانِ ساده بنویسی چی اضافه شده — بدونِ نیاز به تغییرِ کد یا
# ری‌استارتِ سرور، همون لحظه رو جوابِ بعدی اثر می‌ذاره.

@group_guard_router.callback_query(F.data == "gg:ai_knowledge")
async def gg_ai_knowledge_menu(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_ai_custom_knowledge", "")
    text = (
        "🧠 <b>دانشِ سفارشی/آپدیت‌شده</b>\n\n"
        "هر وقت یه قابلیتِ جدید به ربات‌ها اضافه کردی که پرامپتِ پایه ازش خبر "
        "نداره، همین‌جا با زبانِ ساده بنویس (مثلاً «حالا یه بخشِ X هم زیرِ Y "
        "اضافه شده که کارش Z هست»). این متن به پرامپتِ پایه اضافه می‌شه و بدونِ "
        "نیاز به ری‌استارتِ سرور، همون لحظه رو جوابِ بعدی اثر می‌ذاره.\n\n"
        + (f"متنِ فعلی:\n{cur}" if cur else "چیزی اضافه نشده.")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("✏️ ویرایش", callback_data="gg:ai_knowledge_edit")],
        [hx_button("⬅️ بازگشت", callback_data="gg:ai", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data == "gg:ai_knowledge_edit")
async def gg_ai_knowledge_edit_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(GgFSM.ai_custom_knowledge)
    await cb.message.answer(
        "متنِ جدید رو بفرست (جایگزینِ متنِ قبلی می‌شه، نه اضافه‌شونده — اگه "
        "می‌خوای قبلی رو نگه داری، بخشِ جدید رو بهش بچسبون و کلش رو بفرست).\n"
        "برایِ پاک‌کردنِ کامل، بنویس «-».",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.ai_custom_knowledge, F.text)
async def gg_ai_knowledge_edit_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    await set_pset("group_guard_ai_custom_knowledge", "" if txt == "-" else txt)
    await state.clear()
    await msg.answer("✅ ذخیره شد؛ از همین الان تویِ جواب‌ها اثر داره." if txt != "-"
                     else "✅ دانشِ سفارشی پاک شد.")


@group_guard_router.callback_query(F.data == "gg:ai_ask")
async def gg_ai_ask_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    from config import ARVAN_AI_BASE_URL
    if not ARVAN_AI_BASE_URL:
        await cb.answer("اول باید ARVAN_AI_BASE_URL رو تویِ تنظیماتِ سرور ست کنی.", show_alert=True)
        return
    await state.set_state(GgFSM.ai_question)
    await cb.message.answer("❓ سوالت رو بفرست:", reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.ai_question, F.text)
async def gg_ai_ask_recv(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    await state.clear()
    wait_msg = await msg.answer("⏳ در حالِ پرسیدن از هوش مصنوعی…")
    try:
        answer = await _ask_ai(txt)
    except Exception as e:
        await wait_msg.edit_text(f"❌ خطا در ارتباط با هوش مصنوعی:\n{e}")
        return
    try:
        await wait_msg.edit_text(f"🤖 {answer}")
    except Exception:
        await msg.answer(f"🤖 {answer}")


@group_guard_router.callback_query(F.data == "gg:setthreshold")
async def gg_setthreshold_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_warn_threshold", "3")
    await state.set_state(GgFSM.threshold)
    await cb.message.answer(
        f"سقفِ فعلیِ خطا برایِ بنِ خودکار: {cur}\nعددِ جدید رو بفرست (مثلاً 3):",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.threshold, F.text)
async def gg_setthreshold_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if not txt.isdigit() or int(txt) < 1:
        await msg.answer("یک عددِ صحیحِ مثبت بفرست:")
        return
    await set_pset("group_guard_warn_threshold", txt)
    await state.clear()
    await msg.answer(f"✅ سقفِ خطا روی {txt} تنظیم شد.")


# === PATCH_GROUP_GUARD_CAPTCHA_V1 ===

@group_guard_router.callback_query(F.data == "gg:captcha_toggle")
async def gg_captcha_toggle(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_captcha_enabled", "off")
    await set_pset("group_guard_captcha_enabled", "off" if cur == "on" else "on")
    await gg_menu(cb)


@group_guard_router.callback_query(F.data == "gg:captcha_timeout")
async def gg_captcha_timeout_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = int(await get_pset("group_guard_captcha_timeout_sec", "300") or "300")
    await state.set_state(GgFSM.captcha_timeout)
    await cb.message.answer(
        f"مهلتِ فعلیِ کپچا: {cur // 60} دقیقه\nمهلتِ جدید رو به دقیقه بفرست (مثلاً 5):",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.captcha_timeout, F.text)
async def gg_captcha_timeout_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if not txt.isdigit() or int(txt) < 1:
        await msg.answer("یک عددِ صحیحِ مثبت (به دقیقه) بفرست:")
        return
    await set_pset("group_guard_captcha_timeout_sec", str(int(txt) * 60))
    await state.clear()
    await msg.answer(f"✅ مهلتِ کپچا روی {txt} دقیقه تنظیم شد.")


# === PATCH_GROUP_GUARD_FLOOD_V1 ===

@group_guard_router.callback_query(F.data == "gg:flood")
async def gg_flood_menu(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    on = (await get_pset("group_guard_flood_enabled", "off")) == "on"
    max_msgs = await get_pset("group_guard_flood_max_msgs", "5")
    window = await get_pset("group_guard_flood_window_sec", "10")
    mute_sec = int(await get_pset("group_guard_flood_mute_sec", "300") or "300")
    text = (
        "🌊 <b>محدودیتِ فلود</b>\n\n"
        "اگه یه عضوِ عادی (نه ادمین) بیشتر از حدِ مجاز، توی یه بازهٔ زمانیِ کوتاه "
        "پیام بفرسته، خودکار برایِ مدتی سکوت می‌شه.\n\n"
        f"وضعیت: {'🟢 روشن' if on else '🔴 خاموش'}\n"
        f"سقف: <b>{max_msgs}</b> پیام در <b>{window}</b> ثانیه\n"
        f"مدتِ سکوت: <b>{mute_sec // 60}</b> دقیقه"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button(("🔴 خاموش کردن" if on else "🟢 روشن کردن"), callback_data="gg:flood_toggle",
                              style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [hx_button("✏️ تغییرِ تنظیمات", callback_data="gg:flood_edit")],
        [hx_button("⬅️ بازگشت", callback_data="adm:group_guard", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data == "gg:flood_toggle")
async def gg_flood_toggle(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_flood_enabled", "off")
    await set_pset("group_guard_flood_enabled", "off" if cur == "on" else "on")
    await gg_flood_menu(cb)


@group_guard_router.callback_query(F.data == "gg:flood_edit")
async def gg_flood_edit_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(GgFSM.flood_config)
    await cb.message.answer(
        "سه عدد رو با فاصله بفرست: «تعدادِ پیامِ مجاز» «بازهٔ زمانی (ثانیه)» «مدتِ سکوت (دقیقه)»\n"
        "مثلاً: <code>5 10 5</code> یعنی «۵ پیام تو ۱۰ ثانیه → ۵ دقیقه سکوت»",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.flood_config, F.text)
async def gg_flood_edit_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    parts = txt.split()
    if len(parts) != 3 or not all(p.isdigit() and int(p) >= 1 for p in parts):
        await msg.answer("سه عددِ صحیحِ مثبت با فاصله بفرست، مثلاً: 5 10 5")
        return
    max_msgs, window, mute_min = (int(p) for p in parts)
    await set_pset("group_guard_flood_max_msgs", str(max_msgs))
    await set_pset("group_guard_flood_window_sec", str(window))
    await set_pset("group_guard_flood_mute_sec", str(mute_min * 60))
    await state.clear()
    await msg.answer(
        f"✅ تنظیم شد: {max_msgs} پیام در {window} ثانیه → {mute_min} دقیقه سکوت.")


# === PATCH_GROUP_GUARD_TEMPMUTE_V1 ===

@group_guard_router.callback_query(F.data == "gg:mute_default")
async def gg_mute_default_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_mute_default_min", "10")
    await state.set_state(GgFSM.mute_default)
    await cb.message.answer(
        f"مدتِ پیش‌فرضِ فعلیِ سکوتِ دستی (/سکوت بدونِ عدد): {cur} دقیقه\n"
        "عددِ جدید رو به دقیقه بفرست:",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.mute_default, F.text)
async def gg_mute_default_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if not txt.isdigit() or int(txt) < 1:
        await msg.answer("یک عددِ صحیحِ مثبت (به دقیقه) بفرست:")
        return
    await set_pset("group_guard_mute_default_min", txt)
    await state.clear()
    await msg.answer(f"✅ مدتِ پیش‌فرضِ سکوت روی {txt} دقیقه تنظیم شد.")


# === PATCH_GROUP_GUARD_CONTENT_RESTRICT_V1 ===

_CONTENT_RESTRICT_ITEMS = [
    ("link", "🔗 لینک", "group_guard_restrict_link"),
    ("forward", "↪️ فوروارد", "group_guard_restrict_forward"),
    ("sticker", "🏷 استیکر", "group_guard_restrict_sticker"),
    ("animation", "🎞 گیف/انیمیشن", "group_guard_restrict_animation"),
    # === PATCH_GROUP_GUARD_ONLY_PERSONAL_V1 ===
    ("identity", "👤 فقط با اکانتِ شخصی", "group_guard_only_personal"),
]


@group_guard_router.callback_query(F.data == "gg:content_restrict")
async def gg_content_restrict_menu(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    lines = ["🔗 <b>محدودیتِ نوعِ محتوا</b>\n",
            "هرکدوم رو روشن کنی، فقط ادمین‌هایِ گروه اجازهٔ ارسالِ اون نوع رو دارن؛ "
            "پیامِ اعضایِ عادی حذف می‌شه و یک خطا براشون ثبت می‌شه.\n"]
    kb_rows = []
    for key, label, pkey in _CONTENT_RESTRICT_ITEMS:
        on = (await get_pset(pkey, "off")) == "on"
        lines.append(f"{label}: {'🟢 روشن' if on else '🔴 خاموش'}")
        kb_rows.append([hx_button(f"{'🔴 خاموش کردن' if on else '🟢 روشن کردن'} {label}",
                                  callback_data=f"gg:cr_toggle:{key}",
                                  style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))])
    kb_rows.append([hx_button("⬅️ بازگشت", callback_data="adm:group_guard", style=ButtonStyle.DANGER)])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data.startswith("gg:cr_toggle:"))
async def gg_content_restrict_toggle(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    key = cb.data.split(":")[2]
    pkey = next((p for k, _, p in _CONTENT_RESTRICT_ITEMS if k == key), None)
    if not pkey:
        await cb.answer("نامعتبر", show_alert=True)
        return
    cur = await get_pset(pkey, "off")
    await set_pset(pkey, "off" if cur == "on" else "on")
    await gg_content_restrict_menu(cb)


# === PATCH_GROUP_GUARD_WELCOME_V1 ===

@group_guard_router.callback_query(F.data == "gg:welcome")
async def gg_welcome_menu(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    on = (await get_pset("group_guard_welcome_enabled", "off")) == "on"
    template = await get_pset("group_guard_welcome_text", "") or _DEFAULT_WELCOME_TEXT
    text = (
        "👋 <b>پیامِ خوش‌آمد</b>\n\n"
        "وقتی عضوِ جدید وارد گروه بشه، این پیام (کاملاً مستقل از کپچا — می‌تونی "
        "هردو یا فقط یکی رو روشن کنی) براش فرستاده می‌شه.\n\n"
        f"وضعیت: {'🟢 روشن' if on else '🔴 خاموش'}\n\n"
        f"متنِ فعلی:\n{template}\n\n"
        "جای‌گزین‌های قابل‌استفاده: <code>{name}</code> (اسمِ عضو، لینک‌شده) و "
        "<code>{group}</code> (اسمِ گروه)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button(("🔴 خاموش کردن" if on else "🟢 روشن کردن"), callback_data="gg:welcome_toggle",
                              style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [hx_button("✏️ ویرایشِ متن", callback_data="gg:welcome_edit")],
        [hx_button("⬅️ بازگشت", callback_data="adm:group_guard", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data == "gg:welcome_toggle")
async def gg_welcome_toggle(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_welcome_enabled", "off")
    await set_pset("group_guard_welcome_enabled", "off" if cur == "on" else "on")
    await gg_welcome_menu(cb)


@group_guard_router.callback_query(F.data == "gg:welcome_edit")
async def gg_welcome_edit_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(GgFSM.welcome_text)
    await cb.message.answer(
        "متنِ جدیدِ خوش‌آمد رو بفرست. می‌تونی از <code>{name}</code> (اسمِ عضو) و "
        "<code>{group}</code> (اسمِ گروه) استفاده کنی.\n"
        "برایِ برگشتن به متنِ پیش‌فرض، بنویس «-».",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.welcome_text, F.text)
async def gg_welcome_edit_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    await set_pset("group_guard_welcome_text", "" if txt == "-" else txt)
    await state.clear()
    await msg.answer("✅ متنِ خوش‌آمد ذخیره شد." if txt != "-" else "✅ به متنِ پیش‌فرض برگشت.")


# === PATCH_GROUP_GUARD_NIGHT_V1 ===

@group_guard_router.callback_query(F.data == "gg:night")
async def gg_night_menu(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    on = (await get_pset("group_guard_night_enabled", "off")) == "on"
    start_s = await get_pset("group_guard_night_start", "00:00")
    end_s = await get_pset("group_guard_night_end", "07:00")
    active = (await get_pset("group_guard_night_active", "off")) == "on"
    text = (
        "🌙 <b>حالتِ شب</b>\n\n"
        "توی این بازهٔ زمانی (به وقتِ ایران)، گروه خودکار بسته می‌شه (کسی جز "
        "ادمین‌ها نمی‌تونه پیام بده) و سرِ ساعتِ پایان، خودکار باز می‌شه.\n\n"
        f"وضعیت: {'🟢 روشن' if on else '🔴 خاموش'}\n"
        f"بازه: {start_s} تا {end_s}\n"
        + (f"\n🌙 گروه همین الان بستَه‌ست (تویِ بازه‌ایم)." if (on and active) else "")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button(("🔴 خاموش کردن" if on else "🟢 روشن کردن"), callback_data="gg:night_toggle",
                              style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [hx_button("✏️ تغییرِ بازهٔ زمانی", callback_data="gg:night_edit")],
        [hx_button("⬅️ بازگشت", callback_data="adm:group_guard", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data == "gg:night_toggle")
async def gg_night_toggle(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_night_enabled", "off")
    new = "off" if cur == "on" else "on"
    await set_pset("group_guard_night_enabled", new)
    if new == "off":
        # اگه همین الان بسته بود، خاموش‌کردنِ کلیِ قابلیت باید فوراً بازش کنه
        if (await get_pset("group_guard_night_active", "off")) == "on":
            await set_pset("group_guard_night_active", "off")
            try:
                from main_platform import platform_bot as bot
                chat_id_s = await get_pset("group_guard_chat_id", "")
                if bot and chat_id_s:
                    await bot.set_chat_permissions(int(chat_id_s), permissions=_full_permissions())
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await gg_night_menu(cb)


@group_guard_router.callback_query(F.data == "gg:night_edit")
async def gg_night_edit_start(cb: CallbackQuery, state: FSMContext):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await state.set_state(GgFSM.night_config)
    await cb.message.answer(
        "ساعتِ شروع و پایانِ بستن رو با فاصله بفرست (فرمتِ HH:MM، به وقتِ ایران):\n"
        "مثلاً: <code>23:00 07:00</code>",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@group_guard_router.message(GgFSM.night_config, F.text)
async def gg_night_edit_save(msg: Message, state: FSMContext):
    if msg.from_user.id != PLATFORM_ADMIN_ID:
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt == BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    parts = txt.split()
    if len(parts) != 2:
        await msg.answer("دو ساعت با فاصله بفرست، مثلاً: 23:00 07:00")
        return

    def _valid(t: str) -> bool:
        try:
            h, m = t.split(":")
            return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        except Exception:
            return False

    if not (_valid(parts[0]) and _valid(parts[1])):
        await msg.answer("فرمتِ ساعت درست نیست. مثلاً: 23:00 07:00")
        return
    await set_pset("group_guard_night_start", parts[0])
    await set_pset("group_guard_night_end", parts[1])
    await state.clear()
    await msg.answer(f"✅ بازهٔ حالتِ شب: {parts[0]} تا {parts[1]} تنظیم شد.")


# === PATCH_GROUP_GUARD_ANTIPROMO_V1 ===

@group_guard_router.callback_query(F.data == "gg:antipromo_toggle")
async def gg_antipromo_toggle(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("group_guard_antipromo_enabled", "off")
    await set_pset("group_guard_antipromo_enabled", "off" if cur == "on" else "on")
    await gg_menu(cb)


# === PATCH_GROUP_REPORT_V1 ===
@group_guard_router.callback_query(F.data == "gg:report_toggle")
async def gg_report_toggle(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    cur = await get_pset("platform_group_report_enabled", "on")
    await set_pset("platform_group_report_enabled", "off" if cur == "on" else "on")
    await gg_menu(cb)


@group_guard_router.callback_query(F.data.startswith("gg:bans:"))
async def gg_bans_list(cb: CallbackQuery, page: int | None = None):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    if page is None:
        page = int(cb.data.split(":")[2])
    chat_id = await get_pset("group_guard_chat_id", "")
    if not chat_id:
        await cb.answer("گروهی وصل نیست.", show_alert=True)
        return
    async with Session() as s:
        rows = (await s.scalars(
            select(GroupGuardBan).where(GroupGuardBan.chat_id == int(chat_id))
            .order_by(GroupGuardBan.banned_at.desc())
        )).all()
    total = len(rows)
    pages = max(1, (total + GG_PAGE_SIZE - 1) // GG_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * GG_PAGE_SIZE: (page + 1) * GG_PAGE_SIZE]
    kb_rows = []
    for r in chunk:
        label = r.full_name or (f"@{r.username}" if r.username else str(r.user_id))
        kb_rows.append([hx_button(f"🚫 {label}", callback_data=f"gg:ban_view:{r.id}")])
    nav = []
    if page > 0:
        nav.append(hx_button("◀️ قبلی", callback_data=f"gg:bans:{page-1}"))
    if page < pages - 1:
        nav.append(hx_button("بعدی ▶️", callback_data=f"gg:bans:{page+1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([hx_button("⬅️ بازگشت", callback_data="adm:group_guard", style=ButtonStyle.DANGER)])
    text = f"🚫 <b>لیستِ بن‌شده‌ها</b> ({total} نفر) — صفحه {page+1}/{pages}"
    if not chunk:
        text += "\n\nکسی بن نشده."
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data.startswith("gg:ban_view:"))
async def gg_ban_view(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    ban_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(GroupGuardBan, ban_id)
    if not r:
        await cb.answer("یافت نشد (شاید قبلاً رفعِ بن شده).", show_alert=True)
        return
    text = (
        f"🚫 <b>{r.full_name or '—'}</b>\n"
        f"یوزرنیم: {('@' + r.username) if r.username else '—'}\n"
        f"آیدیِ عددی: <code>{r.user_id}</code>\n"
        f"دلیل: {r.reason or '—'}\n"
        f"بن‌شده توسط: <code>{r.banned_by or '—'}</code>\n"
        f"تاریخِ بن: {r.banned_at.strftime('%Y-%m-%d %H:%M') if r.banned_at else '—'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("✅ رفعِ بن", callback_data=f"gg:unban:{ban_id}", style=ButtonStyle.SUCCESS)],
        [hx_button("⬅️ بازگشت به لیست", callback_data="gg:bans:0", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data.startswith("gg:unban:"))
async def gg_unban_btn(cb: CallbackQuery, bot: Bot):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    ban_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(GroupGuardBan, ban_id)
        if not r:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        chat_id, user_id = r.chat_id, r.user_id
        try:
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        except Exception as e:
            await cb.answer(f"خطا در رفعِ بن: {e}", show_alert=True)
            return
        await s.delete(r)
        await s.commit()
    await cb.answer("✅ رفعِ بن انجام شد.")
    await _log_action(
        f"✅ <b>رفعِ بن (از پنلِ ادمین)</b>\n👤 آیدی: <code>{user_id}</code>")
    await gg_bans_list(cb, page=0)


# ── لیستِ خطاکارها ──

@group_guard_router.callback_query(F.data.startswith("gg:warns:"))
async def gg_warns_list(cb: CallbackQuery, page: int | None = None):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    if page is None:
        page = int(cb.data.split(":")[2])
    chat_id = await get_pset("group_guard_chat_id", "")
    if not chat_id:
        await cb.answer("گروهی وصل نیست.", show_alert=True)
        return
    async with Session() as s:
        rows = (await s.scalars(
            select(GroupGuardWarn).where(GroupGuardWarn.chat_id == int(chat_id))
            .order_by(GroupGuardWarn.count.desc())
        )).all()
    total = len(rows)
    pages = max(1, (total + GG_PAGE_SIZE - 1) // GG_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * GG_PAGE_SIZE: (page + 1) * GG_PAGE_SIZE]
    kb_rows = []
    for r in chunk:
        label = r.full_name or (f"@{r.username}" if r.username else str(r.user_id))
        kb_rows.append([hx_button(f"⚠️ {label} — {r.count} خطا", callback_data=f"gg:warn_view:{r.id}")])
    nav = []
    if page > 0:
        nav.append(hx_button("◀️ قبلی", callback_data=f"gg:warns:{page-1}"))
    if page < pages - 1:
        nav.append(hx_button("بعدی ▶️", callback_data=f"gg:warns:{page+1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([hx_button("⬅️ بازگشت", callback_data="adm:group_guard", style=ButtonStyle.DANGER)])
    text = f"⚠️ <b>لیستِ خطاکارها</b> ({total} نفر) — صفحه {page+1}/{pages}"
    if not chunk:
        text += "\n\nکسی خطا نداره."
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data.startswith("gg:warn_view:"))
async def gg_warn_view(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    warn_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(GroupGuardWarn, warn_id)
    if not r:
        await cb.answer("یافت نشد.", show_alert=True)
        return
    text = (
        f"⚠️ <b>{r.full_name or '—'}</b>\n"
        f"یوزرنیم: {('@' + r.username) if r.username else '—'}\n"
        f"آیدیِ عددی: <code>{r.user_id}</code>\n"
        f"تعدادِ خطا: <b>{r.count}</b>\n"
        f"آخرین دلیل: {r.last_reason or '—'}\n"
        f"آخرین خطا: {r.last_warned_at.strftime('%Y-%m-%d %H:%M') if r.last_warned_at else '—'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("🔄 صفر کردنِ خطاها", callback_data=f"gg:warn_reset:{warn_id}")],
        [hx_button("🚫 بن کردن", callback_data=f"gg:warn_to_ban:{warn_id}", style=ButtonStyle.DANGER)],
        [hx_button("⬅️ بازگشت به لیست", callback_data="gg:warns:0", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@group_guard_router.callback_query(F.data.startswith("gg:warn_reset:"))
async def gg_warn_reset(cb: CallbackQuery):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    warn_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(GroupGuardWarn, warn_id)
        if not r:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        user_id, full_name = r.user_id, r.full_name
        await s.delete(r)
        await s.commit()
    await cb.answer("✅ خطاها صفر شد.")
    await _log_action(
        f"🔄 <b>صفرشدنِ خطاها (از پنلِ ادمین)</b>\n👤 {_html.escape(full_name or '')} (<code>{user_id}</code>)")
    await gg_warns_list(cb, page=0)


@group_guard_router.callback_query(F.data.startswith("gg:warn_to_ban:"))
async def gg_warn_to_ban(cb: CallbackQuery, bot: Bot):
    if not _require_admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    warn_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(GroupGuardWarn, warn_id)
        if not r:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        chat_id, user_id = r.chat_id, r.user_id
        ban_ok, ban_error = await _ban_and_verify(bot, chat_id, user_id)
        if not ban_ok:
            await cb.answer(f"خطا در تأیید بن‌کردن: {ban_error}", show_alert=True)
            return
        brow = await s.scalar(select(GroupGuardBan).where(
            GroupGuardBan.chat_id == chat_id, GroupGuardBan.user_id == user_id))
        if not brow:
            brow = GroupGuardBan(chat_id=chat_id, user_id=user_id)
            s.add(brow)
        brow.full_name = r.full_name
        brow.username = r.username
        brow.reason = "بنِ دستی از پنلِ ادمین"
        brow.banned_by = cb.from_user.id
        brow.banned_at = datetime.datetime.now()
        full_name = r.full_name
        await s.delete(r)
        await s.commit()
    await cb.answer("🚫 بن شد.")
    await _log_action(
        f"🚫 <b>بن (از پنلِ ادمین)</b>\n👤 {_html.escape(full_name or '')} (<code>{user_id}</code>)\n"
        f"👮 توسط: {_html.escape(cb.from_user.full_name)}")
    await gg_warns_list(cb, page=0)
