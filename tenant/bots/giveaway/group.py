"""
tenant/bots/giveaway/group.py — دستورهایِ قرعه‌کشی داخلِ گروه (PATCH_GIVEAWAY_BOT_V1).

همه (فقط رباتِ field=giveaway):
    قرعه‌کشی | /giveaway | /gw            ← وضعیتِ قرعه‌کشیِ فعال + دکمهٔ شرکت (باز شدنِ پیویِ ربات)
    نتیجه قرعه‌کشی | /gwresult            ← نتیجهٔ آخرین قرعه‌کشیِ پایان‌یافته
    شرکت‌کنندگان | /gwcount               ← تعدادِ شرکت‌کنندگان
فقط ادمین (ادمینِ ربات یا ادمینِ همان گروه):
    قرعه‌کشی جدید <عنوان> | <تعداد برنده> | <جایزه>      (یا /newgw …)
    پایان قرعه‌کشی | /endgw                ← پایان + قرعه + اعلانِ نتیجه در همین گروه
"""
from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle, ChatType
from aiogram.types import Message
from sqlalchemy import select

from config import Session

from tenant.bots.giveaway import announce as A
from tenant.bots.giveaway import engine as E
from tenant.bots.giveaway.common import (FIELD_KEY, btn, card_text, esc, fa, kb, results_text)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

giveaway_group_router = Router(name="giveaway_group")

_STATUS = re.compile(r"^(?:/giveaway|/gw|قرعه[\s‌]?کشی)(?:@\w+)?$", re.I)
_RESULT = re.compile(r"^(?:/gwresult|نتیجه[\s‌]?قرعه[\s‌]?کشی|نتایج[\s‌]?قرعه[\s‌]?کشی)(?:@\w+)?$", re.I)
_COUNT = re.compile(r"^(?:/gwcount|شرکت[\s‌]?کنندگان)(?:@\w+)?$", re.I)
_END = re.compile(r"^(?:/endgw|پایان[\s‌]?قرعه[\s‌]?کشی)(?:@\w+)?$", re.I)
_NEW = re.compile(r"^(?:/newgw|قرعه[\s‌]?کشی[\s‌]?جدید)(?:@\w+)?\s+(.+)$", re.I | re.S)


def _is_group_cmd(m: Message) -> bool:
    t = (m.text or "").strip()
    return bool(t) and bool(_STATUS.match(t) or _RESULT.match(t) or _COUNT.match(t) or _END.match(t) or _NEW.match(t))


async def _group_admin(bot: Bot, row, msg: Message) -> bool:
    from tenant.bots.giveaway.common import is_admin
    if await is_admin(row, msg.from_user.id):
        return True
    try:
        m = await bot.get_chat_member(msg.chat.id, msg.from_user.id)
        return m.status in ("administrator", "creator")
    except Exception:      # noqa: BLE001
        return False


@giveaway_group_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text, F.func(_is_group_cmd))
async def group_command(msg: Message, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or row.field != FIELD_KEY:
        raise SkipHandler
    t = (msg.text or "").strip()

    if _STATUS.match(t):
        gs = await E.active_giveaways(row.id)
        if not gs:
            return await msg.reply("⏳ الان قرعه‌کشیِ فعالی نیست.")
        for g in gs[:3]:
            await msg.reply(card_text(g, await E.prizes_of(g.id), await E.count_entries(g.id)),
                            reply_markup=await A.post_markup(bot, g), parse_mode="HTML", disable_web_page_preview=True)
        return

    if _RESULT.match(t):
        done = await E.recent_giveaways(row.id, 1, ("ended",))
        if not done:
            return await msg.reply("هنوز قرعه‌کشیِ پایان‌یافته‌ای نیست.")
        g = done[0]
        async with Session() as s:
            left = len((await s.scalars(select(E.GiveawayEntry.id).where(E.GiveawayEntry.giveaway_id == g.id,
                                                                          E.GiveawayEntry.left == True))).all())  # noqa: E712
        return await msg.reply(results_text(g, await E.winners_of(g.id), await E.count_entries(g.id), left, True),
                               parse_mode="HTML")

    if _COUNT.match(t):
        gs = await E.active_giveaways(row.id)
        if not gs:
            return await msg.reply("⏳ الان قرعه‌کشیِ فعالی نیست.")
        lines = [f"👥 «{esc(g.title)}»: <b>{fa(await E.count_entries(g.id))}</b> نفر" for g in gs[:5]]
        return await msg.reply("\n".join(lines), parse_mode="HTML")

    # ── فقط ادمین ──
    if not await _group_admin(bot, row, msg):
        return await msg.reply("⛔️ این دستور فقط برایِ ادمین‌هاست.")

    if _END.match(t):
        gs = await E.active_giveaways(row.id)
        if not gs:
            return await msg.reply("قرعه‌کشیِ فعالی برایِ پایان‌دادن نیست.")
        if len(gs) > 1:
            return await msg.reply("چند قرعه‌کشیِ فعال داری؛ از پنلِ مدیریتِ ربات (/admin) پایانِ هرکدام را بزن.")
        g = gs[0]
        wait = await msg.reply("⏳ در حالِ چکِ لفت و قرعه‌کشی…")
        res = await A.finish_giveaway(bot, row, g.id, notify_admin_chat=msg.from_user.id)
        if not res.ok:
            return await wait.edit_text("⚠️ این قرعه‌کشی قبلاً پایان یافته.")
        g2 = await E.get_giveaway(g.id, row.id)
        text = results_text(g2, res.winners or [], res.participants, res.left, public=True)
        try:
            await wait.edit_text(text, parse_mode="HTML")
        except Exception:      # noqa: BLE001
            await msg.reply(text, parse_mode="HTML")
        return

    m = _NEW.match(t)
    if m:
        parts = [p.strip() for p in m.group(1).split("|")]
        if len(parts) < 3 or not parts[0] or not parts[2]:
            return await msg.reply("فرمت: <code>قرعه‌کشی جدید عنوان | تعداد برنده | جایزه</code>\n"
                                   "مثال: <code>قرعه‌کشی جدید ۵۰ گیگ رایگان | 2 | ۵۰ گیگ</code>", parse_mode="HTML")
        cnt = E.to_ascii_digits(parts[1]).strip()
        if not cnt.isdigit() or not 1 <= int(cnt) <= E.MAX_WINNERS:
            return await msg.reply(f"تعدادِ برنده باید ۱ تا {E.MAX_WINNERS} باشد.")
        g = await E.create_giveaway(row.id, E.NewGiveaway(title=parts[0], winners_count=int(cnt), created_by=msg.from_user.id,
                                                          captcha=True, ref_bonus=1),
                                    [E.NewPrize(parts[2], "title")])
        posted = await msg.reply("🎊 <b>قرعه‌کشیِ جدید شروع شد!</b>\n\n" + card_text(g, await E.prizes_of(g.id), 0),
                                 reply_markup=await A.post_markup(bot, g), parse_mode="HTML", disable_web_page_preview=True)
        from models import GiveawayPost
        async with Session() as s:
            s.add(GiveawayPost(bot_id=row.id, giveaway_id=g.id, chat_id=msg.chat.id, message_id=posted.message_id))
            await s.commit()
        return
    raise SkipHandler
