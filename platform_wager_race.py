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
from sqlalchemy import func, or_, select

import platform_wager as core
from config import Session
from models import PlatformWager
from platform_group_money import (ANON_NOTICE, _bet_enabled, _digits, _group_allowed, _int, _money, _name, _reply_target,
                                  _rial, _user)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_wager_race_router = Router(name="platform_wager_race")

RACE_GAMES = ("guess", "speed", "math", "pattern")
TENANT_RACE_GAMES = RACE_GAMES                         # ربات‌هایِ مشتری هم (فقط اگر افزونهٔ «بازی‌هایِ گروهی» را خریده باشند؛ گیتِ آن در addon_active است)
RACE_FA = {"guess": "حدس عدد", "speed": "سرعت", "math": "ریاضی", "pattern": "الگو"}
SECS = {"guess": 180, "speed": 45, "math": 45, "pattern": 60}
KEY_ON = "pgr_on"
GUESS_GAP = 1.2
_AMT = r"([۰-۹0-9,،]+)"
_PREFIX = r"(?:(?:شرط\s*بندی|بازی)\s+)?"
_PATTERNS = (
    ("guess", re.compile(rf"^{_PREFIX}حدس\s*عدد\s+{_AMT}$")),
    ("speed", re.compile(rf"^{_PREFIX}سرعت\s+{_AMT}$")),
    ("math", re.compile(rf"^{_PREFIX}ریاضی\s+{_AMT}$")),
    ("pattern", re.compile(rf"^{_PREFIX}الگو\s+{_AMT}$")),
)
_AR = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# حافظهٔ پردازش: wid → {"wid", "chat", "kind", "players": {uid,..}, "answer", "n", "until"}
# (چند مسابقهٔ هم‌زمان در یک گروه مجازند؛ پاسخِ هر نفر به مسابقه‌ای می‌رسد که خودش بازیکنش است.)
LIVE: dict[int, dict] = {}


def _live_chat(chat_id: int, bot_id: int = 0) -> list[dict]:
    """مسابقه‌هایِ پولیِ زنده (منتظرِ حریف یا در حالِ بازی) در این گروه برایِ همین ربات (۰ = ربات اصلی؛ مستأجر = شناسهٔ ربات)؛
    مهلتِ گذشته = آزاد."""
    now = time.monotonic()
    return [lv for lv in LIVE.values()
            if lv.get("chat") == chat_id and lv.get("bot", 0) == bot_id and lv.get("until", 0) > now]


def busy_paid(chat_id: int, bot_id: int = 0) -> bool:
    return bool(_live_chat(chat_id, bot_id))


def player_race(chat_id: int, uid: int, bot_id: int = 0) -> dict | None:
    """مسابقهٔ در حالِ بازیِ این کاربر در این گروه (هر نفر هم‌زمان فقط در یک مسابقهٔ زنده بازی می‌کند)."""
    for lv in _live_chat(chat_id, bot_id):
        if uid in lv["players"]:
            return lv
    return None
_LAST_GUESS: dict[tuple[int, int], float] = {}
_LAST_NOTE: dict[tuple[int, int], float] = {}       # «نوبتِ تو نیست» حداکثر هر ۸ ثانیه برایِ هر نفر
_NO_RACE: dict[tuple[int, int, int], float] = {}    # کشِ منفیِ بازیابیِ مسابقه از دیتابیس
TURN_SECS = 180                                      # حدس عدد نوبتی: مهلتِ هر نوبت (نکردن = باخت)


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


def make_pattern(rnd=secrets) -> tuple[str, str]:
    """دنبالهٔ عددیِ تولیدیِ بدونِ ابهام؛ خروجی (متنِ دنباله با «؟» آخرش، جوابِ عددی)."""
    kind = rnd.randbelow(6)
    if kind == 0:                                    # حسابی: +d
        a, d = 1 + rnd.randbelow(20), 2 + rnd.randbelow(8)
        seq = [a + d * i for i in range(5)]
    elif kind == 1:                                  # هندسی: ×r
        a, r = 1 + rnd.randbelow(5), 2 + rnd.randbelow(2)
        seq = [a * r ** i for i in range(5)]
    elif kind == 2:                                  # مربع‌ها
        k = 1 + rnd.randbelow(6)
        seq = [(k + i) ** 2 for i in range(5)]
    elif kind == 3:                                  # گام‌هایِ متناوب +x، +y
        a, x, y = 1 + rnd.randbelow(10), 2 + rnd.randbelow(4), 6 + rnd.randbelow(4)
        seq = [a]
        for i in range(5):
            seq.append(seq[-1] + (x if i % 2 == 0 else y))
    elif kind == 4:                                  # اختلاف‌هایِ افزایشی: +1، +2، +3، …
        a = 1 + rnd.randbelow(10)
        seq = [a]
        for i in range(1, 5):
            seq.append(seq[-1] + i)
    else:                                            # فیبوناچی‌وار
        a, b = 1 + rnd.randbelow(4), 2 + rnd.randbelow(4)
        seq = [a, b]
        for _ in range(3):
            seq.append(seq[-1] + seq[-2])
    shown, ans = seq[:-1], seq[-1]
    return " ، ".join(str(n) for n in shown) + " ، ؟", str(ans)


def new_question(kind: str, scope: tuple = (0, 0)) -> dict:
    """پرسش و پاسخِ تازه برایِ هر نوع (state ذخیره‌شده در ردیفِ شرط)."""
    if kind == "guess":
        return {"n": 1 + secrets.randbelow(100), "tries": {}}
    if kind == "speed":
        from platform_group_game import pick_word      # PATCH_SPEED_WORDS_V1: در همین گروه تکراری نمی‌آید
        word = pick_word(scope)
        return {"q": word, "a": norm_answer(word)}
    if kind == "pattern":
        q, a = make_pattern()
        return {"q": q, "a": a}
    from platform_group_game import make_math
    q, a = make_math()
    return {"q": q, "a": a}


async def enabled() -> bool:
    return (await core._pset(KEY_ON, "0")) == "1"


def _game_key(game: str) -> str:
    return f"platform_wager_{game}"


async def game_on(game: str) -> bool:
    """روشن‌بودنِ یک مسابقه. اگر مدیر هنوز کلیدِ اختصاصیِ آن را نزده باشد، همان کلیدِ کلیِ قبلی (pgr_on) تعیین می‌کند
    (رفتارِ قبلی حفظ می‌شود)."""
    master = await enabled()
    v = (await core._pset(_game_key(game), "")).strip().lower()
    if not v:
        return master and game != "pattern"      # بازیِ تازه تا مدیر روشنش نکند خودکار فعال نمی‌شود
    return v in ("on", "1", "true") and master


async def toggle_game(game: str) -> bool:
    """روشن/خاموشِ یک مسابقه از «تنظیمات شرط‌بندی گروه»؛ روشن‌کردن کلیدِ کلیِ مسابقه‌ها را هم روشن می‌کند."""
    new = not await game_on(game)
    await core._set(_game_key(game), "on" if new else "off")
    if new:
        await core._set(KEY_ON, "1")
    return new


# ───────────────────────── نمایش ─────────────────────────

def _rule(w: PlatformWager) -> str:
    return {"guess": "عددی بین ۱ تا ۱۰۰؛ <b>به‌نوبت</b> حدس می‌زنید (ربات بالاتر/پایین‌تر می‌گوید). هر نوبت ۳ دقیقه مهلت دارد؛ "
                     "نوبتی که حدس نزند می‌بازد. اولین حدسِ دقیق برنده است.",
            "speed": "ربات یک کلمه می‌فرستد؛ اولین نفری که دقیقاً همان را بنویسد برنده است.",
            "math": "ربات یک محاسبه می‌فرستد؛ اولین جوابِ درست برنده است.",
            "pattern": "ربات یک دنبالهٔ عددی می‌فرستد؛ اولین نفری که عددِ بعدیِ دنباله را درست بنویسد برنده است."}[w.game]


def _kb_pending(w: PlatformWager) -> InlineKeyboardMarkup:
    px = "twr" if w.bot_id else "pgr"           # ربات مستأجر: callbackهایِ twr:* (tenant/shared/wager_race.py)
    if w.bot_id:                                # دکمهٔ شیشه‌ایِ ساده (ایموجیِ پرمیومِ بات‌ساز برایِ ربات مشتری معنا ندارد)
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ قبول و پیوستن", callback_data=f"{px}:join:{w.id}", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="❌ لغو", callback_data=f"{px}:cancel:{w.id}", style=ButtonStyle.DANGER)]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        hx_btn("✅ قبول و پیوستن", f"{px}:join:{w.id}", ButtonStyle.SUCCESS),
        hx_btn("❌ لغو", f"{px}:cancel:{w.id}", ButtonStyle.DANGER)]])


def hx_btn(text: str, cb: str, style):
    from premium_emoji import hx_button
    return hx_button(text, callback_data=cb, style=style)


def render_race(w: PlatformWager) -> tuple[str, InlineKeyboardMarkup]:
    wd = core.words(w.bot_id or 0)
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
            q = ("🔢 عددی بین <b>۱ تا ۱۰۰</b> انتخاب شد؛ <b>به‌نوبت</b> حدس بزنید.\n"
                 + (f"🎯 نوبتِ: <b>{core._who(w, w.turn_id)}</b> — عددت را در گروه بنویس." if w.turn_id
                    else "عددت را در گروه بنویس."))
        elif w.game == "speed":
            q = f"✍️ این را دقیقاً بنویس:\n\n👉 <b>{_html.escape(str(st.get('q', '')))}</b>"
        elif w.game == "pattern":
            q = f"🧩 عددِ بعدیِ دنباله چیست؟\n\n👉 <b>{_html.escape(str(st.get('q', '')))}</b>"
        else:
            q = f"🧮 جواب چیست؟\n\n👉 <b>{_html.escape(str(st.get('q', '')))} = ؟</b>"
        foot = ("⏱ هر نوبت ۳ دقیقه مهلت دارد؛ در مهلت حدس نزنی می‌بازی." if w.game == "guess" and w.turn_id
                else f"⏱ مهلت: {core._mins_left(w)}؛ کسی برنده نشد ⇒ مبلغِ هر دو برمی‌گردد.")
        text = (f"{head}\n\n{core._who(w, w.creator_id)}  ⚔️  {core._who(w, w.opponent_id)}\n{core._money_lines(w)}\n\n"
                f"{q}\n\n{foot}")
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
                                                              if w.game in ("math", "pattern") else f"کلمه: <b>{_html.escape(str(ans))}</b>")
        if w.end_reason == "forfeit":
            loser = w.opponent_id if w.winner_id == w.creator_id else w.creator_id
            lines += [f"⏰ <b>{core._who(w, loser)}</b> در مهلتِ نوبتش حدس نزد."]
        lines += [shown, "", f"🏁 برنده: <b>{core._who(w, w.winner_id)}</b>",
                  f"🎁 دریافتی برنده: <b>{_rial(w.payout_amount)}</b> (کارمزد {_rial(w.fee_amount)})"]
    else:
        lines += ["", f"⏰ کسی در مهلت برنده نشد؛ {wd['stake']} به هر دو نفر برگشت (بدون کارمزد)."]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[])


# ───────────────────────── عملیات ─────────────────────────

async def join_race(wid: int, user: tuple, bot_id: int = 0):
    """مثلِ core.join_wager، ولی با ساختِ پرسش؛ خروجی R(w, err, note)."""
    uid, uname, uuser = user
    async with Session() as s:
        w = await s.get(PlatformWager, wid, with_for_update=True)
        if not w or (w.bot_id or 0) != bot_id or w.game not in RACE_GAMES or w.status != "pending":
            return core._res(err="این مسابقه دیگر در انتظار نیست.")
        if w.creator_id == uid:
            return core._res(err="سازنده نمی‌تواند خودش به مسابقه بپیوندد.")
        if w.target_id and w.target_id != uid:
            return core._res(err="این مسابقه برای کاربری است که پیامش ریپلای شده.")
        if w.expires_at and w.expires_at <= core._now():
            await core._timeout_locked(s, w)
            await s.commit()
            return core._res(w=w, err="مهلتِ مسابقه تمام شده؛ مبلغ به سازنده برگشت.")
        if player_race(w.chat_id, uid, bot_id):
            return core._res(err="تا تمام‌شدنِ مسابقهٔ فعلی‌ات نمی‌توانی به مسابقهٔ دیگری بپیوندی.")
        if player_race(w.chat_id, w.creator_id, bot_id):
            return core._res(err="سازندهٔ این مسابقه همین حالا در مسابقهٔ دیگری است؛ چند لحظهٔ دیگر دوباره امتحان کن.")
        opp = await core._lock_user(s, uid, bot_id)
        if not opp or opp.is_banned or _money(opp.balance) < _money(w.amount):
            return core._res(err=f"موجودی کافی نیست؛ نیاز به {_rial(w.amount)} داری.")
        if await core._active_count(s, uid, bot_id) >= core.MAX_ACTIVE:
            return core._res(err=core._t(f"هم‌زمان حداکثر {core.MAX_ACTIVE} {{bet}} باز می‌توانی داشته باشی.", bot_id))
        opp.balance = Decimal(str(opp.balance)) - Decimal(str(w.amount))
        core._add_txn(s, bot_id, opp, -_money(w.amount), "wager_stake", f"پیوستن به {{bet}} #{w.id}")
        w.opponent_id, w.opponent_name, w.opponent_username = uid, uname or "", uuser or None
        w.status = "playing"
        st = new_question(w.game, (w.bot_id or 0, w.chat_id))
        w.state = json.dumps(st, ensure_ascii=False)
        w.expires_at = core._now() + timedelta(seconds=SECS[w.game])
        if w.game == "guess":                                  # نوبتی: قرعه برایِ نفرِ اول
            w.turn_id = secrets.choice([w.creator_id, uid])
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


# ───────────────────────── حدس عدد نوبتی ─────────────────────────

async def guess_step(bot: Bot, msg: Message, live: dict) -> str:
    """یک حدس در بازیِ نوبتی. خروجی: «win» (حدسِ دقیق؛ فراخوان تسویه کند) یا «done» (پاسخ داده شد/نادیده).

    فقط نفری که نوبتش است حدس می‌زند؛ حدسِ غلط نوبت را به حریف می‌دهد و مهلتِ ۳دقیقه‌ایِ تازه شروع می‌شود (در DB:
    turn_id/expires_at). نوبتی که در مهلت حدس نزند در هستهٔ platform_wager (_timeout_locked) می‌بازد."""
    uid, now = msg.from_user.id, time.monotonic()
    n = int(norm_answer(msg.text))
    wid = live["wid"]
    note = None
    async with core._wlock(wid):
        async with Session() as s:
            w = await s.get(PlatformWager, wid, with_for_update=True)
            if not w or w.status != "playing":
                LIVE.pop(wid, None)
                return "done"
            if w.expires_at and w.expires_at <= core._now():
                return "done"                                 # تسویه با مهلت (تایمر/حلقهٔ هسته)
            if w.turn_id and w.turn_id != uid:
                note = f"⏳ نوبتِ <b>{core._who(w, w.turn_id)}</b> است؛ منتظر بمان."
                hint = None
            elif n == live["n"]:
                return "win"
            else:
                hint = "⬆️ بالاتر" if n < live["n"] else "⬇️ پایین‌تر"
                if w.turn_id:
                    w.turn_id = w.opponent_id if uid == w.creator_id else w.creator_id
                    w.expires_at = core._now() + timedelta(seconds=TURN_SECS)
                    await s.commit()
                    live["until"] = now + TURN_SECS + 10
                    note = (f"{hint}\n🎯 نوبتِ <b>{core._who(w, w.turn_id)}</b> — ۳ دقیقه مهلت داری.")
                else:                                         # بازیِ قدیمیِ هم‌زمان (قبل از نوبتی‌شدن)
                    note = hint
    if hint is None:                                          # حدس از نفرِ بی‌نوبت
        if now - _LAST_NOTE.get((msg.chat.id, uid), -1e9) < 8:
            return "done"
        _LAST_NOTE[(msg.chat.id, uid)] = now
        await msg.reply(note, parse_mode="HTML")
        return "done"
    await msg.reply(note, parse_mode="HTML")
    if w.turn_id:
        asyncio.create_task(_expire_later(bot, wid, TURN_SECS))
        await _edit(bot, w)
    return "done"


async def restore_live(chat_id: int, uid: int, bot_id: int = 0) -> dict | None:
    """بعد از ری‌استارتِ ربات، بازیِ حدس عددِ جاری از DB دوباره ساخته می‌شود (عددِ مخفی در state ذخیره است)."""
    key, now = (chat_id, uid, bot_id), time.monotonic()
    if _NO_RACE.get(key, 0) > now:
        return None
    async with Session() as s:
        w = await s.scalar(select(PlatformWager).where(
            PlatformWager.chat_id == chat_id, func.coalesce(PlatformWager.bot_id, 0) == bot_id,
            PlatformWager.game == "guess", PlatformWager.status == "playing",
            or_(PlatformWager.creator_id == uid, PlatformWager.opponent_id == uid)))
    st = core._st(w) if w else {}
    if not w or not w.expires_at or w.expires_at <= core._now() or "n" not in st:
        _NO_RACE[key] = now + 20
        return None
    left = max(5.0, (w.expires_at - core._now()).total_seconds() + 5)
    lv = {"wid": w.id, "chat": chat_id, "bot": bot_id, "kind": "guess", "players": {w.creator_id, w.opponent_id},
          "answer": None, "n": st["n"], "until": now + left}
    LIVE[w.id] = lv
    return lv


def _digit_guess(m: Message) -> bool:
    t = norm_answer(m.text) if m.text else ""
    return bool(m.from_user and t.isdigit() and len(t) <= 3 and 1 <= int(t) <= 100)


# ───────────────────────── هندلرها ─────────────────────────

def _is_create(m: Message) -> bool:
    return parse_race_command(m.text) is not None


_NO_AMOUNT = re.compile(r"^شرط\s*بندی\s+(حدس\s*عدد|سرعت|ریاضی|الگو)$")


def _is_paid_hint(m: Message) -> bool:
    return bool(m.text and _NO_AMOUNT.match(core._norm(m.text)))


@platform_wager_race_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_paid_hint))
async def race_paid_hint(msg: Message):
    """«شرطبندی حدس عدد» بدونِ مبلغ = مسابقهٔ پولی؛ مبلغ را می‌خواهد (بازیِ رایگان فقط با «حدس عدد» شروع می‌شود)."""
    if not msg.from_user or msg.from_user.is_bot or msg.sender_chat:
        raise SkipHandler
    if not await _group_allowed(msg.chat.id) or not await enabled() or not await _bet_enabled():
        raise SkipHandler
    game = {"حدسعدد": "حدس عدد", "سرعت": "سرعت", "ریاضی": "ریاضی", "الگو": "الگو"}[
        re.sub(r"\s+", "", _NO_AMOUNT.match(core._norm(msg.text)).group(1))]
    cfg = await core.load_cfg()
    ex = core.example_amount(cfg)
    lim = (f"از {_rial(cfg['min'])} تا {_rial(cfg['max'])}" if cfg.get("max") else f"حداقل {_rial(cfg['min'])}")
    await msg.reply(f"💰 برای مسابقهٔ پولیِ <b>{game}</b> مبلغ را هم بنویس (به تومان، {lim}):\n"
                    f"<code>شرطبندی {game} {ex}</code>"
                    + ("" if game == "الگو" else f"\n\n🆓 بازیِ رایگان با شیرینی: فقط «{game}» را بنویس."),
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
    if not await game_on(game):
        await msg.reply(f"❌ مسابقهٔ {RACE_FA[game]} فعلاً فعال نیست.")
        return
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
    LIVE[w.id] = {"wid": w.id, "chat": cid, "bot": 0, "kind": game, "players": set(), "answer": None, "n": None,
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
    kb = (await core.result_kb(w)) or kb               # PATCH_WAGER_RESULT_KB_V1 (فقط ربات‌هایِ مستأجر)
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
        LIVE[r.w.id] = {"wid": r.w.id, "chat": r.w.chat_id, "bot": 0, "kind": r.w.game,
                        "players": {r.w.creator_id, r.w.opponent_id},
                        "answer": st.get("a"), "n": st.get("n"),
                        "until": time.monotonic() + SECS[r.w.game] + 5}
        await _edit(cb.bot, r.w)
        asyncio.create_task(_expire_later(cb.bot, r.w.id, SECS[r.w.game]))
        await _announce_turn(cb.bot, r.w)
    elif r.w is not None:
        await _edit(cb.bot, r.w)
    await cb.answer(r.err or "به مسابقه پیوستی؛ موفق باشی!", show_alert=bool(r.err))


async def _announce_turn(bot: Bot, w: PlatformWager) -> None:
    """حدس عدد نوبتی: بعد از پیوستنِ حریف، نفرِ اول (قرعه) را در گروه صدا بزن."""
    if w.game != "guess" or not w.turn_id:
        return
    try:
        await bot.send_message(w.chat_id, f"🎲 قرعه‌کشی شد: نوبتِ <b>{core._who(w, w.turn_id)}</b> است؛ عددی بین ۱ تا ۱۰۰ بنویس "
                                          "(هر نوبت ۳ دقیقه مهلت؛ حدس نزنی می‌بازی).", parse_mode="HTML")
    except Exception:  # noqa: BLE001
        logger.info("race: turn announcement skipped")


async def _expire_later(bot: Bot, wid: int, secs: int) -> None:
    """مهلتِ بازی: اگر هنوز در حالِ بازی است برگشتِ هر دو + ویرایشِ پیام (حلقهٔ هسته هم همین را به‌عنوان پشتیبان دارد)."""
    await asyncio.sleep(secs + 1)
    try:
        async with core._wlock(wid):
            w = await core.expire_one(wid)
        if w is not None:
            LIVE.pop(w.id, None)
            await _edit(bot, w)
            if w.status == "settled" and w.end_reason == "forfeit":      # حدس عددِ نوبتی: نوبتِ‌دارِ ساکت باخت
                loser = w.opponent_id if w.winner_id == w.creator_id else w.creator_id
                await bot.send_message(
                    w.chat_id, f"⏰ <b>{core._who(w, loser)}</b> در ۳ دقیقه حدس نزد و باخت.\n"
                               f"🏁 <b>{core._who(w, w.winner_id)}</b> برنده شد! 🎁 {_rial(w.payout_amount)} به کیف پولش اضافه شد.",
                    parse_mode="HTML", reply_markup=await core.result_kb(w))
            else:
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


def _match(m: Message, bot_id: int = 0) -> dict | None:
    """مسابقه‌ای که این پیام پاسخِ بازیکنِ آن است (هر نفر فقط در یک مسابقهٔ زنده بازیکن است)."""
    if not m.from_user or not m.text:
        return None
    t = norm_answer(m.text)
    lv = player_race(m.chat.id, m.from_user.id, bot_id)
    if not lv:
        return None
    if lv["kind"] == "guess":
        return lv if (t.isdigit() and len(t) <= 3 and 1 <= int(t) <= 100) else None
    return lv if t == lv["answer"] else None


def _is_answer(m: Message) -> bool:
    return _match(m) is not None or _digit_guess(m)       # عددِ ۱–۱۰۰ هم رد می‌شود؛ بازیابیِ بعد از ری‌استارت در هندلر


@platform_wager_race_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_answer))
async def race_answer(msg: Message):
    live = _match(msg)
    if not live and msg.from_user and _digit_guess(msg):
        live = await restore_live(msg.chat.id, msg.from_user.id)
    if not live:
        raise SkipHandler
    uid, now = msg.from_user.id, time.monotonic()
    if live["kind"] == "guess":
        if now - _LAST_GUESS.get((msg.chat.id, uid), -1e9) < GUESS_GAP:
            return
        _LAST_GUESS[(msg.chat.id, uid)] = now
        if await guess_step(msg.bot, msg, live) != "win":
            return
    w = await settle_to(live["wid"], uid)
    if w is None:
        if not await _still_active(live["wid"]):
            LIVE.pop(live["wid"], None)
        return
    LIVE.pop(live["wid"], None)
    await _edit(msg.bot, w)
    await msg.reply(f"🏁 <b>{_html.escape(msg.from_user.full_name or str(uid))}</b> برنده شد!\n"
                    f"🎁 {_rial(w.payout_amount)} به کیف پولش اضافه شد.", parse_mode="HTML",
                    reply_markup=await core.result_kb(w))            # دکمه‌هایِ شیشه‌ایِ جایزه/موجودیِ برنده و بازنده


@platform_wager_race_router.callback_query(F.data.startswith("pgrset:"))
async def race_game_toggle(cb: CallbackQuery):
    from config import PLATFORM_ADMIN_ID
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    game = (cb.data or "").split(":", 1)[1]
    if game not in RACE_GAMES:
        await cb.answer()
        return
    await toggle_game(game)
    from platform_group_money import _edit_settings
    await _edit_settings(cb, "bet")


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
        f"🎯 <b>حدس عدد</b>: «شرطبندی حدس عدد {ex}» — عددی بین ۱ تا ۱۰۰؛ <b>به‌نوبت</b> حدس می‌زنید، هر نوبت ۳ دقیقه مهلت "
        "دارد (حدس نزنی می‌بازی)؛ اولین حدسِ دقیق برنده است.",
        f"⚡️ <b>سرعت</b>: «شرطبندی سرعت {ex}» — اولین نفری که کلمهٔ ربات را دقیق بنویسد برنده است.",
        f"🧮 <b>ریاضی</b>: «شرطبندی ریاضی {ex}» — اولین جوابِ درست برنده است.",
        f"🧩 <b>الگو</b>: «شرطبندی الگو {ex}» — ربات یک دنبالهٔ عددی می‌فرستد؛ اولین نفری که عددِ بعدی را بنویسد برنده است.",
        "ℹ️ اگر فقط «حدس عدد» / «سرعت» / «ریاضی» (بدونِ «شرطبندی» و مبلغ) بنویسی، بازیِ رایگان با شیرینی شروع می‌شود.",
        f"عددِ آخر مبلغِ هر نفر به تومان است ({lim}). هر کس بخواهد می‌تواند مسابقهٔ خودش را بسازد یا به مسابقهٔ دیگران "
        "بپیوندد؛ چند مسابقه هم‌زمان برگزار می‌شود. برای بازی با یک نفرِ مشخص، دستور را روی پیامِ او ریپلای کن.",
        f"{fee} کسی برنده نشد یا حریفی نیامد ⇒ پولِ هر دو کامل برمی‌گردد.",
        "⚠️ ریسکِ مالی دارد؛ مسئولیتش با خودِ توست."])
