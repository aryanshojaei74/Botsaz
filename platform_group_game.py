"""platform_group_game.py — «🎯 حدس عدد» (بازیِ گروهیِ رایگان) فقط در گروهِ متصل به محافظِ بات‌ساز اصلی.

=== PATCH_GROUP_GUESS_GAME_V1 ===
• شروع: هر عضو بنویسد «حدس عدد» (یا /guess). ربات عددی بین ۱ تا ۱۰۰ انتخاب می‌کند (secrets)؛ اعضا فقط عدد می‌نویسند و
  ربات «⬆️ بالاتر» / «⬇️ پایین‌تر» می‌گوید. اولین نفرِ درست برنده است؛ ۵ دقیقه مهلت.
• امتیاز: برنده «شیرینی 🍬» می‌گیرد (۵ تا؛ اگر زیرِ ۳۰ ثانیه حدس بزند +۳). پول/کیف‌پول در کار نیست.
• «جدول» (یا /top): ۱۰ نفرِ برتر. روشن/خاموش: مدیر از پنلِ «🎃 تم هالوین» (کلید pgg_on، پیش‌فرض روشن).
• فقط در گروهِ محافظ؛ هر گروهِ دیگر دست‌نخورده (پیام به بقیهٔ هندلرها می‌رسد).
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
_LAST_END: dict[int, float] = {}
_LAST_GUESS: dict[tuple[int, int], float] = {}

_AR = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_START = re.compile(r"^\s*(?:/guess(?:@\w+)?|حدس[\s‌]*عدد)\s*$", re.I)
_TOP = re.compile(r"^\s*(?:/top(?:@\w+)?|جدول(?:[\s‌]*شیرینی(?:[\s‌]*ها)?)?)\s*$", re.I)


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
