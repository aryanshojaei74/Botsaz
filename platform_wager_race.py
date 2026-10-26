"""platform_wager_race.py — مسابقه‌هایِ پولیِ دونفره در گروهِ بات‌ساز اصلی: «حدس عدد / سرعت / ریاضی» با کیف‌پول.

=== PATCH_WAGER_RACE_V1 ===
• مثلِ بقیهٔ شرط‌بندی‌هایِ گروه (platform_wager): رزرو و تسویهٔ اتمیکِ مبلغ، کارمزد و حداقل/حداکثر از «تنظیمات شرط‌بندیِ گروه»،
  مساوی/بی‌حریف/نیامدن ⇒ برگشتِ کاملِ مبلغ، ریسکِ مالی در متنِ هر پیام. هسته (create_wager، _settle_win، _refund_both،
  sweep_expired) همان است؛ این فایل فقط منطقِ بازی و پیام را دارد.
• دستورِ ساخت (مبلغ = تومانِ هر نفر): «حدس عدد 10000»، «سرعت 10000»، «ریاضی 10000» (پیشوندِ «بازی»/«شرط‌بندی» اختیاری).
  اولین «✅ قبول و پیوستن» حریف است (یا روی پیامِ یک نفر ریپلای کن تا فقط او بتواند بپیوندد).
• قانون: حدس عدد (۱ تا ۱۰۰، ۳ دقیقه): هر دو نفر حدس می‌زنند و ربات بالاتر/پایین‌تر می‌گوید، اولین حدسِ دقیق برنده است.
  سرعت (۴۵ ثانیه): اولین نفری که کلمه را دقیقاً بنویسد. ریاضی (۴۵ ثانیه): اولین جوابِ درست. کسی برنده نشد ⇒ برگشتِ هر دو.
• پیش‌فرض خاموش است (کلیدِ pgr_on)؛ مدیر از «🎃 تم هالوین ← 💰 مسابقه‌هایِ پولی» روشن می‌کند. فقط گروهِ محافظ؛ ربات‌هایِ مشتری
  این بازی‌ها را ندارند (GAMES هسته دست‌نخورده است).
• چند مسابقهٔ پولی هم‌زمان در یک گروه مجازند (هر کس بخواهد می‌سازد/می‌پیوندد؛ هر نفر هم‌زمان فقط در یک مسابقهٔ زنده بازی می‌کند)؛
  بازیِ رایگان هم جداست. برنده‌شدن فقط با حافظهٔ پردازش پی‌گیری می‌شود؛ اگر پردازش وسطِ بازی
  ری‌استارت شود، شرط با مهلتش تمام می‌شود و مبلغِ هر دو نفر برمی‌گردد (هسته).
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import re
import secrets
import time
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

import platform_wager as core
from config import Session
from models import PlatformWager
from platform_group_money import (ANON_NOTICE, _bet_enabled, _digits, _group_allowed, _int, _money, _name, _reply_target,
                                  _rial, _user)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_wager_race_router = Router(name="platform_wager_race")

RACE_GAMES = ("guess", "speed", "math")
RACE_FA = {"guess": "حدس عدد", "speed": "سرعت", "math": "ریاضی"}
SECS = {"guess": 180, "speed": 45, "math": 45}
KEY_ON = "pgr_on"
GUESS_GAP = 1.2
_AMT = r"([۰-۹0-9,،]+)"
_PREFIX = r"(?:(?:شرط\s*بندی|بازی)\s+)?"
_PATTERNS = (
    ("guess", re.compile(rf"^{_PREFIX}حدس\s*عدد\s+{_AMT}$")),
    ("speed", re.compile(rf"^{_PREFIX}سرعت\s+{_AMT}$")),
    ("math", re.compile(rf"^{_PREFIX}ریاضی\s+{_AMT}$")),
)
_AR = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# حافظهٔ پردازش: wid → {"wid", "chat", "kind", "players": {uid,..}, "answer", "n", "until"}
# (چند مسابقهٔ هم‌زمان در یک گروه مجازند؛ پاسخِ هر نفر به مسابقه‌ای می‌رسد که خودش بازیکنش است.)
LIVE: dict[int, dict] = {}


def _live_chat(chat_id: int) -> list[dict]:
    """مسابقه‌هایِ پولیِ زنده (منتظرِ حریف یا در حالِ بازی) در این گروه؛ مهلتِ گذشته = آزاد."""
    now = time.monotonic()
    return [lv for lv in LIVE.values() if lv.get("chat") == chat_id and lv.get("until", 0) > now]


def busy_paid(chat_id: int) -> bool:
    return bool(_live_chat(chat_id))


def player_race(chat_id: int, uid: int) -> dict | None:
    """مسابقهٔ در حالِ بازیِ این کاربر در این گروه (هر نفر هم‌زمان فقط در یک مسابقهٔ زنده بازی می‌کند)."""
    for lv in _live_chat(chat_id):
        if uid in lv["players"]:
            return lv
    return None
_LAST_GUESS: dict[tuple[int, int], float] = {}


# ───────────────────────── منطقِ خالص ─────────────────────────

def parse_race_command(text: str | None) -> tuple[str, int] | None:
    t = core._norm(text)
    for game, rx in _PATTERNS:
        m = rx.match(t)
        if m:
            amount = _int(_digits(m.group(1)))
            return (game, amount) if amount > 0 else None
    return None


def norm_answer(t: str | None) -> str:
    return (t or "").strip().translate(_AR).replace("‌", "").replace(" ", "").lower()


def new_question(kind: str) -> dict:
    """پرسش و پاسخِ تازه برایِ هر نوع (state ذخیره‌شده در ردیفِ شرط)."""
    if kind == "guess":
        return {"n": 1 + secrets.randbelow(100), "tries": {}}
    if kind == "speed":
        from platform_group_game import WORDS
        word = secrets.choice(WORDS)
        return {"q": word, "a": norm_answer(word)}
    from platform_group_game import make_math
    q, a = make_math()
    return {"q": q, "a": a}


async def enabled() -> bool:
    from platform_bot import get_pset
    return (await get_pset(KEY_ON, "0")) == "1"


# ───────────────────────── نمایش ─────────────────────────

def _rule(w: PlatformWager) -> str:
    return {"guess": "عددی بین ۱ تا ۱۰۰؛ هر دو نفر حدس می‌زنید (ربات بالاتر/پایین‌تر می‌گوید). اولین حدسِ دقیق برنده است.",
            "speed": "ربات یک کلمه می‌فرستد؛ اولین نفری که دقیقاً همان را بنویسد برنده است.",
            "math": "ربات یک محاسبه می‌فرستد؛ اولین جوابِ درست برنده است."}[w.game]


def _kb_pending(w: PlatformWager) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        hx_btn("✅ قبول و پیوستن", f"pgr:join:{w.id}", ButtonStyle.SUCCESS),
        hx_btn("❌ لغو", f"pgr:cancel:{w.id}", ButtonStyle.DANGER)]])


def hx_btn(text: str, cb: str, style):
    from premium_emoji import hx_button
    return hx_button(text, callback_data=cb, style=style)


def render_race(w: PlatformWager) -> tuple[str, InlineKeyboardMarkup]:
    wd = core.words(0)
    head = f"{wd['icon']} <b>{wd['bet']} {RACE_FA[w.game]}</b>"
    st = core._st(w)
    if w.status == "pending":
        target = (f"🎯 حریف تعیین‌شده: {_name(w.target_id, w.target_username, w.target_name)}\n"
                  if w.target_id else "👥 اولین نفری که «قبول و پیوستن» بزند حریف است.\n")
        text = (f"{head}\n\n👤 سازنده: {core._who(w, w.creator_id)}\n{target}📜 {_rule(w)}\n"
                f"{core._money_lines(w)}\n\n⏳ منتظر حریف…\n\n{wd['notice']}")
        return text, _kb_pending(w)
    if w.status == "playing":
        q = ""
        if w.game == "guess":
            q = "🔢 عددی بین <b>۱ تا ۱۰۰</b> انتخاب شد؛ عددت را در گروه بنویس."
        elif w.game == "speed":
            q = f"✍️ این را دقیقاً بنویس:\n\n👉 <b>{_html.escape(str(st.get('q', '')))}</b>"
        else:
            q = f"🧮 جواب چیست؟\n\n👉 <b>{_html.escape(str(st.get('q', '')))} = ؟</b>"
        text = (f"{head}\n\n{core._who(w, w.creator_id)}  ⚔️  {core._who(w, w.opponent_id)}\n{core._money_lines(w)}\n\n"
                f"{q}\n\n⏱ مهلت: {core._mins_left(w)}؛ کسی برنده نشد ⇒ مبلغِ هر دو برمی‌گردد.")
        return text, InlineKeyboardMarkup(inline_keyboard=[])
    lines = [head, ""]
    if w.status == "cancelled":
        return "\n".join(lines + ["❌ لغو شد و مبلغ رزروشده به سازنده برگشت."]), InlineKeyboardMarkup(inline_keyboard=[])
    if w.status == "expired" and not w.opponent_id:
        return "\n".join(lines + ["⏰ حریفی پیدا نشد؛ مبلغ سازنده برگشت."]), InlineKeyboardMarkup(inline_keyboard=[])
    lines.append(f"{core._who(w, w.creator_id)}  ⚔️  {core._who(w, w.opponent_id)}")
    if w.status == "settled":
        ans = st.get("n") if w.game == "guess" else st.get("q")
        shown = f"عدد: <b>{ans}</b>" if w.game == "guess" else (f"جواب: <b>{_html.escape(str(st.get('a', '')))}</b>"
                                                              if w.game == "math" else f"کلمه: <b>{_html.escape(str(ans))}</b>")
        lines += [shown, "", f"🏁 برنده: <b>{core._who(w, w.winner_id)}</b>",
                  f"🎁 دریافتی برنده: <b>{_rial(w.payout_amount)}</b> (کارمزد {_rial(w.fee_amount)})"]
    else:
        lines += ["", f"⏰ کسی در مهلت برنده نشد؛ {wd['stake']} به هر دو نفر برگشت (بدون کارمزد)."]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[])


# ───────────────────────── عملیات ─────────────────────────

async def join_race(wid: int, user: tuple):
    """مثلِ core.join_wager، ولی با ساختِ پرسش؛ خروجی R(w, err, note)."""
    uid, uname, uuser = user
    async with Session() as s:
        w = await s.get(PlatformWager, wid, with_for_update=True)
        if not w or (w.bot_id or 0) != 0 or w.game not in RACE_GAMES or w.status != "pending":
            return core._res(err="این مسابقه دیگر در انتظار نیست.")
        if w.creator_id == uid:
            return core._res(err="سازنده نمی‌تواند خودش به مسابقه بپیوندد.")
        if w.target_id and w.target_id != uid:
            return core._res(err="این مسابقه برای کاربری است که پیامش ریپلای شده.")
        if w.expires_at and w.expires_at <= core._now():
            await core._timeout_locked(s, w)
            await s.commit()
            return core._res(w=w, err="مهلتِ مسابقه تمام شده؛ مبلغ به سازنده برگشت.")
        if player_race(w.chat_id, uid):
            return core._res(err="تا تمام‌شدنِ مسابقهٔ فعلی‌ات نمی‌توانی به مسابقهٔ دیگری بپیوندی.")
        if player_race(w.chat_id, w.creator_id):
            return core._res(err="سازندهٔ این مسابقه همین حالا در مسابقهٔ دیگری است؛ چند لحظهٔ دیگر دوباره امتحان کن.")
        opp = await core._lock_user(s, uid, 0)
        if not opp or opp.is_banned or _money(opp.balance) < _money(w.amount):
            return core._res(err=f"موجودی کافی نیست؛ نیاز به {_rial(w.amount)} داری.")
        if await core._active_count(s, uid, 0) >= core.MAX_ACTIVE:
            return core._res(err=f"هم‌زمان حداکثر {core.MAX_ACTIVE} شرط باز می‌توانی داشته باشی.")
        opp.balance = Decimal(str(opp.balance)) - Decimal(str(w.amount))
        core._add_txn(s, 0, opp, -_money(w.amount), "wager_stake", f"پیوستن به شرط #{w.id}")
        w.opponent_id, w.opponent_name, w.opponent_username = uid, uname or "", uuser or None
        w.status = "playing"
        st = new_question(w.game)
        w.state = json.dumps(st, ensure_ascii=False)
        w.expires_at = core._now() + timedelta(seconds=SECS[w.game])
        await s.commit()
        return core._res(w=w)


async def settle_to(wid: int, uid: int):
    """برنده‌کردنِ uid اگر شرط هنوز در حالِ بازی است (قفل‌شده و اتمیک). خروجی: wager یا None."""
    async with core._wlock(wid):
        async with Session() as s:
            w = await s.get(PlatformWager, wid, with_for_update=True)
            if not w or w.status != "playing" or uid not in (w.creator_id, w.opponent_id):
                return None
            if w.expires_at and w.expires_at <= core._now():
                await core._timeout_locked(s, w)
                await s.commit()
                return None
            await core._settle_win(s, w, uid, "win")
            await s.commit()
            return w


# ───────────────────────── هندلرها ─────────────────────────

def _is_create(m: Message) -> bool:
    return parse_race_command(m.text) is not None


_NO_AMOUNT = re.compile(r"^شرط\s*بندی\s+(حدس\s*عدد|سرعت|ریاضی)$")


def _is_paid_hint(m: Message) -> bool:
    return bool(m.text and _NO_AMOUNT.match(core._norm(m.text)))


@platform_wager_race_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_paid_hint))
async def race_paid_hint(msg: Message):
    """«شرطبندی حدس عدد» بدونِ مبلغ = مسابقهٔ پولی؛ مبلغ را می‌خواهد (بازیِ رایگان فقط با «حدس عدد» شروع می‌شود)."""
    if not msg.from_user or msg.from_user.is_bot or msg.sender_chat:
        raise SkipHandler
    if not await _group_allowed(msg.chat.id) or not await enabled() or not await _bet_enabled():
        raise SkipHandler
    game = {"حدسعدد": "حدس عدد", "سرعت": "سرعت", "ریاضی": "ریاضی"}[
        re.sub(r"\s+", "", _NO_AMOUNT.match(core._norm(msg.text)).group(1))]
    cfg = await core.load_cfg()
    ex = core.example_amount(cfg)
    lim = (f"از {_rial(cfg['min'])} تا {_rial(cfg['max'])}" if cfg.get("max") else f"حداقل {_rial(cfg['min'])}")
    await msg.reply(f"💰 برای مسابقهٔ پولیِ <b>{game}</b> مبلغ را هم بنویس (به تومان، {lim}):\n"
                    f"<code>شرطبندی {game} {ex}</code>\n\n🆓 بازیِ رایگان با شیرینی: فقط «{game}» را بنویس.",
                    parse_mode="HTML")


@platform_wager_race_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_create))
async def race_create(msg: Message):
    if not msg.from_user or msg.from_user.is_bot:
        raise SkipHandler
    if not await _group_allowed(msg.chat.id) or not await enabled():
        raise SkipHandler
    if msg.sender_chat:
        await msg.reply(ANON_NOTICE)
        return
    if not await _bet_enabled():
        await msg.reply("❌ شرط‌بندی در بات‌ساز اصلی فعال نیست.")
        return
    cid = msg.chat.id
    game, amount = parse_race_command(msg.text)
    target = _reply_target(msg)
    if target and (target.is_bot or target.id == msg.from_user.id):
        await msg.reply("❌ سازنده نباید خودش یا یک ربات را هدف بگیرد.")
        return
    await core.sweep_expired(msg.bot)
    cfg = await core.load_cfg()
    await _user(msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or "")
    w, err = await core.create_wager(
        game, cid, (msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or ""), amount,
        (target.id, target.full_name or "", target.username or "") if target else None, cfg)
    if err:
        await msg.reply(err)
        return
    LIVE[w.id] = {"wid": w.id, "chat": cid, "kind": game, "players": set(), "answer": None, "n": None,
                  "until": time.monotonic() + cfg["ttl"] + 5}
    text, kb = render_race(w)
    try:
        sent = await msg.reply(text, reply_markup=kb, parse_mode="HTML")
    except Exception:  # noqa: BLE001 — پیام نرفت: مبلغ را برگردان
        logger.exception("race #%s: sending the message failed; refunding", w.id)
        LIVE.pop(w.id, None)
        async with core._wlock(w.id):
            await core.cancel_wager(w.id, w.creator_id)
        raise
    async with Session() as s:
        row = await s.get(PlatformWager, w.id)
        row.message_id = sent.message_id
        await s.commit()


async def _still_active(wid: int) -> bool:
    async with Session() as s:
        w = await s.get(PlatformWager, wid)
    return bool(w and w.status in core.ACTIVE)


async def _edit(bot: Bot, w: PlatformWager) -> None:
    if not w.message_id:
        return
    text, kb = render_race(w)
    try:
        await bot.edit_message_text(text, chat_id=w.chat_id, message_id=w.message_id, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("race edit failed: %s", e)


async def _cb_guard(cb: CallbackQuery) -> bool:
    if not cb.message or not await _group_allowed(cb.message.chat.id) or not await enabled():
        await cb.answer("قابلیت فعال نیست.", show_alert=True)
        return False
    return True


def _wid(cb: CallbackQuery) -> int:
    parts = (cb.data or "").split(":")
    return _int(parts[2]) if len(parts) > 2 else 0


@platform_wager_race_router.callback_query(F.data.startswith("pgr:join:"))
async def race_join(cb: CallbackQuery):
    if not await _cb_guard(cb):
        return
    if not await _bet_enabled():
        await cb.answer("شرط‌بندی فعال نیست.", show_alert=True)
        return
    wid = _wid(cb)
    await _user(cb.from_user.id, cb.from_user.full_name or "", cb.from_user.username or "")
    async with core._wlock(wid):
        r = await join_race(wid, (cb.from_user.id, cb.from_user.full_name or "", cb.from_user.username or ""))
    if r.w is not None and r.w.status == "playing":
        st = core._st(r.w)
        LIVE[r.w.id] = {"wid": r.w.id, "chat": r.w.chat_id, "kind": r.w.game,
                        "players": {r.w.creator_id, r.w.opponent_id},
                        "answer": st.get("a"), "n": st.get("n"),
                        "until": time.monotonic() + SECS[r.w.game] + 5}
        await _edit(cb.bot, r.w)
        if r.w.game != "guess":
            asyncio.create_task(_expire_later(cb.bot, r.w.id, SECS[r.w.game]))
        else:
            asyncio.create_task(_expire_later(cb.bot, r.w.id, SECS["guess"]))
    elif r.w is not None:
        await _edit(cb.bot, r.w)
    await cb.answer(r.err or "به مسابقه پیوستی؛ موفق باشی!", show_alert=bool(r.err))


async def _expire_later(bot: Bot, wid: int, secs: int) -> None:
    """مهلتِ بازی: اگر هنوز در حالِ بازی است برگشتِ هر دو + ویرایشِ پیام (حلقهٔ هسته هم همین را به‌عنوان پشتیبان دارد)."""
    await asyncio.sleep(secs + 1)
    try:
        async with core._wlock(wid):
            w = await core.expire_one(wid)
        if w is not None:
            LIVE.pop(w.id, None)
            await _edit(bot, w)
            await bot.send_message(w.chat_id, "⌛ کسی برنده نشد؛ مبلغِ هر دو نفر برگشت. 👻")
    except Exception:  # noqa: BLE001
        logger.exception("race expire #%s failed", wid)


@platform_wager_race_router.callback_query(F.data.startswith("pgr:cancel:"))
async def race_cancel(cb: CallbackQuery):
    if not await _cb_guard(cb):
        return
    wid = _wid(cb)
    async with core._wlock(wid):
        r = await core.cancel_wager(wid, cb.from_user.id)
    if r.w is not None:
        LIVE.pop(r.w.id, None)
        await _edit(cb.bot, r.w)
    await cb.answer(r.err or "لغو شد و مبلغ برگشت.", show_alert=bool(r.err))


def _match(m: Message) -> dict | None:
    """مسابقه‌ای که این پیام پاسخِ بازیکنِ آن است (هر نفر فقط در یک مسابقهٔ زنده بازیکن است)."""
    if not m.from_user or not m.text:
        return None
    t = norm_answer(m.text)
    lv = player_race(m.chat.id, m.from_user.id)
    if not lv:
        return None
    if lv["kind"] == "guess":
        return lv if (t.isdigit() and len(t) <= 3 and 1 <= int(t) <= 100) else None
    return lv if t == lv["answer"] else None


def _is_answer(m: Message) -> bool:
    return _match(m) is not None


@platform_wager_race_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_answer))
async def race_answer(msg: Message):
    live = _match(msg)
    if not live:
        raise SkipHandler
    uid, now = msg.from_user.id, time.monotonic()
    t = norm_answer(msg.text)
    if live["kind"] == "guess":
        if now - _LAST_GUESS.get((msg.chat.id, uid), -1e9) < GUESS_GAP:
            return
        _LAST_GUESS[(msg.chat.id, uid)] = now
        n = int(t)
        if n < live["n"]:
            return await msg.reply("⬆️ بالاتر")
        if n > live["n"]:
            return await msg.reply("⬇️ پایین‌تر")
    w = await settle_to(live["wid"], uid)
    if w is None:
        if not await _still_active(live["wid"]):
            LIVE.pop(live["wid"], None)
        return
    LIVE.pop(live["wid"], None)
    await _edit(msg.bot, w)
    await msg.reply(f"🏁 <b>{_html.escape(msg.from_user.full_name or str(uid))}</b> برنده شد!\n"
                    f"🎁 {_rial(w.payout_amount)} به کیف پولش اضافه شد.", parse_mode="HTML")


async def help_section() -> str:
    """بخشِ «مسابقه‌هایِ پولیِ حدس عدد / سرعت / ریاضی» برایِ پیامِ راهنمای گروه؛ خالی اگر خاموش است."""
    if not await enabled():
        return ""
    cfg = await core.load_cfg()
    ex = core.example_amount(cfg)
    lim = (f"از {_rial(cfg['min'])} تا {_rial(cfg['max'])}" if cfg.get("max") else f"حداقل {_rial(cfg['min'])}")
    fee = (f"کارمزد {float(cfg['fee_pct']):g}٪ از جایزهٔ برنده کم می‌شود." if float(cfg.get("fee_pct") or 0) > 0
           else "کارمزدی ندارد.")
    return "\n".join([
        "💰 <b>مسابقه‌هایِ پولیِ دونفره (با کیف پول)</b>",
        f"🎯 <b>حدس عدد</b>: «شرطبندی حدس عدد {ex}» — عددی بین ۱ تا ۱۰۰؛ اولین حدسِ دقیق برنده است.",
        f"⚡️ <b>سرعت</b>: «شرطبندی سرعت {ex}» — اولین نفری که کلمهٔ ربات را دقیق بنویسد برنده است.",
        f"🧮 <b>ریاضی</b>: «شرطبندی ریاضی {ex}» — اولین جوابِ درست برنده است.",
        "ℹ️ اگر فقط «حدس عدد» / «سرعت» / «ریاضی» (بدونِ «شرطبندی» و مبلغ) بنویسی، بازیِ رایگان با شیرینی شروع می‌شود.",
        f"عددِ آخر مبلغِ هر نفر به تومان است ({lim}). هر کس بخواهد می‌تواند مسابقهٔ خودش را بسازد یا به مسابقهٔ دیگران "
        "بپیوندد؛ چند مسابقه هم‌زمان برگزار می‌شود. برای بازی با یک نفرِ مشخص، دستور را روی پیامِ او ریپلای کن.",
        f"{fee} کسی برنده نشد یا حریفی نیامد ⇒ پولِ هر دو کامل برمی‌گردد.",
        "⚠️ ریسکِ مالی دارد؛ مسئولیتش با خودِ توست."])
