"""محافظ گروهِ ربات‌های مستأجر.

تمام تنظیمات با bot_id جدا می‌شوند تا چند ربات و چند گروه با هم تداخل نداشته
باشند. قابلیت‌ها از پنل «تنظیمات گروه» قابل مدیریت هستند.
"""
from __future__ import annotations

import asyncio
import html
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle, ChatType
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ChatPermissions, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, select

from config import Session
from models import TenantGroupBan, TenantGroupCaptcha, TenantGroupEvent, TenantGroupWarn
from premium_emoji import hx_button
from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)
tenant_group_guard_router = Router()
_flood: dict[tuple[int, int, int], deque[float]] = defaultdict(deque)
_unmute_tasks: set[asyncio.Task] = set()
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/|joinchat/|لفت\.من|لینک)", re.I)

# نام‌های فنی فقط برای ذخیره‌سازی داخلی می‌مانند؛ در کانال گزارش، عنوان و
# نقش‌ها کاملاً فارسی و قابل‌فهم نمایش داده می‌شوند.
_EVENT_REPORT_META: dict[str, tuple[str, str, str, str]] = {
    "guard_connect": ("🛡", "اتصال و فعال‌سازی محافظ گروه", "فعال‌کننده", "کاربر موردنظر"),
    "guard_disconnect": ("🔴", "غیرفعال‌سازی محافظ گروه", "غیرفعال‌کننده", "کاربر موردنظر"),
    "ban": ("🚫", "مسدود کردن کاربر", "اجراکننده", "کاربر مسدودشده"),
    "unban": ("✅", "رفع مسدودیت کاربر", "اجراکننده", "کاربر رفع‌مسدودشده"),
    "warn": ("⚠️", "ثبت اخطار برای کاربر", "اخطاردهنده", "کاربر اخطارگرفته"),
    "unwarn": ("🧹", "پاک‌کردن اخطارهای کاربر", "اجراکننده", "کاربر موردنظر"),
    "auto_ban": ("⛔️", "مسدودسازی خودکار پس از تکمیل اخطارها", "اخطاردهنده", "کاربر مسدودشده"),
    "mute": ("🔇", "سکوت موقت کاربر", "اجراکننده", "کاربر ساکت‌شده"),
    "unmute": ("🔊", "رفع سکوت کاربر", "اجراکننده", "کاربر رفع‌سکوت‌شده"),
    "unmute_auto": ("🔊", "رفع خودکار سکوت کاربر", "ثبت‌کننده سکوت", "کاربر رفع‌سکوت‌شده"),
    "captcha_required": ("🧩", "ارسال کپچای عضو جدید", "اجراکننده", "عضو جدید"),
    "captcha_pass": ("✅", "تأیید کپچای عضو جدید", "اجراکننده", "عضو تأییدشده"),
    "captcha_expired_ban": ("⏳", "مسدودسازی به‌دلیل پایان مهلت کپچا", "اجراکننده", "عضو مسدودشده"),
    "night_delete": ("🌙", "حذف پیام در حالت شب", "ارسال‌کننده پیام", "کاربر موردنظر"),
    "antipromo_delete": ("🧹", "حذف لینک یا تبلیغ", "ارسال‌کننده پیام", "کاربر موردنظر"),
    "flood_mute": ("🌊", "محدودسازی خودکار به‌دلیل ارسال مکرر", "ارسال‌کننده پیام", "کاربر محدودشده"),
    "wallet_transfer": ("💳", "انتقال موجودی بین کاربران", "فرستنده", "دریافت‌کننده"),
    "bet_created": ("🎲", "ایجاد بازی", "ایجادکننده بازی", "کاربر دعوت‌شده"),
    "bet_settled": ("🏆", "تسویه بازی", "تأییدکننده بازی", "برنده"),
    "bet_cancelled": ("↩️", "لغو بازی و بازگشت وجه", "لغوکننده", "کاربر موردنظر"),
}


def _report_text(chat_id: int, kind: str, actor: int | None, target: int | None,
                 details: str = "", *, happened_at: datetime | None = None) -> str:
    icon, title, actor_label, target_label = _EVENT_REPORT_META.get(
        kind, ("📋", "رویداد ثبت‌شده در گروه", "اجراکننده", "کاربر موردنظر"))
    lines = [
        f"{icon} <b>{html.escape(title)}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🏘 <b>شناسه گروه:</b> <code>\u200e{chat_id}</code>",
    ]
    if actor:
        lines.append(f"👤 <b>{html.escape(actor_label)}:</b> <code>\u200e{actor}</code>")
    if target:
        lines.append(f"🎯 <b>{html.escape(target_label)}:</b> <code>\u200e{target}</code>")
    clean_parts = [part.strip() for part in re.split(r"[؛\n]+", details or "") if part.strip()]
    if clean_parts:
        lines.append("📝 <b>جزئیات:</b>")
        lines.extend(f"• {html.escape(part)}" for part in clean_parts)
    stamp = (happened_at or datetime.now()).strftime("%Y/%m/%d - %H:%M:%S")
    lines.extend(["━━━━━━━━━━━━━━━━━━", f"🕒 <b>زمان ثبت:</b> <code>\u200e{stamp}</code>"])
    return "\n".join(lines)


async def _bot_username(bot: Bot) -> str:
    try:
        return (await bot.get_me()).username or "نام‌کاربری_ربات"
    except Exception:
        return "نام‌کاربری_ربات"


async def _full_help_text(bot: Bot) -> str:
    return (
        "📚 <b>راهنمای کامل امکانات گروه</b>\n\n"
        "🛡 <b>۱) فعال‌سازی محافظ گروه</b>\n"
        "• ربات را در گروه ادمین کنید و مجوز حذف پیام و محدودکردن اعضا را بدهید.\n"
        "• برای نمایش برچسب وضعیت (مثل «اخطار ۱» یا «سکوت») کنار نام کاربر، مجوز «افزودن ادمین جدید» را هم به ربات بدهید؛ بدون این مجوز، اخطار و سکوت طبیعی کار می‌کنند ولی برچسب نمایش داده نمی‌شود.\n"
        "• شخصی که دستور را می‌فرستد نیز باید ادمین همان گروه باشد.\n"
        "• داخل خود گروه بفرستید: <code>/فعال_محافظ</code>\n"
        "• بعد از پیام تأیید، تنظیمات محافظ را از همین پنل انجام دهید.\n\n"
        "👮 <b>دستورات محافظ؛ همگی با ریپلای روی پیام کاربر</b>\n"
        "<code>/بن</code> — مسدود کردن کاربر\n"
        "<code>/انبن</code> — رفع مسدودیت\n"
        "<code>/اخطار</code> — ثبت یک اخطار\n"
        "<code>/رفع_اخطار</code> یا <code>/حذف_اخطار</code> — پاک‌کردن اخطارها\n"
        "<code>/سکوت ۱۰</code> — سکوت کاربر به مدت ۱۰ دقیقه\n"
        "<code>/رفع_سکوت</code> — رفع سکوت\n"
        "<code>/غیرفعال_محافظ</code> — خاموش‌کردن محافظ همین گروه\n\n"
        "🤖 <b>۲) فعال‌سازی هوش مصنوعی در گروه</b>\n"
        "• از «تنظیمات گروه ← راه‌اندازی و تنظیمات هوش مصنوعی»، ارائه‌دهنده و کلید را تنظیم و دستیار را فعال کنید.\n"
        "• نام دستیار را <b>بات ساز</b> بگذارید.\n"
        "• با دکمه «اتصال هوش مصنوعی به گروه» گروه را انتخاب کنید؛ یا داخل گروه بفرستید:\n"
        "<code>/فعال_کردن_دستیار</code>\n"
        "• برای آزمایش، «بات ساز» را بفرستید؛ پس از پاسخ «جانم، سؤالت رو بپرس»، سؤال را ارسال کنید.\n\n"
        "📢 <b>گزارش‌ها</b>\n"
        "برای دریافت گزارش محافظ، انتقال موجودی و بازی، کانال گزارش را فقط از پنل تنظیم کنید. ربات باید در کانال گزارش ادمین و دارای مجوز ارسال پست باشد."
    )


async def _tenant():
    from tenant import get_bot_row, get_setting, set_setting, is_owner_or_admin, smart_cancel_kb
    return get_bot_row, get_setting, set_setting, is_owner_or_admin, smart_cancel_kb


def _fa(s: str) -> str:
    return (s or "").translate(_DIGITS)


def _num(s: str, default: int = 0) -> int:
    try:
        return int(_fa(str(s)).replace(",", "").replace("،", "").strip())
    except Exception:
        return default


def _fmt_user(uid: int | None, username: str | None = None, name: str | None = None) -> str:
    if username:
        return f"@{html.escape(username)} (<code>{uid}</code>)"
    return f"{html.escape(name or str(uid or '—'))} (<code>{uid or '—'}</code>)"


async def tenant_group_log(bot: Bot, row, chat_id: int, kind: str, *, actor: int | None = None,
                          target: int | None = None, details: str = "") -> None:
    """رابط عمومی برای ثبت رویدادهای wallet/betting در لاگ محافظ tenant."""
    await _audit(bot, row, chat_id, kind, actor=actor, target=target, details=details)


async def _get_row(bot: Bot):
    get_bot_row, *_ = await _tenant()
    return await get_bot_row(bot)


async def _get(row_id: int, key: str, default: str = "") -> str:
    _, get_setting, *_ = await _tenant()
    return await get_setting(row_id, key, default)


async def _set(row_id: int, key: str, value: str) -> None:
    *_, set_setting, _, _ = await _tenant()
    await set_setting(row_id, key, value)


async def _is_admin(bot: Bot, chat_id: int, uid: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, uid)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


async def _bot_can_moderate(bot: Bot, chat_id: int) -> tuple[bool, str]:
    """بررسی مجوزهای لازمِ خود ربات برای اجرای عملیات محافظ."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        if member.status == "creator":
            return True, ""
        if member.status != "administrator":
            return False, "❌ برای کارکردن محافظ، ربات باید در همین گروه ادمین باشد."
        if getattr(member, "can_restrict_members", True) is False:
            return False, "❌ ربات ادمین است، اما مجوز محدودکردن/بن‌کردن اعضا را ندارد."
        return True, ""
    except Exception as exc:
        logger.warning("tenant guard bot permission check failed chat=%s: %s", chat_id, exc)
        return False, "❌ وضعیت ادمین ربات در گروه قابل بررسی نیست؛ ربات را ادمین کن و دوباره امتحان کن."


async def _require_group_admin(msg: Message, bot: Bot, *, bot_moderation: bool = False) -> bool:
    """برای جلوگیری از بی‌پاسخ ماندن فرمان، خطای مجوز را به ادمین نشان می‌دهد."""
    if not msg.from_user or not await _is_admin(bot, msg.chat.id, msg.from_user.id):
        await msg.reply("❌ این دستور فقط برای ادمین‌های واقعی همین گروه قابل اجراست.")
        return False
    if bot_moderation:
        ok, reason = await _bot_can_moderate(bot, msg.chat.id)
        if not ok:
            await msg.reply(reason)
            return False
    return True


async def _allowed(row, chat_id: int) -> bool:
    if not row:
        return False
    if (await _get(row.id, "group_guard_enabled", "0")) != "1":
        return False
    configured = (await _get(row.id, "group_guard_chat_id", "")).strip()
    return bool(configured and str(chat_id) == configured)


async def _audit(bot: Bot, row, chat_id: int, kind: str, *, actor: int | None = None,
                 target: int | None = None, details: str = "") -> None:
    try:
        async with Session() as s:
            s.add(TenantGroupEvent(bot_id=row.id, chat_id=chat_id, actor_id=actor,
                                   target_id=target, kind=kind, details=details[:1000]))
            await s.commit()
    except Exception:
        logger.exception("tenant group event save failed")
    channel = (await _get(row.id, "group_guard_log_channel", "")).strip()
    if not channel:
        return
    text = _report_text(chat_id, kind, actor, target, details[:800])
    try:
        await bot.send_message(channel, text, parse_mode="HTML")
    except Exception:
        logger.exception("tenant group log channel send failed: %s", channel)


async def _full_permissions() -> ChatPermissions:
    # Telegram با can_send_messages=True سایر مجوزهای پایه را به حالت مجاز برمی‌گرداند.
    return ChatPermissions(can_send_messages=True)


# === PATCH_GROUP_STATUS_TITLE_V1 ===
# تلگرام فقط برای ادمین‌ها عنوان سفارشی کنار نام را در لیست اعضا نشان می‌دهد.
# طبق مستندات promoteChatMember: «فرستادن False برای همه‌ی مجوزها یعنی
# دی‌پروموت‌کردن کاربر» — پس برای نگه‌داشتن عنوان (بدون دادن هیچ اختیار
# مدیریتی واقعی مثل بن/حذف‌پیام/محدودکردن/دعوت/پین) فقط can_manage_chat روشن
# می‌ماند که صرفاً دسترسی به لاگ رویدادهای گروه است، نه قدرتی روی سایر اعضا.
async def _set_status_title(bot: Bot, chat_id: int, user_id: int, title: str) -> bool:
    try:
        await bot.promote_chat_member(
            chat_id, user_id,
            is_anonymous=False, can_manage_chat=True,
            can_delete_messages=False, can_manage_video_chats=False,
            can_restrict_members=False, can_promote_members=False,
            can_change_info=False, can_invite_users=False,
            can_post_messages=False, can_edit_messages=False, can_pin_messages=False,
        )
        await bot.set_chat_administrator_custom_title(chat_id, user_id, title[:16])
        return True
    except Exception as e:
        _m = str(e).lower()
        if "not enough rights" in _m or "admin_required" in _m or "chat_admin_required" in _m:
            # ربات در این گروه اجازهٔ «افزودنِ ادمین» ندارد — تنظیماتِ گروه است، نه باگ
            logger.warning("group status title: ربات در گروهِ %s اجازهٔ افزودنِ ادمین ندارد", chat_id)
        else:
            logger.exception("tenant group status title set failed chat=%s user=%s", chat_id, user_id)
        return False


async def _clear_status_title(bot: Bot, chat_id: int, user_id: int) -> None:
    """دی‌پروموت واقعی (همه‌ی مجوزها False) — عنوان هم به‌طور خودکار حذف می‌شود."""
    try:
        await bot.promote_chat_member(
            chat_id, user_id,
            is_anonymous=False, can_manage_chat=False,
            can_delete_messages=False, can_manage_video_chats=False,
            can_restrict_members=False, can_promote_members=False,
            can_change_info=False, can_invite_users=False,
            can_post_messages=False, can_edit_messages=False, can_pin_messages=False,
        )
    except Exception:
        logger.exception("tenant group status title clear failed chat=%s user=%s", chat_id, user_id)


async def _refresh_status_title(bot: Bot, row, chat_id: int, user_id: int) -> None:
    """بعد از پایان سکوت، عنوان را بر اساس اخطارهای فعالِ کاربر دوباره تنظیم می‌کند."""
    async with Session() as s:
        warn = await s.scalar(select(TenantGroupWarn).where(
            TenantGroupWarn.bot_id == row.id, TenantGroupWarn.chat_id == chat_id,
            TenantGroupWarn.user_id == user_id))
        count = warn.count if warn else 0
    if count > 0:
        await _set_status_title(bot, chat_id, user_id, f"اخطار {count}")
    else:
        await _clear_status_title(bot, chat_id, user_id)


async def _unmute_later(bot: Bot, chat_id: int, uid: int, seconds: int, row, actor: int):
    try:
        await asyncio.sleep(seconds)
        await bot.restrict_chat_member(chat_id, uid, permissions=await _full_permissions())
        await _refresh_status_title(bot, row, chat_id, uid)
        await _audit(bot, row, chat_id, "unmute_auto", actor=actor, target=uid,
                      details=f"بازگشت خودکار سکوت بعد از {seconds} ثانیه")
    except Exception:
        logger.exception("tenant auto unmute failed")


async def _schedule_unmute(bot: Bot, chat_id: int, uid: int, seconds: int, row, actor: int):
    task = asyncio.create_task(_unmute_later(bot, chat_id, uid, seconds, row, actor))
    _unmute_tasks.add(task)
    task.add_done_callback(_unmute_tasks.discard)


async def _captcha_expire_later(bot: Bot, chat_id: int, uid: int, row, message_id: int, seconds: int):
    try:
        await asyncio.sleep(seconds)
        async with Session() as s:
            cap = await s.scalar(select(TenantGroupCaptcha).where(
                TenantGroupCaptcha.bot_id == row.id,
                TenantGroupCaptcha.chat_id == chat_id,
                TenantGroupCaptcha.user_id == uid).with_for_update())
            if not cap or cap.expires_at > datetime.utcnow():
                return
            await s.delete(cap)
            await s.commit()
        try:
            await bot.ban_chat_member(chat_id, uid)
        except Exception:
            logger.exception("tenant captcha expiry ban failed")
        await _audit(bot, row, chat_id, "captcha_expired_ban", target=uid,
                      details=f"مهلت کپچا تمام شد؛ شناسه پیام: {message_id}")
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("tenant captcha expiry task failed")


async def _schedule_captcha_expiry(bot: Bot, chat_id: int, uid: int, row, message_id: int, seconds: int):
    task = asyncio.create_task(_captcha_expire_later(bot, chat_id, uid, row, message_id, seconds))
    _unmute_tasks.add(task)
    task.add_done_callback(_unmute_tasks.discard)


async def _target(msg: Message):
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target or target.is_bot:
        await msg.reply("❌ این دستور باید با ریپلای روی پیام یک کاربر اجرا شود.")
        return None
    return target


async def _connect_group(msg: Message, bot: Bot, row):
    if not await _is_admin(bot, msg.chat.id, msg.from_user.id):
        return
    await _set(row.id, "group_guard_chat_id", str(msg.chat.id))
    await _set(row.id, "group_guard_enabled", "1")
    await _audit(bot, row, msg.chat.id, "guard_connect", actor=msg.from_user.id,
                  details="گروه به محافظ متصل و محافظ روشن شد")
    await msg.reply("✅ این گروه به محافظ متصل و فعال شد. تنظیمات کامل از پنل ربات قابل مدیریت است.")


_COMMANDS = {
    "/فعال_محافظ", "/غیرفعال_محافظ", "/setguard", "/unsetguard",
    "/بن", "/ban", "/انبن", "/unban", "/اخطار", "/warn", "/رفع_اخطار", "/حذف_اخطار", "/unwarn",
    "/سکوت", "/mute", "/رفع_سکوت", "/unmute", "/راهنمای_محافظ", "/راهنما_محافظ", "/guardhelp",
    "فعال_محافظ", "غیرفعال_محافظ", "بن", "انبن", "اخطار", "رفع_اخطار", "حذف_اخطار", "سکوت", "رفع_سکوت",
    "راهنمای_محافظ", "راهنما_محافظ",
}


def _command(text: str) -> tuple[str | None, str]:
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return None, ""
    token = parts[0].lower().translate(str.maketrans("يىك", "ییک"))
    # Telegram در گروه ممکن است فرمان را به‌صورت /command@botusername بفرستد.
    # username فقط برای مسیریابی تلگرام است و نباید command را ناشناخته کند.
    if token.startswith("/") and "@" in token:
        token = token.split("@", 1)[0]
    return (token if token in _COMMANDS else None), (parts[1].strip() if len(parts) > 1 else "")


async def _command_help(msg: Message, bot: Bot):
    await msg.reply(await _full_help_text(bot), parse_mode="HTML")


@tenant_group_guard_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text)
async def tenant_guard_commands(msg: Message, bot: Bot):
    cmd, arg = _command(msg.text or "")
    if cmd is None:
        raise SkipHandler
    row = await _get_row(bot)
    if not row:
        raise SkipHandler
    if cmd in ("/راهنمای_محافظ", "/راهنما_محافظ", "/guardhelp", "راهنمای_محافظ", "راهنما_محافظ"):
        await _command_help(msg, bot)
        return
    if cmd in ("/فعال_محافظ", "/setguard", "فعال_محافظ"):
        if await _require_group_admin(msg, bot, bot_moderation=True):
            await _connect_group(msg, bot, row)
        return
    if cmd in ("/غیرفعال_محافظ", "/unsetguard", "غیرفعال_محافظ"):
        if await _require_group_admin(msg, bot):
            await _set(row.id, "group_guard_enabled", "0")
            await _audit(bot, row, msg.chat.id, "guard_disconnect", actor=msg.from_user.id, details="محافظ خاموش شد")
            await msg.reply("✅ محافظ این گروه خاموش شد.")
        return
    if (await _get(row.id, "group_guard_enabled", "0")) != "1":
        await msg.reply("❌ محافظ این گروه خاموش است. از تنظیمات گروه آن را روشن کن یا دستور /فعال_محافظ را بفرست.")
        return
    if (str((await _get(row.id, "group_guard_chat_id", "")).strip()) != str(msg.chat.id)):
        await msg.reply("❌ این گروه هنوز به محافظ متصل نشده است. ابتدا دستور /فعال_محافظ را اجرا کن.")
        return
    if not await _require_group_admin(msg, bot, bot_moderation=True):
        return
    target = await _target(msg)
    if cmd in ("/بن", "/ban", "بن"):
        if not target: return
        await bot.ban_chat_member(msg.chat.id, target.id)
        async with Session() as s:
            old = await s.scalar(select(TenantGroupBan).where(TenantGroupBan.bot_id == row.id, TenantGroupBan.chat_id == msg.chat.id, TenantGroupBan.user_id == target.id))
            if old:
                old.full_name, old.username, old.banned_by = target.full_name, target.username, msg.from_user.id
            else:
                s.add(TenantGroupBan(bot_id=row.id, chat_id=msg.chat.id, user_id=target.id, full_name=target.full_name, username=target.username, banned_by=msg.from_user.id, reason=arg or "بن دستی"))
            await s.commit()
        await _audit(bot, row, msg.chat.id, "ban", actor=msg.from_user.id, target=target.id, details=arg or "بن دستی")
        await msg.reply("✅ کاربر بن شد.")
        return
    if cmd in ("/انبن", "/unban", "انبن"):
        if not target: return
        await bot.unban_chat_member(msg.chat.id, target.id, only_if_banned=True)
        async with Session() as s:
            await s.execute(delete(TenantGroupBan).where(TenantGroupBan.bot_id == row.id, TenantGroupBan.chat_id == msg.chat.id, TenantGroupBan.user_id == target.id))
            await s.commit()
        await _clear_status_title(bot, msg.chat.id, target.id)
        await _audit(bot, row, msg.chat.id, "unban", actor=msg.from_user.id, target=target.id)
        await msg.reply("✅ بن کاربر برداشته شد.")
        return
    if cmd in ("/رفع_اخطار", "/حذف_اخطار", "/unwarn", "رفع_اخطار", "حذف_اخطار"):
        if not target: return
        async with Session() as s:
            warn = await s.scalar(select(TenantGroupWarn).where(TenantGroupWarn.bot_id == row.id, TenantGroupWarn.chat_id == msg.chat.id, TenantGroupWarn.user_id == target.id))
            if warn: warn.count = 0
            await s.commit()
        await _clear_status_title(bot, msg.chat.id, target.id)
        await _audit(bot, row, msg.chat.id, "unwarn", actor=msg.from_user.id, target=target.id)
        await msg.reply("✅ اخطارهای کاربر صفر شد.")
        return
    if cmd in ("/سکوت", "/mute", "سکوت"):
        if not target: return
        minutes = _num(arg, _num(await _get(row.id, "group_guard_mute_default_min", "10"), 10))
        minutes = max(1, min(minutes, 10080))
        await bot.restrict_chat_member(msg.chat.id, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.utcnow() + timedelta(minutes=minutes))
        await _schedule_unmute(bot, msg.chat.id, target.id, minutes * 60, row, msg.from_user.id)
        await _set_status_title(bot, msg.chat.id, target.id, "سکوت")
        await _audit(bot, row, msg.chat.id, "mute", actor=msg.from_user.id, target=target.id, details=f"{minutes} دقیقه")
        await msg.reply(f"✅ کاربر برای {minutes} دقیقه ساکت شد.")
        return
    if cmd in ("/رفع_سکوت", "/unmute", "رفع_سکوت"):
        if not target: return
        await bot.restrict_chat_member(msg.chat.id, target.id, permissions=await _full_permissions())
        await _refresh_status_title(bot, row, msg.chat.id, target.id)
        await _audit(bot, row, msg.chat.id, "unmute", actor=msg.from_user.id, target=target.id)
        await msg.reply("✅ سکوت کاربر برداشته شد.")
        return
    if cmd in ("/اخطار", "/warn", "اخطار"):
        if not target: return
        threshold = max(1, min(_num(await _get(row.id, "group_guard_warn_threshold", "3"), 3), 20))
        async with Session() as s:
            warn = await s.scalar(select(TenantGroupWarn).where(TenantGroupWarn.bot_id == row.id, TenantGroupWarn.chat_id == msg.chat.id, TenantGroupWarn.user_id == target.id).with_for_update())
            if not warn:
                warn = TenantGroupWarn(bot_id=row.id, chat_id=msg.chat.id, user_id=target.id, full_name=target.full_name, username=target.username, count=0)
                s.add(warn)
            warn.count = (warn.count or 0) + 1
            count = warn.count
            await s.commit()
        if count < threshold:
            await _set_status_title(bot, msg.chat.id, target.id, f"اخطار {count}")
        await _audit(bot, row, msg.chat.id, "warn", actor=msg.from_user.id, target=target.id, details=f"{count}/{threshold} — {arg or 'اخطار دستی'}")
        if count >= threshold:
            await bot.ban_chat_member(msg.chat.id, target.id)
            async with Session() as s:
                s.add(TenantGroupBan(bot_id=row.id, chat_id=msg.chat.id, user_id=target.id, full_name=target.full_name, username=target.username, banned_by=msg.from_user.id, reason=f"رسیدن به سقف اخطار {threshold}"))
                await s.commit()
            await _audit(bot, row, msg.chat.id, "auto_ban", actor=msg.from_user.id, target=target.id, details=f"سقف اخطار {threshold}")
            await msg.reply(f"🚫 کاربر به {count}/{threshold} اخطار رسید و بن شد.")
        else:
            await msg.reply(f"⚠️ اخطار ثبت شد: {count}/{threshold}")
        return


@tenant_group_guard_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.new_chat_members)
async def tenant_guard_new_members(msg: Message, bot: Bot):
    row = await _get_row(bot)
    if not row or not await _allowed(row, msg.chat.id):
        raise SkipHandler
    captcha_on = (await _get(row.id, "group_guard_captcha_enabled", "0")) == "1"
    welcome_on = (await _get(row.id, "group_guard_welcome_enabled", "0")) == "1"
    for member in msg.new_chat_members or []:
        if member.is_bot:
            continue
        # === PATCH_WELCOME_INDEPENDENT_OF_CAPTCHA_V1 ===
        # قبلاً خوش‌آمد داخلِ elif بود، پس وقتی کپچا هم روشن بود اصلاً
        # چک نمی‌شد و هیچ‌وقت فرستاده نمی‌شد. این دو باید مستقل باشند.
        if welcome_on:
            welcome = await _get(row.id, "group_guard_welcome_text", "👋 خوش آمدی {name}!")
            try:
                await msg.answer(html.escape(welcome.replace("{name}", member.full_name)), parse_mode="HTML")
            except Exception:
                logger.exception("ارسالِ پیامِ خوش‌آمد ناموفق بود")
        if captcha_on:
            timeout = max(60, min(_num(await _get(row.id, "group_guard_captcha_timeout_sec", "300"), 300), 3600))
            try:
                await bot.restrict_chat_member(msg.chat.id, member.id, permissions=ChatPermissions(can_send_messages=False))
                async with Session() as s:
                    old = await s.scalar(select(TenantGroupCaptcha).where(TenantGroupCaptcha.bot_id == row.id, TenantGroupCaptcha.chat_id == msg.chat.id, TenantGroupCaptcha.user_id == member.id))
                    if old:
                        old.expires_at = datetime.utcnow() + timedelta(seconds=timeout)
                    else:
                        s.add(TenantGroupCaptcha(bot_id=row.id, chat_id=msg.chat.id, user_id=member.id, message_id=0, full_name=member.full_name, expires_at=datetime.utcnow() + timedelta(seconds=timeout)))
                    await s.commit()
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ من ربات نیستم", callback_data=f"tgg:captcha:{member.id}")]])
                sent = await msg.answer(f"👋 {html.escape(member.full_name)} عزیز، برای ادامهٔ حضور روی دکمهٔ زیر بزن.\n⏱ مهلت: {timeout // 60} دقیقه", reply_markup=kb, parse_mode="HTML")
                async with Session() as s:
                    cap = await s.scalar(select(TenantGroupCaptcha).where(TenantGroupCaptcha.bot_id == row.id, TenantGroupCaptcha.chat_id == msg.chat.id, TenantGroupCaptcha.user_id == member.id))
                    if cap: cap.message_id = sent.message_id
                    await s.commit()
                await _audit(bot, row, msg.chat.id, "captcha_required", target=member.id, details=f"مهلت {timeout} ثانیه")
                await _schedule_captcha_expiry(bot, msg.chat.id, member.id, row, sent.message_id, timeout)
            except Exception:
                logger.exception("tenant captcha setup failed")
    raise SkipHandler


@tenant_group_guard_router.callback_query(F.data.startswith("tgg:captcha:"))
async def tenant_captcha_pass(cb: CallbackQuery, bot: Bot):
    row = await _get_row(bot)
    uid = _num(cb.data.rsplit(":", 1)[1])
    if not row or uid != cb.from_user.id or not cb.message:
        await cb.answer("این کپچا برای شما نیست.", show_alert=True)
        return
    chat_id = cb.message.chat.id
    async with Session() as s:
        cap = await s.scalar(select(TenantGroupCaptcha).where(TenantGroupCaptcha.bot_id == row.id, TenantGroupCaptcha.chat_id == chat_id, TenantGroupCaptcha.user_id == uid).with_for_update())
        if not cap or cap.expires_at <= datetime.utcnow():
            await cb.answer("مهلت کپچا تمام شده است.", show_alert=True)
            return
        await s.delete(cap)
        await s.commit()
    await bot.restrict_chat_member(chat_id, uid, permissions=await _full_permissions())
    await _audit(bot, row, chat_id, "captcha_pass", target=uid, details="عضو کپچا را با موفقیت گذراند")
    try:
        await cb.message.edit_text("✅ کپچا با موفقیت انجام شد. خوش آمدی!")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("تأیید شد")


@tenant_group_guard_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text)
async def tenant_guard_filters(msg: Message, bot: Bot):
    row = await _get_row(bot)
    if not row or not await _allowed(row, msg.chat.id) or not msg.from_user or msg.from_user.is_bot:
        raise SkipHandler
    if await _is_admin(bot, msg.chat.id, msg.from_user.id):
        raise SkipHandler
    now = datetime.now()
    night_on = (await _get(row.id, "group_guard_night_enabled", "0")) == "1"
    if night_on:
        start, end = await _get(row.id, "group_guard_night_start", "00:00"), await _get(row.id, "group_guard_night_end", "07:00")
        try:
            sh, sm = [int(x) for x in start.split(":")]
            eh, em = [int(x) for x in end.split(":")]
            cur = now.hour * 60 + now.minute
            a, b = sh * 60 + sm, eh * 60 + em
            active = cur >= a or cur < b if a >= b else a <= cur < b
            if active:
                await bot.delete_message(msg.chat.id, msg.message_id)
                await _audit(bot, row, msg.chat.id, "night_delete", actor=msg.from_user.id, details=f"بازه {start}-{end}")
                return
        except Exception:
            logger.exception("tenant night mode check failed")
    anti = (await _get(row.id, "group_guard_antipromo_enabled", "0")) == "1"
    if anti and _URL_RE.search(msg.text or ""):
        await bot.delete_message(msg.chat.id, msg.message_id)
        await _audit(bot, row, msg.chat.id, "antipromo_delete", actor=msg.from_user.id, details=(msg.text or "")[:700])
        return
    flood_on = (await _get(row.id, "group_guard_flood_enabled", "0")) == "1"
    if flood_on:
        max_msgs = max(2, min(_num(await _get(row.id, "group_guard_flood_max_msgs", "10"), 10), 100))
        window = max(3, min(_num(await _get(row.id, "group_guard_flood_window_sec", "10"), 10), 300))
        key = (row.id, msg.chat.id, msg.from_user.id)
        q = _flood[key]
        ts = asyncio.get_running_loop().time()
        q.append(ts)
        while q and ts - q[0] > window:
            q.popleft()
        if len(q) >= max_msgs:
            q.clear()
            mute_min = max(1, min(_num(await _get(row.id, "group_guard_flood_mute_min", "5"), 5), 1440))
            await bot.delete_message(msg.chat.id, msg.message_id)
            await bot.restrict_chat_member(msg.chat.id, msg.from_user.id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.utcnow() + timedelta(minutes=mute_min))
            await _schedule_unmute(bot, msg.chat.id, msg.from_user.id, mute_min * 60, row, msg.from_user.id)
            await _audit(bot, row, msg.chat.id, "flood_mute", actor=msg.from_user.id, target=msg.from_user.id, details=f"{max_msgs} پیام در {window} ثانیه")
            return
    raise SkipHandler


class TggSettingsFSM(StatesGroup):
    log_channel = State()
    welcome_text = State()
    night = State()
    mute_default = State()
    flood = State()
    captcha_timeout = State()
    warn_threshold = State()


async def _owner(cb: CallbackQuery, bot: Bot):
    get_bot_row, _, _, is_owner_or_admin, _ = await _tenant()
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("فقط مالک یا ادمین ربات", show_alert=True)
        return None
    return row


async def _menu_data(row_id: int):
    vals = {}
    for k, d in {
        "enabled": "0", "log": "", "welcome": "0", "night": "0", "mute": "10", "flood": "0",
        "fmax": "10", "fwindow": "10", "fmute": "5", "captcha": "0", "ctime": "300", "warn": "3", "anti": "0",
    }.items():
        key = {"enabled":"group_guard_enabled", "log":"group_guard_log_channel", "welcome":"group_guard_welcome_enabled", "night":"group_guard_night_enabled", "mute":"group_guard_mute_default_min", "flood":"group_guard_flood_enabled", "fmax":"group_guard_flood_max_msgs", "fwindow":"group_guard_flood_window_sec", "fmute":"group_guard_flood_mute_min", "captcha":"group_guard_captcha_enabled", "ctime":"group_guard_captcha_timeout_sec", "warn":"group_guard_warn_threshold", "anti":"group_guard_antipromo_enabled"}[k]
        vals[k] = await _get(row_id, key, d)
    return vals


async def _guard_menu(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    v = await _menu_data(row.id)
    cap_min = max(1, _num(v["ctime"], 300) // 60)
    text = (
        "🛡 <b>محافظ گروه و گزارش‌ها</b>\n\n"
        f"وضعیت محافظ: {'🟢 روشن' if v['enabled']=='1' else '🔴 خاموش'}\n"
        f"کانال گزارش: {html.escape(v['log']) if v['log'] else 'تنظیم نشده'}\n"
        f"خوش‌آمد: {'🟢 روشن' if v['welcome']=='1' else '🔴 خاموش'} | حالت شب: {'🟢 روشن' if v['night']=='1' else '🔴 خاموش'}\n"
        f"کپچای عضو جدید: {'🟢 روشن' if v['captcha']=='1' else '🔴 خاموش'} ({cap_min} دقیقه)\n"
        f"محدودیت ارسال مکرر: {'🟢 روشن' if v['flood']=='1' else '🔴 خاموش'} — {v['fmax']} پیام/{v['fwindow']} ثانیه\n"
        f"حذف لینک و تبلیغ: {'🟢 روشن' if v['anti']=='1' else '🔴 خاموش'}\n"
        f"سکوت پیش‌فرض: {v['mute']} دقیقه | سقف اخطار: {v['warn']}\n\n"
        "🚀 <b>شروع سریع</b>\n"
        "۱) ربات و حساب اجراکننده را در گروه ادمین کنید.\n"
        "۲) داخل گروه بفرستید: <code>/فعال_محافظ</code>\n"
        "۳) کانال گزارش را متصل و سپس تنظیمات دلخواه را فعال کنید."
    )
    rows = [
        [hx_button("🔴 خاموش کردن" if v["enabled"] == "1" else "🟢 روشن کردن", callback_data="tgg:toggle", style=ButtonStyle.DANGER if v["enabled"] == "1" else ButtonStyle.SUCCESS)],
        [hx_button("🔗 راهنمای اتصال همین گروه", callback_data="tgg:connect")],
        [hx_button("📢 تنظیم کانال گزارش‌ها", callback_data="tgg:log")],
        [hx_button("👋 پیام خوش‌آمد", callback_data="tgg:welcome"), hx_button("🌙 حالت شب", callback_data="tgg:night")],
        [hx_button("🔇 سکوت پیش‌فرض", callback_data="tgg:mute"), hx_button("🌊 محدودیت فلود", callback_data="tgg:flood")],
        [hx_button("🧩 کپچای عضو جدید", callback_data="tgg:captcha"), hx_button("⏱ مهلت کپچا", callback_data="tgg:captcha_time")],
        [hx_button("🚫 ضدلینک/تبلیغ", callback_data="tgg:anti")],
        [hx_button("⚠️ سقف خطا", callback_data="tgg:warn")],
        [hx_button("🚫 لیست بن‌شده‌ها", callback_data="tgg:bans"), hx_button("⚠️ لیست اخطارها", callback_data="tgg:warns")],
        [hx_button("📚 راهنمای کامل فعال‌سازی و دستورات", callback_data="tgg:help")],
        [hx_button("⬅️ بازگشت به تنظیمات گروه", callback_data="tadm:groupsettings", style=ButtonStyle.DANGER)],
    ]
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@tenant_group_guard_router.callback_query(F.data == "tgg:menu")
async def tgg_menu(cb: CallbackQuery, bot: Bot):
    await _guard_menu(cb, bot)


@tenant_group_guard_router.callback_query(F.data == "tgg:toggle")
async def tgg_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row: return
    cur = (await _get(row.id, "group_guard_enabled", "0")) == "1"
    if not cur and not (await _get(row.id, "group_guard_chat_id", "")).strip():
        await cb.answer("ابتدا همین گروه را با /فعال_محافظ وصل کن.", show_alert=True)
        return
    await _set(row.id, "group_guard_enabled", "0" if cur else "1")
    await cb.answer("✅ ذخیره شد")
    await _guard_menu(cb, bot)


@tenant_group_guard_router.callback_query(F.data == "tgg:connect")
async def tgg_connect(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row: return
    await cb.answer("راهنمای اتصال ارسال شد")
    await cb.message.answer(
        "🔗 <b>اتصال محافظ به همین گروه</b>\n\n"
        "۱) ربات را در گروه ادمین کنید و مجوزهای حذف پیام و محدودکردن اعضا را بدهید.\n"
        "۲) حسابی که دستور را می‌فرستد نیز باید ادمین همان گروه باشد.\n"
        "۳) داخل خود گروه این دستور را بفرستید:\n\n"
        "<code>/فعال_محافظ</code>\n\n"
        "پس از دریافت پیام تأیید، همین پنل را باز کنید و کانال گزارش و سایر امکانات را تنظیم کنید.",
        parse_mode="HTML")


async def _state_start(cb: CallbackQuery, bot: Bot, state: FSMContext, state_obj: State, prompt: str):
    row = await _owner(cb, bot)
    if not row: return
    await state.set_state(state_obj)
    _, _, _, _, smart_cancel_kb = await _tenant()
    await cb.message.answer(prompt, reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@tenant_group_guard_router.callback_query(F.data == "tgg:log")
async def tgg_log_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _state_start(cb, bot, state, TggSettingsFSM.log_channel, "📢 آیدی یا @username کانال گزارشات را بفرست. ربات باید در کانال ادمین باشد:")


@tenant_group_guard_router.callback_query(F.data == "tgg:welcome")
async def tgg_welcome_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _state_start(cb, bot, state, TggSettingsFSM.welcome_text, "👋 متن خوش‌آمد را بفرست. برای خاموش‌کردن «-» و برای نام عضو از {name} استفاده کن:")


@tenant_group_guard_router.callback_query(F.data == "tgg:night")
async def tgg_night_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _state_start(cb, bot, state, TggSettingsFSM.night, "🌙 بازهٔ حالت شب را مثل این بفرست: 23:00-07:00\nبرای خاموش‌کردن: 0")


@tenant_group_guard_router.callback_query(F.data == "tgg:mute")
async def tgg_mute_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _state_start(cb, bot, state, TggSettingsFSM.mute_default, "🔇 مدت پیش‌فرض سکوت را به دقیقه بفرست (۱ تا ۱۴۴۰):")


@tenant_group_guard_router.callback_query(F.data == "tgg:flood")
async def tgg_flood_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _state_start(cb, bot, state, TggSettingsFSM.flood, "🌊 سه مقدار را با اسلش بفرست: تعداد/بازه‌ثانیه/مدت‌سکوت\nمثال: 10/10/5\nبرای خاموش‌کردن: 0")


@tenant_group_guard_router.callback_query(F.data == "tgg:captcha_time")
async def tgg_captcha_time_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _state_start(cb, bot, state, TggSettingsFSM.captcha_timeout, "⏱ مهلت کپچا را به دقیقه بفرست (۱ تا ۶۰):")


@tenant_group_guard_router.callback_query(F.data == "tgg:captcha")
async def tgg_captcha_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row: return
    cur = (await _get(row.id, "group_guard_captcha_enabled", "0")) == "1"
    await _set(row.id, "group_guard_captcha_enabled", "0" if cur else "1")
    await cb.answer("✅ ذخیره شد")
    await _guard_menu(cb, bot)


@tenant_group_guard_router.callback_query(F.data == "tgg:anti")
async def tgg_anti_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row: return
    cur = (await _get(row.id, "group_guard_antipromo_enabled", "0")) == "1"
    await _set(row.id, "group_guard_antipromo_enabled", "0" if cur else "1")
    await cb.answer("✅ ذخیره شد")
    await _guard_menu(cb, bot)


@tenant_group_guard_router.callback_query(F.data == "tgg:warn")
async def tgg_warn_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _state_start(cb, bot, state, TggSettingsFSM.warn_threshold, "⚠️ سقف اخطار را بفرست (۱ تا ۲۰):")


@tenant_group_guard_router.message(TggSettingsFSM.log_channel, F.text)
async def tgg_log_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _get_row(bot)
    if not row:
        await state.clear()
        return
    value = (msg.text or "").strip()
    if value in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        return
    # لینک عمومی t.me را هم به username قابل‌فهم برای Telegram تبدیل کن.
    # برای کانال خصوصی همچنان باید شناسهٔ عددی -100... ارسال شود.
    if value.startswith("https://t.me/") or value.startswith("http://t.me/"):
        value = "@" + value.split("/", 3)[-1].split("?", 1)[0].strip("/")
    elif value.startswith("t.me/"):
        value = "@" + value.split("/", 1)[1].split("?", 1)[0].strip("/")
    try:
        chat = await bot.get_chat(value)
        if chat.type != "channel":
            await msg.answer("❌ باید آیدی یا username یک کانال باشد.")
            return
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
        if me.status not in ("administrator", "creator"):
            await msg.answer("❌ ربات در کانال ادمین نیست.")
            return
        if me.status == "administrator" and getattr(me, "can_post_messages", True) is False:
            await msg.answer("❌ ربات ادمین است، اما مجوز ارسال پست در کانال را ندارد.")
            return
        await _set(row.id, "group_guard_log_channel", str(chat.id))
        await state.clear()
        await msg.answer(f"✅ کانال گزارشات وصل شد: <code>{chat.id}</code>", parse_mode="HTML")
    except Exception as exc:
        logger.exception("tenant log channel validation failed")
        await msg.answer(f"❌ کانال قابل دسترسی نیست یا ربات ادمین نیست.\n<code>{html.escape(str(exc)[:250])}</code>", parse_mode="HTML")


@tenant_group_guard_router.message(TggSettingsFSM.welcome_text, F.text)
async def tgg_welcome_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _get_row(bot); await state.clear()
    if not row: return
    value = (msg.text or "").strip()
    await _set(row.id, "group_guard_welcome_enabled", "0" if value == "-" else "1")
    await _set(row.id, "group_guard_welcome_text", "" if value == "-" else value[:1000])
    await msg.answer("✅ پیام خوش‌آمد ذخیره شد.")


@tenant_group_guard_router.message(TggSettingsFSM.night, F.text)
async def tgg_night_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _get_row(bot); await state.clear()
    if not row: return
    value = (msg.text or "").strip()
    if value == "0":
        await _set(row.id, "group_guard_night_enabled", "0")
        await msg.answer("✅ حالت شب خاموش شد."); return
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", value):
        await msg.answer("❌ قالب درست: 23:00-07:00"); return
    start, end = value.split("-")
    await _set(row.id, "group_guard_night_start", start); await _set(row.id, "group_guard_night_end", end); await _set(row.id, "group_guard_night_enabled", "1")
    await msg.answer("✅ حالت شب ذخیره و روشن شد.")


@tenant_group_guard_router.message(TggSettingsFSM.mute_default, F.text)
async def tgg_mute_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _get_row(bot); await state.clear()
    v = _num(msg.text or "")
    if not row or not 1 <= v <= 1440: await msg.answer("❌ عدد باید بین ۱ تا ۱۴۴۰ باشد."); return
    await _set(row.id, "group_guard_mute_default_min", str(v)); await msg.answer("✅ مدت سکوت پیش‌فرض ذخیره شد.")


@tenant_group_guard_router.message(TggSettingsFSM.flood, F.text)
async def tgg_flood_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _get_row(bot); await state.clear()
    value = (msg.text or "").strip()
    if value == "0": await _set(row.id, "group_guard_flood_enabled", "0"); await msg.answer("✅ فلود خاموش شد."); return
    try:
        a, b, c = [int(_fa(x)) for x in value.split("/")]
        if not (2 <= a <= 100 and 3 <= b <= 300 and 1 <= c <= 1440): raise ValueError
    except Exception:
        await msg.answer("❌ قالب درست: تعداد/بازه‌ثانیه/مدت‌سکوت؛ مثال 10/10/5"); return
    await _set(row.id, "group_guard_flood_max_msgs", str(a)); await _set(row.id, "group_guard_flood_window_sec", str(b)); await _set(row.id, "group_guard_flood_mute_min", str(c)); await _set(row.id, "group_guard_flood_enabled", "1")
    await msg.answer("✅ محدودیت فلود ذخیره و روشن شد.")


@tenant_group_guard_router.message(TggSettingsFSM.captcha_timeout, F.text)
async def tgg_captcha_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _get_row(bot); await state.clear()
    v = _num(msg.text or "")
    if not row or not 1 <= v <= 60: await msg.answer("❌ مهلت کپچا باید بین ۱ تا ۶۰ دقیقه باشد."); return
    await _set(row.id, "group_guard_captcha_timeout_sec", str(v * 60)); await _set(row.id, "group_guard_captcha_enabled", "1"); await msg.answer("✅ مهلت کپچا ذخیره شد و کپچا روشن است.")


@tenant_group_guard_router.message(TggSettingsFSM.warn_threshold, F.text)
async def tgg_warn_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _get_row(bot); await state.clear()
    v = _num(msg.text or "")
    if not row or not 1 <= v <= 20: await msg.answer("❌ سقف اخطار باید بین ۱ تا ۲۰ باشد."); return
    await _set(row.id, "group_guard_warn_threshold", str(v)); await msg.answer("✅ سقف اخطار ذخیره شد.")


@tenant_group_guard_router.callback_query(F.data == "tgg:help")
async def tgg_help(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row: return
    await cb.message.answer(await _full_help_text(bot), parse_mode="HTML")
    await cb.answer()


async def _list_screen(cb: CallbackQuery, bot: Bot, kind: str):
    row = await _owner(cb, bot)
    if not row: return
    chat_id = (await _get(row.id, "group_guard_chat_id", "")).strip()
    if not chat_id:
        await cb.answer("ابتدا گروه را وصل کن.", show_alert=True); return
    chat = int(chat_id)
    async with Session() as s:
        if kind == "ban":
            rows = (await s.scalars(select(TenantGroupBan).where(TenantGroupBan.bot_id == row.id, TenantGroupBan.chat_id == chat).order_by(TenantGroupBan.id.desc()).limit(20))).all()
            text = "🚫 <b>لیست بن‌شده‌ها</b>\n\n" + ("\n".join(f"{i+1}. {_fmt_user(x.user_id, x.username, x.full_name)} — {html.escape(x.reason or '')}" for i, x in enumerate(rows)) or "لیست خالی است.")
        else:
            rows = (await s.scalars(select(TenantGroupWarn).where(TenantGroupWarn.bot_id == row.id, TenantGroupWarn.chat_id == chat, TenantGroupWarn.count > 0).order_by(TenantGroupWarn.count.desc()).limit(20))).all()
            text = "⚠️ <b>لیست اخطارها</b>\n\n" + ("\n".join(f"{i+1}. {_fmt_user(x.user_id, x.username, x.full_name)} — {x.count} اخطار" for i, x in enumerate(rows)) or "لیست خالی است.")
    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()


@tenant_group_guard_router.callback_query(F.data == "tgg:bans")
async def tgg_bans(cb: CallbackQuery, bot: Bot): await _list_screen(cb, bot, "ban")


@tenant_group_guard_router.callback_query(F.data == "tgg:warns")
async def tgg_warns(cb: CallbackQuery, bot: Bot): await _list_screen(cb, bot, "warn")
