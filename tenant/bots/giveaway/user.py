"""
tenant/bots/giveaway/user.py — سمتِ کاربرِ رباتِ قرعه‌کشی (PATCH_GIVEAWAY_BOT_V1).

جریان: لیست/کارتِ قرعه‌کشی ← «شرکت» ← عضویتِ اجباری ← (شمارهٔ ایرانی) ← (سؤالِ ریاضی) ← ثبت‌نام + لینکِ دعوت.
همهٔ صفحه‌ها با ادیتِ همان پیام عوض می‌شوند و دکمهٔ بازگشت دارند.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from sqlalchemy import select

from config import Session
from models import BotUser

from tenant.bots.giveaway import engine as E
from tenant.bots.giveaway.common import (HOME_ROW, FIELD_KEY, back_row, btn, card_text, deep_link, edit_by_id,
                                         edit_or_send, esc, fa, kb, parse_payload, results_text)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

giveaway_user_router = Router(name="giveaway_user")

MAX_TRIES = 3
LOCK_SECS = 300
_LOCK: dict[tuple[int, int], float] = {}        # (bot_id, uid) → تا این زمان قفل


class GwUserFSM(StatesGroup):
    phone = State()
    captcha = State()


async def _row(bot: Bot):
    from tenant import get_bot_row
    return await get_bot_row(bot)


# ───────────────────────── فهرست و کارت ─────────────────────────

async def show_list(event, bot: Bot, row, uid: int) -> None:
    gs = await E.active_giveaways(row.id)
    if len(gs) == 1:
        return await show_detail(event, bot, row, gs[0].id, uid, back="home")
    if not gs:
        recent = [g for g in await E.recent_giveaways(row.id, 5, ("ended",))]
        rows = [[btn(f"🏆 {g.title[:30]}", f"gw:res:{g.id}")] for g in recent]
        await edit_or_send(event, "⏳ <b>در حال حاضر قرعه‌کشیِ فعالی وجود ندارد.</b>\n\nبعداً دوباره سر بزن!"
                           + ("\n\n🏆 نتایجِ قرعه‌کشی‌هایِ قبلی:" if recent else ""), kb(*rows, HOME_ROW))
        return
    rows = []
    for g in gs[:20]:
        n = await E.count_entries(g.id)
        rows.append([btn(f"🎰 {g.title[:34]} · {fa(n)} نفر", f"gw:v:{g.id}", style=ButtonStyle.SUCCESS)])
    await edit_or_send(event, "🎰 <b>قرعه‌کشی‌هایِ فعال</b>\n\nیکی را انتخاب کن:", kb(*rows, HOME_ROW))


async def show_detail(event, bot: Bot, row, gid: int, uid: int, back: str = "list", note: str = "") -> None:
    g = await E.get_giveaway(gid, row.id)
    if g is None or g.status == "draft":
        await edit_or_send(event, "این قرعه‌کشی پیدا نشد.", kb(back_row("gw:list"), HOME_ROW))
        return
    prizes = await E.prizes_of(gid)
    n = await E.count_entries(gid)
    entry = await E.entry_of(gid, uid)
    link = invited = None
    ch = 0
    if entry is not None:
        ch = E.chances(entry.bonus)
        invited = await E.referrals_count(gid, uid)
        if g.ref_bonus > 0 and g.status == "active":
            link = await deep_link(bot, gid, uid)
    text = card_text(g, prizes, n, entry, ch, invited or 0, link)
    if note:
        text = note + "\n\n" + text
    rows = []
    if g.status == "active":
        if entry is None:
            rows.append([btn("🎰 شرکت در قرعه‌کشی", f"gw:j:{gid}", style=ButtonStyle.SUCCESS)])
        else:
            rows.append([btn("🔄 به‌روزرسانی", f"gw:v:{gid}", style=ButtonStyle.PRIMARY)])
            if link:
                share = f"https://t.me/share/url?url={quote(link)}&text={quote('🎰 ' + g.title + ' — با این لینک ثبت‌نام کن!')}"
                rows.append([btn("📤 ارسال لینکِ دعوت به دوستان", url=share, style=ButtonStyle.SUCCESS)])
    elif g.status == "ended":
        rows.append([btn("🏆 نتایج", f"gw:res:{gid}", style=ButtonStyle.SUCCESS)])
    rows.append(back_row("gw:list") if back == "list" else HOME_ROW)
    await edit_or_send(event, text, kb(*rows))


async def show_results_list(event, bot: Bot, row, uid: int) -> None:
    done = await E.recent_giveaways(row.id, 15, ("ended",))
    if not done:
        await edit_or_send(event, "🏆 هنوز قرعه‌کشیِ پایان‌یافته‌ای نیست.", kb(HOME_ROW))
        return
    rows = [[btn(f"🏆 {g.title[:34]}", f"gw:res:{g.id}", style=ButtonStyle.SUCCESS)] for g in done]
    await edit_or_send(event, "🏆 <b>نتایجِ قرعه‌کشی‌ها</b>\n\nیکی را انتخاب کن:", kb(*rows, HOME_ROW))


# ───────────────────────── ثبت‌نام ─────────────────────────

async def _forcejoin_block(event, bot: Bot, row, uid: int, gid: int) -> bool:
    """True = کاربر هنوز عضو نیست (صفحهٔ عضویت نشان داده شد)."""
    from tenant import check_force_join
    nj = await check_force_join(bot, row.id, uid, bypass_cache=True)
    if not nj:
        return False
    rows = []
    for ch in nj:
        cid_s = str(ch["chat_id"])
        link = ch.get("invite_link") or (f"https://t.me/{cid_s[1:]}" if cid_s.startswith("@")
                                         else (f"https://t.me/c/{cid_s[4:]}" if cid_s.startswith("-100") else None))
        rows.append([btn(f"📢 {ch['title']}", url=link) if link else btn(f"📢 {ch['title']}", "noop")])
    rows.append([btn("✅ عضو شدم", f"gw:j:{gid}", style=ButtonStyle.SUCCESS)])
    rows.append(back_row(f"gw:v:{gid}"))
    await edit_or_send(event, "⚠️ برای شرکت در قرعه‌کشی باید اول در کانال‌هایِ زیر عضو شوی، بعد «✅ عضو شدم» را بزن:", kb(*rows))
    return True


def _locked(bot_id: int, uid: int) -> int:
    left = int(_LOCK.get((bot_id, uid), 0) - time.monotonic())
    return left if left > 0 else 0


async def _bu(row, uid: int):
    async with Session() as s:
        return await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == uid))


@giveaway_user_router.callback_query(F.data == "gw:list")
async def gw_list(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    await state.set_state(None)          # خودِ حالت پاک می‌شود، دادهٔ دعوت‌کننده (gw_inv) می‌ماند
    await show_list(cb, bot, row, cb.from_user.id)
    await cb.answer()


@giveaway_user_router.callback_query(F.data.regexp(r"^gw:v:\d+$"))
async def gw_view(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    await state.set_state(None)
    await show_detail(cb, bot, row, int(cb.data.split(":")[2]), cb.from_user.id)
    await cb.answer()


async def _begin_join(ev, bot: Bot, row, state: FSMContext, gid: int, uid: int, say, ack) -> None:
    """مسیرِ مشترکِ شروعِ ثبت‌نام (دکمهٔ «شرکت» و ورود با لینک). ev باید from_user و message داشته باشد.
    say(text, alert) پیامِ کوتاه می‌دهد؛ ack() پاسخِ خالیِ کال‌بک (برایِ ورود با لینک بی‌اثر است)."""
    u = await _bu(row, uid)
    if u is not None and u.is_banned:
        return await say("شما از این ربات مسدود شده‌اید.", True)
    g = await E.get_giveaway(gid, row.id)
    if g is None or g.status != "active":
        await say("این قرعه‌کشی پایان یافته است.", True)
        return await show_detail(ev, bot, row, gid, uid)
    if await E.entry_of(gid, uid):
        await say("قبلاً ثبت‌نام کرده‌ای ✅", False)
        return await show_detail(ev, bot, row, gid, uid)
    left = _locked(row.id, uid)
    if left:
        return await say(f"به‌خاطرِ پاسخِ غلطِ مکرر، {fa(left // 60 + 1)} دقیقه صبر کن.", True)
    await ack()
    if await _forcejoin_block(ev, bot, row, uid, gid):
        return
    data = await state.get_data()
    await state.update_data(gw_id=gid, gw_chat=ev.message.chat.id, gw_msg=ev.message.message_id,
                            gw_inv=data.get("gw_inv_for") == gid and data.get("gw_inv") or None, gw_tries=0)
    phone = None
    if g.require_phone:
        phone = E.normalize_iran_phone(u.phone_number) if (u is not None and u.phone_verified and u.phone_number) else None
        if not phone:
            await state.set_state(GwUserFSM.phone)
            await edit_or_send(ev, "☎️ <b>تأییدِ شماره</b>\n\nبرایِ شرکت باید شمارهٔ <b>ایرانیِ</b> خودت را با دکمهٔ پایینِ صفحه "
                                   "تأیید کنی (تایپِ دستی قبول نیست).", kb(back_row(f"gw:cx:{gid}", "❌ انصراف")))
            await ev.message.answer(
                "👇 با دکمهٔ زیر شمارهٔ خودت را بفرست:",
                reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 ارسالِ شمارهٔ من", request_contact=True)]],
                                                 resize_keyboard=True, one_time_keyboard=True))
            return
    await state.update_data(gw_phone=phone)
    await _after_phone(ev, bot, row, state, g, uid)


@giveaway_user_router.callback_query(F.data.regexp(r"^gw:j:\d+$"))
async def gw_join(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    uid, gid = cb.from_user.id, int(cb.data.split(":")[2])

    async def say(text: str, alert: bool) -> None:
        await cb.answer(text, show_alert=alert)
    await _begin_join(cb, bot, row, state, gid, uid, say, cb.answer)


async def _after_phone(event, bot: Bot, row, state: FSMContext, g, uid: int) -> None:
    if g.captcha:
        q, ans = E.make_captcha()
        await state.set_state(GwUserFSM.captcha)
        await state.update_data(gw_ans=ans)
        text = (f"🧮 <b>تأییدِ انسان بودن</b>\n\nپاسخِ این سؤال را (فقط عدد) بفرست:\n\n<b>{q} = ؟</b>\n\n"
                f"فرصت: {fa(MAX_TRIES)} بار")
        await edit_or_send(event, text, kb(back_row(f"gw:cx:{g.id}", "❌ انصراف")))
        return
    await _finish(event, bot, row, state, g, uid)


async def _finish(event, bot: Bot, row, state: FSMContext, g, uid: int) -> None:
    d = await state.get_data()
    u = event.from_user
    res = await E.join(g.id, row.id, uid, u.full_name or "", u.username or "", d.get("gw_phone"), d.get("gw_inv"))
    await state.clear()
    if not res.ok:
        msg = {"closed": "این قرعه‌کشی پایان یافته است.", "not_started": "این قرعه‌کشی هنوز شروع نشده است.",
               "phone_used": "این شماره قبلاً برایِ شخصِ دیگری ثبت شده است.", "phone_needed": "تأییدِ شماره لازم است.",
               "exists": "قبلاً ثبت‌نام کرده‌ای ✅"}.get(res.reason, "ثبت‌نام ممکن نشد.")
        await edit_or_send(event, "❌ " + msg, kb(back_row(f"gw:v:{g.id}"), HOME_ROW))
        return
    if res.inviter_tg:
        try:
            await bot.send_message(res.inviter_tg, f"🎉 یک نفر با لینکِ تو در قرعه‌کشیِ «{esc(g.title)}» ثبت‌نام کرد!\n"
                                                   f"🎯 شانس‌هایِ تو الان: <b>{fa(res.inviter_chances)}</b>", parse_mode="HTML")
        except Exception:      # noqa: BLE001
            logger.debug("giveaway: خبر به دعوت‌کننده نرفت")
    await show_detail(event, bot, row, g.id, uid, note="🎉 <b>ثبت‌نامت انجام شد!</b>")


@giveaway_user_router.callback_query(F.data.regexp(r"^gw:cx:\d+$"))
async def gw_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    await state.set_state(None)
    await cb.answer("لغو شد.")
    await show_detail(cb, bot, row, int(cb.data.split(":")[2]), cb.from_user.id)


@giveaway_user_router.message(GwUserFSM.phone, F.contact)
async def gw_phone_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    c = msg.contact
    d = await state.get_data()
    gid = d.get("gw_id")
    g = await E.get_giveaway(gid, row.id) if gid else None
    if g is None or g.status != "active":
        await state.clear()
        return await msg.answer("این قرعه‌کشی پایان یافته است.", reply_markup=ReplyKeyboardRemove())
    if c.user_id != msg.from_user.id:
        return await msg.answer("❌ فقط شمارهٔ <b>خودِ تو</b> قبول است؛ با دکمهٔ «📱 ارسالِ شمارهٔ من» بفرست.", parse_mode="HTML")
    phone = E.normalize_iran_phone(c.phone_number)
    if not phone:
        return await msg.answer("❌ فقط شمارهٔ موبایلِ ایران (+98) پذیرفته می‌شود.", reply_markup=ReplyKeyboardRemove())
    async with Session() as s:
        used = await s.scalar(select(E.GiveawayEntry.id).where(E.GiveawayEntry.giveaway_id == gid, E.GiveawayEntry.phone == phone))
    if used is not None:
        await state.clear()
        return await msg.answer("❌ این شماره قبلاً برایِ شخصِ دیگری ثبت شده است.", reply_markup=ReplyKeyboardRemove())
    await state.update_data(gw_phone=phone)
    await msg.answer("✅ شماره تأیید شد.", reply_markup=ReplyKeyboardRemove())
    # صفحهٔ اصلیِ فلو را (همان پیامِ قبلی) ادامه می‌دهیم
    class _Ev:                      # ادیتِ پیامِ قبلی با شناسهٔ ذخیره‌شده
        def __init__(self, m, bot_, d_):
            self.from_user, self.message = m.from_user, _Msg(bot_, d_["gw_chat"], d_["gw_msg"], m)
            self.data = "x"
    await _after_phone(_Ev(msg, bot, d), bot, row, state, g, msg.from_user.id)


@giveaway_user_router.message(GwUserFSM.phone)
async def gw_phone_other(msg: Message):
    await msg.answer("📱 لطفاً فقط با دکمهٔ «ارسالِ شمارهٔ من» شمارهات را بفرست (یا از پیامِ قبلی «❌ انصراف» را بزن).")


class _Msg:
    """پیامِ قبلیِ ربات (برایِ ادیتِ درجا از داخلِ هندلرِ پیام)."""
    def __init__(self, bot, chat_id, message_id, origin):
        self.bot, self.chat, self.message_id, self._origin = bot, type("C", (), {"id": chat_id})(), message_id, origin

    async def edit_text(self, text, reply_markup=None, **kw):
        await self.bot.edit_message_text(chat_id=self.chat.id, message_id=self.message_id, text=text,
                                         reply_markup=reply_markup, **kw)

    async def answer(self, text, **kw):
        return await self.bot.send_message(self.chat.id, text, **kw)


@giveaway_user_router.message(GwUserFSM.captcha, F.text)
async def gw_captcha_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    d = await state.get_data()
    gid = d.get("gw_id")
    g = await E.get_giveaway(gid, row.id) if gid else None
    if g is None or g.status != "active":
        await state.clear()
        return await msg.answer("این قرعه‌کشی پایان یافته است.")
    ev = type("Ev", (), {"from_user": msg.from_user, "data": "x", "message": _Msg(bot, d["gw_chat"], d["gw_msg"], msg)})()
    if E.check_captcha(msg.text, d.get("gw_ans", -1)):
        return await _finish(ev, bot, row, state, g, msg.from_user.id)
    tries = int(d.get("gw_tries", 0)) + 1
    if tries >= MAX_TRIES:
        _LOCK[(row.id, msg.from_user.id)] = time.monotonic() + LOCK_SECS
        await state.clear()
        return await edit_by_id(bot, d["gw_chat"], d["gw_msg"],
                                f"⛔️ پاسخِ غلطِ مکرر. {fa(LOCK_SECS // 60)} دقیقه بعد دوباره امتحان کن.",
                                kb(back_row(f"gw:v:{gid}")))
    q, ans = E.make_captcha()
    await state.update_data(gw_ans=ans, gw_tries=tries)
    await edit_by_id(bot, d["gw_chat"], d["gw_msg"],
                     f"❌ پاسخ درست نبود. سؤالِ تازه:\n\n<b>{q} = ؟</b>\n\nفرصتِ باقی‌مانده: {fa(MAX_TRIES - tries)}",
                     kb(back_row(f"gw:cx:{gid}", "❌ انصراف")))


# ───────────────────────── نتایج ─────────────────────────

@giveaway_user_router.callback_query(F.data.regexp(r"^gw:res:\d+$"))
async def gw_results(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    gid = int(cb.data.split(":")[2])
    g = await E.get_giveaway(gid, row.id)
    if g is None or g.status != "ended":
        return await cb.answer("نتیجه‌ای موجود نیست.", show_alert=True)
    winners = await E.winners_of(gid)
    n = await E.count_entries(gid)
    left = 0
    async with Session() as s:
        left = len((await s.scalars(select(E.GiveawayEntry.id).where(E.GiveawayEntry.giveaway_id == gid,
                                                                       E.GiveawayEntry.left == True))).all())  # noqa: E712
    text = results_text(g, winners, n, left, public=True)
    mine = next((w for w, e, p in winners if e.user_tg == cb.from_user.id), None)
    if mine:
        text = f"🎉 <b>تبریک! تو برندهٔ {fa(mine.rank)}ام هستی!</b>\n\n" + text
    await edit_or_send(cb, text, kb(back_row(f"gw:v:{gid}"), HOME_ROW))
    await cb.answer()


# ───────────────────────── ورودِ /start ─────────────────────────

async def on_start(msg: Message, bot: Bot, row, payload: str | None, state: FSMContext) -> bool:
    """بعد از منویِ خوش‌آمد صدا زده می‌شود. اگر payload «gw_…» بود کارتِ همان قرعه‌کشی؛ در رباتِ field=giveaway
    بدونِ payload هم قرعه‌کشی(های) فعال را نشان می‌دهد. True = چیزی نشان داده شد."""
    parsed = parse_payload(payload) if payload and payload.startswith("gw_") else None
    if parsed:
        gid, inv = parsed
        await state.update_data(gw_inv=inv, gw_inv_for=gid)
        # === PATCH_GW_LINK_AUTOJOIN_V1 === (مهدی) با لینک (دعوت یا اعلان) که آمد، لازم نیست دکمهٔ «شرکت» را بزند:
        # مستقیم می‌رود سراغِ سؤالِ ریاضی (و شماره/عضویتِ اجباری اگر لازم باشد) و با لینکِ دعوت جزوِ رفرالِ دعوت‌کننده می‌شود.
        uid = msg.from_user.id
        g = await E.get_giveaway(gid, row.id)
        if g is not None and g.status == "active" and inv != uid and await E.entry_of(gid, uid) is None:
            ph = await msg.answer("🎰 <b>" + esc(g.title) + "</b>\n\n⏳ در حالِ شروعِ ثبت‌نام…", parse_mode="HTML")
            ev = type("Ev", (), {"from_user": msg.from_user, "data": "x", "message": ph})()

            async def say(text: str, alert: bool) -> None:
                await msg.answer(text)

            async def ack() -> None:
                return None
            await _begin_join(ev, bot, row, state, gid, uid, say, ack)
            return True
        await show_detail(msg, bot, row, gid, uid, back="home")
        return True
    if row.field == FIELD_KEY:
        await show_list(msg, bot, row, msg.from_user.id)
        return True
    return False


@giveaway_user_router.message(Command("giveaway"), F.chat.type == "private")
async def cmd_giveaway(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row or row.field != FIELD_KEY:
        from aiogram.dispatcher.event.bases import SkipHandler
        raise SkipHandler
    await state.set_state(None)
    await show_list(msg, bot, row, msg.from_user.id)


@giveaway_user_router.message(Command("cancel"), StateFilter(GwUserFSM))
async def cmd_cancel(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    d = await state.get_data()
    await state.set_state(None)
    await msg.answer("لغو شد.", reply_markup=ReplyKeyboardRemove())
    if row and d.get("gw_id"):
        await show_detail(msg, bot, row, d["gw_id"], msg.from_user.id)
