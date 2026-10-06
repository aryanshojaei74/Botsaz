"""
raffle.py — قرعه‌کشیِ کانال/گروهِ بات‌ساز.

جریانِ کامل:
  ۱) ادمین یه پیامِ «نظرسنجیِ دستی» (متنِ سؤال + خط‌هایی مثلِ «گزینه ۱: ...»)
     توی گروه/کانالی که بات‌ساز توش ادمینه پست می‌کنه.
  ۲) روی همون پیام ریپلای می‌زنه و توش یه عدد+«ساعت» می‌نویسه (مثلاً
     «۳ ساعت وقت داره») — این پیام رو این ماژول تشخیص می‌ده و نظرسنجی رو
     با شروع از زمانِ خودِ پیامِ سؤال، برای همون تعداد ساعت «باز» می‌کنه.
  ۳) هرکسی زیرِ پیامِ سؤال ریپلای بزنه و متنش فقط یه عددِ معتبر (بین
     شماره‌ی گزینه‌ها) باشه، به‌عنوانِ رأی ثبت می‌شه (اگه چندبار ریپلای کنه،
     آخرین رأیش حساب می‌شه).
  ۴) ادمین با ریپلای‌زدن روی پیامِ سؤال و نوشتنِ چیزی شامل «قرعه» و «شروع»
     (مثلاً «قرعه‌کشی رو شروع کن باتساز») بازه رو می‌بنده؛ بات با دکمه‌های
     اینلاین می‌پرسه کدوم گزینه درست بوده.
  ۵) بعدِ انتخابِ ادمین، بات آمار (کل رأی‌دهنده/پاسخِ درست/پاسخِ غلط) رو
     پست می‌کنه و می‌پرسه همین الان N نفر برنده (به تعدادِ تنظیم‌شده در پنل)
     رو رندوم انتخاب کنه یا نه.
  ۶) اگه بله، از بینِ کسایی که جوابِ درست دادن، حداکثر N نفر رندوم انتخاب
     و با یه پیامِ تبریکِ شیک اعلام می‌شن؛ نتیجه (هم آمار هم برنده‌ها) توی
     دیتابیس ذخیره می‌شه تا از پنلِ مدیریتِ بات‌ساز («🎉 قرعه‌کشی کانال
     بات‌ساز») قابلِ مرور و اهدای جایزه باشه.

⚠️ نکتهٔ فنیِ مهم برایِ ادمین: برایِ اینکه بات بتونه ریپلای‌های کاربرانِ
دیگه (نه فقط ریپلای‌به‌خودِ‌بات) رو توی گروه ببینه، باید حالتِ Privacy Mode
بات از طریقِ @BotFather → Bot Settings → Group Privacy → Turn off غیرفعال
باشه؛ وگرنه بات اصلاً این پیام‌ها رو دریافت نمی‌کنه.
"""
from __future__ import annotations

import asyncio   # PATCH_RAFFLE_SILENT_PICK_V1
import datetime
from zoneinfo import ZoneInfo
import json
import random
import re

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReactionTypeEmoji,
)
from sqlalchemy import func, select

from config import Session, PLATFORM_ADMIN_ID
from models import ChannelRaffle, ChannelRaffleVote, ChannelRaffleWinner, PlatformUser

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

raffle_router = Router()

# تعدادِ برنده‌های هر دور دیگه ثابت نیست — با _get_winners_count/_set_winners_count
# قابل‌تنظیم شده (پیش‌فرض ۵)، از پنلِ مدیریتِ قرعه‌کشی.


class RaffleFSM(StatesGroup):
    prize_amount = State()
    setchat = State()
    winners_count = State()
    manual_add_username = State()  # === PATCH_RAFFLE_MANUAL_ADD_BY_USERNAME_V1 ===
    correct_number = State()       # === PATCH_RAFFLE_FREE_NUMBER_MODE_V1 ===
    forward_collect = State()      # === PATCH_RAFFLE_FORWARD_COLLECT_V1 ===

_HOUR_RE = re.compile(r"(?:(\d+(?:[./]\d+)?)\s*ساعت|ساعت\s*(\d+(?:[./]\d+)?))")
_OPTION_LINE_RE = re.compile(r"گزینه\s*[۰-۹0-9]+\s*[:\-–]?\s*(.+)")

_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _fa_int(text: str) -> int | None:
    digits = (text or "").translate(_FA_DIGITS).replace(",", "").replace("،", "").strip()
    return int(digits) if digits.isdigit() else None


async def _get_winners_count() -> int:
    from platform_bot import get_pset
    raw = await get_pset("raffle_winners_count", "5")
    try:
        n = int(raw)
        return n if n > 0 else 5
    except (TypeError, ValueError):
        return 5


async def _set_winners_count(n: int) -> None:
    from platform_bot import set_pset
    await set_pset("raffle_winners_count", str(n))


def _medal_for_rank(rank: int) -> str:
    """۱-پایه؛ سه رتبه‌ی اول مدال‌هایِ اختصاصی دارن، بقیه 🏅 (چون تعدادِ
    برنده‌ها دیگه ثابت نیست، ممکنه بیشتر از ۵ تا هم باشه)."""
    fixed = {1: "🥇", 2: "🥈", 3: "🥉"}
    return fixed.get(rank, "🏅")


async def _is_raffle_admin(uid: int) -> bool:
    """فقط ادمینِ اصلی یا ادمین‌های فرعیِ بات‌ساز اجازه دارن قرعه‌کشی
    راه بندازن/جواب درست رو تعیین کنن — نه هرکسی توی گروه."""
    if uid == PLATFORM_ADMIN_ID:
        return True
    from platform_bot import has_perm
    return await has_perm(uid, "adm:users")


async def _get_raffle_channel() -> tuple[int | None, str]:
    from platform_bot import get_pset
    raw = await get_pset("raffle_channel_id", "")
    title = await get_pset("raffle_channel_title", "")
    return (int(raw) if raw else None, title)


async def _set_raffle_channel(chat_id: int, title: str) -> None:
    from platform_bot import set_pset
    await set_pset("raffle_channel_id", str(chat_id))
    await set_pset("raffle_channel_title", title)


async def _get_raffle_group() -> tuple[int | None, str]:
    from platform_bot import get_pset
    raw = await get_pset("raffle_group_id", "")
    title = await get_pset("raffle_group_title", "")
    return (int(raw) if raw else None, title)


async def _get_raffle_group_username() -> str:
    from platform_bot import get_pset
    return await get_pset("raffle_group_username", "")


async def _set_raffle_group(chat_id: int, title: str, username: str = "") -> None:
    from platform_bot import set_pset
    await set_pset("raffle_group_id", str(chat_id))
    await set_pset("raffle_group_title", title)
    await set_pset("raffle_group_username", username or "")


async def is_group_raffle_active(chat_id: int) -> bool:
    """آیا در این گروه در همین لحظه یه قرعه‌کشیِ باز (بازه‌ی رأی/منشن باز)
    وجود داره؟ — برای ماژولِ ضدِ تبلیغ استفاده می‌شه: در طولِ قرعه‌کشیِ فعال،
    لینک/منشن مجازه چون کاربرها برای شرکت باید کسی رو منشن کنن."""
    async with Session() as s:
        raffle = await s.scalar(select(ChannelRaffle).where(
            ChannelRaffle.chat_id == chat_id,
            ChannelRaffle.status == "open",
        ).order_by(ChannelRaffle.id.desc()))
    if not raffle or not raffle.poll_started_at:
        return False
    now = _raffle_now()
    window_end = raffle.poll_started_at + datetime.timedelta(hours=raffle.duration_hours)
    return raffle.poll_started_at <= now <= window_end


async def _is_authorized_sender(msg: Message) -> bool:
    """برایِ پیام‌های متنیِ گروه: یا واقعاً از طرفِ یه ادمینِ بات‌ساز باشه،
    یا (چون خیلی از گروه‌های متصل‌به‌کانال پیام‌ها رو «ناشناس، به‌نمایندگیِ
    کانال» می‌فرستن — یعنی msg.from_user دیگه آیدیِ واقعیِ فرستنده نیست)
    از طرفِ همون کانال/گروهی باشه که برایِ قرعه‌کشی وصل کرده‌ایم؛ چون فقط
    ادمین‌هایِ واقعیِ اون کانال می‌تونن به‌نمایندگیِ کانال پیام بفرستن، این
    خودش یه تاییدِ معتبره."""
    if msg.from_user and await _is_raffle_admin(msg.from_user.id):
        return True
    if msg.sender_chat:
        channel_id, _ = await _get_raffle_channel()
        group_id, _ = await _get_raffle_group()
        if msg.sender_chat.id in (channel_id, group_id):
            return True
    return False


def _parse_poll_text(text: str) -> tuple[str, list[str]] | None:
    """متنِ پیامِ نظرسنجی رو به (موضوع, [گزینه۱, گزینه۲, ...]) می‌شکنه.
    اگه هیچ خطِ «گزینه N: ...» توش نباشه، None برمی‌گردونه (یعنی این پیام
    اصلاً نظرسنجی نیست)."""
    if not text:
        return None
    lines = text.splitlines()
    options = []
    topic_lines = []
    for line in lines:
        m = _OPTION_LINE_RE.match(line.strip())
        if m:
            options.append(m.group(1).strip())
        elif not options:
            topic_lines.append(line)
    if len(options) < 2:
        return None
    topic = "\n".join(topic_lines).strip() or "بدون عنوان"
    return topic, options


def _extract_poll_content(msg: Message) -> tuple[str, list[str]] | None:
    """پیامِ سؤال می‌تونه یا یه نظرسنجیِ بومیِ خودِ تلگرام (native Poll —
    چیزی که با آیکونِ 📊 و دکمه‌های واقعی ساخته می‌شه) باشه، یا یه پیامِ
    متنیِ دستی با خط‌هایی مثلِ «گزینه ۱: ...».

    ⚠️ نکته‌ی مهم: چه نظرسنجیِ بومی باشه چه متنی، تلگرام به بات فقط برایِ
    نظرسنجی‌هایی که خودِ بات فرستاده باشه اجازه‌ی دیدنِ رأی‌ها رو می‌ده — پس
    حتی برایِ نظرسنجیِ بومی هم رأی‌گیریِ واقعی از طریقِ تپ‌کردن روی گزینه‌ها
    نیست، بلکه از همون روشِ «ریپلای با عدد» انجام می‌شه؛ اینجا فقط سؤال و
    گزینه‌ها رو از روی نظرسنجیِ بومی می‌خونیم تا مجبور نباشی دستی تایپشون کنی.
    """
    if msg.poll:
        return msg.poll.question, [o.text for o in msg.poll.options]
    return _parse_poll_text(msg.text or msg.caption or "")


def _extract_hours(text: str) -> float | None:
    m = _HOUR_RE.search(text or "")
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or "").translate(_FA_DIGITS).replace("/", ".")
    try:
        h = float(raw)
        return h if h > 0 else None
    except ValueError:
        return None


def _is_start_trigger(text: str) -> bool:
    t = (text or "").strip()
    return "قرعه" in t and ("شروع" in t or "استارت" in t)


# === PATCH_RAFFLE_START_FROM_COMMAND_V1 ===
# قبلاً بازهٔ رأی‌گیری از تاریخِ *پیامِ سؤال* شمرده می‌شد:
#     poll_started_at = replied.date
# منطقش این بود که اگر پست ساعت ۲۱ گذاشته شده و ادمین ۲۱:۰۵ ریپلای زده،
# وقت از ۲۱ حساب شود. ولی وقتی پست چند ساعت قدیمی باشد، نتیجه فاجعه است:
# ادمین «۱.۵ ساعت» می‌نویسد و قرعه‌کشی *همان لحظه* منقضی است. دو بار
# پشتِ‌هم همین اتفاق افتاد و رأی‌ها بی‌صدا رد شدند.
#
# حالا: اگر پست تازه باشد (زیرِ GRACE دقیقه) از تاریخِ خودِ پست حساب
# می‌شود — همان منطقِ منصفانهٔ قبلی. اگر قدیمی‌تر باشد، از لحظهٔ خودِ
# دستور. یعنی «۱.۵ ساعت» همیشه یعنی دستِ‌کم ۱.۵ ساعتِ واقعی.
_START_GRACE_MIN = 10
# همهٔ زمان‌های قرعه‌کشی با تقویم/ساعت تهران محاسبه می‌شوند؛ سرور UTC است.
_RAFFLE_TZ = ZoneInfo("Asia/Tehran")


def _raffle_now() -> datetime.datetime:
    """زمان فعلیِ تهران به‌صورت naive برای سازگاری با ستون‌های DateTime فعلی."""
    return datetime.datetime.now(_RAFFLE_TZ).replace(tzinfo=None)


def _raffle_start_at(replied_date) -> datetime.datetime:
    """زمانِ شروعِ بازه: تاریخِ پست اگر تازه باشد، وگرنه همین حالا.

    === PATCH_RAFFLE_UTC_FIX_V1 — باگِ ریشه‌ای ===
    تلگرام `message.date` را همیشه با منطقهٔ زمانیِ **UTC** می‌دهد. کدِ
    قبلی می‌نوشت `replied_date.replace(tzinfo=None)` که برچسبِ UTC را
    *دور می‌ریزد* و عددِ UTC را به‌عنوانِ «وقتِ محلی» ذخیره می‌کرد — بعد با
    `datetime.now()` که وقتِ محلیِ سرور (+03:30) است مقایسه می‌شد.
    نتیجه: هر پستی ۳ ساعت و نیم «در گذشته» دیده می‌شد.

    شواهدِ واقعی: پستِ ساعت ۲۲:۵۶ به وقتِ ایران، در پنل ۱۹:۲۶ ثبت شده بود؛
    با مهلتِ ۱.۵ ساعت، پایانِ بازه ۲۰:۵۶ می‌شد — یعنی قرعه‌کشی در همان
    لحظهٔ ساخته‌شدن منقضی بود و همهٔ رأی‌ها بی‌صدا رد می‌شدند.

    حالا اول به وقتِ محلیِ سرور تبدیل می‌شود، بعد مقایسه.
    """
    now = _raffle_now()
    try:
        posted = replied_date
        if getattr(posted, "tzinfo", None) is not None:
            # تاریخِ آگاهِ تلگرام معمولاً UTC است؛ آن را به تهران تبدیل می‌کنیم.
            posted = posted.astimezone(_RAFFLE_TZ).replace(tzinfo=None)
        else:
            # در adapterهای قدیمی، Message.date ممکن است naive ولی UTC باشد.
            posted = (posted.replace(tzinfo=datetime.timezone.utc)
                            .astimezone(_RAFFLE_TZ).replace(tzinfo=None))
    except Exception:
        return now
    if posted > now:                       # اختلافِ ساعتِ سرور
        return now
    if (now - posted) <= datetime.timedelta(minutes=_START_GRACE_MIN):
        return posted
    return now


# === PATCH_RAFFLE_COMMENT_MODE_V1 ===
# درخواستِ ادمین: «بین کسانی که زیرِ پست کامنت گذاشتن قرعه‌کشی کن» —
# بدونِ اینکه لازم باشه عددِ خاصی بفرستن یا کسی رو منشن کنن.
# این حالت برایِ پستِ کانال که کامنتش به گروهِ گفت‌وگو می‌ره ساخته شده:
# هر کسی در بازهٔ زمانی زیرِ همون پست کامنت بذاره، یک‌بار ثبت می‌شه.
def _is_comment_mode_trigger(text: str) -> bool:
    return "کامنت" in (text or "")


def _is_mention_mode_trigger(text: str) -> bool:
    """آیا این پیامِ ثبتِ نظرسنجی، در واقع درخواستِ یه قرعه‌کشیِ «منشن‌محور»ه
    (نه نظرسنجیِ چندگزینه‌ای)؟ — با کلمه‌ی «منشن» تشخیص داده می‌شه."""
    return "منشن" in (text or "")


def _has_mention(msg: Message) -> bool:
    """آیا این پیام حداقل یه نفرِ دیگه رو منشن کرده (چه با @username، چه
    با منشنِ بدونِ‌یوزرنیم/text_mention)."""
    for e in (msg.entities or []):
        if e.type in ("mention", "text_mention"):
            return True
    return False


# === PATCH_RAFFLE_MANUAL_BACKFILL_V1 ===
# === PATCH_RAFFLE_BACKFILL_TRIGGER_STRICT_V1 ===
# باگِ گزارش‌شده: شرط قبلاً «هر پیامی که کلمهٔ دستی توش باشه» بود. یعنی هر
# ریپلایِ عادی در گروهِ قرعه‌کشی که تصادفاً این کلمه را داشت — مثلاً
# «رسیدهای مشکوک به بررسیِ دستی می‌روند» — به‌عنوانِ دستورِ ثبتِ دستی
# تفسیر می‌شد و ربات وسطِ گفت‌وگو جواب می‌داد «هیچ قرعه‌کشیِ منشن‌محوری
# فعال نیست».
#
# حالا فقط یک پیامِ کوتاهِ مستقل دستور حساب می‌شود: خودِ کلمه (با /
# اختیاری و علائمِ نگارشیِ ته‌ش). جمله‌ای که کلمه را در دلِ متن دارد،
# دیگر دستور نیست.
# === PATCH_RAFFLE_MANUAL_PICK_V1 ===
# «ثبت» و «+» اضافه شد: ادمین می‌خواهد سریع رویِ پیامِ هر شرکت‌کننده بزند
# و ثبتش کند؛ کلمهٔ «دستی» را کسی حدس نمی‌زد.
_SILENT_TASKS: set = set()   # PATCH_RAFFLE_SILENT_PICK_V1

# === PATCH_RAFFLE_MANUAL_ALWAYS_ELIGIBLE_V1 ===
# کسی که ادمین *دستی* اضافه کرده (با ریپلایِ «ثبت»، فوروارد، یا یوزرنیم)
# باید همیشه در قرعه‌کشی باشد — چون خودِ ادمین تأییدش کرده.
#
# قبلاً `chosen_option` او برابرِ جوابِ درستِ **همان لحظه** ذخیره می‌شد؛
# اگر هنوز جوابی ثبت نشده بود عددِ ۱ می‌گرفت. بعد اگر ادمین جوابِ درست را
# مثلاً ۲ می‌گذاشت، همهٔ آن افراد از فیلترِ
#     chosen_option == correct_option
# بیرون می‌افتادند و بی‌سروصدا از قرعه‌کشی حذف می‌شدند.
#
# این مقدارِ منفی هیچ‌وقت با یک گزینهٔ واقعی برابر نمی‌شود، و در فیلترِ
# قرعه‌کشی صریحاً کنارِ پاسخ‌هایِ درست آورده شده.
_MANUAL_OPTION = -1

_BACKFILL_WORDS = {"دستی", "/دستی", "دستی✅", "ثبت دستی", "/ثبت_دستی",
                   "ثبت", "+", "➕", "شرکت", "ثبت کن", "/ثبت"}


def _is_manual_backfill_trigger(text: str) -> bool:
    """ادمین با ریپلای‌زدن روی پیامِ قدیمیِ یه شرکت‌کننده و نوشتنِ کلمه‌ی
    «دستی» می‌تونه اون فرد رو دستی به‌عنوانِ شرکت‌کننده ثبت کنه (برایِ
    کسایی که قبل از فعال‌شدنِ حالتِ بدونِ‌ریپلای شرکت کرده بودن).

    فقط پیامِ کوتاهی که *خودش* همین دستور است پذیرفته می‌شود — نه هر
    جمله‌ای که این کلمه در آن آمده.
    """
    # === PATCH_RAFFLE_MANUAL_PICK_V1 ===
    # «دستی» تنها کلمهٔ ثبت بود و کسی حدسش را نمی‌زد. کلماتِ طبیعی‌تر
    # اضافه شد تا ادمین بتواند سریع رویِ پیامِ هر نفر بزند «ثبت» یا «+».
    t = (text or "").strip().strip("!.،؛:؟ ")
    if not t or len(t) > 20:
        return False
    return t in _BACKFILL_WORDS


# ═══════════════════════ ثبتِ نظرسنجی (ریپلای با «N ساعت») ═══════════════════════

# === PATCH_RAFFLE_REACTION_DIAGNOSTIC_V1 ===
# گزارشِ ادمین: «قبلاً رأیِ شخص رو لایک می‌کرد یعنی ثبت شد، الان کار نمی‌کنه».
#
# خودِ ثبتِ رأی سالم است؛ فقط ری‌اکشن نمی‌نشیند. مشکل این بود که خطایش با
# یک پیامِ کاملاً عمومی («خطای نادیده‌گرفته‌شده») لاگ می‌شد، پس هیچ‌وقت
# معلوم نبود *چرا*. دلایلِ محتملِ setMessageReaction در تلگرام:
#   • REACTION_INVALID — ادمینِ گروه فهرستِ ری‌اکشن‌هایِ مجاز را محدود
#     کرده و 👍 داخلش نیست (شایع‌ترین حالت، و «یک‌دفعه قطع شدن» را توضیح
#     می‌دهد چون تنظیماتِ گروه عوض می‌شود نه کد)
#   • ربات در گروه ادمین نیست یا دسترسی‌اش کم شده
#   • پیام خیلی قدیمی است
#
# این تابع چند ایموجی را پشتِ‌هم امتحان می‌کند و اگر همه شکست خوردند،
# علتِ واقعی را با نام لاگ می‌کند تا قابلِ پیگیری باشد.
_VOTE_REACTIONS = ("👍", "🔥", "❤️", "🎉", "👌")


async def _react_safe(msg, preferred: str = "👍") -> bool:
    """ری‌اکشن می‌زند؛ اگر ایموجیِ دلخواه مجاز نبود، جایگزین‌ها را امتحان
    می‌کند. True یعنی یکی از آن‌ها نشست."""
    tried = [preferred] + [e for e in _VOTE_REACTIONS if e != preferred]
    last = None
    for emoji in tried:
        try:
            await msg.react([ReactionTypeEmoji(emoji=emoji)])
            return True
        except Exception as e:
            last = e
            # اگر مشکل «ایموجیِ غیرمجاز» نیست، امتحانِ بقیه بی‌فایده است
            if "REACTION_INVALID" not in str(e) and "reaction is invalid" not in str(e).lower():
                break
    logger.warning(
        "ری‌اکشنِ تاییدِ رأی نشست نکرد (chat=%s): %s — "
        "معمولاً یعنی ادمینِ گروه ری‌اکشن‌ها را محدود کرده یا ربات ادمین نیست.",
        getattr(getattr(msg, "chat", None), "id", "?"), last)
    return False


# === PATCH_RAFFLE_FREE_NUMBER_MODE_V1 ===
# عمداً *بالاتر* از raffle_group_reply تعریف شده: در aiogram ترتیبِ ثبتِ
# هندلرها همان ترتیبِ تعریفشان در فایل است، و اگر این پایین‌تر بود،
# دیسپچرِ عمومیِ ریپلای‌ها اول عددِ ادمین را می‌گرفت و آن را «رأی» حساب
# می‌کرد — یعنی جوابِ درست هیچ‌وقت ثبت نمی‌شد.
@raffle_router.message(RaffleFSM.correct_number, F.text)
async def raffle_correct_number_recv(msg: Message, state: FSMContext, bot: Bot):
    if not await _is_raffle_admin(msg.from_user.id):
        return
    txt = (msg.text or "").strip()
    if txt in {"لغو", "❌ لغو", "انصراف", "بازگشت", "/cancel"}:
        data = await state.get_data()
        await state.clear()
        # قفلِ رأی‌گیری را باز نمی‌کنیم (نتیجه دیده شده)، فقط منتظر می‌مانیم
        await msg.reply("باشه، فعلاً قرعه‌کشی نشد. هر وقت خواستی دوباره "
                        "بنویس «قرعه‌کشی رو شروع کن».")
        async with Session() as s:
            r = await s.get(ChannelRaffle, data.get("raffle_id"))
            if r and r.status == "awaiting_answer":
                r.status = "open"
                await s.commit()
        return
    num = _fa_int(txt)
    if num is None or num <= 0:
        await msg.reply("عددِ معتبر بفرست (مثلاً <code>۱</code>) یا بنویس «لغو».")
        return
    data = await state.get_data()
    await state.clear()
    raffle_id = data.get("raffle_id")
    async with Session() as s:
        r = await s.get(ChannelRaffle, raffle_id)
        if not r or r.status != "awaiting_answer":
            await msg.reply("این قرعه‌کشی دیگه در وضعیتِ انتظارِ جواب نیست.")
            return
        r.correct_option = num
        r.status = "closed"
        await s.commit()
        votes = (await s.scalars(select(ChannelRaffleVote).where(
            ChannelRaffleVote.raffle_id == raffle_id))).all()
        topic = r.topic
    total = len(votes)
    # PATCH_RAFFLE_MANUAL_ALWAYS_ELIGIBLE_V1 — دستی‌ها هم واجدِ شرایط‌اند
    correct = sum(1 for v in votes if v.chosen_option in (num, _MANUAL_OPTION))
    winners_n = await _get_winners_count()
    draw_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🎲 بله، الان {winners_n} نفر برنده رو انتخاب کن",
                             callback_data=f"raffle:draw:{raffle_id}",
                             style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="⏳ بعداً از پنل ادمین",
                             callback_data=f"raffle:later:{raffle_id}"),
    ]])
    await msg.answer(
        "مدیر به روی چشم 🫡\n\n"
        f"📋 <b>{topic}</b>\n"
        f"✅ عددِ درست: <b>{num}</b>\n\n"
        f"👥 آمار کل نفرات رأی‌دهنده: <b>{total}</b>\n"
        f"✅ آمار کل نفرات پاسخِ درست: <b>{correct}</b>\n"
        f"❌ آمار نفرات پاسخِ اشتباه: <b>{total - correct}</b>",
        reply_markup=draw_kb)


@raffle_router.message(F.chat.type.in_({"group", "supergroup"}), F.reply_to_message, F.text)
async def raffle_group_reply(msg: Message, bot: Bot, state: FSMContext):
    """دیسپچرِ اصلیِ همه‌ی ریپلای‌های گروه — تشخیص می‌ده کدومِ سه‌حالته:
    ثبتِ نظرسنجیِ جدید / رأی / شروعِ قرعه‌کشی."""
    bound_chat_id, _ = await _get_raffle_group()
    # === PATCH_RAFFLE_DEBUG_LOG_V1 ===
    logger.info(f"[RAFFLE_DEBUG] msg.chat.id={msg.chat.id} bound_chat_id={bound_chat_id} "
                f"text={(msg.text or '')!r} from_user={msg.from_user.id if msg.from_user else None} "
                f"reply_to={msg.reply_to_message.message_id if msg.reply_to_message else None}")
    if not bound_chat_id or msg.chat.id != bound_chat_id:
        # === PATCH_GROUP_ROUTER_SKIPHANDLER_V1 ===
        # این هندلر رویِ *همهٔ* گروه‌هایِ متصل به این پروسه ثبت شده، نه فقط
        # گروهِ قرعه‌کشی. قبلاً همین‌جا فقط return می‌شد — یعنی برایِ هر گروهِ
        # دیگه‌ای (مثلِ گروهی که «محافظِ گروه» بهش وصله) کلِ آپدیت بی‌سروصدا
        # بلعیده می‌شد و هرگز به group_guard_router (که بعداً ثبت می‌شه)
        # نمی‌رسید — یعنی مثلاً /فعال_محافظ تویِ اون گروه هیچ واکنشی نشون
        # نمی‌داد. SkipHandler یعنی «این آپدیت مالِ من نیست، برو سراغِ روترِ بعدی».
        raise SkipHandler

    # === PATCH_RAFFLE_MODERATION_REMOVED_V1 ===
    # قبلاً اینجا دستوراتِ ریپلای‌محورِ ادمین (بن/اخطار/ریست) و ضدِ تبلیغ
    # (group_moderation.py) هم اجرا می‌شد. طبقِ خواستِ ادمین حذف شد — این
    # بخش الان فقط برایِ خودِ قرعه‌کشی عمل می‌کنه؛ مدیریت/بن/اخطارِ گروه رو
    # کلاً «🛡 محافظ گروه» (platform_group_guard.py) پوشش می‌ده.

    replied = msg.reply_to_message
    text = (msg.text or "").strip()

    async with Session() as s:
        raffle = await s.scalar(select(ChannelRaffle).where(
            ChannelRaffle.chat_id == msg.chat.id,
            ChannelRaffle.poll_message_id == replied.message_id,
        ))

    if raffle is None:
        # === PATCH_RAFFLE_MANUAL_BACKFILL_V1 ===
        if _is_manual_backfill_trigger(text):
            if not msg.from_user or not await _is_raffle_admin(msg.from_user.id):
                return
            target = replied.from_user
            if not target:
                await msg.reply("❌ نمی‌تونم فرستنده‌ی این پیام رو تشخیص بدم "
                                 "(احتمالاً پیام ناشناس/به‌نمایندگیِ کانال بوده).")
                return
            async with Session() as s:
                # === PATCH_RAFFLE_MANUAL_PICK_V1 ===
                # قبلاً فقط رویِ قرعه‌کشیِ «منشن‌محور» کار می‌کرد. دلیلی
                # ندارد: ثبتِ دستی باید در هر حالتی ممکن باشد — به‌ویژه
                # حالتِ کامنتی و عددی که ادمین می‌خواهد از رویِ کامنت‌هایِ
                # موجود شرکت‌کننده‌ها را جمع کند.
                # PATCH_RAFFLE_COLLECT_STATUS_ONLY_V1 — `awaiting_answer`
                # هم باید قبول شود: به‌محضِ زدنِ «قرعه‌کشی رو شروع کن»
                # وضعیت از open به awaiting_answer می‌رود، ولی تا وقتی
                # قرعه کشیده نشده هنوز می‌شود کسی را اضافه کرد. بدونِ این،
                # ادمین پیامِ «هیچ قرعه‌کشیِ بازی نیست» می‌گرفت درحالی‌که
                # صفحهٔ نتایج همان دوره را باز نشان می‌داد.
                backfill_raffle = await s.scalar(select(ChannelRaffle).where(
                    ChannelRaffle.chat_id == msg.chat.id,
                    ChannelRaffle.status.in_(("open", "awaiting_answer")),
                ).order_by(ChannelRaffle.id.desc()))
            if not backfill_raffle:
                # پیامِ خطا باید بگوید *چرا*، وگرنه ادمین گیج می‌شود.
                async with Session() as s:
                    _any = (await s.scalars(select(ChannelRaffle).where(
                        ChannelRaffle.chat_id == msg.chat.id)
                        .order_by(ChannelRaffle.id.desc()).limit(1))).all()
                if not _any:
                    await msg.reply(
                        "❌ در این گروه هیچ قرعه‌کشی‌ای ثبت نشده.\n"
                        "<i>شاید گروهِ وصل‌شده در پنل، گروهِ دیگری است.</i>",
                        parse_mode="HTML")
                else:
                    _r = _any[0]
                    await msg.reply(
                        f"❌ آخرین قرعه‌کشیِ این گروه (#{_r.id}) در وضعیتِ "
                        f"<b>{_r.status}</b> است، نه باز.\n\n"
                        "برایِ افزودنِ نفر، دوره باید «باز» یا «منتظرِ جواب» "
                        "باشد.", parse_mode="HTML")
                return
            # PATCH_RAFFLE_COLLECT_STATUS_ONLY_V1 — ثبتِ دستیِ ادمین به
            # پنجرهٔ زمانی محدود نیست؛ تا وقتی دوره بسته/قرعه‌کشی نشده باشد
            # می‌شود اضافه کرد.
            async with Session() as s:
                existing_bf = await s.scalar(select(ChannelRaffleVote).where(
                    ChannelRaffleVote.raffle_id == backfill_raffle.id,
                    ChannelRaffleVote.telegram_id == target.id))
                if existing_bf:
                    await msg.reply(f"ℹ️ {target.full_name} از قبل ثبت شده بود.")
                    return
                s.add(ChannelRaffleVote(
                    raffle_id=backfill_raffle.id, telegram_id=target.id,
                    full_name=target.full_name, username=target.username,
                    # PATCH_RAFFLE_MANUAL_PICK_V1 — با جوابِ درست هم‌خوان
                    # باشد، وگرنه موقعِ قرعه‌کشی از فیلتر بیرون می‌افتد.
                    # PATCH_RAFFLE_MANUAL_ALWAYS_ELIGIBLE_V1
                    chosen_option=_MANUAL_OPTION,
                ))
                await s.commit()
                _cnt = await s.scalar(select(func.count()).select_from(ChannelRaffleVote)
                                      .where(ChannelRaffleVote.raffle_id == backfill_raffle.id))
            # === PATCH_RAFFLE_SILENT_PICK_V1 ===
            # خواستهٔ ادمین: «پیامِ ثبتِ من پاک بشه جوری که کسی متوجه نشه،
            # ولی اون شخص در قرعه‌کشی شرکت کنه».
            #
            # پس در گروه هیچ ردی نمی‌گذاریم: نه ری‌اکشن رویِ پیامِ طرف، نه
            # پیامِ تأیید. فقط دستورِ خودِ ادمین پاک می‌شود و بازخورد به
            # پیویِ خودش می‌رود تا بداند کار انجام شده.
            _deleted = False
            try:
                await msg.delete()
                _deleted = True
            except Exception:
                logger.warning("حذفِ پیامِ «ثبت» ناموفق بود — ربات باید در گروه "
                               "دسترسیِ «حذفِ پیام‌ها» داشته باشد.")
            _note = (f"✅ {target.full_name} در قرعه‌کشی #{backfill_raffle.id} "
                     f"ثبت شد. (مجموع: {_cnt} نفر)")
            try:
                await msg.bot.send_message(msg.from_user.id, _note)
            except Exception:
                # اگر پیویِ ادمین در دسترس نبود، ناچار در گروه اطلاع بده
                logger.exception("ارسالِ تأییدِ ثبت به پیویِ ادمین ناموفق بود")
                _deleted = False
            if not _deleted:
                _m = await msg.reply(_note + "\n<i>(این پیام تا ۵ ثانیه دیگر پاک می‌شود)</i>",
                                     parse_mode="HTML")
                async def _cleanup(m):
                    await asyncio.sleep(5)
                    try:
                        await m.delete()
                    except Exception:
                        logger.exception("حذفِ پیامِ تأیید ناموفق بود")
                _t = asyncio.create_task(_cleanup(_m))
                _SILENT_TASKS.add(_t)
                _t.add_done_callback(_SILENT_TASKS.discard)
            return

        # حالتِ ۱: شاید ادمین داره یه نظرسنجی/قرعه‌کشیِ جدید رو با «N ساعت» فعال می‌کنه
        hours = _extract_hours(text)
        if hours is None:
            # === PATCH_RAFFLE_NONRAFFLE_REPLY_SKIPHANDLER_V1 ===
            # باگِ گزارش‌شده: تویِ گروهی که هم برایِ قرعه‌کشی هم برایِ «🛡 محافظِ
            # گروه» استفاده می‌شه، ریپلای‌زدن و نوشتنِ «اخطار»/«رفع اخطار» هیچ
            # واکنشی نداشت. علتش دقیقاً همون باگیه که قبلاً بالاتر (خطِ چکِ
            # bound_chat_id) پیدا و فیکس شده بود: اینجا هم فقط return می‌شد،
            # یعنی aiogram آپدیت رو «پردازش‌شده» حساب می‌کرد و هرگز به
            # group_guard_router (که بعداً ثبت می‌شه) نمی‌رسید. یک ریپلایِ
            # معمولی که فرمتِ «N ساعت» نداره ربطی به قرعه‌کشی نداره، پس باید
            # SkipHandler بشه تا نوبت به هندلرهایِ بن/اخطار/سکوتِ محافظِ گروه
            # برسه.
            raise SkipHandler
        if not await _is_authorized_sender(msg):
            return

        # === PATCH_RAFFLE_NO_SELF_REPLY_V1 ===
        # باگِ دیده‌شده در دیتابیس: یک قرعه‌کشی با عنوانِ
        #   «✅ نظرسنجی ثبت شد و برای 1.5 ساعت باز است»
        # یعنی ادمین رویِ *پیامِ تأییدِ خودِ ربات* ریپلای زده و یک دورهٔ
        # جعلی ساخته شده. قبلاً این با خطایِ «فرمتِ نظرسنجی نداره» رد
        # می‌شد، ولی از وقتی حالتِ «عددِ آزاد» اضافه شد هر متنی قبول
        # می‌شود — پس این اشتباه بی‌صدا یک دورهٔ تکراری می‌سازد و
        # رأی‌ها بینِ دو دوره گم می‌شوند.
        if replied.from_user and replied.from_user.is_bot:
            await msg.reply(
                "❌ این پیامِ خودِ منه، نه پیامِ سؤال.\n\n"
                "رویِ همون پیامی ریپلای بزن که خودت سؤال رو توش نوشتی.")
            return

        # هشدار اگر همین حالا یک دورهٔ باز در این گروه هست — دو دورهٔ
        # هم‌زمان یعنی رأی‌ها بینشان تقسیم می‌شوند و ادمین سردرگم می‌شود.
        async with Session() as s:
            _open = (await s.scalars(select(ChannelRaffle).where(
                ChannelRaffle.chat_id == msg.chat.id,
                ChannelRaffle.status == "open"))).all()
        _now = _raffle_now()
        _live = [r for r in _open
                 if r.poll_started_at <= _now <= r.poll_started_at
                 + datetime.timedelta(hours=r.duration_hours)]
        # === PATCH_RAFFLE_AUTOCLOSE_PREV_V1 ===
        # نسخهٔ قبلی اینجا ساختِ دورهٔ جدید را *رد* می‌کرد و می‌گفت «اول
        # قبلی رو ببند». این اصطکاکی بود که خودم اضافه کردم و لازم نبود:
        # مشکلِ اصلی (ساختِ دورهٔ تکراری با ریپلای رویِ پیامِ خودِ ربات)
        # با محافظِ is_bot حل شده. اگر ادمین رویِ پستِ *جدیدی* ریپلای بزند،
        # یعنی می‌خواهد دورهٔ تازه — پس قبلی را خودمان می‌بندیم و راه را
        # باز می‌گذاریم، به‌جایِ اینکه کارش را متوقف کنیم.
        if _live:
            async with Session() as s:
                for _r in _live:
                    _row = await s.get(ChannelRaffle, _r.id)
                    if _row and _row.status == "open":
                        _row.status = "expired"
                await s.commit()
            _ids = "، ".join(f"#{r.id}" for r in _live)
            await msg.reply(f"ℹ️ دورهٔ قبلی ({_ids}) بسته شد و دورهٔ جدید شروع می‌شود.")

        # === PATCH_RAFFLE_COMMENT_MODE_V1 ===
        # PATCH_RAFFLE_START_FROM_COMMAND_V1 — یک‌بار حساب، در همهٔ حالت‌ها یکی
        _start = _raffle_start_at(replied.date)

        if _is_comment_mode_trigger(text):
            topic = (replied.text or replied.caption or "🎁 قرعه‌کشیِ کامنتی").strip()[:255]
            creator_id = (msg.from_user.id if msg.from_user
                          else (msg.sender_chat.id if msg.sender_chat else 0))
            async with Session() as s:
                r = ChannelRaffle(
                    chat_id=msg.chat.id, poll_message_id=replied.message_id,
                    mode="comment", topic=topic, options_json="[]",
                    poll_started_at=_start,
                    duration_hours=hours, created_by=creator_id,
                )
                s.add(r)
                await s.commit()
            _end = _start + datetime.timedelta(hours=hours)
            await msg.reply(
                f"✅ قرعه‌کشیِ کامنتی ثبت شد و تا <b>{_end:%H:%M}</b> بازه "
                f"({hours:g} ساعت).\n\n"
                f"📋 موضوع: {topic}\n\n"
                "از حالا <b>هرکسی زیرِ همون پست کامنت بذاره</b> — با هر متنی — "
                "به‌عنوانِ شرکت‌کننده ثبت می‌شه (هر نفر فقط یک‌بار).\n\n"
                "هر وقت خواستی بنویس «قرعه‌کشی رو شروع کن».")
            return

        if _is_mention_mode_trigger(text):
            # === قرعه‌کشیِ منشن‌محور ===
            # اینجا نیازی به فرمتِ «گزینه ۱: ...» نیست؛ موضوع همون متنِ
            # پیامیه که روش ریپلای زده شده (یا اگه متن نداشت، یه عنوانِ کلی).
            topic = (replied.text or replied.caption or "🎁 قرعه‌کشیِ منشن").strip()[:255]
            creator_id = msg.from_user.id if msg.from_user else (msg.sender_chat.id if msg.sender_chat else 0)
            async with Session() as s:
                r = ChannelRaffle(
                    chat_id=msg.chat.id, poll_message_id=replied.message_id,
                    mode="mention", topic=topic, options_json="[]",
                    poll_started_at=_start, duration_hours=hours,
                    created_by=creator_id,
                )
                s.add(r)
                await s.commit()
            await msg.reply(
                f"✅ قرعه‌کشیِ منشن‌محور ثبت شد و تا "
                f"<b>{_start + datetime.timedelta(hours=hours):%H:%M}</b> بازه "
                f"({hours:g} ساعت).\n\n"
                f"📋 موضوع: {topic}\n\n"
                "از حالا هرکی زیرِ همون پیام یه نفرِ دیگه رو منشن کنه، به‌عنوانِ شرکت‌کننده "
                "ثبت می‌شه (اگه چندبار بفرسته، فقط یه‌بار حساب می‌شه)."
            )
            return

        parsed = _extract_poll_content(replied)
        if not parsed:
            # === PATCH_RAFFLE_FREE_NUMBER_MODE_V1 ===
            # قبلاً اگر پیامِ سؤال خط‌هایِ «گزینه ۱: ...» نداشت، ثبت رد می‌شد.
            # ولی خیلی وقت‌ها ادمین فقط یک سؤالِ ساده می‌پرسد و می‌خواهد
            # کاربران عددِ جوابشان را ریپلای کنند — بدونِ اینکه مجبور باشد
            # گزینه‌ها را دستی تایپ کند.
            #
            # حالا این حالت به‌جایِ خطا، به‌عنوانِ «عددِ آزاد» ثبت می‌شود:
            #   • هر عددِ مثبتی که کاربر ریپلای کند رأیِ معتبر است
            #   • موقعِ «قرعه‌کشی رو شروع کن»، به‌جایِ دکمه‌ها، ادمین خودش
            #     عددِ درست را می‌فرستد
            # حالتِ گزینه‌دار دست‌نخورده باقی مانده؛ این فقط یک مسیرِ تازه است.
            topic = (replied.text or replied.caption or "🎲 قرعه‌کشی").strip()[:255]
            creator_id = (msg.from_user.id if msg.from_user
                          else (msg.sender_chat.id if msg.sender_chat else 0))
            async with Session() as s:
                r = ChannelRaffle(
                    chat_id=msg.chat.id, poll_message_id=replied.message_id,
                    mode="number", topic=topic, options_json="[]",
                    poll_started_at=_start,
                    duration_hours=hours, created_by=creator_id,
                )
                s.add(r)
                await s.commit()
            await msg.reply(
                f"✅ قرعه‌کشیِ عددی ثبت شد و تا "
                f"<b>{_start + datetime.timedelta(hours=hours):%H:%M}</b> بازه "
                f"({hours:g} ساعت).\n\n"
                f"📋 موضوع: {topic}\n\n"
                "از حالا هرکی زیرِ همون پیام یه <b>عدد</b> ریپلای کنه، رأیش ثبت می‌شه.\n"
                "هر وقت خواستی بنویس «قرعه‌کشی رو شروع کن» تا ازت بپرسم "
                "کدوم عدد درست بوده.")
            return
        topic, options = parsed
        creator_id = msg.from_user.id if msg.from_user else (msg.sender_chat.id if msg.sender_chat else 0)
        async with Session() as s:
            r = ChannelRaffle(
                chat_id=msg.chat.id, poll_message_id=replied.message_id,
                mode="poll", topic=topic, options_json=json.dumps(options, ensure_ascii=False),
                poll_started_at=_start, duration_hours=hours,
                created_by=creator_id,
            )
            s.add(r)
            await s.commit()
        opts_txt = "\n".join(f"  {i+1}. {o}" for i, o in enumerate(options))
        await msg.reply(
            f"✅ نظرسنجی ثبت شد و تا "
            f"<b>{_start + datetime.timedelta(hours=hours):%H:%M}</b> بازه "
            f"({hours:g} ساعت).\n\n"
            f"📋 موضوع: {topic}\n{opts_txt}\n\n"
            "از حالا هرکی زیرِ همون پیام با یه عدد ریپلای کنه، رأیش ثبت می‌شه."
        )
        return

    # حالتِ ۲ یا ۳: نظرسنجی از قبل ثبت شده
    now = _raffle_now()
    window_end = raffle.poll_started_at + datetime.timedelta(hours=raffle.duration_hours)
    in_window = raffle.poll_started_at <= now <= window_end

    if _is_start_trigger(text):
        if not await _is_authorized_sender(msg):
            return
        if raffle.status != "open":
            await msg.reply("این نظرسنجی قبلاً بسته/قرعه‌کشی شده است.")
            return
        if raffle.mode in ("mention", "comment"):
            # PATCH_RAFFLE_COMMENT_MODE_V1 — این دو حالت «جوابِ درست» ندارند
            await _close_mention_raffle_and_show_stats(msg, raffle.id)
            return
        # === PATCH_RAFFLE_INSTANT_LOCK_V1 ===
        # قبلاً اینجا فقط دکمه‌ی «کدوم گزینه درست بود» نشون داده می‌شد ولی
        # status هنوز "open" می‌موند — یعنی تا لحظه‌ای که ادمین واقعاً روی
        # یکی از گزینه‌ها تپ می‌کرد، هرکسی می‌تونست بازم رأی بده یا (چون
        # اولین رأی ثابته) اگه هنوز رأی نداده بود، همون لحظه با دیدنِ سؤال
        # حدس بزنه/رأیِ اول رو بده و نتیجه رو دستکاری کنه. الان همین لحظه‌ی
        # «قرعه‌کشی رو شروع کن»، رأی‌گیری قفل می‌شه (نه بعداً موقعِ انتخابِ
        # جواب) — دقیقاً هم‌رفتار با حالتِ منشن‌محور که از اول همینطوری بود.
        async with Session() as s:
            r2 = await s.get(ChannelRaffle, raffle.id)
            if not r2 or r2.status != "open":
                return  # یکی دیگه (یا خودِ همین ادمین دوبار) هم‌زمان بسته بودتش
            r2.status = "awaiting_answer"
            await s.commit()
        options = json.loads(raffle.options_json)
        # === PATCH_RAFFLE_FREE_NUMBER_MODE_V1 ===
        # در حالتِ «عددِ آزاد» گزینه‌ای نداریم که دکمه بسازیم، پس از ادمین
        # می‌خواهیم عددِ درست را تایپ کند. رأی‌گیری همین‌جا قفل شده، پس
        # فرصتی برایِ دستکاریِ نتیجه نیست.
        if raffle.mode == "number" or not options:
            await state.set_state(RaffleFSM.correct_number)
            await state.update_data(raffle_id=raffle.id)
            await msg.reply(
                "🔒 رأی‌گیری همین الان قفل شد؛ دیگه کسی نمی‌تونه رأیِ جدید بده.\n\n"
                "🤔 <b>عددِ درست چند بود؟</b>\n"
                "همین‌جا عددش رو بفرست (مثلاً <code>۱</code>) تا از بینِ کسانی "
                "که همون عدد رو داده بودن قرعه‌کشی کنم.\n\n"
                "<i>برایِ انصراف بنویس «لغو».</i>")
            return
        kb_rows = [[InlineKeyboardButton(text=f"{i+1}. {o}",
                                         callback_data=f"raffle:correct:{raffle.id}:{i+1}")]
                   for i, o in enumerate(options)]
        await msg.reply(
            "🔒 رأی‌گیری همین الان قفل شد؛ دیگه کسی نمی‌تونه رأیِ جدید بده.\n\n"
            "🤔 کدوم گزینه درست بود؟",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
        return

    # حالتِ ۲: تلاش برای ثبتِ رأی/شرکت — بدونِ from_user واقعی (پیامِ کاملاً
    # ناشناس) قابلِ ثبت نیست چون معلوم نمی‌شه جایزه رو باید به کی داد.
    if not msg.from_user:
        return
    if not in_window:
        return  # بازه تمام شده یا هنوز نرسیده؛ رأی/شرکت قبول نیست (بی‌صدا نادیده می‌گیریم)
    if raffle.status != "open":
        return

    # === PATCH_RAFFLE_COMMENT_MODE_V1 ===
    # حالتِ کامنتی: محتوایِ پیام اصلاً مهم نیست؛ خودِ کامنت‌گذاشتن یعنی شرکت.
    if raffle.mode == "comment":
        async with Session() as s:
            existing = await s.scalar(select(ChannelRaffleVote).where(
                ChannelRaffleVote.raffle_id == raffle.id,
                ChannelRaffleVote.telegram_id == msg.from_user.id))
            if existing:
                existing.full_name = msg.from_user.full_name
                existing.username = msg.from_user.username
            else:
                s.add(ChannelRaffleVote(
                    raffle_id=raffle.id, telegram_id=msg.from_user.id,
                    full_name=msg.from_user.full_name,
                    username=msg.from_user.username,
                    chosen_option=1,   # حالتِ کامنتی جوابِ درست ندارد
                ))
            await s.commit()
        await _react_safe(msg, "👍")
        return

    if raffle.mode == "mention":
        if not _has_mention(msg):
            return  # ریپلایِ عادی، منشنی توش نیست
        async with Session() as s:
            existing = await s.scalar(select(ChannelRaffleVote).where(
                ChannelRaffleVote.raffle_id == raffle.id,
                ChannelRaffleVote.telegram_id == msg.from_user.id))
            if existing:
                # قبلاً شرکت کرده — طبقِ خواسته، ارسالِ تکراری اضافه حساب نمی‌شه؛
                # فقط نام/یوزرنیمش رو (اگه عوض شده) به‌روز می‌کنیم.
                existing.full_name = msg.from_user.full_name
                existing.username = msg.from_user.username
            else:
                s.add(ChannelRaffleVote(
                    raffle_id=raffle.id, telegram_id=msg.from_user.id,
                    full_name=msg.from_user.full_name, username=msg.from_user.username,
                    chosen_option=1,  # قرعه‌کشیِ منشن‌محور گزینه ندارد؛ فقط یعنی «شرکت کرد»
                ))
            await s.commit()
        await _react_safe(msg, "🔥")   # PATCH_RAFFLE_REACTION_DIAGNOSTIC_V1
        return

    choice = _fa_int(text)
    options = json.loads(raffle.options_json)
    # === PATCH_RAFFLE_FREE_NUMBER_MODE_V1 ===
    # در حالتِ «عددِ آزاد» فهرستِ گزینه‌ای وجود ندارد، پس سقفی هم برایِ
    # اعتبارسنجی نیست؛ هر عددِ مثبت یک رأیِ معتبر است.
    if choice is None or choice <= 0:
        return
    if raffle.mode != "number" and not (1 <= choice <= len(options)):
        return  # حالتِ گزینه‌دار: عدد باید در بازهٔ گزینه‌ها باشد
    async with Session() as s:
        existing = await s.scalar(select(ChannelRaffleVote).where(
            ChannelRaffleVote.raffle_id == raffle.id,
            ChannelRaffleVote.telegram_id == msg.from_user.id))
        if existing:
            # === PATCH_RAFFLE_FIRST_VOTE_STICKS_V1 ===
            # قبلاً اینجا رأیِ فرد با هر ریپلایِ بعدی عوض می‌شد (آخرین رأی
            # حساب می‌شد)؛ الان طبقِ خواسته، فقط اولین رأیِ هرکس حساب می‌شه و
            # ریپلای‌های بعدیِ همون فرد (تکراری یا با عددِ دیگه) نادیده گرفته
            # می‌شن — دقیقاً هم‌رفتار با حالتِ منشن‌محور.
            existing.full_name = msg.from_user.full_name
            existing.username = msg.from_user.username
        else:
            s.add(ChannelRaffleVote(
                raffle_id=raffle.id, telegram_id=msg.from_user.id,
                full_name=msg.from_user.full_name, username=msg.from_user.username,
                chosen_option=choice,
            ))
        await s.commit()
    await _react_safe(msg, "👍")   # PATCH_RAFFLE_REACTION_DIAGNOSTIC_V1


# === PATCH_RAFFLE_MENTION_NO_REPLY_V1 ===
# خیلی از شرکت‌کننده‌ها به‌جای ریپلای‌زدن زیرِ پیامِ قرعه‌کشی، مستقیم توی
# گروه می‌نویسن «@username» بدونِ ریپلای — این هندلر همون‌ها رو هم، اگه
# قرعه‌کشیِ منشن‌محورِ بازی توی همون چت فعال باشه، به‌عنوانِ شرکت‌کننده
# ثبت می‌کنه. (پیام‌هایِ ریپلای‌دار همچنان از طریقِ raffle_group_reply
# بالا پردازش می‌شن؛ اینجا فقط پیامِ بدونِ‌ریپلای رو می‌گیره تا دوباره‌کاری
# نشه.)
@raffle_router.message(F.chat.type.in_({"group", "supergroup"}), F.text, ~F.reply_to_message)
async def raffle_group_mention_no_reply(msg: Message):
    # === PATCH_GROUP_ROUTER_SKIPHANDLER_V1 ===
    # همون مشکلِ raffle_group_reply؛ این هندلر هم رویِ *همهٔ* پیام‌هایِ
    # غیرِریپلایِ گروهی ثبت شده (مثلاً همون دستورِ /فعال_محافظ که غیرِریپلایه)
    # — پس هر جا تشخیص می‌ده این پیام اصلاً ربطی به قرعه‌کشی نداره، باید
    # SkipHandler بزنه تا روترهایِ بعدی (group_guard) بتونن پردازشش کنن.
    bound_chat_id, _ = await _get_raffle_group()
    if not bound_chat_id or msg.chat.id != bound_chat_id:
        raise SkipHandler
    if not msg.from_user:
        raise SkipHandler
    if not _has_mention(msg):
        raise SkipHandler
    async with Session() as s:
        raffle = await s.scalar(select(ChannelRaffle).where(
            ChannelRaffle.chat_id == msg.chat.id,
            ChannelRaffle.mode == "mention",
            ChannelRaffle.status == "open",
        ).order_by(ChannelRaffle.id.desc()))
    if not raffle:
        raise SkipHandler
    now = _raffle_now()
    window_end = raffle.poll_started_at + datetime.timedelta(hours=raffle.duration_hours)
    if not (raffle.poll_started_at <= now <= window_end):
        raise SkipHandler
    async with Session() as s:
        existing = await s.scalar(select(ChannelRaffleVote).where(
            ChannelRaffleVote.raffle_id == raffle.id,
            ChannelRaffleVote.telegram_id == msg.from_user.id))
        if existing:
            existing.full_name = msg.from_user.full_name
            existing.username = msg.from_user.username
        else:
            s.add(ChannelRaffleVote(
                raffle_id=raffle.id, telegram_id=msg.from_user.id,
                full_name=msg.from_user.full_name, username=msg.from_user.username,
                chosen_option=1,
            ))
        await s.commit()
    await _react_safe(msg, "🔥")   # PATCH_RAFFLE_REACTION_DIAGNOSTIC_V1


async def _close_mention_raffle_and_show_stats(msg: Message, raffle_id: int):
    """بستنِ قرعه‌کشیِ منشن‌محور و نمایشِ آمار + دکمه‌هایِ قرعه‌کشیِ الان/بعداً
    — بدونِ نیاز به «جوابِ درست» (همه‌ی شرکت‌کننده‌ها واجدِ شرایط‌اند)."""
    async with Session() as s:
        raffle = await s.get(ChannelRaffle, raffle_id)
        if not raffle or raffle.status != "open":
            return
        raffle.status = "closed"
        raffle.correct_option = 1  # صرفاً برایِ سازگاری با draw_winners (فیلترِ chosen_option==correct_option)
        _mode = raffle.mode        # PATCH_RAFFLE_COMMENT_MODE_V1
        await s.commit()
        total = await s.scalar(select(func.count()).select_from(ChannelRaffleVote).where(
            ChannelRaffleVote.raffle_id == raffle_id))

    winners_n = await _get_winners_count()
    text = (
        "مدیر به روی چشم 🫡\n\n"
        f"📋 <b>{raffle.topic}</b>\n"
        f"🎯 نوع: {'قرعه‌کشیِ کامنتی' if _mode == 'comment' else 'قرعه‌کشیِ منشن‌محور'}\n\n"
        f"👥 آمار کل نفراتِ شرکت‌کننده: <b>{total}</b>"
    )
    draw_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🎲 بله، الان {winners_n} نفر برنده رو انتخاب کن",
                             callback_data=f"raffle:draw:{raffle_id}", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="⏳ بعداً از پنل ادمین", callback_data=f"raffle:later:{raffle_id}"),
    ]])
    await msg.reply(text, reply_markup=draw_kb)


# ═══════════════════════ تعیینِ پاسخِ درست → آمار ═══════════════════════

@raffle_router.callback_query(F.data.startswith("raffle:correct:"))
async def raffle_set_correct(cb: CallbackQuery):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("فقط ادمین‌های بات‌ساز", show_alert=True)
        return
    _, _, raffle_id, opt = cb.data.split(":")
    raffle_id, opt = int(raffle_id), int(opt)
    async with Session() as s:
        raffle = await s.get(ChannelRaffle, raffle_id)
        if not raffle or raffle.status != "awaiting_answer":
            await cb.answer("این نظرسنجی دیگر در دسترس نیست.", show_alert=True)
            return
        raffle.correct_option = opt
        raffle.status = "closed"
        await s.commit()
        votes = (await s.scalars(select(ChannelRaffleVote).where(
            ChannelRaffleVote.raffle_id == raffle_id))).all()

    options = json.loads(raffle.options_json)
    total = len(votes)
    # PATCH_RAFFLE_MANUAL_ALWAYS_ELIGIBLE_V1
    correct = sum(1 for v in votes if v.chosen_option in (opt, _MANUAL_OPTION))
    wrong = total - correct

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    winners_n = await _get_winners_count()
    text = (
        "مدیر به روی چشم 🫡\n\n"
        f"📋 <b>{raffle.topic}</b>\n"
        f"✅ گزینه‌ی درست: <b>{opt}. {options[opt-1]}</b>\n\n"
        f"👥 آمار کل نفرات رأی‌دهنده: <b>{total}</b>\n"
        f"✅ آمار کل نفرات پاسخِ درست: <b>{correct}</b>\n"
        f"❌ آمار نفرات پاسخِ اشتباه: <b>{wrong}</b>"
    )
    draw_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🎲 بله، الان {winners_n} نفر برنده رو انتخاب کن",
                             callback_data=f"raffle:draw:{raffle_id}", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="⏳ بعداً از پنل ادمین", callback_data=f"raffle:later:{raffle_id}"),
    ]])
    await cb.message.answer(text, reply_markup=draw_kb)
    await cb.answer()


@raffle_router.callback_query(F.data.startswith("raffle:later:"))
async def raffle_draw_later(cb: CallbackQuery):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("فقط ادمین‌های بات‌ساز", show_alert=True)
        return
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "باشه، هر وقت خواستی از «🛠 پنل مدیریت → 🎉 قرعه‌کشی کانال بات‌ساز → "
        "📊 نتایج قرعه‌کشی» می‌تونی برنده‌ها رو انتخاب کنی.")
    await cb.answer()


@raffle_router.callback_query(F.data.startswith("raffle:draw:"))
async def raffle_draw_now(cb: CallbackQuery, bot: Bot):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("فقط ادمین‌های بات‌ساز", show_alert=True)
        return
    raffle_id = int(cb.data.split(":")[2])
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer()
    text, err = await draw_winners(raffle_id, bot)
    target = cb.message
    if err:
        await target.answer(err)
        return
    await target.answer(text)


async def draw_winners(raffle_id: int, bot: Bot | None = None) -> tuple[str | None, str | None]:
    """قرعه‌کشیِ واقعی: حداکثر N نفر (به تعدادِ تنظیم‌شده در پنل، پیش‌فرض ۵)
    رندوم از بینِ پاسخ‌درست‌ها انتخاب و ذخیره می‌کند.
    خروجی (متنِ اعلامِ برنده‌ها, پیامِ خطا) — دقیقاً یکی از این
    دوتا None است. اگه bot داده بشه و کانالی وصل باشه، خلاصه‌ی نتایج خودکار
    اونجا هم پست می‌شه."""
    async with Session() as s:
        raffle = await s.get(ChannelRaffle, raffle_id)
        if not raffle:
            return None, "❌ این دورِ قرعه‌کشی پیدا نشد."
        if raffle.status == "drawn":
            return None, "این دور قبلاً قرعه‌کشی شده است."
        if raffle.correct_option is None:
            return None, "❌ هنوز جوابِ درست تعیین نشده."
        votes = (await s.scalars(select(ChannelRaffleVote).where(
            ChannelRaffleVote.raffle_id == raffle_id,
            # PATCH_RAFFLE_MANUAL_ALWAYS_ELIGIBLE_V1 — دستی‌ها همیشه داخل‌اند
            ChannelRaffleVote.chosen_option.in_(
                (raffle.correct_option, _MANUAL_OPTION))))).all()
        if not votes:
            raffle.status = "drawn"
            await s.commit()
            if raffle.mode == "comment":   # PATCH_RAFFLE_COMMENT_MODE_V1
                return None, "😕 هیچ‌کس زیرِ این پست کامنت نذاشته بود؛ این دور بدونِ برنده بسته شد."
            if raffle.mode == "mention":
                return None, "😕 هیچ‌کس زیرِ این پیام کسی رو منشن نکرده بود؛ این دور بدونِ برنده بسته شد."
            return None, "😕 هیچ‌کس پاسخِ درست نداده بود؛ این دور بدونِ برنده بسته شد."
        winners_count = await _get_winners_count()
        winners = random.sample(list(votes), k=min(winners_count, len(votes)))
        # === PATCH_RAFFLE_WON_AT_V1 ===
        # قبلاً won_at ست نمی‌شد (همیشه None می‌موند)، و صفحهٔ «برندگانِ
        # قرعه‌کشی» به‌جاش تاریخِ poll_started_at (زمانِ ارسالِ خودِ نظرسنجی)
        # رو نشون می‌داد — که اگه قرعه‌کشی چند روز بعدِ شروعِ نظرسنجی انجام
        # می‌شد، تاریخِ نمایش‌داده‌شده قدیمی/غلط بود. الان لحظهٔ واقعیِ
        # قرعه‌کشی ثبت می‌شه.
        drawn_at = _raffle_now()
        for i, w in enumerate(winners, start=1):
            s.add(ChannelRaffleWinner(
                raffle_id=raffle_id, telegram_id=w.telegram_id,
                full_name=w.full_name, username=w.username, rank=i,
                won_at=drawn_at,
            ))
        raffle.status = "drawn"
        await s.commit()
        topic = raffle.topic

    lines = [
        "🎉🎊 <b>نتایجِ قرعه‌کشیِ امروز</b> 🎊🎉",
        f"📋 موضوع: {topic}",
        "",
        "✨ برندگانِ این دوره:",
    ]
    for i, w in enumerate(winners, start=1):
        name = f"@{w.username}" if w.username else (w.full_name or f"کاربر {w.telegram_id}")
        lines.append(f"{_medal_for_rank(i)} {name}")
    lines.append("")
    lines.append(f"🎁 تبریک می‌گیم! شما برندهٔ قرعه‌کشیِ امروز ({_today_fa()}) شدید 🥳")
    lines.append("جایزه‌تون به‌زودی توسطِ مدیریت واریز می‌شه.")
    text = "\n".join(lines)

    if bot is not None:
        await _post_results_to_channel(bot, text)
    return text, None


async def _post_results_to_channel(bot: Bot, text: str) -> None:
    """اگه یه کانال برایِ قرعه‌کشی وصل باشه، همون متنِ اعلامِ برنده‌ها رو
    خودکار اونجا هم پست می‌کنه — صرفاً اطلاع‌رسانیه؛ اگه ارسال به هر دلیلی
    (کانال وصل نیست، بات دیگه ادمین نیست و…) شکست بخوره، کاملاً بی‌صدا رد
    می‌شه چون خودِ قرعه‌کشی/برنده‌ها همیشه در پنلِ مدیریت و برایِ برنده‌ها هم
    (وقتِ اهدایِ جایزه) قابل‌دسترسیه — این فقط یک مزیتِ اضافه است."""
    try:
        channel_id, _ = await _get_raffle_channel()
        if not channel_id:
            return
        await bot.send_message(channel_id, text)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


def _today_fa() -> str:
    try:
        import jdatetime
        return jdatetime.datetime.fromgregorian(
            datetime=_raffle_now()).strftime("%Y/%m/%d")
    except Exception:
        return _raffle_now().date().isoformat()


# ═══════════════════════ پنلِ مدیریت: منوی اصلی ═══════════════════════

def _jalali(dt) -> str:
    if not dt:
        return "—"
    try:
        import jdatetime
        return jdatetime.datetime.fromgregorian(datetime=dt).strftime("%Y/%m/%d — %H:%M")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M")


async def raffle_admin_menu_text_kb():
    ch_id, ch_title = await _get_raffle_channel()
    gr_id, gr_title = await _get_raffle_group()
    winners_n = await _get_winners_count()
    ch_line = f"📢 کانال: <b>{ch_title or ch_id}</b>" if ch_id else "📢 کانال: ⚠️ وصل نشده (اختیاری)"
    gr_line = f"👥 گروه: <b>{gr_title or gr_id}</b>" if gr_id else "👥 گروه: ⚠️ وصل نشده"
    return (
        "🎉 <b>قرعه‌کشی کانال بات‌ساز</b>\n\n"
        f"{ch_line}\n{gr_line}\n"
        f"🏆 تعدادِ برنده‌های هر دور: <b>{winners_n} نفر</b>\n\n"
        "💡 <b>روشِ کار:</b>\n"
        "  ۱) خودت نظرسنجی رو دستی توی گروه پست کن (با خط‌هایی مثلِ «گزینه ۱: ...»)\n"
        "  ۲) روش ریپلای بزن و بنویس «۳ ساعت» (یا هر عددی) تا ثبتش کنم\n"
        "  ۳) هرکی زیرش با یه عدد ریپلای بزنه، رأیش ثبت می‌شه\n"
        "  ۴) هروقت خواستی، ریپلای بزن «قرعه‌کشی رو شروع کن»\n\n"
        "اگه <b>کانال</b> رو هم وصل کنی، خلاصه‌ی برنده‌ها بعدِ هر قرعه‌کشی خودکار اونجا هم پست می‌شه.\n\n"
        "اینجا می‌تونی نتایجِ دورهای قبلیِ قرعه‌کشی رو ببینی و به برنده‌ها جایزه بدی.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 اتصالِ کانال", callback_data="raffle:setchat:channel"),
             InlineKeyboardButton(text="👥 اتصالِ گروه", callback_data="raffle:setchat:group")],
            [InlineKeyboardButton(text=f"🏆 تعدادِ برنده‌ها: {winners_n} نفر",
                                  callback_data="raffle:setwinners")],
            [InlineKeyboardButton(text="📊 نتایج قرعه‌کشی", callback_data="raffle:results")],
            [InlineKeyboardButton(text="🏆 نفرات برنده", callback_data="raffle:winnerslist")],
            # === PATCH_RAFFLE_FORWARD_COLLECT_V1 ===
            [InlineKeyboardButton(text="📥 ثبتِ نفرات با فوروارد",
                                  callback_data="raffle:collect")],
            # === PATCH_RAFFLE_HELP_V1 ===
            [InlineKeyboardButton(text="📘 راهنمای کامل", callback_data="raffle:help")],
            [InlineKeyboardButton(text="⬅️ بازگشت به پنل", callback_data="adm:back",
                                  style=ButtonStyle.DANGER)],
        ])
    )


@raffle_router.callback_query(F.data == "raffle:menu")
async def raffle_menu_cb(cb: CallbackQuery):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    text, kb = await raffle_admin_menu_text_kb()
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


# === PATCH_RAFFLE_HELP_V1 ===
# متنِ منو فقط خلاصه‌ای چهارخطی داشت و حالتِ «منشن‌محور» و پیش‌نیازِ
# Privacy Mode را اصلاً نمی‌گفت — همان دو چیزی که اگر ندانی، قرعه‌کشی
# بی‌سروصدا کار نمی‌کند. این راهنما مستقیم از رویِ الگوهایِ واقعیِ کد
# نوشته شده (_HOUR_RE و _OPTION_LINE_RE و _is_start_cmd).
# === PATCH_RAFFLE_FORWARD_COLLECT_V1 ===
# درخواستِ ادمین: «بتونم پیامِ این‌ها رو از گروه فوروارد کنم به ربات و در
# قرعه‌کشیِ باز ثبت بشن».
#
# چرا فوروارد (و نه لایک): آپدیتِ message_reaction نویسندهٔ پیام را نمی‌دهد،
# ولی پیامِ فورواردشده در `forward_origin.sender_user` خودِ کاربر را دارد.
#
# محدودیت: اگر کاربر در تنظیماتِ حریمِ خصوصی «Forwarded messages» را بسته
# باشد، تلگرام فقط نامش را می‌دهد (MessageOriginHiddenUser) و آیدیِ عددی
# ندارد — چنین موردی قابلِ ثبت نیست و همان‌جا اعلام می‌شود تا ادمین از
# راهِ ریپلایِ «ثبت» داخلِ گروه اضافه‌اش کند.
@raffle_router.callback_query(F.data == "raffle:collect")
async def raffle_collect_start(cb: CallbackQuery, state: FSMContext):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    gid, _ = await _get_raffle_group()
    if not gid:
        await cb.answer("اول گروه را در همین پنل وصل کن.", show_alert=True)
        return
    # === PATCH_RAFFLE_COLLECT_STATUS_ONLY_V1 ===
    # قبلاً اینجا علاوه بر status، پنجرهٔ زمانی هم چک می‌شد. نتیجه‌اش
    # ناسازگاری بود: صفحهٔ «نتایج قرعه‌کشی» همان دوره را «🟡 هنوز باز است»
    # نشان می‌داد (چون فقط به status نگاه می‌کند) ولی این دکمه می‌گفت
    # «هیچ قرعه‌کشیِ بازی نیست».
    #
    # ثبتِ دستی/فورواردی کارِ خودِ ادمین است، نه رأیِ کاربر — پس محدودکردنش
    # به پنجرهٔ زمانی بی‌معنی است. حالا هر دوره‌ای که status=="open" باشد
    # قابلِ جمع‌آوری است، و ملاک با صفحهٔ نتایج یکی شد.
    async with Session() as s:
        rows = (await s.scalars(select(ChannelRaffle).where(
            ChannelRaffle.chat_id == int(gid),
            ChannelRaffle.status.in_(("open", "awaiting_answer")))
            .order_by(ChannelRaffle.id.desc()))).all()
    if not rows:
        await cb.answer("الان هیچ قرعه‌کشیِ بازی نیست.", show_alert=True)
        return
    r = rows[0]
    async with Session() as s:
        n = await s.scalar(select(func.count()).select_from(ChannelRaffleVote)
                           .where(ChannelRaffleVote.raffle_id == r.id))
    await state.set_state(RaffleFSM.forward_collect)
    await state.update_data(raffle_id=r.id)
    await cb.message.answer(
        f"📥 <b>ثبتِ نفرات با فوروارد</b>\n\n"
        f"قرعه‌کشیِ باز: <b>#{r.id}</b> — {r.topic[:60]}\n"
        f"👥 تا الان: <b>{n}</b> نفر\n\n"
        "حالا پیامِ هر شرکت‌کننده را از گروه <b>فوروارد کن به همین‌جا</b>. "
        "می‌توانی چندتا پشتِ‌هم بفرستی.\n\n"
        "هر کدام را که ثبت کنم خبر می‌دهم.\n"
        "در پایان بنویس <code>تمام</code>.",
        parse_mode="HTML")
    await cb.answer()


@raffle_router.message(RaffleFSM.forward_collect)
async def raffle_collect_recv(msg: Message, state: FSMContext):
    if not await _is_raffle_admin(msg.from_user.id):
        await state.clear()
        return
    txt = (msg.text or "").strip()
    if txt in {"تمام", "پایان", "لغو", "❌ لغو", "بازگشت", "/cancel"}:
        data = await state.get_data()
        await state.clear()
        async with Session() as s:
            n = await s.scalar(select(func.count()).select_from(ChannelRaffleVote)
                               .where(ChannelRaffleVote.raffle_id == data.get("raffle_id")))
        from platform_bot import smart_home_kb
        await msg.answer(f"✅ تمام شد. مجموعِ شرکت‌کننده‌ها: <b>{n}</b> نفر.",
                         reply_markup=await smart_home_kb(msg.from_user.id),
                         parse_mode="HTML")
        return

    origin = getattr(msg, "forward_origin", None)
    user = getattr(origin, "sender_user", None) or getattr(msg, "forward_from", None)
    if user is None:
        hidden = getattr(origin, "sender_user_name", None)
        if hidden:
            await msg.reply(
                f"⚠️ «{hidden}» حسابش را از فوروارد مخفی کرده، پس آیدی‌اش را "
                "ندارم.\n\nبه‌جایش داخلِ گروه رویِ پیامِ خودش ریپلای بزن و "
                "بنویس <code>ثبت</code>.", parse_mode="HTML")
        else:
            await msg.reply("این یک پیامِ فورواردشده نیست. پیامِ شرکت‌کننده را "
                            "از گروه فوروارد کن، یا بنویس <code>تمام</code>.",
                            parse_mode="HTML")
        return
    if user.is_bot:
        await msg.reply("این پیامِ یک ربات است؛ ثبت نشد.")
        return

    data = await state.get_data()
    raffle_id = data.get("raffle_id")
    async with Session() as s:
        r = await s.get(ChannelRaffle, raffle_id)
        if not r or r.status != "open":
            await state.clear()
            await msg.reply("این قرعه‌کشی دیگر باز نیست.")
            return
        existing = await s.scalar(select(ChannelRaffleVote).where(
            ChannelRaffleVote.raffle_id == raffle_id,
            ChannelRaffleVote.telegram_id == user.id))
        if existing:
            n = await s.scalar(select(func.count()).select_from(ChannelRaffleVote)
                               .where(ChannelRaffleVote.raffle_id == raffle_id))
            await msg.reply(f"ℹ️ {user.full_name} از قبل ثبت شده بود. "
                            f"(مجموع: {n} نفر)")
            return
        s.add(ChannelRaffleVote(
            raffle_id=raffle_id, telegram_id=user.id,
            full_name=user.full_name, username=user.username,
            chosen_option=_MANUAL_OPTION,   # PATCH_RAFFLE_MANUAL_ALWAYS_ELIGIBLE_V1
        ))
        await s.commit()
        n = await s.scalar(select(func.count()).select_from(ChannelRaffleVote)
                           .where(ChannelRaffleVote.raffle_id == raffle_id))
    await msg.reply(f"✅ {user.full_name} ثبت شد. (مجموع: {n} نفر)")


@raffle_router.callback_query(F.data == "raffle:help")
async def raffle_help(cb: CallbackQuery):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    txt = (
        "📘 <b>راهنمای قرعه‌کشی</b>\n\n"

        "⚠️ <b>پیش‌نیازِ حتمی</b>\n"
        "در @BotFather → Bot Settings → Group Privacy → <b>Turn off</b>\n"
        "بدونِ این، ربات ریپلای‌هایِ کاربران را اصلاً نمی‌بیند و قرعه‌کشی "
        "بدونِ شرکت‌کننده بسته می‌شود.\n"
        "همچنین ربات باید در گروه <b>ادمین</b> باشد.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🅰️ <b>حالتِ نظرسنجی (چندگزینه‌ای)</b>\n\n"
        "<b>۱.</b> خودت سؤال را در گروه پست کن. هر گزینه در یک خط، "
        "دقیقاً با این قالب:\n"
        "<code>گزینه ۱: پاسخ اول\n"
        "گزینه ۲: پاسخ دوم\n"
        "گزینه ۳: پاسخ سوم</code>\n"
        "<i>کلمهٔ «گزینه» و شمارهٔ آن الزامی است؛ دونقطه یا خط تیره هر دو قبول است.</i>\n\n"
        "<b>۲.</b> رویِ همان پیام <b>ریپلای</b> بزن و مدت را بنویس:\n"
        "<code>۳ ساعت</code>  یا  <code>ساعت ۳</code>  یا  <code>۱.۵ ساعت وقت داره</code>\n"
        "<i>عددِ اعشاری هم قبول است. ربات با ✅ ثبتش را تأیید می‌کند.</i>\n\n"
        "<b>۳.</b> کاربران زیرِ پیامِ سؤال فقط <b>یک عدد</b> ریپلای می‌کنند "
        "(شمارهٔ گزینه). اگر چند بار بزنند، آخرین رأیشان حساب می‌شود.\n\n"
        "<b>۴.</b> هر وقت خواستی، دوباره رویِ پیامِ سؤال ریپلای بزن:\n"
        "<code>قرعه‌کشی رو شروع کن</code>\n"
        "<i>هر متنی که هم «قرعه» داشته باشد و هم «شروع» یا «استارت» کار می‌کند.</i>\n\n"
        "<b>۵.</b> ربات می‌پرسد کدام گزینه درست بود → انتخاب کن → "
        "آمار پست می‌شود → تأیید کن تا برنده‌ها رندوم انتخاب شوند.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🅱️ <b>حالتِ کامنتی</b> — برایِ پستِ کانال\n\n"
        "هرکس زیرِ پست کامنت بذارد (با هر متنی) شرکت‌کننده می‌شود؛ نه عدد "
        "لازم است نه جوابِ درست.\n"
        "رویِ پست ریپلای بزن: <code>۳ ساعت کامنت</code>\n"
        "بعد فقط بنویس «قرعه‌کشی رو شروع کن» و برنده‌ها انتخاب می‌شوند.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🅲 <b>حالتِ عددِ آزاد</b>\n\n"
        "اگر حوصلهٔ نوشتنِ «گزینه ۱: …» را نداری، فقط سؤالت را ساده پست کن:\n"
        "<code>عددِ درست رو حدس بزن!</code>\n"
        "بعد ریپلای بزن <code>۳ ساعت</code> — چون خطِ گزینه ندارد، خودکار "
        "به‌صورتِ «عددی» ثبت می‌شود.\n"
        "کاربران هر <b>عددی</b> ریپلای کنند رأیشان ثبت می‌شود، و موقعِ "
        "«قرعه‌کشی رو شروع کن» ربات می‌پرسد «عددِ درست چند بود؟» — تو "
        "<code>۱</code> را می‌فرستی و از بینِ همان‌ها قرعه‌کشی می‌شود.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🅳 <b>حالتِ منشن‌محور</b>\n\n"
        "برایِ «دوستت را دعوت کن» — بدونِ گزینه و بدونِ پاسخِ درست.\n"
        "کافی است در پیامِ ریپلایِ مرحلهٔ ۲، کلمهٔ <b>منشن</b> را هم بنویسی:\n"
        "<code>۵ ساعت منشن</code>\n"
        "بعد هرکس زیرِ آن پیام یک نفرِ دیگر را منشن کند شرکت‌کننده می‌شود.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🎁 <b>اهدایِ جایزه</b>\n\n"
        "از همین پنل → «🏆 نفرات برنده» → دوره را انتخاب کن → "
        "«اهدا به یک نفر» یا «اهدا به همه» → مبلغ را بفرست.\n"
        "مبلغ به کیفِ پولِ پلتفرمیِ برنده واریز و به او اطلاع داده می‌شود.\n"
        "<i>کسی که یک بار جایزه گرفته باشد، بارِ دوم پرداخت نمی‌شود.</i>\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "📥 <b>ثبتِ نفرات با فوروارد</b> (از داخلِ ربات)\n\n"
        "در همین پنل دکمهٔ «📥 ثبتِ نفرات با فوروارد» را بزن، بعد پیامِ هر "
        "شرکت‌کننده را از گروه به ربات فوروارد کن. چندتا پشتِ‌هم هم می‌شود. "
        "در پایان بنویس <code>تمام</code>.\n"
        "<i>اگر کسی حسابش را از فوروارد مخفی کرده باشد آیدی‌اش را نمی‌دهد؛ "
        "آن یکی را از راهِ زیر اضافه کن.</i>\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "👆 <b>ثبتِ دستیِ یک نفر</b> (داخلِ گروه)\n\n"
        "رویِ پیامِ خودِ آن شخص در گروه ریپلای بزن و بنویس "
        "<code>ثبت</code> (یا <code>+</code>).\n"
        "همان‌جا ثبت می‌شود، پیامش لایک می‌خورد و تعدادِ کل را می‌گویم.\n"
        "رویِ پیام‌هایِ <b>قدیمی</b> هم کار می‌کند، پس اگر کسی قبل از باز شدنِ "
        "قرعه‌کشی کامنت گذاشته بود می‌توانی همین حالا اضافه‌اش کنی.\n\n"

        "💡 اگر کسی در فهرستِ برنده‌ها نبود ولی باید می‌بود، از همان صفحه "
        "می‌توانی با یوزرنیم دستی اضافه‌اش کنی."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="raffle:menu",
                              style=ButtonStyle.DANGER)]])
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@raffle_router.callback_query(F.data == "raffle:setwinners")
async def raffle_setwinners_start(cb: CallbackQuery, state: FSMContext):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    cur = await _get_winners_count()
    await state.set_state(RaffleFSM.winners_count)
    from platform_bot import smart_cancel_kb
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        f"🏆 <b>تعدادِ برنده‌های هر دور</b>\n\n"
        f"الان تنظیم‌شده رو <b>{cur} نفر</b>. یه عددِ جدید بفرست (مثلاً 3):\n\n"
        "⚠️ این تنظیم فقط رویِ قرعه‌کشی‌هایی که <b>بعداً</b> اجرا می‌کنی اثر می‌ذاره؛ "
        "دورهایی که قبلاً قرعه‌کشی شدن دست‌نخورده می‌مونن.",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@raffle_router.message(RaffleFSM.winners_count, F.text)
async def raffle_setwinners_recv(msg: Message, state: FSMContext):
    if not await _is_raffle_admin(msg.from_user.id):
        await state.clear()
        return
    from platform_bot import smart_home_kb
    if msg.text.strip() == "❌ لغو":
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id))
        return
    n = _fa_int(msg.text)
    if n is None or n <= 0:
        await msg.answer("یه عددِ صحیحِ مثبت بفرست (مثلاً 3):")
        return
    if n > 50:
        await msg.answer("عددِ خیلی بزرگیه؛ حداکثر ۵۰ نفر رو پشتیبانی می‌کنم. یه عددِ کوچیک‌تر بفرست:")
        return
    await _set_winners_count(n)
    await state.clear()
    await msg.answer(f"✅ تعدادِ برنده‌های هر دور رو {n} نفر تنظیم کردم.",
                     reply_markup=await smart_home_kb(msg.from_user.id))
    text, kb = await raffle_admin_menu_text_kb()
    await msg.answer(text, reply_markup=kb)


@raffle_router.callback_query(F.data.startswith("raffle:setchat:"))
async def raffle_setchat_start(cb: CallbackQuery, state: FSMContext):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    kind = cb.data.split(":")[2]  # "channel" یا "group"
    await state.set_state(RaffleFSM.setchat)
    await state.update_data(setchat_kind=kind)
    from platform_bot import smart_cancel_kb
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    kind_fa = "کانال" if kind == "channel" else "گروه"
    hint = (
        "اگه کانالته و کامنت/بحث داره، همینجا خودِ کانال رو معرفی کن (فقط برایِ مرجع/اطلاع‌رسانیه)."
        if kind == "channel" else
        "اگه گروهِ بحثِ یه کانال (Discussion Group) داره، همونو معرفی کن، نه خودِ کانال — "
        "چون نظرسنجیِ واقعی و رأی‌ها اونجا رد و بدل می‌شن."
    )
    await cb.message.answer(
        f"🔗 <b>اتصالِ {kind_fa}</b>\n\n"
        f"یه پیام از همون {kind_fa} برام فوروارد کن (یا یوزرنیمش رو با @ بفرست).\n\n"
        f"⚠️ قبلش مطمئن شو ربات توی اون {kind_fa} عضو (ترجیحاً ادمین) هست. {hint}",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@raffle_router.message(RaffleFSM.setchat)
async def raffle_setchat_recv(msg: Message, state: FSMContext, bot: Bot):
    if not await _is_raffle_admin(msg.from_user.id):
        await state.clear()
        return
    from platform_bot import smart_home_kb
    data = await state.get_data()
    kind = data.get("setchat_kind", "group")
    kind_fa = "کانال" if kind == "channel" else "گروه"
    if (msg.text or "").strip() == "❌ لغو":
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await smart_home_kb(msg.from_user.id))
        text, kb = await raffle_admin_menu_text_kb()
        await msg.answer(text, reply_markup=kb)
        return

    # === PATCH_RAFFLE_FORWARD_ORIGIN_V1 ===
    target = None
    fwd_origin = getattr(msg, "forward_origin", None)
    fwd_origin_type = getattr(fwd_origin, "type", None)
    if fwd_origin_type == "channel":
        target = fwd_origin.chat.id
    elif fwd_origin_type == "chat":
        target = fwd_origin.sender_chat.id
    elif msg.forward_from_chat:
        target = msg.forward_from_chat.id
    else:
        raw = (msg.text or "").strip()
        if raw.startswith("@") and len(raw) > 1:
            target = raw
        elif raw.lstrip("-").isdigit():
            target = int(raw)

    if target is None:
        await msg.answer(
            f"❌ این یک فورواردِ معتبر یا یوزرنیم نبود. یه پیام از همون {kind_fa} "
            "فوروارد کن، یا یوزرنیمش رو با @ بفرست، یا «❌ لغو» رو بزن.")
        return

    try:
        chat = await bot.get_chat(target)
    except Exception as e:
        await msg.answer(f"❌ به این {kind_fa} دسترسی ندارم: {e}")
        return

    expect_type = "channel" if kind == "channel" else "group/supergroup"
    is_channel = chat.type == "channel"
    if (kind == "channel") != is_channel:
        await msg.answer(
            f"❌ این چت از نوعِ «{chat.type}» است، نه {expect_type}. "
            f"چتِ درستِ نوعِ «{kind_fa}» رو فوروارد کن.")
        return

    # === نکته: bot.get_chat موفق‌شدن به این معنا نیست که بات واقعاً عضوِ
    # این چته (برای چت‌های عمومی حتی بدونِ عضویت هم resolve می‌شه)؛ این
    # همون چیزیه که باعثِ خطای بعدیِ «bot is not a member» موقعِ فرستادنِ
    # نظرسنجی می‌شد. اینجا صریحاً با get_chat_member چک می‌کنیم.
    try:
        member = await bot.get_chat_member(chat.id, bot.id)
    except Exception as e:
        await msg.answer(
            f"❌ نتونستم وضعیتِ عضویتِ بات توی این {kind_fa} رو چک کنم: {e}\n"
            f"مطمئن شو بات واقعاً عضوِ این {kind_fa} است.")
        return
    if member.status in ("left", "kicked"):
        await msg.answer(
            f"❌ بات عضوِ این {kind_fa} نیست (وضعیت: {member.status}). اول بات رو به این "
            f"{kind_fa} اضافه کن (ترجیحاً به‌عنوانِ ادمین)، بعد دوباره امتحان کن.")
        return
    admin_warning = ""
    if kind == "group" and member.status != "administrator":
        admin_warning = (
            "\n\n⚠️ بات توی این گروه عضوِ عادیه، ادمین نیست — فرستادنِ نظرسنجی معمولاً حتی "
            "بدونِ ادمین‌بودن هم کار می‌کنه، ولی برای اطمینانِ کامل بهتره از پنلِ گروه ادمینش کنی."
        )

    if kind == "channel":
        await _set_raffle_channel(chat.id, chat.title or chat.username or str(chat.id))
    else:
        await _set_raffle_group(chat.id, chat.title or chat.username or str(chat.id), chat.username or "")
    await state.clear()
    await msg.answer(f"✅ «{chat.title or chat.username}» به‌عنوانِ {kind_fa}ِ قرعه‌کشی وصل شد.{admin_warning}",
                     reply_markup=await smart_home_kb(msg.from_user.id))
    text, kb = await raffle_admin_menu_text_kb()
    await msg.answer(text, reply_markup=kb)



# ═══════════════════════ نتایج قرعه‌کشی ═══════════════════════

@raffle_router.callback_query(F.data == "raffle:results")
async def raffle_results_list(cb: CallbackQuery):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    async with Session() as s:
        rows = (await s.scalars(
            select(ChannelRaffle).order_by(ChannelRaffle.created_at.desc()).limit(30)
        )).all()
    if not rows:
        await cb.message.edit_text(
            "🗂 هنوز هیچ دورِ قرعه‌کشی‌ای ثبت نشده.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data="raffle:menu",
                                     style=ButtonStyle.DANGER)]]))
        await cb.answer()
        return
    status_icon = {"open": "🟡", "awaiting_answer": "🔒", "closed": "🟠", "drawn": "🟢"}
    kb_rows = []
    for r in rows:
        mode_icon = "🏷" if r.mode == "mention" else "📝"
        label = f"{status_icon.get(r.status, '⚪️')}{mode_icon} {_jalali(r.poll_started_at)} — {r.topic[:28]}"
        kb_rows.append([InlineKeyboardButton(text=label, callback_data=f"raffle:resultdetail:{r.id}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="raffle:menu",
                                         style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        "📊 <b>نتایج قرعه‌کشی</b>\n\n"
        "🟡 باز — 🟠 بسته (منتظرِ قرعه‌کشی) — 🟢 قرعه‌کشی‌شده\n"
        "📝 نظرسنجی — 🏷 منشن‌محور",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await cb.answer()


@raffle_router.callback_query(F.data.startswith("raffle:resultdetail:"))
async def raffle_result_detail(cb: CallbackQuery):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    raffle_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(ChannelRaffle, raffle_id)
        if not r:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        votes = (await s.scalars(select(ChannelRaffleVote).where(
            ChannelRaffleVote.raffle_id == raffle_id))).all()
    total = len(votes)
    status_txt = {"open": "🟡 هنوز باز است", "awaiting_answer": "🔒 قفل‌شده — منتظرِ انتخابِ جوابِ درست",
                 "closed": "🟠 بسته — منتظرِ قرعه‌کشی",
                 "drawn": "🟢 قرعه‌کشی انجام شده"}.get(r.status, r.status)
    if r.mode == "mention":
        text = (
            f"📋 <b>{r.topic}</b>\n\n"
            f"🗓 تاریخ/ساعت: {_jalali(r.poll_started_at)}\n"
            f"⏳ مدت: {r.duration_hours:g} ساعت\n"
            f"🎯 نوع: منشن‌محور\n"
            f"وضعیت: {status_txt}\n\n"
            f"👥 کل شرکت‌کننده (منشن‌کننده): <b>{total}</b>"
        )
    else:
        options = json.loads(r.options_json)
        # PATCH_RAFFLE_MANUAL_ALWAYS_ELIGIBLE_V1
        correct = (sum(1 for v in votes
                       if v.chosen_option in (r.correct_option, _MANUAL_OPTION))
                   if r.correct_option else
                   sum(1 for v in votes if v.chosen_option == _MANUAL_OPTION))
        wrong = total - correct if r.correct_option else 0
        # === PATCH_RAFFLE_FREE_NUMBER_MODE_V1 ===
        # در حالتِ «عددِ آزاد» options خالی است؛ بدونِ این محافظ، بازکردنِ
        # جزئیاتِ چنین دوره‌ای با IndexError می‌ترکید.
        if not r.correct_option:
            correct_txt = "—"
        elif 1 <= r.correct_option <= len(options):
            correct_txt = f"{r.correct_option}. {options[r.correct_option-1]}"
        else:
            correct_txt = f"عدد {r.correct_option}"
        text = (
            f"📋 <b>{r.topic}</b>\n\n"
            f"🗓 تاریخ/ساعت: {_jalali(r.poll_started_at)}\n"
            f"⏳ مدت: {r.duration_hours:g} ساعت\n"
            f"وضعیت: {status_txt}\n"
            f"✅ گزینه‌ی درست: {correct_txt}\n\n"
            f"👥 کل رأی‌دهنده: <b>{total}</b>\n"
            f"✅ پاسخِ درست: <b>{correct}</b>\n"
            f"❌ پاسخِ اشتباه: <b>{wrong}</b>"
        )
    kb_rows = []
    # === PATCH_RAFFLE_SET_ANSWER_FROM_PANEL_V1 ===
    # تا امروز «جوابِ درست» فقط از داخلِ گروه و با ریپلایِ «قرعه‌کشی رو
    # شروع کن» قابلِ تعیین بود. اگر آن مرحله از دست می‌رفت (یا ادمین
    # می‌خواست از همین‌جا کار را تمام کند)، دوره در وضعیتِ «باز» گیر
    # می‌کرد و هیچ راهی برایِ قرعه‌کشی نبود.
    if r.status in ("open", "awaiting_answer"):
        if r.correct_option:
            # جواب از قبل ثبت شده — مستقیم قرعه‌کشی کن
            kb_rows.append([InlineKeyboardButton(
                text="🎲 شروعِ قرعه‌کشی",
                callback_data=f"raffle:drawopen:{raffle_id}",
                style=ButtonStyle.SUCCESS)])
        kb_rows.append([InlineKeyboardButton(
            text=("✏️ تغییرِ جوابِ درست" if r.correct_option
                  else "✅ ثبتِ جوابِ درست و بستنِ دوره"),
            callback_data=f"raffle:setanswer:{raffle_id}",
            style=None if r.correct_option else ButtonStyle.SUCCESS)])
    # === PATCH_RAFFLE_MANUAL_ADD_BY_USERNAME_V1 ===
    # PATCH_RAFFLE_COLLECT_STATUS_ONLY_V1 — افزودن با یوزرنیم دلیلی ندارد
    # فقط برایِ حالتِ منشن باشد؛ در همهٔ حالت‌ها به کار می‌آید.
    kb_rows.append([InlineKeyboardButton(
        text="➕ افزودنِ شرکت‌کننده با یوزرنیم",
        callback_data=f"raffle:manualadd:{raffle_id}")])
    if r.status == "closed":
        kb_rows.append([InlineKeyboardButton(text="🎲 اجرای قرعه‌کشی (۵ نفر رندوم)",
                                              callback_data=f"raffle:draw:{raffle_id}",
                                              style=ButtonStyle.SUCCESS)])
    elif r.status == "drawn":
        kb_rows.append([InlineKeyboardButton(text="🏆 مشاهده‌ی برنده‌ها",
                                              callback_data=f"raffle:winnerdetail:{raffle_id}")])
    kb_rows.append([InlineKeyboardButton(text="🗑 حذفِ این دور", callback_data=f"raffle:delask:{raffle_id}",
                                         style=ButtonStyle.DANGER)])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="raffle:results",
                                         style=ButtonStyle.DANGER)])
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await cb.answer()


# === PATCH_RAFFLE_MANUAL_ADD_BY_USERNAME_V1 ===
# === PATCH_RAFFLE_SET_ANSWER_FROM_PANEL_V1 ===
@raffle_router.callback_query(F.data.startswith("raffle:drawopen:"))
async def raffle_draw_from_open(cb: CallbackQuery, bot: Bot):
    """بستنِ دورهٔ باز و قرعه‌کشیِ فوری — وقتی جوابِ درست از قبل ثبت شده.

    draw_winners فقط رویِ دورهٔ "closed" کار می‌کند، پس اول می‌بندیمش.
    """
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    raffle_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(ChannelRaffle, raffle_id)
        if not r:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if not r.correct_option:
            await cb.answer("اول جوابِ درست را ثبت کن.", show_alert=True)
            return
        if r.status in ("open", "awaiting_answer"):
            r.status = "closed"
            await s.commit()
    await cb.answer("در حال قرعه‌کشی…")
    text, err = await draw_winners(raffle_id, bot)
    await cb.message.answer(err or text)


# === PATCH_RAFFLE_SET_ANSWER_FROM_PANEL_V1 ===
@raffle_router.callback_query(F.data.startswith("raffle:setanswer:"))
async def raffle_set_answer_start(cb: CallbackQuery, state: FSMContext):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    raffle_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(ChannelRaffle, raffle_id)
        if not r or r.status not in ("open", "awaiting_answer"):
            await cb.answer("این دوره دیگر باز نیست.", show_alert=True)
            return
        n = await s.scalar(select(func.count()).select_from(ChannelRaffleVote)
                           .where(ChannelRaffleVote.raffle_id == raffle_id))
        opts = json.loads(r.options_json or "[]")
        # از این لحظه رأیِ جدید قبول نیست
        r.status = "awaiting_answer"
        await s.commit()
    await state.set_state(RaffleFSM.correct_number)
    await state.update_data(raffle_id=raffle_id)
    hint = ""
    if opts:
        hint = "\n\n" + "\n".join(f"  {i+1}. {o}" for i, o in enumerate(opts))
    await cb.message.answer(
        f"🔒 رأی‌گیریِ دورهٔ #{raffle_id} بسته شد ({n} شرکت‌کننده).\n\n"
        f"🤔 <b>عددِ درست چند بود؟</b>{hint}\n\n"
        "عددش را همین‌جا بفرست (مثلاً <code>۱</code>).\n"
        "<i>برایِ انصراف بنویس «لغو».</i>", parse_mode="HTML")
    await cb.answer()


@raffle_router.callback_query(F.data.startswith("raffle:manualadd:"))
async def raffle_manual_add_start(cb: CallbackQuery, state: FSMContext):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    raffle_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(ChannelRaffle, raffle_id)
        if not r:
            await cb.answer("پیدا نشد", show_alert=True)
            return
    await state.set_state(RaffleFSM.manual_add_username)
    await state.update_data(raffle_id=raffle_id)
    from platform_bot import smart_cancel_kb
    await cb.message.answer(
        "🔍 یوزرنیمِ کاربر رو بفرست (با یا بدونِ @):\n\n"
        "⚠️ فقط برایِ کاربرهایی جواب می‌ده که قبلاً حداقل یه‌بار بات‌ساز رو "
        "استارت کرده باشن.",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


# === PATCH_RAFFLE_MANUAL_ADD_FIX_V1 ===
@raffle_router.message(RaffleFSM.manual_add_username, F.text)
async def raffle_manual_add_recv(msg: Message, state: FSMContext, bot: Bot):
    if not await _is_raffle_admin(msg.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    raffle_id = data.get("raffle_id")
    await state.clear()
    from platform_bot import smart_home_kb
    uname = (msg.text or "").strip().lstrip("@")
    if not uname:
        await msg.answer("یوزرنیمِ نامعتبر.", reply_markup=await smart_home_kb(msg.from_user.id))
        return

    target_id = None
    target_name = None

    # روش ۱: تلاش برایِ resolve مستقیم از طریقِ خودِ API تلگرام — فقط
    # برایِ کاربرهایی جواب می‌ده که بات قبلاً یه‌جوری دیده‌تشون.
    try:
        chat = await bot.get_chat(f"@{uname}")
        target_id = chat.id
        target_name = getattr(chat, "full_name", None) or uname
    except Exception:
        logger.exception("یافتنِ چتِ هدفِ قرعه‌کشی ناموفق بود")

    # روش ۲ (فال‌بک): بینِ همه‌یِ رأی‌هایِ قبلاً ثبت‌شده (هر دوری) دنبالِ
    # همین یوزرنیم بگرد — شاید قبلاً یه‌جایِ دیگه شرکت کرده بوده.
    if target_id is None:
        async with Session() as s:
            old_vote = await s.scalar(select(ChannelRaffleVote).where(
                func.lower(ChannelRaffleVote.username) == uname.lower()
            ).order_by(ChannelRaffleVote.id.desc()))
        if old_vote:
            target_id = old_vote.telegram_id
            target_name = old_vote.full_name or uname

    if target_id is None:
        await msg.answer(
            f"❌ نتونستم @{uname} رو پیدا کنم.\n\n"
            "این معمولاً یعنی بات تا حالا این کاربر رو ندیده (نه توی PV، نه "
            "توی گروهِ قرعه‌کشی). راه‌حل: از خودِ فرد بخواه یه پیامِ ساده توی "
            "گروهِ قرعه‌کشی بفرسته، یا آیدیِ عددیشو با یه بات مثلِ "
            "@userinfobot بگیر و به من بده تا دستی اضافه‌ش کنم.",
            reply_markup=await smart_home_kb(msg.from_user.id))
        return

    async with Session() as s:
        r = await s.get(ChannelRaffle, raffle_id)
        if not r:
            await msg.answer("این دورِ قرعه‌کشی دیگه پیدا نشد.",
                             reply_markup=await smart_home_kb(msg.from_user.id))
            return
        existing = await s.scalar(select(ChannelRaffleVote).where(
            ChannelRaffleVote.raffle_id == raffle_id,
            ChannelRaffleVote.telegram_id == target_id))
        if existing:
            await msg.answer(f"ℹ️ @{uname} از قبل توی این دور ثبت بود.",
                             reply_markup=await smart_home_kb(msg.from_user.id))
            return
        # PATCH_RAFFLE_SET_ANSWER_FROM_PANEL_V1 — با جوابِ درستِ همان دوره
        # هم‌خوان باشد، وگرنه موقعِ قرعه‌کشی از فیلتر بیرون می‌افتد.
        _r = await s.get(ChannelRaffle, raffle_id)
        s.add(ChannelRaffleVote(
            raffle_id=raffle_id, telegram_id=target_id,
            full_name=target_name, username=uname,
            chosen_option=_MANUAL_OPTION,   # PATCH_RAFFLE_MANUAL_ALWAYS_ELIGIBLE_V1
        ))
        await s.commit()
        total = await s.scalar(select(func.count()).select_from(ChannelRaffleVote).where(
            ChannelRaffleVote.raffle_id == raffle_id))
    await msg.answer(
        f"✅ @{uname} به‌صورتِ دستی به این دور اضافه شد.\n"
        f"👥 تعدادِ کلِ شرکت‌کننده‌هایِ الان: {total}",
        reply_markup=await smart_home_kb(msg.from_user.id))


@raffle_router.callback_query(F.data.startswith("raffle:delask:"))
async def raffle_delete_ask(cb: CallbackQuery):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    raffle_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(ChannelRaffle, raffle_id)
        if not r:
            await cb.answer("پیدا نشد", show_alert=True)
            return
    await cb.message.edit_text(
        f"⚠️ مطمئنی می‌خوای این دورِ قرعه‌کشی («{r.topic[:60]}») رو کامل حذف کنی؟\n\n"
        "همه‌ی رأی‌ها و برنده‌های ثبت‌شده‌ش هم پاک می‌شن. این کار قابلِ بازگشت نیست "
        "(ولی اگه قبلاً جایزه‌ای واریز کردی، همون واریز دست‌نخورده می‌مونه، چون از کیفِ‌پول جدا حساب می‌شه).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"raffle:delyes:{raffle_id}",
                                 style=ButtonStyle.DANGER),
            InlineKeyboardButton(text="❌ نه", callback_data=f"raffle:resultdetail:{raffle_id}"),
        ]]))
    await cb.answer()


@raffle_router.callback_query(F.data.startswith("raffle:delyes:"))
async def raffle_delete_yes(cb: CallbackQuery):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    raffle_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(ChannelRaffle, raffle_id)
        if not r:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        votes = (await s.scalars(select(ChannelRaffleVote).where(
            ChannelRaffleVote.raffle_id == raffle_id))).all()
        for v in votes:
            await s.delete(v)
        winners = (await s.scalars(select(ChannelRaffleWinner).where(
            ChannelRaffleWinner.raffle_id == raffle_id))).all()
        for w in winners:
            await s.delete(w)
        await s.delete(r)
        await s.commit()
    await cb.answer("🗑 حذف شد")
    kb_rows = [[InlineKeyboardButton(text="⬅️ بازگشت به نتایج", callback_data="raffle:results",
                                     style=ButtonStyle.DANGER)]]
    await cb.message.edit_text("🗑 این دورِ قرعه‌کشی کامل حذف شد.",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


# ═══════════════════════ نفرات برنده ═══════════════════════

@raffle_router.callback_query(F.data == "raffle:winnerslist")
async def raffle_winners_list(cb: CallbackQuery):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    async with Session() as s:
        rows = (await s.scalars(
            select(ChannelRaffle).where(ChannelRaffle.status == "drawn")
            .order_by(ChannelRaffle.created_at.desc()).limit(30)
        )).all()
    if not rows:
        await cb.message.edit_text(
            "🏆 هنوز هیچ دوره‌ای قرعه‌کشی نشده.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت", callback_data="raffle:menu",
                                     style=ButtonStyle.DANGER)]]))
        await cb.answer()
        return
    kb_rows = []
    async with Session() as s:
        for r in rows:
            wcount = await s.scalar(select(func.count()).select_from(ChannelRaffleWinner).where(
                ChannelRaffleWinner.raffle_id == r.id))
            # === PATCH_RAFFLE_WON_AT_V1 ===
            # نگاه کن به توضیحِ کاملِ همین پچ توی raffle_winner_detail —
            # همون مشکل اینجا هم بود (تاریخِ نمایش‌داده‌شده در لیست هم
            # poll_started_at بود، نه تاریخِ واقعیِ قرعه‌کشی).
            draw_dt = r.poll_started_at
            if wcount:
                first_won_at = await s.scalar(select(ChannelRaffleWinner.won_at).where(
                    ChannelRaffleWinner.raffle_id == r.id).limit(1))
                if first_won_at:
                    draw_dt = first_won_at
            label = (f"🏆 {_jalali(draw_dt)} — {r.topic[:30]}" if wcount
                    else f"😕 (بدون برنده) {_jalali(draw_dt)} — {r.topic[:30]}")
            kb_rows.append([InlineKeyboardButton(text=label, callback_data=f"raffle:winnerdetail:{r.id}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="raffle:menu",
                                         style=ButtonStyle.DANGER)])
    await cb.message.edit_text("🏆 <b>نفرات برنده</b>\n\nیه دوره رو انتخاب کن:",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await cb.answer()


@raffle_router.callback_query(F.data.startswith("raffle:winnerdetail:"))
async def raffle_winner_detail(cb: CallbackQuery):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    raffle_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(ChannelRaffle, raffle_id)
        if not r:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        winners = (await s.scalars(select(ChannelRaffleWinner).where(
            ChannelRaffleWinner.raffle_id == raffle_id).order_by(ChannelRaffleWinner.rank))).all()
    # === PATCH_RAFFLE_WON_AT_V1 ===
    # قبلاً اینجا همیشه r.poll_started_at (زمانِ ارسالِ خودِ نظرسنجی) نشون
    # داده می‌شد که با تاریخِ واقعیِ خودِ قرعه‌کشی فرق داشت (مخصوصاً اگه
    # قرعه‌کشی چند روز بعدِ شروعِ نظرسنجی انجام شده باشه) — برای همین کاربر
    # می‌گفت «تاریخ به‌روز نیست». الان اگه برنده‌ای ثبت شده، تاریخِ واقعیِ
    # قرعه‌کشی (won_at) رو نشون می‌ده؛ فقط برای دورهای بدونِ برنده هنوز
    # poll_started_at نشون داده می‌شه (چون won_at اصلاً وجود نداره).
    draw_date = winners[0].won_at if (winners and winners[0].won_at) else r.poll_started_at
    kb_rows = []
    for i, w in enumerate(winners, start=1):
        name = f"@{w.username}" if w.username else (w.full_name or f"کاربر {w.telegram_id}")
        prize_txt = f" — 🎁 {int(w.prize_amount):,} تومان" if w.prize_amount else ""
        kb_rows.append([InlineKeyboardButton(
            text=f"{_medal_for_rank(i)} {name}{prize_txt}",
            callback_data=f"raffle:winnerprofile:{w.id}")])
    if winners:
        # === PATCH_RAFFLE_ZERO_WINNERS_V1 ===
        # متنِ دکمه بسته به تعدادِ واقعیِ برنده‌هاست، نه همیشه «۵ نفر» —
        # چون ممکنه یه دور کمتر از ۵ نفر پاسخِ درست داشته باشه.
        give_all_txt = (f"🎁 اهدای جایزه به هر {len(winners)} نفر" if len(winners) > 1
                        else "🎁 اهدای جایزه به همین نفر")
        kb_rows.append([InlineKeyboardButton(text=give_all_txt,
                                             callback_data=f"raffle:giveall:{raffle_id}",
                                             style=ButtonStyle.SUCCESS)])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="raffle:winnerslist",
                                         style=ButtonStyle.DANGER)])
    if winners:
        body = "روی هرکسی بزنی، مشخصاتش (کیف پول، ربات‌ها و...) رو نشونت می‌دم."
    else:
        # قبلاً اینجا فقط یه لیستِ خالی + دکمه‌ی «اهدا به هر ۵ نفر» نشون داده
        # می‌شد که اگه می‌زدی، بدونِ هیچ خطایی «برای ۰ نفر واریز شد» می‌گفت —
        # گیج‌کننده بود. الان صریح می‌گه کسی برنده نشده.
        body = "😕 <b>کسی در این دوره برنده نشد</b> (هیچ‌کس پاسخِ درست نداده بود)."
    text = f"🏆 <b>برندگانِ قرعه‌کشی</b>\n📋 {r.topic}\n🗓 {_jalali(draw_date)}\n\n{body}"
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await cb.answer()


@raffle_router.callback_query(F.data.startswith("raffle:winnerprofile:"))
async def raffle_winner_profile(cb: CallbackQuery):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    winner_id = int(cb.data.split(":")[2])
    async with Session() as s:
        w = await s.get(ChannelRaffleWinner, winner_id)
        if not w:
            await cb.answer("پیدا نشد", show_alert=True)
            return
    from platform_bot import _build_user_profile
    built = await _build_user_profile(w.telegram_id)
    prize_line = (f"\n\n🎁 جایزه‌ی این دوره: <b>{int(w.prize_amount):,} تومان</b> "
                 f"(اهدا در {_jalali(w.prize_given_at)})") if w.prize_amount else \
                "\n\n🎁 هنوز جایزه‌ای برای این دوره اهدا نشده."
    if built:
        text = built[0] + prize_line
    else:
        name = f"@{w.username}" if w.username else (w.full_name or f"کاربر {w.telegram_id}")
        text = f"👤 {name}\n<code>{w.telegram_id}</code>\n\n(هنوز با بات‌ساز شروع نکرده)" + prize_line
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 اهدای جایزه به همین نفر", callback_data=f"raffle:giveone:{winner_id}",
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"raffle:winnerdetail:{w.raffle_id}",
                              style=ButtonStyle.DANGER)],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


# ═══════════════════════ اهدای جایزه ═══════════════════════

@raffle_router.callback_query(F.data.startswith("raffle:giveone:"))
async def raffle_give_one_start(cb: CallbackQuery, state: FSMContext):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    winner_id = int(cb.data.split(":")[2])
    await state.set_state(RaffleFSM.prize_amount)
    await state.update_data(target="one", winner_id=winner_id)
    from platform_bot import smart_cancel_kb
    await cb.message.answer("💰 مبلغِ جایزه رو به تومان بفرست:", reply_markup=await smart_cancel_kb())
    await cb.answer()


@raffle_router.callback_query(F.data.startswith("raffle:giveall:"))
async def raffle_give_all_start(cb: CallbackQuery, state: FSMContext):
    if not await _is_raffle_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    raffle_id = int(cb.data.split(":")[2])
    await state.set_state(RaffleFSM.prize_amount)
    await state.update_data(target="all", raffle_id=raffle_id)
    from platform_bot import smart_cancel_kb
    # === PATCH_RAFFLE_HARDCODED_5_V1 ===
    # قبلاً این متن همیشه «هر ۵ نفر» می‌گفت، مهم نیست تعدادِ واقعیِ برنده‌ها
    # چقدر بود (مثلاً اگه تنظیماتِ تعدادِ برنده رو ۳ گذاشته باشی، یا یه دور
    # کمتر از حدِ تنظیم‌شده برندهٔ واقعی داشته باشه). الان از تعدادِ واقعیِ
    # برنده‌های همین دور می‌خونه — دقیقاً همون عددی که تو دکمه‌ی قبلی
    # («🎁 اهدای جایزه به هر N نفر») هم دیده بودی.
    async with Session() as s:
        n = await s.scalar(select(func.count()).select_from(ChannelRaffleWinner).where(
            ChannelRaffleWinner.raffle_id == raffle_id))
    n = n or await _get_winners_count()
    await cb.message.answer(
        f"💰 مبلغی که می‌خوای به <b>هر {n} نفر</b> جداگانه اهدا بشه رو به تومان بفرست "
        "(همین مبلغ برای هرکدوم جدا واریز می‌شه):",
        reply_markup=await smart_cancel_kb())
    await cb.answer()


@raffle_router.message(RaffleFSM.prize_amount, F.text)
async def raffle_give_amount_recv(msg: Message, state: FSMContext, bot: Bot):
    if not await _is_raffle_admin(msg.from_user.id):
        await state.clear()
        return
    # PATCH_RAFFLE_FSM_ESCAPE_V1 — راهِ خروج، تا ادمین در این مرحله گیر نکند
    if (msg.text or "").strip() in {"لغو", "❌ لغو", "انصراف", "بازگشت",
                                    "⬅️ بازگشت", "/cancel"}:
        await state.clear()
        await msg.answer("لغو شد.")
        return
    amount = _fa_int(msg.text)
    if not amount or amount <= 0:
        await msg.answer("مبلغ نامعتبر. یه عددِ مثبت بفرست:")
        return
    data = await state.get_data()
    await state.clear()
    from platform_bot import smart_home_kb
    already = 0
    if data.get("target") == "one":
        winner_ids = [data["winner_id"]]
    else:
        async with Session() as s:
            ws = (await s.scalars(select(ChannelRaffleWinner).where(
                ChannelRaffleWinner.raffle_id == data["raffle_id"]))).all()
        # === PATCH_RAFFLE_DOUBLE_PAY_GUARD_V1 ===
        # کسانی که جایزه‌شان قبلاً واریز شده از فهرست بیرون می‌روند تا
        # زدنِ دوبارهٔ «اهدا به همه» پرداختِ تکراری نسازد.
        already = sum(1 for w in ws if w.prize_given_at is not None)
        winner_ids = [w.id for w in ws if w.prize_given_at is None]
    if not winner_ids:
        _extra = f"\n({already} نفر قبلاً جایزه گرفته‌اند.)" if already else ""
        await msg.answer("😕 برنده‌ای بدونِ جایزه در این دوره نیست." + _extra,
                         reply_markup=await smart_home_kb(msg.from_user.id))
        return
    done, failed = [], []
    for wid in winner_ids:
        ok = await _give_prize(bot, wid, amount)
        (done if ok else failed).append(wid)
    summary = f"✅ جایزه‌ی {int(amount):,} تومانی برای {len(done)} نفر واریز شد."
    if failed:
        summary += f"\n⚠️ برای {len(failed)} نفر ناموفق بود (لاگ رو چک کن)."
    await msg.answer(summary, reply_markup=await smart_home_kb(msg.from_user.id))


async def _give_prize(bot: Bot, winner_row_id: int, amount: int) -> bool:
    """جایزه رو به کیف‌پولِ پلتفرمیِ برنده اضافه می‌کنه (get-or-create) و
    خبرش می‌ده. True یعنی موفق بود.

    === PATCH_RAFFLE_DOUBLE_PAY_GUARD_V1 ===
    قبلاً هیچ محافظی برایِ پرداختِ تکراری نبود: دکمهٔ «اهدا به همه» اگر دو
    بار زده می‌شد (یا ادمین بعد از «اهدا به یک نفر» دوباره «به همه» را
    می‌زد)، همان مبلغ دوباره به کیفِ همه اضافه می‌شد. چون این‌جا پولِ
    واقعی جابه‌جا می‌شود، حالا برنده‌ای که `prize_given_at` دارد رد
    می‌شود.

    ضمناً تغییرِ موجودی در `platform_txns` هم ثبت می‌شود؛ قبلاً موجودی
    بی‌هیچ ردّی بالا می‌رفت و در گزارش‌هایِ مالی قابلِ پیگیری نبود.
    """
    try:
        async with Session() as s:
            # FOR UPDATE: دو فراخوانیِ هم‌زمان جایزه را دوبار نپردازند
            w = await s.get(ChannelRaffleWinner, winner_row_id, with_for_update=True)
            if not w:
                return False
            if w.prize_given_at is not None:
                logger.info("قرعه‌کشی: جایزهٔ برندهٔ #%s قبلاً (%s) پرداخت شده؛ "
                            "دوباره پرداخت نشد.", winner_row_id, w.prize_given_at)
                return False
            pu = await s.scalar(select(PlatformUser).where(
                PlatformUser.telegram_id == w.telegram_id).with_for_update())
            if not pu:
                pu = PlatformUser(telegram_id=w.telegram_id, full_name=w.full_name)
                s.add(pu)
                await s.flush()
            pu.balance = float(pu.balance or 0) + amount
            w.prize_amount = amount
            w.prize_given_at = _raffle_now()
            from models import PlatformTxn
            s.add(PlatformTxn(user_telegram_id=w.telegram_id, amount=amount,
                              kind="raffle_prize",
                              note=f"جایزهٔ قرعه‌کشی #{w.raffle_id}"))
            new_balance = pu.balance
            tg_id = w.telegram_id
            await s.commit()
        try:
            from tenant.gateways.wallet_receipt import build_wallet_charge_message
            text, kb = build_wallet_charge_message(amount, new_balance, "admin")
            text = "🎉 <b>تبریک! جایزه‌ی قرعه‌کشی واریز شد</b>\n\n" + text
            await bot.send_message(tg_id, text, reply_markup=kb)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return True
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return False
