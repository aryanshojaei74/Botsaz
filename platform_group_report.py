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
=== PATCH_REPORT_ACTION_REASON_V1 === (گزارش حاج‌عمیر/مهدی): مدیر روی «اخطار / سکوت / بن» که بزند، علت را می‌پرسد (ریپلای به
پیامِ «دلیل را بنویس» یا «بدون دلیل»)؛ برایِ سکوت مدت هم انتخاب می‌شود. بعد از اقدام، ربات در همان گروه اعلام می‌کند: چه کسی، چه
اقدامی و چند دقیقه، بابتِ کدام گزارش، علتِ تخلف — و زیرش با دکمه‌هایِ شیشه‌ایِ غیرفعال: اخطارها (n/سقف)، «تا بن چند اخطارِ دیگر» و
تعدادِ گزارش‌هایِ علیه او.
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
MAX_ACTION_REASON = 300
MUTE_CHOICES = (10, 60, 360, 1440)       # دقیقه؛ دکمه‌هایِ انتخابِ مدتِ سکوت
_PENDING: dict[int, dict] = {}           # شناسهٔ پیامِ «دلیل را بنویس» (در پیوی مدیر) → {"rid","act","minutes"}
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


def dur_fa(minutes: int) -> str:
    """۶۰ ⇒ «۶۰ دقیقه»، ۳۶۰ ⇒ «۶ ساعت»، ۲۸۸۰ ⇒ «۲ روز»."""
    from fa_num import fa_digits
    m = int(minutes)
    if m >= 2880 and m % 1440 == 0:
        return f"{fa_digits(m // 1440)} روز"
    if m >= 120 and m % 60 == 0:
        return f"{fa_digits(m // 60)} ساعت"
    return f"{fa_digits(m)} دقیقه"


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
    if row.admin_reason:
        lines.append(f"🗒 علتِ اقدام: {html.escape(row.admin_reason)}")
    return "\n".join(lines)


def admin_kb(row: GroupReport, mode=None) -> InlineKeyboardMarkup | None:
    """mode: None = دکمه‌هایِ اصلی | ("mute",) = انتخابِ مدتِ سکوت | ("reason", act, minutes) = منتظرِ علتِ اقدام."""
    if row.status == "closed":
        return None
    if mode and mode[0] == "mute":
        btns = [InlineKeyboardButton(text="🔇 " + dur_fa(m), callback_data=f"grpa:dur:{row.id}:{m}") for m in MUTE_CHOICES]
        return InlineKeyboardMarkup(inline_keyboard=[btns[:2], btns[2:],
                                                     [InlineKeyboardButton(text="↩️ انصراف", callback_data=f"grpa:back:{row.id}")]])
    if mode and mode[0] == "reason":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⏩ بدون دلیل", callback_data=f"grpa:go:{row.id}:{mode[1]}:{int(mode[2] or 0)}",
                                 style=ButtonStyle.DANGER),
            InlineKeyboardButton(text="↩️ انصراف", callback_data=f"grpa:back:{row.id}")]])
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

async def _do_action(bot: Bot, row: GroupReport, act: str, admin_id: int, note: str | None = None,
                     minutes: int | None = None) -> tuple[str, bool, dict]:
    """اقدام را اجرا می‌کند؛ (متن کوتاه نتیجه، آیا کاربر بن شد، اطلاعاتِ اعلامِ گروه). در شکست استثنا می‌اندازد (چیزی ثبت نمی‌شود)."""
    from platform_group_guard import _apply_warn, _ban_and_verify, _log_action
    why = (note or row.reason or "").strip()
    reason = f"گزارش #{row.id}" + (f": {why}" if why else "")
    u = SimpleNamespace(id=row.target_id, full_name=row.target_name or str(row.target_id), username=row.target_username)
    info: dict = {}
    if act == "warn":
        cnt, th, auto = await _apply_warn(bot, row.chat_id, u, reason[:250], admin_id)
        info.update(cnt=cnt, th=th, auto=bool(auto))
        return f"⚠️ اخطار ثبت شد ({cnt}/{th})" + (" و به‌خاطر رسیدن به سقف بن شد" if auto else ""), bool(auto), info
    if act == "mute":
        minutes = int(minutes or int(await _pset("group_guard_mute_default_min", "10") or "10"))
        await bot.restrict_chat_member(row.chat_id, row.target_id, permissions=ChatPermissions(can_send_messages=False),
                                       until_date=datetime.now() + timedelta(minutes=minutes))
        await _log_action(f"🔇 <b>سکوت</b> ({minutes} دقیقه) از گزارش #{row.id}\n👤 {html.escape(u.full_name)} (<code>{u.id}</code>)"
                          + (f"\n📝 {html.escape(why)}" if why else ""))
        info.update(minutes=minutes)
        return f"🔇 {minutes} دقیقه سکوت شد", False, info
    if act == "del":
        await bot.delete_message(row.chat_id, row.target_message_id)
        return "🗑 پیام حذف شد", False, info
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
        await _log_action(f"🚫 <b>بن</b> از گزارش #{row.id}\n👤 {html.escape(u.full_name)} (<code>{u.id}</code>)"
                          + (f"\n📝 {html.escape(why)}" if why else ""))
        return "🚫 بن شد", True, info
    return "✅ بسته شد", False, info


async def _announce(bot: Bot, row: GroupReport, act: str, why: str, info: dict, banned: bool) -> None:
    """اعلامِ اقدام در گروه + دکمه‌هایِ شیشه‌ایِ غیرفعال (اخطارها / تا بن / تعدادِ گزارش‌ها)."""
    from fa_num import fa_digits
    from premium_emoji import hx_button
    who = _mention(row.target_id, row.target_name, row.target_username)
    if act == "mute":
        head = f"🔇 {who} به مدتِ <b>{dur_fa(info.get('minutes') or 10)}</b> سکوت شد."
    elif act == "warn":
        head = (f"🚫 {who} به‌خاطر رسیدن به سقفِ اخطارها از گروه بن شد." if info.get("auto")
                else f"⚠️ به {who} اخطار داده شد.")
    else:
        head = f"🚫 {who} از گروه بن شد."
    lines = [head, "", f"📌 بابتِ گزارش #{row.id}"]
    if row.message_text:
        lines.append(f"💬 پیام: «{html.escape(row.message_text[:120])}»")
    if why:
        lines.append(f"📝 علتِ تخلف: {html.escape(why)}")
    prev, warns, _b = await _stats(row)
    th = int(await _pset("group_guard_warn_threshold", "3") or "3")
    cnt = int(info.get("cnt") or warns)
    noop = {"callback_data": "grpn:noop", "style": ButtonStyle.PRIMARY}
    rows = [[hx_button(f"⚠️ اخطارها: {fa_digits(cnt)} از {fa_digits(th)}", **noop),
             hx_button(f"📊 گزارش‌ها: {fa_digits(prev + 1)}", **noop)]]
    if banned:
        rows.append([hx_button("🚫 بن‌شده", callback_data="grpn:noop", style=ButtonStyle.DANGER)])
    else:
        left = max(th - cnt, 0)
        rows.append([hx_button(f"⏳ تا بن: {fa_digits(left)} اخطارِ دیگر", callback_data="grpn:noop", style=ButtonStyle.DANGER)])
    try:
        await bot.send_message(row.chat_id, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True,
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                               reply_parameters=ReplyParameters(message_id=row.target_message_id, allow_sending_without_reply=True)
                               if row.target_message_id else None)
    except Exception:  # noqa: BLE001 — اعلام شکست خورد؛ خودِ اقدام انجام شده
        logger.exception("group report #%s: group announcement failed", row.id)


async def _execute(bot: Bot, rid: int, act: str, admin_id: int, note: str | None = None, minutes: int | None = None):
    """اقدام را (اتمیک) اجرا می‌کند. خروجی: (خطا یا None، متنِ نتیجه، ردیفِ به‌روز، آیا alert باشد)."""
    note = (note or "").strip()[:MAX_ACTION_REASON] or None
    async with Session() as s:
        row = await s.get(GroupReport, rid, with_for_update=True)
        if row is None or row.status not in ("sent", "closed"):
            return "گزارش پیدا نشد.", "", None
        if row.status == "closed":
            await s.commit()
            return "این گزارش بسته شده است.", "", row
        if act not in ACTION_FA:
            await s.commit()
            return "", "", row
        done = _acts(row)
        if act in done and act != "ok":
            await s.commit()
            return "این اقدام قبلا انجام شده.", "", row
        try:
            result, banned_now, info = await _do_action(bot, row, act, admin_id, note, minutes)
        except Exception as e:  # noqa: BLE001
            logger.exception("group report #%s: action %s failed", rid, act)
            await s.commit()
            return f"❌ انجام نشد: {str(e)[:150]}", "", row
        done.append(act)
        if banned_now and "ban" not in done:                # اخطار منجر به بن خودکار
            done.append("ban")
        row.actions = ",".join(done)
        row.handled_by = admin_id
        if note:
            row.admin_reason = note
        if act in ("ban", "ok") or "ban" in done:
            row.status, row.closed_at = "closed", datetime.utcnow()
        await s.commit()
    if act in ("warn", "mute", "ban"):
        await _announce(bot, row, act, note or (row.reason or ""), info, banned_now or act == "ban")
    return None, result, row


async def _render_admin(cb: CallbackQuery, row: GroupReport, mode=None, extra: str = "") -> None:
    prev, warns, banned = await _stats(row)
    try:
        await cb.message.edit_text(admin_text(row, prev, warns, banned) + extra, reply_markup=admin_kb(row, mode),
                                   parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001 — بدون تغییر
        logger.info("group report #%s: admin edit skipped: %s", row.id, e)


async def _render_admin_by_id(bot: Bot, row: GroupReport) -> None:
    """بعد از اقدام با ریپلایِ علت (کلیکی در کار نیست): پیامِ گزارشِ مدیر را با شناسه‌اش به‌روز کن."""
    if not row.admin_message_id:
        return
    prev, warns, banned = await _stats(row)
    try:
        await bot.edit_message_text(admin_text(row, prev, warns, banned), chat_id=PLATFORM_ADMIN_ID,
                                    message_id=row.admin_message_id, reply_markup=admin_kb(row),
                                    parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001
        logger.info("group report #%s: admin edit skipped: %s", row.id, e)


async def _drop_pending(bot: Bot, rid: int) -> None:
    """پیام‌هایِ «دلیل را بنویس»ِ باز برایِ این گزارش را پاک و از حافظه برمی‌دارد."""
    for mid, p in [(m, p) for m, p in _PENDING.items() if p.get("rid") == rid]:
        _PENDING.pop(mid, None)
        try:
            await bot.delete_message(PLATFORM_ADMIN_ID, mid)
        except Exception as e:  # noqa: BLE001
            logger.info("group report #%s: prompt delete skipped: %s", rid, e)


async def _ask_reason(cb: CallbackQuery, bot: Bot, row: GroupReport, act: str, minutes: int = 0) -> None:
    await _drop_pending(bot, row.id)
    what = {"warn": "اخطار", "mute": f"سکوتِ {dur_fa(minutes)}" if minutes else "سکوت", "ban": "بن"}[act]
    try:
        sent = await bot.send_message(
            PLATFORM_ADMIN_ID, f"✍️ علتِ {what} را به همین پیام ریپلای کن و بفرست (حداکثر {MAX_ACTION_REASON} کاراکتر).\n"
                               "این علت در گروه هم اعلام می‌شود. اگر نمی‌خواهی علتی بنویسی، روی «⏩ بدون دلیل» بزن.",
            reply_markup=ForceReply(input_field_placeholder="علتِ تخلف…"))
    except Exception:  # noqa: BLE001
        logger.exception("group report #%s: reason prompt failed", row.id)
        await cb.answer("❌ ارسال پیام ناموفق بود.", show_alert=True)
        return
    _PENDING[sent.message_id] = {"rid": row.id, "act": act, "minutes": int(minutes or 0)}
    await _render_admin(cb, row, ("reason", act, minutes), extra=f"\n\n⏳ منتظرِ علتِ {what}…")
    await cb.answer("علت را ریپلای کن یا «بدون دلیل» بزن")


def _is_admin_reason(m: Message) -> bool:
    r = m.reply_to_message
    return bool(m.text and m.from_user and m.from_user.id == PLATFORM_ADMIN_ID and r and r.message_id in _PENDING)


@platform_group_report_router.message(F.chat.type == ChatType.PRIVATE, F.func(_is_admin_reason))
async def report_admin_reason_reply(msg: Message, bot: Bot):
    p = _PENDING.pop(msg.reply_to_message.message_id, None)
    if not p:
        raise SkipHandler
    err, result, row = await _execute(bot, p["rid"], p["act"], msg.from_user.id, msg.text, p["minutes"])
    try:
        await bot.delete_message(PLATFORM_ADMIN_ID, msg.reply_to_message.message_id)
    except Exception as e:  # noqa: BLE001
        logger.info("group report #%s: prompt delete skipped: %s", p["rid"], e)
    if err:
        await msg.reply(err)
        return
    await _drop_pending(bot, p["rid"])
    await _render_admin_by_id(bot, row)
    await msg.reply(result)


@platform_group_report_router.callback_query(F.data == "grpn:noop")
async def report_notice_noop(cb: CallbackQuery):
    """دکمه‌هایِ شیشه‌ایِ زیرِ اعلامِ گروه فقط نمایشی‌اند."""
    await cb.answer()


@platform_group_report_router.callback_query(F.data.startswith("grpa:"))
async def report_admin_action(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    parts = (cb.data or "").split(":")
    act, rid = parts[1], _rid(cb)
    async with Session() as s:
        row = await s.get(GroupReport, rid)
    if row is None or row.status not in ("sent", "closed"):
        await cb.answer("گزارش پیدا نشد.", show_alert=True)
        return
    if row.status == "closed":
        await cb.answer("این گزارش بسته شده است.", show_alert=True)
        return
    done = _acts(row)
    if act in ("warn", "mute", "ban", "dur") and (act if act != "dur" else "mute") in done:
        await cb.answer("این اقدام قبلا انجام شده.", show_alert=True)
        return
    if act in ("warn", "ban"):                              # PATCH_REPORT_ACTION_REASON_V1: اول علت را بپرس
        await _ask_reason(cb, bot, row, act)
        return
    if act == "mute":                                       # اول مدت
        await _render_admin(cb, row, ("mute",))
        await cb.answer("مدتِ سکوت را انتخاب کن")
        return
    if act == "dur":
        try:
            minutes = int(parts[3])
        except (IndexError, ValueError):
            await cb.answer()
            return
        if minutes not in MUTE_CHOICES:
            await cb.answer("مدتِ نامعتبر.", show_alert=True)
            return
        await _ask_reason(cb, bot, row, "mute", minutes)
        return
    if act == "back":
        await _drop_pending(bot, rid)
        await _render_admin(cb, row)
        await cb.answer()
        return
    minutes = None
    note = None
    if act == "go":                                         # «بدون دلیل»
        try:
            act, minutes = parts[3], int(parts[4])
        except (IndexError, ValueError):
            await cb.answer()
            return
        if act not in ("warn", "mute", "ban"):
            await cb.answer()
            return
    elif act == "banyes":                                   # دکمهٔ قدیمیِ پیام‌هایِ پیشین
        act = "ban"
    err, result, row2 = await _execute(bot, rid, act, cb.from_user.id, note, minutes or None)
    if err:
        await cb.answer(err, show_alert=True)
        return
    if not result:
        await cb.answer()
        return
    await _drop_pending(bot, rid)
    await _render_admin(cb, row2)
    await cb.answer(result)
