"""platform_group_game.py — «🎯 حدس عدد» (بازیِ گروهیِ رایگان) فقط در گروهِ متصل به محافظِ بات‌ساز اصلی.

=== PATCH_GROUP_GUESS_GAME_V1 ===
• شروع: هر عضو بنویسد «حدس عدد» (یا /guess). ربات عددی بین ۱ تا ۱۰۰ انتخاب می‌کند (secrets)؛ اعضا فقط عدد می‌نویسند و
  ربات «⬆️ بالاتر» / «⬇️ پایین‌تر» می‌گوید. اولین نفرِ درست برنده است؛ ۵ دقیقه مهلت.
• امتیاز: برنده «شیرینی 🍬» می‌گیرد (۵ تا؛ اگر زیرِ ۳۰ ثانیه حدس بزند +۳). پول/کیف‌پول در کار نیست.
• «جدول» (یا /top): ۱۰ نفرِ برتر. روشن/خاموش: مدیر از پنلِ «🎃 تم هالوین» (کلید pgg_on، پیش‌فرض روشن).
• فقط در گروهِ محافظ؛ هر گروهِ دیگر دست‌نخورده (پیام به بقیهٔ هندلرها می‌رسد).

=== PATCH_GROUP_RACE_GAMES_V1 === دو بازیِ «مسابقهٔ سرعت» هم کنارش:
  ⚡️ «سرعت» (/speed): ربات یک کلمهٔ هالوینی می‌فرستد؛ اولین نفری که دقیقاً همان را بنویسد برنده است (۳ 🍬).
  🧮 «ریاضی» (/math): یک محاسبهٔ ساده؛ اولین جوابِ درست برنده است (۳ 🍬، زیرِ ۱۰ ثانیه ۵ 🍬).
  مهلتِ هر مسابقه ۶۰ ثانیه؛ پاسخِ غلط بی‌صدا نادیده گرفته می‌شود (گروه شلوغ نشود). در هر لحظه فقط یک بازی (از هر نوع).
"""
from __future__ import annotations

import asyncio
import html as _html
import re
import secrets
import time

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import select

from config import Session
from models import Setting
from platform_group_money import _group_allowed, _pset

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_group_game_router = Router(name="platform_group_game")

LOW, HIGH, TIMEOUT = 1, 100, 300
START_COOLDOWN, GUESS_GAP = 45, 2.5
BASE_POINTS, FAST_BONUS, FAST_SECS = 5, 3, 30
KEY_ON = "pgg_on"

_GAMES: dict[int, dict] = {}              # chat_id → {"n", "t0", "guesses", "mid"}
_RACES: dict[int, dict] = {}              # chat_id → {"kind", "answer", "t0"}
RACE_TIMEOUT, RACE_POINTS, RACE_FAST_BONUS, RACE_FAST_SECS = 60, 3, 2, 10
WORDS = ["کدوتقلبی", "جادوگر", "خون‌آشام", "اسکلت", "شبح", "عنکبوت", "خفاش", "قبرستان", "طلسم", "گربه‌سیاه",
         "قلعهٔ‌متروک", "جاروپرنده", "مومیایی", "دیوانه", "شیرینی‌هالوین", "TrickOrTreat", "HappyHalloween", "Boo"]
_LAST_END: dict[int, float] = {}
_LAST_GUESS: dict[tuple[int, int], float] = {}

_AR = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_START = re.compile(r"^\s*(?:/guess(?:@\w+)?|حدس[\s‌]*عدد)\s*$", re.I)
_HELP = re.compile(r"^\s*(?:/games(?:@\w+)?|بازی|بازی[\s‌]*ها|راهنمای[\s‌]*بازی(?:[\s‌]*ها)?)\s*$", re.I)
HELP_COOLDOWN = 20
_LAST_HELP: dict[int, float] = {}
_SPEED = re.compile(r"^\s*(?:/speed(?:@\w+)?|سرعت)\s*$", re.I)
_MATH = re.compile(r"^\s*(?:/math(?:@\w+)?|ریاضی)\s*$", re.I)
_TOP = re.compile(r"^\s*(?:/top(?:@\w+)?|جدول(?:[\s‌]*شیرینی(?:[\s‌]*ها)?)?)\s*$", re.I)


def _paid_busy(chat_id: int) -> bool:
    try:
        from platform_wager_race import busy_paid
        return busy_paid(chat_id)
    except Exception:  # noqa: BLE001
        return False


def parse_guess(text: str | None) -> int | None:
    t = (text or "").strip().translate(_AR)
    if not t.isdigit() or len(t) > 3:
        return None
    n = int(t)
    return n if LOW <= n <= HIGH else None


def points_for(elapsed: float) -> int:
    return BASE_POINTS + (FAST_BONUS if elapsed <= FAST_SECS else 0)


async def enabled() -> bool:
    return (await _pset(KEY_ON, "1")) != "0"


def _akey(uid: int) -> str:
    return f"pgg:{uid}"


async def add_points(uid: int, name: str, pts: int) -> int:
    """امتیازِ کلِ کاربر را زیاد می‌کند (Setting پلتفرم؛ مقدار «امتیاز|نام»)."""
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == _akey(uid)))
        cur = 0
        if r and "|" in (r.value or ""):
            try:
                cur = int(r.value.split("|", 1)[0])
            except ValueError:
                cur = 0
        val = f"{cur + pts}|{name[:40]}"
        if r:
            r.value = val
        else:
            s.add(Setting(bot_id=0, key=_akey(uid), value=val))
        await s.commit()
    return cur + pts


async def top(limit: int = 10) -> list[tuple[str, int]]:
    async with Session() as s:
        rows = (await s.scalars(select(Setting).where(Setting.bot_id == 0, Setting.key.like("pgg:%")))).all()
    out = []
    for r in rows:
        try:
            p, name = (r.value or "0|").split("|", 1)
            if int(p) > 0:
                out.append((name or "ناشناس", int(p)))
        except ValueError:
            continue
    out.sort(key=lambda x: -x[1])
    return out[:limit]


def _is_start(m: Message) -> bool:
    return bool(m.text and _START.match(m.text))


def _is_top(m: Message) -> bool:
    return bool(m.text and _TOP.match(m.text))


def _is_guess(m: Message) -> bool:
    return m.chat.id in _GAMES and parse_guess(m.text) is not None


async def _gate(msg: Message) -> bool:
    if not msg.from_user or msg.from_user.is_bot or msg.sender_chat:
        raise SkipHandler
    if not await _group_allowed(msg.chat.id) or not await enabled():
        raise SkipHandler
    return True


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_start))
async def gg_start(msg: Message):
    await _gate(msg)
    cid, now = msg.chat.id, time.monotonic()
    if _paid_busy(cid):
        return await msg.reply("🎮 یک مسابقهٔ پولی در گروه جریان دارد؛ بعد از تمام‌شدنش دوباره بنویس.")
    if cid in _RACES:
        return await msg.reply("🎮 یک مسابقهٔ سرعت جریان دارد؛ بعد از تمام‌شدنش دوباره بنویس.")
    if cid in _GAMES:
        return await msg.reply("🎯 یک بازیِ حدس عدد جریان دارد؛ عددت را بنویس (۱ تا ۱۰۰).")
    wait = START_COOLDOWN - (now - _LAST_END.get(cid, -1e9))
    if wait > 0:
        return await msg.reply(f"⏳ {int(wait) + 1} ثانیهٔ دیگر می‌شود دوباره شروع کرد.")
    g = {"n": LOW + secrets.randbelow(HIGH - LOW + 1), "t0": now, "guesses": 0}
    _GAMES[cid] = g
    sent = await msg.reply(
        "🎃 <b>حدس عدد!</b>\n\nیک عدد بین <b>۱ تا ۱۰۰</b> در ذهنم است. فقط عدد را بنویس؛ "
        "می‌گویم بالاتر است یا پایین‌تر.\n🍬 اولین نفرِ درست، شیرینی می‌گیرد (زیرِ ۳۰ ثانیه جایزهٔ بیشتر!)\n"
        f"⏱ مهلت: {TIMEOUT // 60} دقیقه", parse_mode="HTML")
    g["mid"] = sent.message_id
    asyncio.create_task(_expire(msg.bot, cid, g))


async def _expire(bot, cid: int, g: dict) -> None:
    await asyncio.sleep(TIMEOUT)
    if _GAMES.get(cid) is g:
        _GAMES.pop(cid, None)
        _LAST_END[cid] = time.monotonic()
        try:
            await bot.send_message(cid, f"⌛ وقت تمام شد! عدد <b>{g['n']}</b> بود 👻", parse_mode="HTML")
        except Exception:
            logger.debug("guess-game: پیامِ پایانِ مهلت نرفت")


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_guess))
async def gg_guess(msg: Message):
    await _gate(msg)
    cid, uid, now = msg.chat.id, msg.from_user.id, time.monotonic()
    g = _GAMES.get(cid)
    if not g:
        raise SkipHandler
    if now - _LAST_GUESS.get((cid, uid), -1e9) < GUESS_GAP:
        return                                   # فاصلهٔ بینِ حدس‌هایِ یک نفر؛ بی‌صدا
    _LAST_GUESS[(cid, uid)] = now
    n = parse_guess(msg.text)
    g["guesses"] += 1
    if n < g["n"]:
        return await msg.reply("⬆️ بالاتر")
    if n > g["n"]:
        return await msg.reply("⬇️ پایین‌تر")
    if _GAMES.get(cid) is not g:                 # هم‌زمان نفرِ دیگری برد
        return
    _GAMES.pop(cid, None)
    _LAST_END[cid] = now
    pts = points_for(now - g["t0"])
    name = msg.from_user.full_name or str(uid)
    total = await add_points(uid, name, pts)
    await msg.reply(f"🎉 <b>{_html.escape(name)}</b> درست حدس زد: <b>{g['n']}</b>!\n"
                    f"🍬 +{pts} شیرینی (مجموع: {total}) · {g['guesses']} حدس\n\n«جدول» را بنویس تا برترین‌ها را ببینی.",
                    parse_mode="HTML")


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_top))
async def gg_top(msg: Message):
    await _gate(msg)
    rows = await top()
    medals = ["🥇", "🥈", "🥉"] + ["🔸"] * 7
    lines = ["🏆 <b>جدولِ شیرینی‌ها</b>\n"]
    if not rows:
        lines.append("هنوز کسی امتیاز نگرفته؛ «حدس عدد» را بنویس و اولین نفر باش 🎃")
    for i, (name, p) in enumerate(rows):
        lines.append(f"{medals[i]} {_html.escape(name)} — {p} 🍬")
    await msg.reply("\n".join(lines), parse_mode="HTML")


# ───────────────────────── مسابقهٔ سرعت (کلمه / ریاضی) ─────────────────────────

def _norm_answer(t: str | None) -> str:
    return (t or "").strip().translate(_AR).replace("\u200c", "").replace(" ", "").lower()


def make_math(rnd=secrets) -> tuple[str, str]:
    a, b = 2 + rnd.randbelow(18), 2 + rnd.randbelow(18)
    if rnd.randbelow(3) == 0:
        return f"{a} × {b % 10 + 2}", str(a * (b % 10 + 2))
    if rnd.randbelow(2):
        return f"{a} + {b}", str(a + b)
    hi, lo = max(a, b) + 10, min(a, b)
    return f"{hi} − {lo}", str(hi - lo)


def _is_speed(m: Message) -> bool:
    return bool(m.text and _SPEED.match(m.text))


def _is_math(m: Message) -> bool:
    return bool(m.text and _MATH.match(m.text))


def _is_race_answer(m: Message) -> bool:
    r = _RACES.get(m.chat.id)
    return bool(r and m.text and _norm_answer(m.text) == r["answer"])


async def _start_race(msg: Message, kind: str) -> None:
    await _gate(msg)
    cid, now = msg.chat.id, time.monotonic()
    if cid in _GAMES or cid in _RACES or _paid_busy(cid):
        return await msg.reply("🎮 یک بازی جریان دارد؛ بعد از تمام‌شدنش دوباره بنویس.")
    wait = START_COOLDOWN - (now - _LAST_END.get(cid, -1e9))
    if wait > 0:
        return await msg.reply(f"⏳ {int(wait) + 1} ثانیهٔ دیگر می‌شود دوباره شروع کرد.")
    if kind == "word":
        word = secrets.choice(WORDS)
        race = {"kind": kind, "answer": _norm_answer(word), "shown": word, "t0": now}
        text = (f"⚡️ <b>مسابقهٔ سرعت!</b>\n\nاولین نفری که دقیقاً این را بنویسد برنده است:\n\n"
                f"👉 <b>{_html.escape(word)}</b>\n\n⏱ {RACE_TIMEOUT} ثانیه")
    else:
        q, ans = make_math()
        race = {"kind": kind, "answer": ans, "shown": ans, "t0": now}
        text = f"🧮 <b>ریاضیِ سریع!</b>\n\nجوابِ این چیست؟\n\n👉 <b>{q} = ؟</b>\n\n⏱ {RACE_TIMEOUT} ثانیه"
    _RACES[cid] = race
    await msg.reply(text, parse_mode="HTML")
    asyncio.create_task(_expire_race(msg.bot, cid, race))


async def _expire_race(bot, cid: int, race: dict) -> None:
    await asyncio.sleep(RACE_TIMEOUT)
    if _RACES.get(cid) is race:
        _RACES.pop(cid, None)
        _LAST_END[cid] = time.monotonic()
        try:
            await bot.send_message(cid, f"⌛ وقت تمام شد؛ کسی نتوانست! جواب: <b>{_html.escape(str(race['shown']))}</b> 👻",
                                   parse_mode="HTML")
        except Exception:
            logger.debug("race: پیامِ پایانِ مهلت نرفت")


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_speed))
async def gg_speed(msg: Message):
    await _start_race(msg, "word")


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_math))
async def gg_math(msg: Message):
    await _start_race(msg, "math")


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_race_answer))
async def gg_race_answer(msg: Message):
    await _gate(msg)
    cid, now = msg.chat.id, time.monotonic()
    race = _RACES.get(cid)
    if not race or _norm_answer(msg.text) != race["answer"]:
        raise SkipHandler
    _RACES.pop(cid, None)                       # اولین پاسخِ درست برنده است؛ هم‌زمان‌ها دیگر چیزی نمی‌بینند
    _LAST_END[cid] = now
    pts = RACE_POINTS + (RACE_FAST_BONUS if race["kind"] == "math" and now - race["t0"] <= RACE_FAST_SECS else 0)
    name = msg.from_user.full_name or str(msg.from_user.id)
    total = await add_points(msg.from_user.id, name, pts)
    await msg.reply(f"🏁 <b>{_html.escape(name)}</b> اول شد! ({now - race['t0']:.1f} ثانیه)\n🍬 +{pts} شیرینی (مجموع: {total})",
                    parse_mode="HTML")


# ───────────────────────── راهنمای بازی‌ها («بازی») ─────────────────────────
# === PATCH_GROUP_GAMES_HELP_V1 === هر کس در گروه بنویسد «بازی»، فهرستِ بازی‌ها و طرزِ شروعشان را می‌بیند.

def _is_help(m: Message) -> bool:
    return bool(m.text and _HELP.match(m.text))


async def help_text() -> str:
    lines = ["🎮 <b>بازی‌هایِ گروه</b>\n",
             "🆓 <b>رایگان (شیرینی 🍬)</b>",
             "🎯 <b>حدس عدد</b> — بنویس «حدس عدد»؛ عددی بین ۱ تا ۱۰۰ را حدس بزن، ربات می‌گوید بالاتر/پایین‌تر. اولین نفرِ درست ۵ 🍬 (زیرِ ۳۰ ثانیه ۸ 🍬).",
             "⚡️ <b>سرعت</b> — بنویس «سرعت»؛ اولین نفری که کلمهٔ ربات را دقیقاً بنویسد ۳ 🍬 می‌گیرد.",
             "🧮 <b>ریاضی</b> — بنویس «ریاضی»؛ اولین جوابِ درست ۳ 🍬 (زیرِ ۱۰ ثانیه ۵ 🍬).",
             "🏆 <b>جدول</b> — بنویس «جدول» تا ۱۰ نفرِ برترِ شیرینی‌ها را ببینی."]
    try:
        from platform_wager import _bet_enabled, game_enabled
        if await _bet_enabled():
            parts = []
            if await game_enabled("xo"):
                parts.append("ایکس‌او: «xo 10000»")
            if await game_enabled("rps"):
                parts.append("سنگ‌کاغذ‌قیچی: «سنگ کاغذ قیچی 10000»")
            if await game_enabled("dice"):
                parts.append("تاس: «تاس 10000»")
            try:
                from platform_wager_race import enabled as _race_on
                if await _race_on():
                    parts += ["حدس عدد: «حدس عدد 10000»", "سرعت: «سرعت 10000»", "ریاضی: «ریاضی 10000»"]
            except Exception:
                logger.debug("games help: بخشِ مسابقهٔ پولی خوانده نشد")
            if parts:
                lines += ["", "💰 <b>دونفره با کیف پول</b> (عددِ آخر مبلغِ هر نفر به تومان؛ روی پیامِ حریف ریپلای کن تا مستقیم بازی کنید):",
                          "• " + "\n• ".join(parts),
                          "⚠️ ریسکِ مالی دارد؛ مسئولیتش با خودِ توست."]
    except Exception:
        logger.debug("games help: بخشِ شرط‌بندی خوانده نشد")
    lines += ["", "ℹ️ در هر لحظه فقط یک بازیِ رایگان فعال است و بین دو بازی چند ثانیه فاصله می‌افتد."]
    return "\n".join(lines)


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_help))
async def gg_help(msg: Message):
    await _gate(msg)
    now = time.monotonic()
    if now - _LAST_HELP.get(msg.chat.id, -1e9) < HELP_COOLDOWN:
        return                                   # تکرارِ پشتِ‌سرهم: بی‌صدا (گروه شلوغ نشود)
    _LAST_HELP[msg.chat.id] = now
    await msg.reply(await help_text(), parse_mode="HTML")
