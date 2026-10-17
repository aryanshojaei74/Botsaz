"""
community.py — «مدیریتِ گروه و کانال» (PATCH_COMMUNITY_MGMT_V1)

نوعِ جدیدِ رباتِ فروشی، کنارِ فروشِ VPN، فروشگاهِ دیجیتال و ربات تبلیغاتی.
مالکِ این ربات آن را به‌عنوانِ ادمین به گروه/کانالِ خودش اضافه می‌کند و از
همان پنلِ مدیریتِ رباتش (پی‌وی) تنظیمش می‌کند. هیچ «مشتری» ای در کار نیست؛
این ابزار مستقیم برایِ خودِ مالک است.

قابلیت‌ها:
  ۱) ضداسپم — حذفِ لینک/فوروارد/پیامِ تکراری از کاربرانِ عادی
  ۲) خوشامدگویی — پیامِ خوش‌آمد به عضوِ تازه، با جای‌گذاریِ نام/گروه
  ۳) فیلترِ کلمات — حذفِ پیامِ حاویِ کلماتِ ممنوعه
  ۴) زمان‌بندیِ پست — از ماژولِ ad_broadcast (که قبلاً برایِ رباتِ تبلیغاتی
     ساخته شده) عیناً استفاده می‌شود؛ اینجا کدِ تازه‌ای ندارد.
  ۵) نظرسنجی — از سیستمِ Poll که در tenant/extras.py هست استفاده می‌شود.
  ۶) مدیریتِ اعضا — بن/اخطار/سکوت با ریپلای در خودِ گروه، دقیقاً همان الگویِ
     محافظِ گروهِ پلتفرم (platform_group_guard.py)، اما اینجا برایِ هر گروهِ
     متصل جدا و بدونِ وابستگی به آن فایل (پروسهٔ رباتِ مستأجر جداست).

فقط برایِ ربات‌هایی که field == "community_mgmt" باشد فعال می‌شود؛ رویِ
بقیهٔ انواعِ ربات (VPN، فروشگاه، …) هیچ اثری ندارد.
"""
from __future__ import annotations

import asyncio
import html
import time
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle, ChatType
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from config import Session
from models import Bot as BotRow, CommunityChat, CommunityFilterWord, CommunityWarn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

community_router = Router()


# === PATCH_TENANT_SCOPE_V1 ===
# همهٔ دکمه‌هایِ مدیریتِ گروه «cmty:…:<chat_row_id>…» هستند و گروه را فقط با id
# می‌خواندند؛ ادمینِ یک ربات با callbackِ دست‌ساز تنظیماتِ گروهِ رباتِ دیگری
# (ضداسپم، فیلتر، خوش‌آمد، قطعِ اتصال، …) را عوض می‌کرد. اولین عددِ داخلِ
# callback همیشه idِ گروه است؛ این‌جا یک‌جا چک می‌شود که مالِ همین ربات باشد.
async def _cmty_scope_mw(handler, event, data):
    cbd = getattr(event, "data", None) or ""
    if not cbd.startswith("cmty:"):
        return await handler(event, data)
    cid = next((int(x) for x in cbd.split(":")[1:] if x.isdigit()), None)
    if cid is None:
        return await handler(event, data)
    from tenant import get_bot_row
    row = await get_bot_row(data.get("bot"))
    async with Session() as s:
        c = await s.get(CommunityChat, cid)
    if not row or not c or c.bot_id != row.id:
        await event.answer("پیدا نشد", show_alert=True)
        return None
    return await handler(event, data)


community_router.callback_query.middleware(_cmty_scope_mw)

FIELD_KEY = "community_mgmt"


# ═══════════════════════════════════════════
#  کشِ سبکِ تنظیماتِ هر چت — تا رویِ هر پیامِ گروه دیتابیس نخوریم
# ═══════════════════════════════════════════

_CACHE_TTL = 20.0
_chat_cache: dict[tuple[int, int], tuple[CommunityChat | None, float]] = {}


def _cache_key(bot_id: int, chat_id: int) -> tuple[int, int]:
    return (bot_id, chat_id)


def invalidate_chat_cache(bot_id: int, chat_id: int) -> None:
    _chat_cache.pop(_cache_key(bot_id, chat_id), None)


async def _get_chat_row(bot_id: int, chat_id: int) -> CommunityChat | None:
    key = _cache_key(bot_id, chat_id)
    hit = _chat_cache.get(key)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    async with Session() as s:
        row = await s.scalar(select(CommunityChat).where(
            CommunityChat.bot_id == bot_id, CommunityChat.chat_id == chat_id,
            CommunityChat.is_active == True))  # noqa: E712
    _chat_cache[key] = (row, time.monotonic() + _CACHE_TTL)
    return row


async def _get_bot_row(bot: Bot) -> BotRow | None:
    from tenant import get_bot_row
    return await get_bot_row(bot)


# ═══════════════════════════════════════════
#  چک‌هایِ کمکی
# ═══════════════════════════════════════════

def _is_bot_service_message(msg: Message) -> bool:
    """پیامِ به‌نمایندگیِ کانالِ لینک‌شده (ادمینِ ناشناس) را با پیامِ بات‌هایِ
    دیگر اشتباه نگیر — همان باگی که در محافظِ گروهِ پلتفرم پیدا شد."""
    if getattr(msg, "sender_chat", None):
        return False
    return not msg.from_user or msg.from_user.is_bot


_admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}
_ADMIN_CACHE_TTL = 60.0


async def _is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    key = (chat_id, user_id)
    hit = _admin_cache.get(key)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    # === PATCH_COMMUNITY_ADMIN_CHECK_TIMEOUT_V1 ===
    # همان الگویِ محافظِ عضویتِ اجباری (tenant/__init__.py): بدونِ سقفِ
    # زمانیِ خودمان، یک تایم‌اوتِ گذرا در تلگرام باعث می‌شد این تابع «ادمین
    # نیست» نتیجه بگیرد، و آن نتیجهٔ اشتباه ۶۰ ثانیه (_ADMIN_CACHE_TTL) کش
    # می‌شد — یعنی یک ادمینِ واقعی تا یک دقیقه نمی‌توانست دستور بدهد، فقط
    # به‌خاطرِ یک لختیِ گذرایِ شبکه.
    try:
        member = await asyncio.wait_for(
            bot.get_chat_member(chat_id, user_id), timeout=5.0)
        ok = member.status in ("administrator", "creator")
    except Exception:
        ok = False
    _admin_cache[key] = (ok, time.monotonic() + _ADMIN_CACHE_TTL)
    return ok


async def _is_authorized_sender(msg: Message, bot: Bot) -> bool:
    """فرستنده ادمین است؟ — یا شخصِ واقعی، یا ادمینی که از طریقِ هویتِ
    کانالِ لینک‌شده پیام گذاشته (sender_chat)."""
    if msg.from_user and await _is_chat_admin(bot, msg.chat.id, msg.from_user.id):
        return True
    sc = getattr(msg, "sender_chat", None)
    if sc and sc.id == msg.chat.id:
        return True  # ادمینِ ناشناس، پیام از طرفِ خودِ گروه/کانال
    return False


async def _delete_silently(bot: Bot, chat_id: int, message_id: int) -> bool:
    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except Exception:
        return False


async def _bump_warn(chat_row_id: int, user_id: int) -> int:
    async with Session() as s:
        w = await s.scalar(select(CommunityWarn).where(
            CommunityWarn.chat_row_id == chat_row_id, CommunityWarn.user_id == user_id))
        if not w:
            w = CommunityWarn(chat_row_id=chat_row_id, user_id=user_id, count=0)
            s.add(w)
        w.count = (w.count or 0) + 1
        await s.commit()
        return w.count


async def _apply_action(bot: Bot, chat: CommunityChat, msg: Message, action: str) -> None:
    """action: delete / warn / mute — بعد از تشخیصِ تخلف اجرا می‌شود."""
    if not msg.from_user:
        return
    await _delete_silently(bot, msg.chat.id, msg.message_id)
    if action == "delete":
        return
    n = await _bump_warn(chat.id, msg.from_user.id)
    await _bump_stat(chat.id, "stat_warns_issued")  # PATCH_COMMUNITY_STATS_V1
    if action == "warn":
        try:
            note = await bot.send_message(
                msg.chat.id,
                f"⚠️ {html.escape(msg.from_user.full_name)} اخطار گرفت ({n}/3).")
            if n >= 3:
                await bot.send_message(msg.chat.id,
                                       f"🔇 {html.escape(msg.from_user.full_name)} به‌خاطرِ "
                                       "سه اخطار، ۱ ساعت سکوت شد.")
                try:
                    from aiogram.types import ChatPermissions
                    await bot.restrict_chat_member(
                        msg.chat.id, msg.from_user.id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=datetime.now() + timedelta(hours=1))
                except Exception:
                    logger.exception("سکوتِ خودکار بعدِ ۳ اخطار ناموفق بود")
        except Exception:
            logger.exception("ارسالِ پیامِ اخطار ناموفق بود")
    elif action == "mute":
        try:
            from aiogram.types import ChatPermissions
            await bot.restrict_chat_member(
                msg.chat.id, msg.from_user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now() + timedelta(hours=1))
            await bot.send_message(
                msg.chat.id,
                f"🔇 {html.escape(msg.from_user.full_name)} به‌مدتِ ۱ ساعت سکوت شد.")
        except Exception:
            logger.exception("سکوتِ خودکار ناموفق بود (شاید ربات ادمین نیست)")


# ═══════════════════════════════════════════
#  ضداسپم
# ═══════════════════════════════════════════

_HAS_LINK_ENTITIES = {"url", "text_link", "mention", "text_mention"}
_dup_cache: dict[tuple[int, int], tuple[str, float]] = {}
_DUP_WINDOW_SEC = 45.0


def _has_link(msg: Message) -> bool:
    entities = (msg.entities or []) + (msg.caption_entities or [])
    if any(e.type in _HAS_LINK_ENTITIES for e in entities):
        return True
    text = (msg.text or msg.caption or "")
    return "t.me/" in text or "://" in text


def _is_forward(msg: Message) -> bool:
    return bool(getattr(msg, "forward_origin", None) or getattr(msg, "forward_date", None))


def _is_duplicate(chat_id: int, user_id: int, text: str) -> bool:
    if not text:
        return False
    key = (chat_id, user_id)
    hit = _dup_cache.get(key)
    now = time.monotonic()
    _dup_cache[key] = (text, now + _DUP_WINDOW_SEC)
    return bool(hit and hit[1] > now and hit[0] == text)


# === PATCH_COMMUNITY_STATS_V1 ===
async def _bump_stat(chat_row_id: int, field: str, by: int = 1) -> None:
    async with Session() as s:
        c = await s.get(CommunityChat, chat_row_id)
        if not c:
            return
        setattr(c, field, (getattr(c, field) or 0) + by)
        await s.commit()


async def _antispam_check(msg: Message, bot: Bot, chat: CommunityChat) -> bool:
    """True یعنی پیام حذف/رسیدگی شد و پردازشِ بیشتر لازم نیست."""
    if not chat.antispam_on:
        return False
    if await _is_authorized_sender(msg, bot):
        return False
    text = (msg.text or msg.caption or "")
    triggered = False
    if chat.antispam_links and _has_link(msg):
        triggered = True
    elif chat.antispam_forwards and _is_forward(msg):
        triggered = True
    elif chat.antispam_duplicate and msg.from_user and _is_duplicate(
            msg.chat.id, msg.from_user.id, text):
        triggered = True
    if not triggered:
        return False
    await _apply_action(bot, chat, msg, chat.antispam_action)
    await _bump_stat(chat.id, "stat_antispam_deleted")  # PATCH_COMMUNITY_STATS_V1
    return True


# ═══════════════════════════════════════════
#  فیلترِ کلمات
# ═══════════════════════════════════════════

_words_cache: dict[int, tuple[list[str], float]] = {}


async def _get_filter_words(chat_row_id: int) -> list[str]:
    hit = _words_cache.get(chat_row_id)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    async with Session() as s:
        rows = (await s.scalars(select(CommunityFilterWord).where(
            CommunityFilterWord.chat_row_id == chat_row_id))).all()
    words = [r.word.lower() for r in rows]
    _words_cache[chat_row_id] = (words, time.monotonic() + _CACHE_TTL)
    return words


def invalidate_words_cache(chat_row_id: int) -> None:
    _words_cache.pop(chat_row_id, None)


async def _filter_check(msg: Message, bot: Bot, chat: CommunityChat) -> bool:
    if not chat.filter_on:
        return False
    if await _is_authorized_sender(msg, bot):
        return False
    text = (msg.text or msg.caption or "").lower()
    if not text:
        return False
    words = await _get_filter_words(chat.id)
    if not any(w in text for w in words if w):
        return False
    await _apply_action(bot, chat, msg, chat.filter_action)
    await _bump_stat(chat.id, "stat_filter_deleted")  # PATCH_COMMUNITY_STATS_V1
    return True


# ═══════════════════════════════════════════
#  خوشامدگویی
# ═══════════════════════════════════════════

def _render_welcome(template: str, member_name: str, group_title: str) -> str:
    return (template
            .replace("{نام}", member_name).replace("{name}", member_name)
            .replace("{گروه}", group_title).replace("{group}", group_title))


async def _welcome_new_members(msg: Message, bot: Bot, chat: CommunityChat) -> None:
    if not chat.welcome_on or not msg.new_chat_members:
        return
    if chat.welcome_delete_service_msg:
        await _delete_silently(bot, msg.chat.id, msg.message_id)
    template = chat.welcome_text or "سلام {نام} عزیز، به {گروه} خوش اومدی! 🌹"
    for member in msg.new_chat_members:
        if member.is_bot and member.id == bot.id:
            continue  # اضافه‌شدنِ خودِ ربات، پیامِ خوش‌آمد نمی‌خواهد
        text = _render_welcome(template, member.full_name, msg.chat.title or "")
        await _bump_stat(chat.id, "stat_welcomed")  # PATCH_COMMUNITY_STATS_V1
        try:
            sent = await bot.send_message(msg.chat.id, text)
            if chat.welcome_delete_after_sec > 0:
                import asyncio
                async def _later(cid=msg.chat.id, mid=sent.message_id,
                                 sec=chat.welcome_delete_after_sec):
                    await asyncio.sleep(sec)
                    await _delete_silently(bot, cid, mid)
                asyncio.create_task(_later())
        except Exception:
            logger.exception("ارسالِ پیامِ خوش‌آمد ناموفق بود")


# ═══════════════════════════════════════════
#  آنبوردینگ — تشخیصِ اضافه‌شدنِ ربات به گروه/کانال
# ═══════════════════════════════════════════

@community_router.my_chat_member()
async def on_membership_change(event: ChatMemberUpdated, bot: Bot):
    row = await _get_bot_row(bot)
    if not row or row.field != FIELD_KEY:
        return  # این قابلیت فقط برایِ ربات‌هایِ «مدیریتِ گروه و کانال» است

    chat = event.chat
    new_status = event.new_chat_member.status
    if chat.type not in ("group", "supergroup", "channel"):
        return

    if new_status in ("left", "kicked"):
        async with Session() as s:
            existing = await s.scalar(select(CommunityChat).where(
                CommunityChat.bot_id == row.id, CommunityChat.chat_id == chat.id))
            if existing:
                existing.is_active = False
                await s.commit()
        invalidate_chat_cache(row.id, chat.id)
        return

    if new_status not in ("administrator", "member"):
        return

    async with Session() as s:
        existing = await s.scalar(select(CommunityChat).where(
            CommunityChat.bot_id == row.id, CommunityChat.chat_id == chat.id))
        is_new = existing is None
        if not existing:
            existing = CommunityChat(
                bot_id=row.id, chat_id=chat.id,
                chat_type=chat.type, title=chat.title, username=chat.username,
                added_by=event.from_user.id if event.from_user else None)
            s.add(existing)
        else:
            existing.is_active = True
            existing.title = chat.title
            existing.username = chat.username
        await s.commit()
    invalidate_chat_cache(row.id, chat.id)

    if not is_new:
        return

    kind = "کانال" if chat.type == "channel" else "گروه"
    admin_note = ""
    if new_status != "administrator":
        admin_note = ("\n\n⚠️ ربات هنوز ادمین نیست — بدونِ دسترسیِ ادمین "
                      "(حذفِ پیام/بن/سکوت)، ضداسپم و فیلترِ کلمات کار نمی‌کنند. "
                      "از تنظیماتِ گروه، ربات را ادمین کن.")
    # === PATCH_COMMUNITY_PRIVACY_MODE_V1 ===
    # همان هشدارِ Privacy Mode را همین‌جا هم می‌گذاریم — اولین پیامی که
    # ادمین بعدِ اتصال می‌بیند، نه فقط داخلِ راهنمایی که ممکن است هیچ‌وقت
    # بازش نکند.
    privacy_note = (
        "\n\n🔒 <b>یک قدمِ حیاتیِ دیگر (خارج از تلگرام‌بات‌ساز):</b> با "
        "@BotFather برو → /mybots → این ربات → Bot Settings → Group Privacy "
        "→ Turn off. بدونِ این، ربات پیام‌هایِ سادهٔ گروه (مثلِ نوشتنِ «سکوت» "
        "بدونِ اسلش) را اصلاً نمی‌بیند و بن/اخطار/سکوت/فیلترِ کلمات کار "
        "نمی‌کنند.")
    text = (f"✅ {kind}ِ «{html.escape(chat.title or '')}» به ربات وصل شد!"
           + admin_note + privacy_note +
           "\n\nبرای تنظیمش: 📦 سرویس‌ها و محصولات → 🛡 مدیریت گروه و کانال")
    async with Session() as s:
        from models import AdminSupport
        admin_ids = {row.owner_id}
        admin_ids |= {a.telegram_id for a in
                     (await s.scalars(select(AdminSupport).where(
                         AdminSupport.bot_id == row.id))).all()}
    for uid in admin_ids:
        try:
            await bot.send_message(uid, text)
        except Exception:
            logger.exception("اطلاع‌رسانیِ اتصالِ چت به مالک/ادمین ناموفق بود (%s)", uid)


# ═══════════════════════════════════════════
#  پردازشِ پیام‌هایِ گروه/کانالِ متصل
# ═══════════════════════════════════════════

@community_router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.new_chat_members)
async def handle_new_members(msg: Message, bot: Bot):
    row = await _get_bot_row(bot)
    if not row or row.field != FIELD_KEY:
        return
    chat = await _get_chat_row(row.id, msg.chat.id)
    if not chat:
        return
    await _welcome_new_members(msg, bot, chat)


@community_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def handle_group_message(msg: Message, bot: Bot, state: FSMContext):
    # === PATCH_COMMUNITY_GROUP_SKIP_V1 ===
    # این هندلر روی «همهٔ» پیام‌های گروه ثبت شده و چون در هر مسیر return
    # می‌کرد، پیام همین‌جا می‌مرد و هرگز به روترهای بعدی (مثلِ dice_game
    # که خط ۱۵۷ ثبت شده) نمی‌رسید. نتیجه: «بازی کن و جایزه بگیر» در گروه
    # کار نمی‌کرد. حالا هر جا که این ماژول کارِ واقعی‌ای انجام نداده،
    # به‌جای return با SkipHandler نوبت را به بقیه می‌دهد.
    row = await _get_bot_row(bot)
    if not row or row.field != FIELD_KEY:
        raise SkipHandler
    if _is_bot_service_message(msg):
        raise SkipHandler

    chat = await _get_chat_row(row.id, msg.chat.id)
    if not chat:
        raise SkipHandler

    # دستورهایِ مدیریتیِ گروه (بن/اخطار/سکوت/…) — قبل از ضداسپم/فیلتر،
    # چون خودِ این دستورها معمولاً لینک ندارند ولی بهتره اول رسیدگی شوند.
    if await _try_moderation_command(msg, bot, chat):
        return

    if await _antispam_check(msg, bot, chat):
        return
    await _filter_check(msg, bot, chat)
    # فیلتر/ضداسپم پیام را حذف نکرد؛ بگذار روترهایِ بعدی (بازی‌ها،
    # دستیارِ AI و …) هم شانسِ پردازش داشته باشند.
    raise SkipHandler


# === PATCH_COMMUNITY_MGMT_EDIT_EVASION_V1 ===
# باگ/سوراخ: پیامِ ویرایش‌شده اصلاً چک نمی‌شد. یک ترفندِ شناخته‌شدهٔ اسپم
# این است که پیامِ اول بی‌ضرر فرستاده شود (رد می‌شود)، بعد ویرایش شود و
# لینک/کلمهٔ ممنوعه داخلش گذاشته شود — بدونِ این هندلر، آن نسخهٔ ویرایش‌شده
# هیچ‌وقت بازبینی نمی‌شد.
@community_router.edited_message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def handle_group_message_edited(msg: Message, bot: Bot):
    row = await _get_bot_row(bot)
    if not row or row.field != FIELD_KEY:
        return
    if _is_bot_service_message(msg):
        return
    chat = await _get_chat_row(row.id, msg.chat.id)
    if not chat:
        return
    if await _antispam_check(msg, bot, chat):
        return
    await _filter_check(msg, bot, chat)


@community_router.channel_post(F.chat.type == ChatType.CHANNEL)
async def handle_channel_post(msg: Message, bot: Bot):
    """ضداسپم/فیلتر برایِ کانال هم کار می‌کند (مثلاً کامنت‌هایِ گروهِ بحثِ
    لینک‌شده که به شکلِ channel_post می‌آیند در بعضی حالت‌ها)."""
    row = await _get_bot_row(bot)
    if not row or row.field != FIELD_KEY:
        return
    chat = await _get_chat_row(row.id, msg.chat.id)
    if not chat:
        return
    if await _antispam_check(msg, bot, chat):
        return
    await _filter_check(msg, bot, chat)


# === PATCH_COMMUNITY_MGMT_EDIT_EVASION_V1 ===
@community_router.edited_channel_post(F.chat.type == ChatType.CHANNEL)
async def handle_channel_post_edited(msg: Message, bot: Bot):
    row = await _get_bot_row(bot)
    if not row or row.field != FIELD_KEY:
        return
    chat = await _get_chat_row(row.id, msg.chat.id)
    if not chat:
        return
    if await _antispam_check(msg, bot, chat):
        return
    await _filter_check(msg, bot, chat)


# ═══════════════════════════════════════════
#  مدیریتِ اعضا از داخلِ گروه — بن/اخطار/سکوت با ریپلای
# ═══════════════════════════════════════════

_BARE_WORD_CMDS = {
    "بن": "ban", "مسدود": "ban", "ban": "ban",
    "انبن": "unban", "آنبن": "unban", "رفع بن": "unban", "unban": "unban",
    "اخطار": "warn", "خطا": "warn", "warn": "warn",
    "پاک اخطار": "unwarn", "حذف اخطار": "unwarn", "رفع اخطار": "unwarn", "unwarn": "unwarn",
    "سکوت": "mute", "میوت": "mute", "mute": "mute",
    "رفع سکوت": "unmute", "آنمیوت": "unmute", "unmute": "unmute",
}


def _mod_command(text: str) -> str | None:
    t = " ".join((text or "").split()).strip()
    if not t or t.startswith("/"):
        first = t.split()[0].lstrip("/").lower() if t else ""
        return _BARE_WORD_CMDS.get(first)
    return _BARE_WORD_CMDS.get(t.lower())


async def _try_moderation_command(msg: Message, bot: Bot, chat: CommunityChat) -> bool:
    cmd = _mod_command(msg.text or "")
    if not cmd:
        return False
    # === PATCH_COMMUNITY_ANON_ADMIN_V1 ===
    # باگ: این خط بی‌واسطه چک می‌کرد و msg.from_user بودنش را شرط می‌کرد،
    # پس اگر ادمین با «ارسالِ ناشناس» دستور می‌داد (sender_chat=خودِ گروه)،
    # مستقیم رد می‌شد — با اینکه _is_authorized_sender بالاترِ همین فایل
    # از قبل درست این حالت را پوشش می‌داد. اینجا به‌جایِ بازنویسیِ ناقصِ
    # همان چک، از همان تابعِ درست استفاده می‌کنیم.
    if not await _is_authorized_sender(msg, bot):
        return False
    target = msg.reply_to_message
    if not target or not target.from_user:
        await msg.reply("برای این کار روی پیامِ همون شخص ریپلای کن.")
        return True
    u = target.from_user
    try:
        if cmd == "ban":
            await bot.ban_chat_member(msg.chat.id, u.id)
            await _bump_stat(chat.id, "stat_bans_issued")  # PATCH_COMMUNITY_STATS_V1
            await msg.reply(f"🚫 {html.escape(u.full_name)} بن شد.")
        elif cmd == "unban":
            await bot.unban_chat_member(msg.chat.id, u.id, only_if_banned=True)
            await msg.reply(f"✅ بنِ {html.escape(u.full_name)} برداشته شد.")
        elif cmd == "warn":
            n = await _bump_warn(chat.id, u.id)
            await _bump_stat(chat.id, "stat_warns_issued")  # PATCH_COMMUNITY_STATS_V1
            await msg.reply(f"⚠️ {html.escape(u.full_name)} اخطار گرفت ({n}/3).")
            if n >= 3:
                from aiogram.types import ChatPermissions
                await bot.restrict_chat_member(
                    msg.chat.id, u.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=datetime.now() + timedelta(hours=1))
                await msg.reply(f"🔇 {html.escape(u.full_name)} به‌خاطرِ سه اخطار سکوت شد.")
        elif cmd == "unwarn":
            async with Session() as s:
                w = await s.scalar(select(CommunityWarn).where(
                    CommunityWarn.chat_row_id == chat.id, CommunityWarn.user_id == u.id))
                had = w.count if w else 0
                if w:
                    await s.delete(w)
                    await s.commit()
            await msg.reply(f"✅ خطاهایِ {html.escape(u.full_name)} صفر شد (قبلاً {had} تا).")
        elif cmd == "mute":
            from aiogram.types import ChatPermissions
            await bot.restrict_chat_member(
                msg.chat.id, u.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now() + timedelta(hours=1))
            await msg.reply(f"🔇 {html.escape(u.full_name)} به‌مدتِ ۱ ساعت سکوت شد.")
        elif cmd == "unmute":
            from aiogram.types import ChatPermissions
            await bot.restrict_chat_member(
                msg.chat.id, u.id,
                permissions=ChatPermissions(
                    can_send_messages=True, can_send_photos=True, can_send_videos=True,
                    can_send_other_messages=True, can_add_web_page_previews=True))
            await msg.reply(f"🔊 سکوتِ {html.escape(u.full_name)} برداشته شد.")
    except TelegramForbiddenError:
        await msg.reply("❌ ربات دسترسیِ کافی ندارد؛ باید ادمینِ گروه با «محدودکردنِ کاربران» باشد.")
    except Exception as e:
        logger.exception("دستورِ مدیریتیِ گروه ناموفق بود (%s)", cmd)
        await msg.reply(f"❌ ناموفق: <code>{type(e).__name__}</code>")
    return True


# ═══════════════════════════════════════════
#  پنلِ مدیریت (پی‌وی با ربات)
# ═══════════════════════════════════════════

class CmtyFSM(StatesGroup):
    welcome_text = State()
    filter_word_add = State()


async def _owner_ok(cb: CallbackQuery, bot: Bot) -> BotRow | None:
    row = await _get_bot_row(bot)
    if not row:
        await cb.answer()
        return None
    from tenant import is_owner_or_admin
    if not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("دسترسی نداری", show_alert=True)
        return None
    return row


async def _list_chats(bot_id: int) -> list[CommunityChat]:
    async with Session() as s:
        return list((await s.scalars(select(CommunityChat).where(
            CommunityChat.bot_id == bot_id, CommunityChat.is_active == True
        ).order_by(CommunityChat.id))).all())  # noqa: E712


def _chat_icon(chat_type: str) -> str:
    return "📢" if chat_type == "channel" else "👥"


async def _main_menu_content(bot_id: int):
    chats = await _list_chats(bot_id)
    lines = ["🛡 <b>مدیریتِ گروه و کانال</b>\n"]
    rows = []
    if not chats:
        lines.append(
            "هنوز هیچ گروه/کانالی وصل نشده.\n\n"
            "برای اتصال: ربات را به‌عنوانِ <b>ادمین</b> به گروه یا کانالِ "
            "موردنظرت اضافه کن — خودش همین‌جا ظاهر می‌شود.\n\n"
            "راهنمایِ گام‌به‌گام و دسترسی‌هایِ لازم پایینِ همین صفحه است 👇")
    else:
        lines.append("گروه/کانال‌هایِ متصل:")
        for c in chats:
            state = "🟢" if (c.antispam_on or c.welcome_on or c.filter_on) else "⚪️"
            rows.append([InlineKeyboardButton(
                text=f"{state} {_chat_icon(c.chat_type)} {c.title or c.chat_id}",
                callback_data=f"cmty:chat:{c.id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="❓ راهنمایِ اتصال و دسترسی‌ها",
                                      callback_data="cmty:guide",
                                      style=ButtonStyle.PRIMARY)])
    if chats:
        rows.append([InlineKeyboardButton(text="📊 آمارِ کلی", callback_data="cmty:statsall",
                                          style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="📅 زمان‌بندیِ پست", callback_data="adb:menu",
                                      style=ButtonStyle.PRIMARY),
                InlineKeyboardButton(text="📋 نظرسنجی‌ها", callback_data="tadm:polls",
                                     style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:products",
                                      style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@community_router.callback_query(F.data == "cmty:menu")
async def cmty_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    txt, kb = await _main_menu_content(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


def _yn(v: bool) -> str:
    return "✅" if v else "❌"


def _act_label(a: str) -> str:
    return {"delete": "🗑 فقط حذف", "warn": "⚠️ حذف + اخطار", "mute": "🔇 حذف + سکوت"}.get(a, a)


async def _chat_detail_content(chat_row_id: int):
    async with Session() as s:
        c = await s.get(CommunityChat, chat_row_id)
    if not c:
        return None, None
    words = await _get_filter_words(c.id)
    txt = (
        f"{_chat_icon(c.chat_type)} <b>{html.escape(c.title or str(c.chat_id))}</b>\n\n"
        f"🚫 ضداسپم: {_yn(c.antispam_on)}"
        + (f" — لینک:{_yn(c.antispam_links)} فوروارد:{_yn(c.antispam_forwards)} "
           f"تکراری:{_yn(c.antispam_duplicate)} | {_act_label(c.antispam_action)}"
           if c.antispam_on else "") + "\n"
        f"👋 خوشامدگویی: {_yn(c.welcome_on)}\n"
        f"🧹 فیلترِ کلمات: {_yn(c.filter_on)}"
        + (f" ({len(words)} کلمه، {_act_label(c.filter_action)})" if c.filter_on else "") + "\n"
    )
    rows = [
        [InlineKeyboardButton(
            text=f"{'🔴 خاموش‌کردنِ' if c.antispam_on else '🟢 روشن‌کردنِ'} ضداسپم",
            callback_data=f"cmty:as:toggle:{c.id}",
            style=ButtonStyle.DANGER if c.antispam_on else ButtonStyle.SUCCESS)],
    ]
    if c.antispam_on:
        rows.append([
            InlineKeyboardButton(text=f"لینک {_yn(c.antispam_links)}",
                                 callback_data=f"cmty:as:links:{c.id}", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text=f"فوروارد {_yn(c.antispam_forwards)}",
                                 callback_data=f"cmty:as:fwd:{c.id}", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text=f"تکراری {_yn(c.antispam_duplicate)}",
                                 callback_data=f"cmty:as:dup:{c.id}", style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(
            text=f"واکنش: {_act_label(c.antispam_action)}",
            callback_data=f"cmty:as:action:{c.id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text=f"{'🔴 خاموش‌کردنِ' if c.welcome_on else '🟢 روشن‌کردنِ'} خوشامدگویی",
        callback_data=f"cmty:wc:toggle:{c.id}",
        style=ButtonStyle.DANGER if c.welcome_on else ButtonStyle.SUCCESS)])
    if c.welcome_on:
        rows.append([InlineKeyboardButton(text="✏️ ویرایشِ متنِ خوش‌آمد",
                                          callback_data=f"cmty:wc:text:{c.id}",
                                          style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text=f"{'🔴 خاموش‌کردنِ' if c.filter_on else '🟢 روشن‌کردنِ'} فیلترِ کلمات",
        callback_data=f"cmty:fw:toggle:{c.id}",
        style=ButtonStyle.DANGER if c.filter_on else ButtonStyle.SUCCESS)])
    if c.filter_on:
        rows.append([
            InlineKeyboardButton(text="➕ افزودنِ کلمه", callback_data=f"cmty:fw:add:{c.id}",
                                 style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="📋 فهرستِ کلمات", callback_data=f"cmty:fw:list:{c.id}:0",
                                 style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="واکنش: " + _act_label(c.filter_action),
                                 callback_data=f"cmty:fw:action:{c.id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="📊 آمار", callback_data=f"cmty:stats:{c.id}",
                                      style=ButtonStyle.PRIMARY),
                InlineKeyboardButton(text="🔍 بررسیِ دسترسیِ ربات",
                                     callback_data=f"cmty:checkperm:{c.id}",
                                     style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="🗑 قطعِ اتصالِ این چت",
                                      callback_data=f"cmty:disconnect:{c.id}",
                                      style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cmty:menu",
                                      style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _refresh_detail(cb: CallbackQuery, chat_row_id: int):
    txt, kb = await _chat_detail_content(chat_row_id)
    if not txt:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


@community_router.callback_query(F.data.startswith("cmty:chat:"))
async def cmty_chat_detail(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await _refresh_detail(cb, int(cb.data.split(":")[2]))


async def _toggle_field(chat_row_id: int, field: str) -> CommunityChat | None:
    async with Session() as s:
        c = await s.get(CommunityChat, chat_row_id)
        if not c:
            return None
        setattr(c, field, not getattr(c, field))
        await s.commit()
        bot_id, chat_id = c.bot_id, c.chat_id
    invalidate_chat_cache(bot_id, chat_id)
    return c


@community_router.callback_query(F.data.startswith("cmty:as:toggle:"))
async def cmty_as_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await _toggle_field(int(cb.data.split(":")[3]), "antispam_on")
    await _refresh_detail(cb, int(cb.data.split(":")[3]))


@community_router.callback_query(F.data.startswith("cmty:as:links:"))
async def cmty_as_links(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[3])
    await _toggle_field(cid, "antispam_links")
    await _refresh_detail(cb, cid)


@community_router.callback_query(F.data.startswith("cmty:as:fwd:"))
async def cmty_as_fwd(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[3])
    await _toggle_field(cid, "antispam_forwards")
    await _refresh_detail(cb, cid)


@community_router.callback_query(F.data.startswith("cmty:as:dup:"))
async def cmty_as_dup(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[3])
    await _toggle_field(cid, "antispam_duplicate")
    await _refresh_detail(cb, cid)


_ACTIONS_CYCLE = ["delete", "warn", "mute"]


async def _cycle_action(chat_row_id: int, field: str) -> None:
    async with Session() as s:
        c = await s.get(CommunityChat, chat_row_id)
        if not c:
            return
        cur = getattr(c, field)
        nxt = _ACTIONS_CYCLE[(_ACTIONS_CYCLE.index(cur) + 1) % len(_ACTIONS_CYCLE)] \
            if cur in _ACTIONS_CYCLE else _ACTIONS_CYCLE[0]
        setattr(c, field, nxt)
        await s.commit()
        bot_id, chat_id = c.bot_id, c.chat_id
    invalidate_chat_cache(bot_id, chat_id)


@community_router.callback_query(F.data.startswith("cmty:as:action:"))
async def cmty_as_action(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[3])
    await _cycle_action(cid, "antispam_action")
    await _refresh_detail(cb, cid)


@community_router.callback_query(F.data.startswith("cmty:wc:toggle:"))
async def cmty_wc_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await _toggle_field(int(cb.data.split(":")[3]), "welcome_on")
    await _refresh_detail(cb, int(cb.data.split(":")[3]))


@community_router.callback_query(F.data.startswith("cmty:wc:text:"))
async def cmty_wc_text_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[3])
    await state.set_state(CmtyFSM.welcome_text)
    await state.update_data(cmty_chat_id=cid)
    await cb.message.answer(
        "✏️ متنِ خوش‌آمد را بفرست.\n\n"
        "می‌توانی از <code>{نام}</code> برایِ اسمِ عضو و <code>{گروه}</code> "
        "برایِ اسمِ گروه استفاده کنی.\n\n"
        "مثال: <code>سلام {نام} عزیز، به {گروه} خوش اومدی! 🌹</code>")
    await cb.answer()


@community_router.message(CmtyFSM.welcome_text, F.text)
async def cmty_wc_text_save(msg: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    cid = int(data.get("cmty_chat_id") or 0)
    await state.clear()
    async with Session() as s:
        c = await s.get(CommunityChat, cid)
        if not c:
            await msg.answer("پیدا نشد.")
            return
        c.welcome_text = msg.text
        await s.commit()
        bot_id, chat_id = c.bot_id, c.chat_id
    invalidate_chat_cache(bot_id, chat_id)
    preview = _render_welcome(msg.text, "علی", "گروهِ نمونه")
    await msg.answer(f"✅ ذخیره شد. پیش‌نمایش:\n\n{preview}")
    txt, kb = await _chat_detail_content(cid)
    if txt:
        await msg.answer(txt, reply_markup=kb)


@community_router.callback_query(F.data.startswith("cmty:fw:toggle:"))
async def cmty_fw_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await _toggle_field(int(cb.data.split(":")[3]), "filter_on")
    await _refresh_detail(cb, int(cb.data.split(":")[3]))


@community_router.callback_query(F.data.startswith("cmty:fw:action:"))
async def cmty_fw_action(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[3])
    await _cycle_action(cid, "filter_action")
    await _refresh_detail(cb, cid)


@community_router.callback_query(F.data.startswith("cmty:fw:add:"))
async def cmty_fw_add_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[3])
    await state.set_state(CmtyFSM.filter_word_add)
    await state.update_data(cmty_chat_id=cid)
    await cb.message.answer(
        "➕ کلمه یا عبارتِ ممنوعه را بفرست.\n"
        "برایِ چند کلمه با هم، هر کدام را در یک خط بفرست.")
    await cb.answer()


@community_router.message(CmtyFSM.filter_word_add, F.text)
async def cmty_fw_add_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    cid = int(data.get("cmty_chat_id") or 0)
    await state.clear()
    words = [w.strip() for w in (msg.text or "").split("\n") if w.strip()]
    if not words:
        await msg.answer("چیزی دریافت نشد.")
        return
    async with Session() as s:
        c = await s.get(CommunityChat, cid)
        if c is not None and await _is_foreign(c, msg.bot):   # PATCH_TENANT_SCOPE_V1
            c = None
        if not c:
            await msg.answer("پیدا نشد.")
            return
        for w in words:
            s.add(CommunityFilterWord(chat_row_id=cid, word=w[:120]))
        await s.commit()
    invalidate_words_cache(cid)
    await msg.answer(f"✅ {len(words)} کلمه اضافه شد.")
    txt, kb = await _chat_detail_content(cid)
    if txt:
        await msg.answer(txt, reply_markup=kb)


_FW_PAGE = 15


async def _cmty_fw_list_render(cb: CallbackQuery, cid: int, page: int) -> None:
    """رندرِ فهرستِ کلماتِ فیلترشده — بدونِ خواندن از cb.data (نگاه کن به
    PATCH_CB_DATA_FROZEN_FIX_V1 در تنانت/smm.py برایِ توضیحِ کاملِ باگ)."""
    async with Session() as s:
        rows_db = (await s.scalars(select(CommunityFilterWord).where(
            CommunityFilterWord.chat_row_id == cid).order_by(
            CommunityFilterWord.id))).all()
    total = len(rows_db)
    chunk = rows_db[page * _FW_PAGE:(page + 1) * _FW_PAGE]
    lines = [f"📋 <b>کلماتِ فیلترشده</b> ({total})\n"]
    rows = []
    for w in chunk:
        lines.append(f"• {html.escape(w.word)}")
        rows.append([InlineKeyboardButton(text=f"🗑 {w.word[:30]}",
                                          callback_data=f"cmty:fw:del:{cid}:{w.id}:{page}",
                                          style=ButtonStyle.DANGER)])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی",
                                        callback_data=f"cmty:fw:list:{cid}:{page-1}",
                                        style=ButtonStyle.PRIMARY))
    if (page + 1) * _FW_PAGE < total:
        nav.append(InlineKeyboardButton(text="بعدی ▶️",
                                        callback_data=f"cmty:fw:list:{cid}:{page+1}",
                                        style=ButtonStyle.PRIMARY))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"cmty:chat:{cid}",
                                      style=ButtonStyle.DANGER)])
    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@community_router.callback_query(F.data.startswith("cmty:fw:list:"))
async def cmty_fw_list(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    parts = cb.data.split(":")
    cid, page = int(parts[3]), int(parts[4])
    await _cmty_fw_list_render(cb, cid, page)


@community_router.callback_query(F.data.startswith("cmty:fw:del:"))
async def cmty_fw_del(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    parts = cb.data.split(":")
    cid, wid, page = int(parts[3]), int(parts[4]), int(parts[5])
    async with Session() as s:
        w = await s.get(CommunityFilterWord, wid)
        if w and w.chat_row_id == cid:
            await s.delete(w)
            await s.commit()
    invalidate_words_cache(cid)
    # === PATCH_CB_DATA_FROZEN_FIX_V1 ===
    await _cmty_fw_list_render(cb, cid, page)


@community_router.callback_query(F.data.startswith("cmty:disconnect:"))
async def cmty_disconnect(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[2])
    async with Session() as s:
        c = await s.get(CommunityChat, cid)
        if not c:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        c.is_active = False
        bot_id, chat_id = c.bot_id, c.chat_id
        await s.commit()
    invalidate_chat_cache(bot_id, chat_id)
    await cb.answer("قطع شد؛ می‌توانی ربات را از گروه هم حذف کنی.", show_alert=True)
    txt, kb = await _main_menu_content(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb)


# ═══════════════════════════════════════════
#  راهنما + بررسیِ زندهٔ دسترسی‌ها
# ═══════════════════════════════════════════

_GUIDE_TEXT = (
    "❓ <b>راهنمایِ اتصال و دسترسی‌ها</b>\n\n"

    # === PATCH_COMMUNITY_PRIVACY_MODE_V1 ===
    # باگِ گزارش‌شده: کاربرها می‌گفتند «سکوت/اخطار/فیلترِ کلمات کلا کار
    # نمی‌کنه». علتِ ریشه‌ای این بود: تلگرام یک تنظیمِ پیش‌فرض به اسمِ
    # «Privacy Mode» دارد که وقتی روشن باشد (پیش‌فرضِ هر ربات تازه‌ساخته‌شده)،
    # ربات اصلاً پیام‌هایِ سادهٔ گروه را نمی‌بیند — فقط دستورهایِ اسلش‌دار،
    # منشن‌ها، و ریپلای‌رویِ‌پیامِ‌خودش. راهنما پایین‌تر (بخشِ ۴) دقیقاً
    # همان چیزی را توصیه می‌کرد که با Privacy Mode روشن هرگز به ربات
    # نمی‌رسد («سکوت» بدونِ اسلش) — بدونِ اینکه جایی این تنظیم را بگوید.
    "⚠️ <b>۰) قبل از هر چیز — حالتِ حریمِ‌خصوصیِ ربات را خاموش کن</b>\n"
    "این مهم‌ترین قدم است و اگر انجامش ندهی، هیچ‌کدامِ قابلیت‌هایِ زیر کار "
    "نمی‌کنند (نه سکوت، نه اخطار، نه فیلترِ کلمات، نه ضداسپم):\n"
    "با @BotFather چت کن → /mybots → همین ربات را انتخاب کن → "
    "«Bot Settings» → «Group Privacy» → «Turn off».\n"
    "بدونِ این کار، تلگرام پیام‌هایِ معمولیِ گروه را اصلاً به ربات نمی‌رساند "
    "— نه اینکه ربات خراب باشد، فقط اصلاً چیزی نمی‌بیند.\n\n"

    "<b>۱) اضافه‌کردنِ ربات</b>\n"
    "به تنظیماتِ گروه یا کانالت برو → «ادمین‌ها» → «افزودنِ ادمین» → "
    "یوزرنیمِ همین ربات را جستجو و اضافه کن.\n"
    "همین که ادمین شد، خودش اینجا (پایینِ همین منو) ظاهر می‌شود؛ نیازی به "
    "کارِ دیگری نیست.\n\n"

    "<b>۲) کدام دسترسی برایِ کدام قابلیت لازم است</b>\n"
    "• 🗑 <b>حذفِ پیام‌ها</b> — برایِ ضداسپم، فیلترِ کلمات، و پاک‌کردنِ "
    "پیامِ «فلانی به گروه اضافه شد» در خوشامدگویی\n"
    "• 🚫 <b>محدودکردنِ کاربران</b> — برایِ سکوت/رفعِ سکوت (چه دستیِ ادمین، "
    "چه خودکارِ ضداسپم/فیلتر)\n"
    "• ➕ <b>دعوت با لینک</b> — لازم نیست، ولی خالی از لطف هم نیست\n"
    "• 📝 <b>ارسالِ پیام</b> (مخصوصِ کانال) — برایِ زمان‌بندیِ پست\n\n"
    "ساده‌ترین راه: هنگامِ افزودنِ ادمین، تمامِ دسترسی‌ها را تیک بزن. اگر "
    "بعضی‌ها را ندهی، فقط همان قابلیت‌هایِ مربوط کار نمی‌کنند — بقیه سالم‌اند.\n\n"

    "<b>۳) گروه یا کانال؟</b>\n"
    "🚫 ضداسپم و 🧹 فیلترِ کلمات: هم گروه هم کانال\n"
    "👋 خوشامدگویی و 👮 بن/اخطار/سکوت: فقط گروه (کانال عضوگیریِ قابلِ‌مشاهده ندارد)\n"
    "📅 زمان‌بندیِ پست و 📋 نظرسنجی: هم گروه هم کانال\n\n"

    "<b>۴) دستورهایِ داخلِ گروه</b>\n"
    "هر ادمینِ گروه می‌تواند رویِ پیامِ کسی ریپلای کند و فقط بنویسد: "
    "«بن»، «انبن»، «اخطار»، «پاک اخطار»، «سکوت»، «رفع سکوت». نیازی به "
    "دستورِ اسلش‌دار نیست — <b>ولی این فقط با خاموش‌بودنِ Privacy Mode "
    "(بندِ ۰ بالا) کار می‌کند.</b>\n\n"

    "مشکلی بود؟ داخلِ همین منو، رویِ گروه/کانالت بزن و «🔍 بررسیِ دسترسیِ "
    "ربات» را انتخاب کن تا دقیقاً بگوید کدام دسترسی را کم داری."
)


@community_router.callback_query(F.data == "cmty:guide")
async def cmty_guide(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cmty:menu",
                             style=ButtonStyle.DANGER)]])
    try:
        await cb.message.edit_text(_GUIDE_TEXT, reply_markup=kb)
    except Exception:
        await cb.message.answer(_GUIDE_TEXT, reply_markup=kb)
    await cb.answer()


async def _check_permissions(bot: Bot, chat_id: int) -> tuple[bool, str]:
    """وضعیتِ ادمین‌بودنِ ربات و دسترسی‌هایش را زنده از تلگرام می‌خواند."""
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except Exception as e:
        return False, f"❌ نتوانستم وضعیتِ ربات را بخوانم: <code>{type(e).__name__}</code>"

    if member.status != "administrator":
        return False, ("❌ ربات هنوز <b>ادمین</b> نیست.\n"
                       "از تنظیماتِ گروه/کانال، ادمینش کن.")

    checks = [
        ("🗑 حذفِ پیام‌ها", getattr(member, "can_delete_messages", False)),
        ("🚫 محدودکردنِ کاربران", getattr(member, "can_restrict_members", False)),
    ]
    lines = ["✅ ربات ادمین است. وضعیتِ دسترسی‌ها:\n"]
    all_ok = True
    for label, ok in checks:
        lines.append(f"{'✅' if ok else '❌'} {label}")
        all_ok = all_ok and ok
    if not all_ok:
        lines.append("\n⚠️ برایِ آنچه ❌ است، به تنظیماتِ ادمینِ ربات در گروه/کانال "
                     "برو و تیکش را بزن.")
    # === PATCH_COMMUNITY_PRIVACY_MODE_V1 ===
    # این چک فقط دسترسی‌هایِ ادمین را می‌بیند، نه Privacy Mode — چون
    # تلگرام هیچ APIای برایِ خواندنِ آن به خودِ ربات نمی‌دهد. چون این دقیقاً
    # همان صفحه‌ای است که ادمینِ گیج‌شده سراغش می‌آید، این یادآوری همیشه
    # نشان داده می‌شود، حتی اگر همه‌چیزِ بالا ✅ باشد.
    lines.append(
        "\nℹ️ اگر با اینکه همه‌چیزِ بالا سبز است، بازهم دستورهایی مثلِ "
        "«سکوت»/«اخطار» جواب نمی‌دهند: با @BotFather برو → /mybots → این "
        "ربات → Bot Settings → Group Privacy → مطمئن شو Off است. این را "
        "تلگرام‌بات‌ساز نمی‌تواند خودکار چک کند.")
    return all_ok, "\n".join(lines)


@community_router.callback_query(F.data.startswith("cmty:checkperm:"))
async def cmty_checkperm(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[2])
    async with Session() as s:
        c = await s.get(CommunityChat, cid)
        if c is not None and c.bot_id != row.id:   # PATCH_TENANT_SCOPE_V1: رکوردِ رباتِ دیگر
            c = None
    if not c:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    await cb.answer("در حالِ بررسی…")
    _, text = await _check_permissions(bot, c.chat_id)
    await cb.message.answer(text)


# ═══════════════════════════════════════════
#  آمار — PATCH_COMMUNITY_STATS_V1
# ═══════════════════════════════════════════

def _fmt_since(dt: datetime) -> str:
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "—"


def _chat_stats_text(c: CommunityChat) -> str:
    return (
        f"📊 <b>آمار — {html.escape(c.title or str(c.chat_id))}</b>\n"
        f"از {_fmt_since(c.stats_since)}\n\n"
        f"🚫 پیامِ حذف‌شده (ضداسپم): {c.stat_antispam_deleted:,}\n"
        f"🧹 پیامِ حذف‌شده (فیلترِ کلمات): {c.stat_filter_deleted:,}\n"
        f"⚠️ اخطارِ صادرشده (مجموع): {c.stat_warns_issued:,}\n"
        f"🚷 بنِ صادرشده: {c.stat_bans_issued:,}\n"
        f"👋 عضوِ خوشامدگفته‌شده: {c.stat_welcomed:,}\n\n"
        f"برایِ دیدن و پاک‌کردنِ اخطارهایِ <b>فعلاً فعال</b> (کسانی که هنوز "
        f"اخطارِ رفع‌نشده دارند)، پایین بزن 👇"
    )


async def _cmty_stats_render(cb: CallbackQuery, cid: int) -> None:
    """رندرِ صفحهٔ آمار — بدونِ خواندن از cb.data (نگاه کن به
    PATCH_CB_DATA_FROZEN_FIX_V1 در tenant/smm.py برایِ توضیحِ کاملِ باگ)."""
    async with Session() as s:
        c = await s.get(CommunityChat, cid)
    if not c:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    # === PATCH_COMMUNITY_WARN_LIST_V1 ===
    # قبلاً «اخطارِ صادرشده» فقط یک عددِ خام بود — نه می‌شد دید چه کسانی
    # الان اخطارِ فعال دارند، نه می‌شد از همین‌جا پاکش کرد (باید حتماً
    # می‌رفتی داخلِ گروه و دستورِ «پاک اخطار» را با ریپلای می‌زدی). حالا
    # یک دکمهٔ اینلاین اضافه شد که فهرستِ کسانی که الان اخطارِ رفع‌نشده
    # دارند را نشان می‌دهد؛ رویِ هرکدام بزنی، می‌توانی همان‌جا پاکش کنی.
    async with Session() as s:
        active_warns = (await s.scalars(select(CommunityWarn).where(
            CommunityWarn.chat_row_id == cid, CommunityWarn.count > 0
        ).order_by(CommunityWarn.count.desc()))).all()
    kb_rows = [
        [InlineKeyboardButton(
            text=f"⚠️ مدیریتِ اخطارهایِ فعال ({len(active_warns)} نفر)",
            callback_data=f"cmty:warnlist:{cid}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="♻️ صفرکردنِ آمار", callback_data=f"cmty:statsreset:{cid}",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"cmty:chat:{cid}",
                              style=ButtonStyle.DANGER)],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await cb.message.edit_text(_chat_stats_text(c), reply_markup=kb)
    except Exception:
        await cb.message.answer(_chat_stats_text(c), reply_markup=kb)
    await cb.answer()


@community_router.callback_query(F.data.startswith("cmty:stats:"))
async def cmty_stats(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[2])
    await _cmty_stats_render(cb, cid)


async def _resolve_display_name(bot: Bot, chat_id: int, user_id: int) -> str:
    """نامِ نمایشیِ یک کاربر برایِ فهرستِ اخطارها — از تلگرام زنده می‌خواند؛
    اگر نشد (کاربر گروه را ترک کرده و…)، فقط آیدیِ عددی نشان داده می‌شود."""
    try:
        member = await asyncio.wait_for(bot.get_chat_member(chat_id, user_id), timeout=4.0)
        return member.user.full_name or f"کاربر {user_id}"
    except Exception:
        return f"کاربر {user_id}"


@community_router.callback_query(F.data.startswith("cmty:warnlist:"))
async def cmty_warn_list(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[2])
    async with Session() as s:
        c = await s.get(CommunityChat, cid)
        if c is not None and c.bot_id != row.id:   # PATCH_TENANT_SCOPE_V1: رکوردِ رباتِ دیگر
            c = None
        if not c:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        active_warns = (await s.scalars(select(CommunityWarn).where(
            CommunityWarn.chat_row_id == cid, CommunityWarn.count > 0
        ).order_by(CommunityWarn.count.desc()))).all()
    kb_rows = []
    for w in active_warns[:40]:  # سقفِ منطقی برایِ یک صفحه
        name = await _resolve_display_name(bot, c.chat_id, w.user_id)
        kb_rows.append([InlineKeyboardButton(
            text=f"⚠️ {name} — {w.count}/3", callback_data=f"cmty:warndetail:{cid}:{w.user_id}",
            style=ButtonStyle.PRIMARY)])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"cmty:stats:{cid}",
                                         style=ButtonStyle.DANGER)])
    if not active_warns:
        text = f"✅ در «{html.escape(c.title or str(c.chat_id))}» فعلاً هیچ‌کس اخطارِ فعال ندارد."
    else:
        text = (f"⚠️ <b>اخطارهایِ فعال — {html.escape(c.title or str(c.chat_id))}</b>\n\n"
               f"روی هرکدام بزن تا جزئیات و گزینهٔ پاک‌کردن را ببینی:")
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await cb.answer()


@community_router.callback_query(F.data.startswith("cmty:warndetail:"))
async def cmty_warn_detail(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, _, cid, uid = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    async with Session() as s:
        c = await s.get(CommunityChat, cid)
        if c is not None and c.bot_id != row.id:   # PATCH_TENANT_SCOPE_V1: رکوردِ رباتِ دیگر
            c = None
        w = await s.scalar(select(CommunityWarn).where(
            CommunityWarn.chat_row_id == cid, CommunityWarn.user_id == uid))
    if not c or not w:
        await cb.answer("پیدا نشد (شاید همین الان پاک شده)", show_alert=True)
        return
    name = await _resolve_display_name(bot, c.chat_id, uid)
    text = (
        f"⚠️ <b>جزئیاتِ اخطار</b>\n\n"
        f"کاربر: {html.escape(name)}\n"
        f"آیدیِ عددی: <code>{uid}</code>\n"
        f"تعدادِ اخطار: {w.count}/3\n"
        f"آخرین به‌روزرسانی: {_fmt_since(w.updated_at)}\n\n"
        f"با پاک‌کردن، شمارنده صفر می‌شود — دقیقاً همان کاری که دستورِ "
        f"«پاک اخطار» با ریپلای در گروه انجام می‌دهد."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 پاک‌کردنِ این اخطار", callback_data=f"cmty:warnclear:{cid}:{uid}",
                              style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="⬅️ بازگشت به فهرست", callback_data=f"cmty:warnlist:{cid}",
                              style=ButtonStyle.PRIMARY)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@community_router.callback_query(F.data.startswith("cmty:warnclear:"))
async def cmty_warn_clear(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, _, cid, uid = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    async with Session() as s:
        w = await s.scalar(select(CommunityWarn).where(
            CommunityWarn.chat_row_id == cid, CommunityWarn.user_id == uid))
        if w:
            await s.delete(w)
            await s.commit()
    await cb.answer("✅ اخطار پاک شد")
    await cmty_warn_list(cb, bot)


@community_router.callback_query(F.data.startswith("cmty:statsreset:"))
async def cmty_stats_reset(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cid = int(cb.data.split(":")[2])
    async with Session() as s:
        c = await s.get(CommunityChat, cid)
        if c is not None and c.bot_id != row.id:   # PATCH_TENANT_SCOPE_V1: رکوردِ رباتِ دیگر
            c = None
        if not c:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        c.stat_antispam_deleted = 0
        c.stat_filter_deleted = 0
        c.stat_warns_issued = 0
        c.stat_bans_issued = 0
        c.stat_welcomed = 0
        c.stats_since = datetime.now()
        await s.commit()
    await cb.answer("آمار صفر شد")
    # === PATCH_CB_DATA_FROZEN_FIX_V1 ===
    await _cmty_stats_render(cb, cid)


@community_router.callback_query(F.data == "cmty:statsall")
async def cmty_stats_all(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok(cb, bot)
    if not row:
        return
    chats = await _list_chats(row.id)
    if not chats:
        await cb.answer("هنوز چتی وصل نیست", show_alert=True)
        return
    tot = dict(antispam=0, filter=0, warns=0, bans=0, welcomed=0)
    for c in chats:
        tot["antispam"] += c.stat_antispam_deleted or 0
        tot["filter"] += c.stat_filter_deleted or 0
        tot["warns"] += c.stat_warns_issued or 0
        tot["bans"] += c.stat_bans_issued or 0
        tot["welcomed"] += c.stat_welcomed or 0
    text = (
        f"📊 <b>آمارِ کلی</b> ({len(chats)} گروه/کانالِ متصل)\n\n"
        f"🚫 پیامِ حذف‌شده (ضداسپم): {tot['antispam']:,}\n"
        f"🧹 پیامِ حذف‌شده (فیلترِ کلمات): {tot['filter']:,}\n"
        f"⚠️ اخطارِ صادرشده: {tot['warns']:,}\n"
        f"🚷 بنِ صادرشده: {tot['bans']:,}\n"
        f"👋 عضوِ خوشامدگفته‌شده: {tot['welcomed']:,}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cmty:menu",
                             style=ButtonStyle.DANGER)]])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


async def _is_foreign(obj, bot) -> bool:
    """=== PATCH_TENANT_SCOPE_V1 === True اگر رکوردی که با idِ داخلِ
    callback_data/state خوانده شده مالِ رباتِ جاریِ این آپدیت نباشد."""
    if bot is None or getattr(obj, "bot_id", None) is None:
        return False
    from tenant import get_bot_row as _gbr
    _r = await _gbr(bot)
    return bool(_r) and obj.bot_id != _r.id
