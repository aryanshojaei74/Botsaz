"""
tenant/bots/giveaway/common.py — ابزارهایِ مشترکِ نمایش/ارسال (PATCH_GIVEAWAY_BOT_V1).

قاعده‌هایِ UI: هر صفحه با ادیتِ همان پیام (edit_text) عوض می‌شود، نه ارسال+حذف؛ هر صفحه دکمهٔ بازگشت دارد.
"""
from __future__ import annotations

import datetime as _dt
import html
from datetime import datetime

from aiogram import Bot
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from models import Giveaway, GiveawayEntry, GiveawayPrize, GiveawayWinner

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

try:
    import jdatetime as _jdt
except ImportError:      # pragma: no cover
    _jdt = None

FIELD_KEY = "giveaway"
_DIGITS = "۰۱۲۳۴۵۶۷۸۹"


def fa(n) -> str:
    return str(n).translate(str.maketrans("0123456789", _DIGITS))


def esc(s) -> str:
    return html.escape(str(s or ""))


def btn(text: str, cb: str | None = None, url: str | None = None, style=None) -> InlineKeyboardButton:
    kw: dict = {"text": text}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = cb or "noop"
    if style is not None:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def back_row(cb: str = "gw:list", label: str = "⬅️ بازگشت") -> list:
    return [btn(label, cb, style=ButtonStyle.DANGER)]


HOME_ROW = [btn("🏠 منوی اصلی", "mbtn:home", style=ButtonStyle.DANGER)]


def fmt_time(dt: datetime | None) -> str:
    """UTC ⇒ تاریخ/ساعتِ شمسیِ تهران."""
    if dt is None:
        return "—"
    local = dt + _dt.timedelta(hours=3, minutes=30)
    if _jdt is not None:
        try:
            return fa(_jdt.datetime.fromgregorian(datetime=local).strftime("%Y/%m/%d — %H:%M"))
        except Exception:      # noqa: BLE001
            logger.debug("giveaway: jalali format failed")
    return fa(local.strftime("%Y/%m/%d — %H:%M"))


def time_left(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "تا زمانی که مدیر پایان دهد"
    secs = int((dt - (now or datetime.utcnow())).total_seconds())
    if secs <= 0:
        return "در حالِ پایان"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{fa(d)} روز")
    if h:
        parts.append(f"{fa(h)} ساعت")
    if m and not d:
        parts.append(f"{fa(m)} دقیقه")
    return " و ".join(parts) or "کمتر از یک دقیقه"


_MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}


def medal(rank: int) -> str:
    return _MEDAL.get(rank, "🏅")


def prize_lines(prizes: list[GiveawayPrize]) -> str:
    if not prizes:
        return "—"
    return "\n".join(f"{medal(p.rank)} برنده {fa(p.rank)}: <b>{esc(p.title)}</b>" for p in prizes)


async def bot_username(bot: Bot) -> str:
    cached = getattr(bot, "_gw_username", None)
    if cached:
        return cached
    me = await bot.get_me()
    try:
        bot._gw_username = me.username        # type: ignore[attr-defined]
    except Exception:      # noqa: BLE001
        logger.debug("giveaway: username cache failed")
    return me.username


async def deep_link(bot: Bot, gid: int, inviter: int | None = None) -> str:
    uname = await bot_username(bot)
    return f"https://t.me/{uname}?start=gw_{gid}" + (f"_{inviter}" if inviter else "")


def parse_payload(payload: str | None) -> tuple[int, int | None] | None:
    """«gw_12» یا «gw_12_345» (بدونِ پیشوندِ start=) ⇒ (gid, inviter)."""
    p = (payload or "").strip()
    if p.startswith("gw_"):
        p = p[3:]
    parts = p.split("_")
    try:
        gid = int(parts[0])
        inv = int(parts[1]) if len(parts) > 1 and parts[1] else None
    except (ValueError, IndexError):
        return None
    return gid, inv


def card_text(g: Giveaway, prizes: list[GiveawayPrize], participants: int, entry: GiveawayEntry | None = None,
              chances_n: int = 0, invited: int = 0, link: str | None = None) -> str:
    st = {"active": "🟢 فعال", "ended": "🏁 پایان‌یافته", "cancelled": "⛔️ لغوشده", "draft": "📝 پیش‌نویس"}.get(g.status, g.status)
    lines = [f"🎰 <b>{esc(g.title)}</b>", ""]
    if g.description:
        lines += [esc(g.description), ""]
    lines += [f"🏆 تعدادِ برنده‌ها: <b>{fa(g.winners_count)}</b> نفر", "", "🎁 <b>جوایز:</b>", prize_lines(prizes), "",
              f"👥 شرکت‌کنندگان: <b>{fa(participants)}</b> نفر", f"📌 وضعیت: {st}"]
    if g.status == "active":
        lines.append(f"⏳ زمانِ باقی‌مانده: <b>{time_left(g.ends_at)}</b>")
        if g.ends_at:
            lines.append(f"🕒 پایان: {fmt_time(g.ends_at)}")
    rules = []
    if g.require_phone:
        rules.append("☎️ تأییدِ شمارهٔ ایرانی")
    if g.captcha:
        rules.append("🧮 پاسخ به یک سؤالِ ریاضیِ ساده")
    if rules and g.status == "active" and entry is None:
        lines += ["", "📋 شرایط: " + "، ".join(rules)]
    if entry is not None:
        lines += ["", "✅ <b>تو در قرعه‌کشی ثبت‌نام کرده‌ای!</b>", f"🎯 شانس‌هایِ تو: <b>{fa(chances_n)}</b>"]
        if g.ref_bonus > 0 and g.status == "active":
            lines.append(f"👥 دعوت‌هایِ موفق: <b>{fa(invited)}</b>")
            if link:
                cap = f" (حداکثر {fa(g.ref_max)} شانسِ اضافه)" if g.ref_max else ""
                lines += ["", f"🔗 لینکِ دعوتِ اختصاصی{cap}:", f"<code>{esc(link)}</code>",
                          f"هر نفری که با لینکِ تو ثبت‌نام کند = {fa(g.ref_bonus)} شانسِ بیشتر!"]
    return "\n".join(lines)


def results_text(g: Giveaway, winners: list[tuple[GiveawayWinner, GiveawayEntry, GiveawayPrize | None]],
                 participants: int, left: int = 0, public: bool = True) -> str:
    lines = [f"🏆 <b>نتایجِ قرعه‌کشی «{esc(g.title)}»</b>", ""]
    if not winners:
        lines.append("برنده‌ای انتخاب نشد (شرکت‌کننده‌ای نبود).")
    for w, e, p in winners:
        who = (f"<code>{esc(e.code)}</code>" if (public and g.hide_winners)
               else (f"{esc(e.name or 'کاربر')}" + (f" (@{esc(e.username)})" if e.username else "") + f" — <code>{e.user_tg}</code>"))
        lines.append(f"{medal(w.rank)} برنده {fa(w.rank)}: {who}" + (f" — {esc(p.title)}" if p else ""))
    lines += ["", f"👥 کلِ شرکت‌کنندگان: <b>{fa(participants)}</b> نفر"]
    if left:
        lines.append(f"❌ حذف‌شده (لفتِ کانال): <b>{fa(left)}</b> نفر")
    if public and g.hide_winners and winners:
        lines += ["", "🪪 شناسهٔ خودت را در پیامِ شخصیِ ربات ببین."]
    return "\n".join(lines)


async def edit_or_send(event, text: str, markup: InlineKeyboardMarkup | None = None, parse_mode: str = "HTML") -> Message | None:
    """CallbackQuery ⇒ ادیتِ همان پیام (و اگر نشد، پیامِ جدید)؛ Message ⇒ پیامِ جدید."""
    if isinstance(event, CallbackQuery) or hasattr(event, "message") and hasattr(event, "data"):
        msg = event.message
        try:
            await msg.edit_text(text, reply_markup=markup, parse_mode=parse_mode, disable_web_page_preview=True)
            return msg
        except Exception as e:      # noqa: BLE001
            if "not modified" in str(e).lower():
                return msg
            logger.debug("giveaway edit failed → send: %s", e)
        return await msg.answer(text, reply_markup=markup, parse_mode=parse_mode, disable_web_page_preview=True)
    return await event.answer(text, reply_markup=markup, parse_mode=parse_mode, disable_web_page_preview=True)


async def edit_by_id(bot: Bot, chat_id: int, message_id: int, text: str, markup=None) -> bool:
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup,
                                    parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception as e:      # noqa: BLE001
        logger.debug("giveaway edit_by_id failed: %s", e)
        return False


async def is_admin(row, uid: int) -> bool:
    from tenant import _is_platform_admin, is_owner_or_admin
    return bool(await is_owner_or_admin(row.id, uid, row.owner_id) or _is_platform_admin(uid))


def parse_hours(text: str | None) -> int | None:
    """«6h»، «۶ ساعت»، «2d»، «1d 12h»، «30m»(⇒۱) یا فقط عدد (ساعت) ⇒ ساعتِ صحیح (۱..۷۲۰)؛ نامعتبر ⇒ None."""
    import re
    t = (text or "").strip().lower().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    t = t.replace("ساعت", "h").replace("روز", "d").replace("hours", "h").replace("hour", "h").replace("days", "d").replace("day", "d")
    t = " ".join(t.split())
    if t.isdigit():
        total = int(t)
    else:
        found = list(re.finditer(r"(\d+)\s*([dh])", t))
        if not found or re.sub(r"(\d+)\s*([dh])", "", t).strip():
            return None
        total = sum(int(m.group(1)) * (24 if m.group(2) == "d" else 1) for m in found)
    return total if 1 <= total <= 720 else None
