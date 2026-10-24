"""گزارش اعضا در گروه محافظ‌شدهٔ ربات اصلی.

=== PATCH_GROUP_REPORT_V1 ===
جریان:
  ۱) یکی از اعضا روی پیام فرد دیگری ریپلای می‌کند و می‌نویسد «گزارش»  (یا «/report دلیل» با دلیل همان‌جا).
  ۲) ربات در گروه تأیید می‌دهد و دکمه‌ها را نشان می‌دهد: «✍️ نوشتن دلیل» / «ارسال بدون دلیل» / «لغو».
     با زدن «نوشتن دلیل»، ربات یک پیام «دلیل را به همین پیام ریپلای کن» می‌فرستد (ForceReply، فقط برای گزارش‌دهنده)
     و گزارش‌دهنده دلیل را همان‌جا می‌نویسد؛ پیام دلیل برای خلوت‌ماندن گروه پاک می‌شود.
  ۳) گزارش با دلیل (یا ۵ دقیقه بعد بدون دلیل) در پیوی مدیر اصلی می‌آید: مشخصات، متن پیام، لینک، دلیل، سوابق، و
     دکمه‌های ⚠️ اخطار / 🔇 سکوت / 🗑 حذف پیام / 🚫 بن (با تأیید) / ✅ رد گزارش — اخطار و بن همان سامانهٔ محافظ گروه
     (شمارندهٔ خطا، سقف و بن خودکار، لاگ) را به کار می‌گیرند.
حفاظ‌ها: فقط در گروه متصل روشن محافظ؛ ادمین/ربات/خود را نمی‌شود گزارش کرد؛ هر نفر ساعتی حداکثر ۵ گزارش؛ گزارش
تکراری همان پیام پذیرفته نمی‌شود؛ هر گزارش فقط یک بار به مدیر می‌رسد. کلید: pset platform_group_report_enabled (پیش‌فرض روشن).
"""
from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle, ChatType
from aiogram.types import (CallbackQuery, ChatPermissions, ForceReply, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, ReplyParameters)
from sqlalchemy import func, select

from config import PLATFORM_ADMIN_ID, Session
from logging_setup import get_logger as _get_logger
from models import GroupGuardBan, GroupGuardWarn, GroupReport
from platform_group_money import _group_allowed, _pset, _reply_target

logger = _get_logger(__name__)

platform_group_report_router = Router(name="platform_group_report")

REASON_WINDOW_SECS = 300          # بعد از این مدت بدون دلیل به مدیر می‌رسد
MAX_REASON = 500
MAX_PER_HOUR = 5                  # گزارش‌های هر نفر در یک ساعت
SNIPPET = 300
ACTION_FA = {"warn": "⚠️ اخطار", "mute": "🔇 سکوت", "del": "🗑 حذف پیام", "ban": "🚫 بن", "ok": "✅ رد شد"}

_ZW = dict.fromkeys(map(ord, "‌‍‎‏"), None)
_SLASH_RE = re.compile(r"^/(?:report|گزارش)(?:@\w+)?\s+(.+)$", re.IGNORECASE | re.DOTALL)
_WORDS = {"گزارش", "report", "/report", "/گزارش"}


def parse_report_command(text: str | None):
    """None = دستور نیست؛ (True, None) = «گزارش»؛ (True, دلیل) = «/report دلیل»."""
    t = (text or "").translate(_ZW).replace("ي", "ی").replace("ك", "ک").strip()
    if t.lower() in _WORDS:
        return (True, None)
    m = _SLASH_RE.match(t)
    if m:
        return (True, m.group(1).strip()[:MAX_REASON])
    return None


def _mention(uid: int, name: str | None, username: str | None) -> str:
    label = html.escape(name or (("@" + username) if username else str(uid)))
    return f'<a href="tg://user?id={uid}">{label}</a>' + (f" (@{html.escape(username)})" if username else "")


def message_link(chat_id: int, message_id: int) -> str | None:
    s = str(chat_id)
    if message_id and s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{message_id}"
    return None


def _acts(row: GroupReport) -> list[str]:
    return [a for a in (row.actions or "").split(",") if a]


# ───────────────────────── مدل: ساخت و ثبت ─────────────────────────

async def create_report(chat_id: int, reporter, target, reply_message_id: int, snippet: str, command_message_id: int):
    """reporter/target = (id, full_name, username). خروجی: (row|None, پیام خطا|None)."""
    since = datetime.utcnow() - timedelta(hours=1)
    async with Session() as s:
        dup = await s.scalar(select(GroupReport.id).where(
            GroupReport.chat_id == chat_id, GroupReport.reporter_id == reporter[0],
            GroupReport.target_message_id == reply_message_id, GroupReport.status != "cancelled"))
        if dup:
            return None, "این پیام را قبلا گزارش کرده‌ای."
        recent = int(await s.scalar(select(func.count(GroupReport.id)).where(
            GroupReport.reporter_id == reporter[0], GroupReport.created_at >= since)) or 0)
        if recent >= MAX_PER_HOUR:
            return None, f"در هر ساعت حداکثر {MAX_PER_HOUR} گزارش می‌توانی بدهی؛ کمی بعد دوباره امتحان کن."
        row = GroupReport(
            chat_id=chat_id, reporter_id=reporter[0], reporter_name=reporter[1] or "", reporter_username=reporter[2] or None,
            target_id=target[0], target_name=target[1] or "", target_username=target[2] or None,
            target_message_id=reply_message_id, message_text=(snippet or "")[:SNIPPET],
            command_message_id=command_message_id, status="draft", created_at=datetime.utcnow())
        s.add(row)
        await s.commit()
        return row, None


async def _stats(row: GroupReport) -> tuple[int, int, bool]:
    async with Session() as s:
        prev = int(await s.scalar(select(func.count(GroupReport.id)).where(
            GroupReport.chat_id == row.chat_id, GroupReport.target_id == row.target_id,
            GroupReport.id != row.id, GroupReport.status.in_(("sent", "closed")))) or 0)
        warns = int(await s.scalar(select(GroupGuardWarn.count).where(
            GroupGuardWarn.chat_id == row.chat_id, GroupGuardWarn.user_id == row.target_id)) or 0)
        banned = bool(await s.scalar(select(GroupGuardBan.id).where(
            GroupGuardBan.chat_id == row.chat_id, GroupGuardBan.user_id == row.target_id)))
    return prev, warns, banned


def admin_text(row: GroupReport, prev: int, warns: int, banned: bool) -> str:
    link = message_link(row.chat_id, row.target_message_id)
    lines = [
        f"🚨 <b>گزارش جدید #{row.id}</b>", "",
        f"👤 گزارش‌شده: {_mention(row.target_id, row.target_name, row.target_username)} — <code>{row.target_id}</code>",
        f"🙋 گزارش‌دهنده: {_mention(row.reporter_id, row.reporter_name, row.reporter_username)} — <code>{row.reporter_id}</code>",
        "", f"💬 پیام گزارش‌شده:\n«{html.escape(row.message_text or '— (بدون متن)')}»",
    ]
    if link:
        lines.append(f'🔗 <a href="{link}">رفتن به پیام در گروه</a>')
    lines += ["", f"📝 دلیل: {html.escape(row.reason) if row.reason else '— (بدون دلیل)'}", "",
              f"📊 گزارش‌های دیگر علیه او: {prev} | اخطارها: {warns} | {'🚫 بن‌شده' if banned else 'عضو فعال'}"]
    done = [ACTION_FA[a] for a in _acts(row) if a in ACTION_FA]
    if done:
        lines += ["", "✅ انجام‌شده: " + "، ".join(done)]
    return "\n".join(lines)


def admin_kb(row: GroupReport, confirm_ban: bool = False) -> InlineKeyboardMarkup | None:
    if row.status == "closed":
        return None
    if confirm_ban:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ بله، بن کن", callback_data=f"grpa:banyes:{row.id}", style=ButtonStyle.DANGER),
            InlineKeyboardButton(text="↩️ نه", callback_data=f"grpa:back:{row.id}")]])
    done = set(_acts(row))
    first = [InlineKeyboardButton(text=ACTION_FA[a], callback_data=f"grpa:{a}:{row.id}") for a in ("warn", "mute") if a not in done]
    second = [InlineKeyboardButton(text=ACTION_FA[a], callback_data=f"grpa:{a}:{row.id}") for a in ("del", "ban") if a not in done]
    rows = [r for r in (first, second) if r]
    rows.append([InlineKeyboardButton(text="✅ رد گزارش / بستن", callback_data=f"grpa:ok:{row.id}", style=ButtonStyle.SUCCESS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def submit_report(rid: int, reason: str | None, bot: Bot) -> GroupReport | None:
    """گزارش را (یک‌بار) برای مدیر می‌فرستد. اگر قبلا ارسال/لغو شده None."""
    async with Session() as s:
        row = await s.get(GroupReport, rid, with_for_update=True)
        if row is None or row.status != "draft":
            return None
        row.reason = (reason or "").strip()[:MAX_REASON] or None
        row.status = "sent"
        row.submitted_at = datetime.utcnow()
        await s.commit()
    prev, warns, banned = await _stats(row)
    try:
        m = await bot.send_message(PLATFORM_ADMIN_ID, admin_text(row, prev, warns, banned), reply_markup=admin_kb(row),
                                   parse_mode="HTML", disable_web_page_preview=True)
    except Exception:  # noqa: BLE001 — مدیر ربات را بلاک کرده یا خطای شبکه؛ گم نشود
        logger.exception("group report #%s: sending to the admin failed", rid)
        async with Session() as s:
            r2 = await s.get(GroupReport, rid)
            r2.status = "failed"
            await s.commit()
        row.status = "failed"
        return row
    async with Session() as s:
        r2 = await s.get(GroupReport, rid)
        r2.admin_message_id = m.message_id
        await s.commit()
    row.admin_message_id = m.message_id
    return row


async def _cleanup_group(bot: Bot, row: GroupReport, final_text: str | None, delete_ids=()) -> None:
    for mid in (row.prompt_message_id, *delete_ids):
        if mid:
            try:
                await bot.delete_message(row.chat_id, mid)
            except Exception as e:  # noqa: BLE001 — پاک‌شده یا بدون دسترسی
                logger.info("group report #%s: delete %s skipped: %s", row.id, mid, e)
    if row.ack_message_id and final_text:
        try:
            await bot.edit_message_text(final_text, chat_id=row.chat_id, message_id=row.ack_message_id, reply_markup=None)
        except Exception as e:  # noqa: BLE001
            logger.info("group report #%s: ack edit skipped: %s", row.id, e)


# ───────────────────────── گروه: دستور و دکمه‌ها ─────────────────────────

def _is_cmd(m: Message) -> bool:
    return parse_report_command(m.text) is not None


@platform_group_report_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_cmd))
async def report_command(msg: Message, bot: Bot):
    if not await _group_allowed(msg.chat.id) or (await _pset("platform_group_report_enabled", "on")) != "on":
        raise SkipHandler
    inline_reason = parse_report_command(msg.text)[1]
    if msg.sender_chat or not msg.from_user:
        await msg.reply("❌ با حالت «ادمین ناشناس» یا هویت کانال نمی‌شود گزارش داد؛ حالت ناشناس را خاموش کن و دوباره بنویس.")
        return
    target = _reply_target(msg)
    if not target:
        await msg.reply("برای گزارش یک نفر، روی پیام همان فرد ریپلای کن و فقط بنویس «گزارش».")
        return
    if target.is_bot or target.id == msg.from_user.id:
        await msg.reply("❌ نمی‌شود خودت یا یک ربات را گزارش کرد.")
        return
    from platform_group_guard import _is_group_admin
    if await _is_group_admin(bot, msg.chat.id, target.id):
        await msg.reply("❌ ادمین‌ها را نمی‌شود با این روش گزارش کرد؛ موضوع را مستقیم به مدیر بگو.")
        return
    r = msg.reply_to_message
    snippet = (r.text or r.caption or "") or f"[{r.content_type}]"
    row, err = await create_report(
        msg.chat.id, (msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or ""),
        (target.id, target.full_name or "", target.username or ""), r.message_id, snippet, msg.message_id)
    if err:
        await msg.reply("❌ " + err)
        return
    if inline_reason:                                   # «/report دلیل»: همان‌جا ثبت می‌شود
        sent = await submit_report(row.id, inline_reason, bot)
        text = "✅ گزارش شما برای مدیر ارسال شد. ممنون 🙏" if sent and sent.status == "sent" \
            else "❌ ارسال گزارش به مدیر انجام نشد؛ کمی بعد دوباره امتحان کن."
        await msg.reply(text)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ نوشتن دلیل گزارش", callback_data=f"grpt:reason:{row.id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📤 ارسال بدون دلیل", callback_data=f"grpt:skip:{row.id}"),
         InlineKeyboardButton(text="❌ لغو", callback_data=f"grpt:cancel:{row.id}", style=ButtonStyle.DANGER)]])
    sent = await msg.reply(
        "✅ گزارش شما ثبت شد.\nبرای اینکه مدیر دلیل را ببیند روی «نوشتن دلیل گزارش» بزن و دلیل را بنویس. "
        f"اگر تا {REASON_WINDOW_SECS // 60} دقیقه ننویسی، گزارش بدون دلیل ارسال می‌شود.\n(فقط گزارش‌دهنده می‌تواند دکمه‌ها را بزند.)",
        reply_markup=kb)
    async with Session() as s:
        r2 = await s.get(GroupReport, row.id)
        r2.ack_message_id = sent.message_id
        await s.commit()


def _rid(cb: CallbackQuery) -> int:
    try:
        return int((cb.data or "").split(":")[2])
    except (IndexError, ValueError):
        return 0


async def _own_draft(cb: CallbackQuery):
    """ردیف گزارش در حال تکمیل که مالکش همین کاربر است؛ وگرنه پاسخ و None."""
    async with Session() as s:
        row = await s.get(GroupReport, _rid(cb))
    if row is None or row.status != "draft":
        await cb.answer("این گزارش دیگر در حال تکمیل نیست.", show_alert=True)
        return None
    if cb.from_user.id != row.reporter_id:
        await cb.answer("فقط گزارش‌دهنده می‌تواند این دکمه را بزند.", show_alert=True)
        return None
    return row


@platform_group_report_router.callback_query(F.data.startswith("grpt:reason:"))
async def report_ask_reason(cb: CallbackQuery, bot: Bot):
    row = await _own_draft(cb)
    if not row:
        return
    if row.prompt_message_id:
        await cb.answer("دلیل را به پیام «دلیل را بنویس» ریپلای کن.", show_alert=True)
        return
    try:
        sent = await bot.send_message(
            row.chat_id, f"✍️ {html.escape(row.reporter_name or 'دوست من')}، دلیل گزارش را همین‌جا به همین پیام ریپلای کن "
                         f"و بفرست (حداکثر {MAX_REASON} کاراکتر).",
            parse_mode="HTML",
            reply_parameters=ReplyParameters(message_id=row.command_message_id or row.ack_message_id,
                                             allow_sending_without_reply=True),
            reply_markup=ForceReply(selective=True, input_field_placeholder="دلیل گزارش…"))
    except Exception:  # noqa: BLE001
        logger.exception("group report #%s: prompt failed", row.id)
        await cb.answer("❌ ارسال پیام ناموفق بود؛ دوباره امتحان کن.", show_alert=True)
        return
    async with Session() as s:
        r2 = await s.get(GroupReport, row.id)
        r2.prompt_message_id = sent.message_id
        await s.commit()
    await cb.answer("دلیل را به پیام جدید ریپلای کن ✍️")


def _is_reply_to_bot(m: Message) -> bool:
    r = m.reply_to_message
    return bool(r and r.from_user and r.from_user.is_bot and m.text)


@platform_group_report_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_reply_to_bot))
async def report_reason_reply(msg: Message, bot: Bot):
    if not msg.from_user:
        raise SkipHandler
    async with Session() as s:
        row = await s.scalar(select(GroupReport).where(
            GroupReport.chat_id == msg.chat.id, GroupReport.prompt_message_id == msg.reply_to_message.message_id,
            GroupReport.status == "draft"))
    if row is None or row.reporter_id != msg.from_user.id:
        raise SkipHandler                                # ریپلای عادی به ربات (مثلا هوش مصنوعی)؛ به بقیه بده
    reason = (msg.text or "").strip()[:MAX_REASON]
    sent = await submit_report(row.id, reason, bot)
    ok = bool(sent and sent.status == "sent")
    await _cleanup_group(bot, row, "✅ گزارش شما برای مدیر ارسال شد. ممنون 🙏" if ok
                         else "❌ ارسال گزارش به مدیر انجام نشد؛ کمی بعد دوباره امتحان کن.",
                         delete_ids=(msg.message_id,))


@platform_group_report_router.callback_query(F.data.startswith("grpt:skip:"))
async def report_skip(cb: CallbackQuery, bot: Bot):
    row = await _own_draft(cb)
    if not row:
        return
    sent = await submit_report(row.id, None, bot)
    ok = bool(sent and sent.status == "sent")
    await _cleanup_group(bot, row, "✅ گزارش شما (بدون دلیل) برای مدیر ارسال شد. ممنون 🙏" if ok
                         else "❌ ارسال گزارش به مدیر انجام نشد؛ کمی بعد دوباره امتحان کن.")
    await cb.answer("ارسال شد" if ok else "ناموفق", show_alert=not ok)


@platform_group_report_router.callback_query(F.data.startswith("grpt:cancel:"))
async def report_cancel(cb: CallbackQuery, bot: Bot):
    row = await _own_draft(cb)
    if not row:
        return
    async with Session() as s:
        r2 = await s.get(GroupReport, row.id, with_for_update=True)
        if r2.status != "draft":
            await s.commit()
            await cb.answer("این گزارش دیگر در حال تکمیل نیست.", show_alert=True)
            return
        r2.status = "cancelled"
        r2.closed_at = datetime.utcnow()
        await s.commit()
    await _cleanup_group(bot, row, "❌ گزارش لغو شد.")
    await cb.answer("لغو شد")


async def report_sweep_once(bot: Bot) -> int:
    """گزارش‌هایی که دلیلشان تا مهلت نیامده بدون دلیل به مدیر می‌رسند."""
    cutoff = datetime.utcnow() - timedelta(seconds=REASON_WINDOW_SECS)
    async with Session() as s:
        rows = (await s.scalars(select(GroupReport).where(
            GroupReport.status == "draft", GroupReport.created_at <= cutoff).limit(50))).all()
    n = 0
    for row in rows:
        sent = await submit_report(row.id, None, bot)
        if sent is None:
            continue
        n += 1
        await _cleanup_group(bot, row, "✅ گزارش شما (بدون دلیل) برای مدیر ارسال شد. ممنون 🙏"
                             if sent.status == "sent" else None)
    return n


async def report_sweep_loop(platform_bot=None, interval_sec: int = 30):
    await asyncio.sleep(20)
    while True:
        try:
            if platform_bot is not None:
                await report_sweep_once(platform_bot)
        except Exception:  # noqa: BLE001
            logger.exception("report_sweep_loop error")
        await asyncio.sleep(interval_sec)


# ───────────────────────── مدیر: اقدام‌ها ─────────────────────────

async def _do_action(bot: Bot, row: GroupReport, act: str, admin_id: int) -> tuple[str, bool]:
    """اقدام را اجرا می‌کند؛ (متن کوتاه نتیجه، آیا کاربر بن شد). در صورت شکست استثنا می‌اندازد (چیزی ثبت نمی‌شود)."""
    from platform_group_guard import _apply_warn, _ban_and_verify, _log_action
    reason = f"گزارش #{row.id}" + (f": {row.reason}" if row.reason else "")
    u = SimpleNamespace(id=row.target_id, full_name=row.target_name or str(row.target_id), username=row.target_username)
    if act == "warn":
        cnt, th, auto = await _apply_warn(bot, row.chat_id, u, reason[:250], admin_id)
        return f"⚠️ اخطار ثبت شد ({cnt}/{th})" + (" و به‌خاطر رسیدن به سقف بن شد" if auto else ""), bool(auto)
    if act == "mute":
        minutes = int(await _pset("group_guard_mute_default_min", "10") or "10")
        await bot.restrict_chat_member(row.chat_id, row.target_id, permissions=ChatPermissions(can_send_messages=False),
                                       until_date=datetime.now() + timedelta(minutes=minutes))
        await _log_action(f"🔇 <b>سکوت</b> ({minutes} دقیقه) از گزارش #{row.id}\n👤 {html.escape(u.full_name)} (<code>{u.id}</code>)")
        return f"🔇 {minutes} دقیقه سکوت شد", False
    if act == "del":
        await bot.delete_message(row.chat_id, row.target_message_id)
        return "🗑 پیام حذف شد", False
    if act == "ban":
        ok, err = await _ban_and_verify(bot, row.chat_id, row.target_id)
        if not ok:
            raise RuntimeError(err or "بن تأیید نشد")
        async with Session() as s:
            b = await s.scalar(select(GroupGuardBan).where(GroupGuardBan.chat_id == row.chat_id, GroupGuardBan.user_id == row.target_id))
            if not b:
                b = GroupGuardBan(chat_id=row.chat_id, user_id=row.target_id)
                s.add(b)
            b.full_name, b.username, b.reason = u.full_name, u.username, reason[:250]
            b.banned_by, b.banned_at = admin_id, datetime.now()
            await s.commit()
        await _log_action(f"🚫 <b>بن</b> از گزارش #{row.id}\n👤 {html.escape(u.full_name)} (<code>{u.id}</code>)")
        return "🚫 بن شد", True
    return "✅ بسته شد", False


async def _render_admin(cb: CallbackQuery, row: GroupReport, confirm_ban: bool = False) -> None:
    prev, warns, banned = await _stats(row)
    try:
        await cb.message.edit_text(admin_text(row, prev, warns, banned), reply_markup=admin_kb(row, confirm_ban),
                                   parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001 — بدون تغییر
        logger.info("group report #%s: admin edit skipped: %s", row.id, e)


@platform_group_report_router.callback_query(F.data.startswith("grpa:"))
async def report_admin_action(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    parts = (cb.data or "").split(":")
    act, rid = parts[1], _rid(cb)
    async with Session() as s:
        row = await s.get(GroupReport, rid, with_for_update=True)
        if row is None or row.status not in ("sent", "closed"):
            await cb.answer("گزارش پیدا نشد.", show_alert=True)
            return
        if row.status == "closed":
            await s.commit()
            await cb.answer("این گزارش بسته شده است.", show_alert=True)
            return
        if act == "ban":                                   # اول تأیید بگیر
            await s.commit()
            await _render_admin(cb, row, confirm_ban=True)
            await cb.answer("مطمئنی؟")
            return
        if act == "back":
            await s.commit()
            await _render_admin(cb, row)
            await cb.answer()
            return
        act = "ban" if act == "banyes" else act
        if act not in ACTION_FA:
            await s.commit()
            await cb.answer()
            return
        done = _acts(row)
        if act in done and act != "ok":
            await s.commit()
            await cb.answer("این اقدام قبلا انجام شده.", show_alert=True)
            return
        try:
            result, banned_now = await _do_action(bot, row, act, cb.from_user.id)
        except Exception as e:  # noqa: BLE001
            logger.exception("group report #%s: action %s failed", rid, act)
            await s.commit()
            await cb.answer(f"❌ انجام نشد: {str(e)[:150]}", show_alert=True)
            return
        done.append(act)
        if banned_now and "ban" not in done:                # اخطار منجر به بن خودکار
            done.append("ban")
        row.actions = ",".join(done)
        row.handled_by = cb.from_user.id
        if act in ("ban", "ok") or "ban" in done:
            row.status, row.closed_at = "closed", datetime.utcnow()
        await s.commit()
    await _render_admin(cb, row)
    await cb.answer(result)
