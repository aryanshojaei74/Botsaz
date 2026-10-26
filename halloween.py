"""halloween.py — «🎃 تم هالوین» برایِ همهٔ ربات‌هایِ بات‌ساز (مدیرِ پلتفرم با یک دکمه روشن/خاموش می‌کند).

کلیدِ سراسری: Setting(bot_id=0, key="halloween_on") + مهلتِ خودکار «halloween_until» (UTC iso)؛ بعد از مهلت خودبه‌خود خاموش
می‌شود تا فراموش‌شده نماند. هیچ دادهٔ رباتی دست‌کاری نمی‌شود؛ هر جا لازم است، لحظهٔ نمایش کلید خوانده می‌شود، پس خاموش‌کردن
فوری و کاملاً برگشت‌پذیر است.

اثرِ روشن‌بودن (برایِ هر ربات مشتری):
  • مینی‌اپ: اگر مالک رنگِ پس‌زمینه/کارت نگذاشته باشد، بنفشِ تیره (مالکِ رنگ‌گذار دست‌نخورده).
  • /start: یک پیامِ «🎃 Trick or Treat» با دکمه (هر کاربر روزی یک بار بازی می‌کند).
  • بازی: نتیجه سمتِ سرور (secrets) تعیین می‌شود؛ جایزه‌اش «شیرینی 🍬» است (امتیازِ سرگرمی)، نه پول — پس هزینه‌ای برایِ مالکِ ربات ندارد.
  • جدولِ شیرینی‌هایِ هر ربات (۱۰ نفرِ برتر).
callback: hw:tot (بازی) · hw:top (جدول).
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from config import Session
from models import BotUser, Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

halloween_router = Router(name="halloween")

KEY_ON, KEY_UNTIL = "halloween_on", "halloween_until"
THEME_BG, THEME_CARD = "#1b0f2e", "#2a1744"
DEFAULT_DAYS = 7
_CACHE: dict = {"v": None, "t": 0.0}
_TTL = 8.0

# (وزن، شیرینی، عنوان، متن)
OUTCOMES = [
    (60, 2, "🍬 Treat!", "یک مشت شیرینیِ خوشمزه گیرت اومد! +۲ 🍬"),
    (20, 4, "🍫 Big Treat!", "جعبهٔ شکلات! +۴ 🍬"),
    (5, 10, "🎃 جکپاتِ کدوی طلایی!", "کدوی طلایی نصیبت شد! +۱۰ 🍬🍬🍬"),
    (15, 0, "👻 Trick!", "روحِ شیطون شیرینی‌هات رو قاپید… این دفعه چیزی نصیبت نشد! 😈"),
]


def _tehran_day(now: datetime | None = None) -> str:
    now = now or datetime.utcnow()
    return (now + timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d")


async def _pget(key: str) -> str:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == key))
    return (r.value or "") if r else ""


async def _pset(key: str, value: str) -> None:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == key))
        if r:
            r.value = value
        else:
            s.add(Setting(bot_id=0, key=key, value=value))
        await s.commit()
    _CACHE["t"] = 0.0


async def is_on(now: datetime | None = None) -> bool:
    """روشن بودنِ تم؛ مهلتِ گذشته = خاموش. نتیجه ۸ ثانیه کش می‌شود (همهٔ پردازش‌ها زود هم‌خوان می‌شوند)."""
    t = time.monotonic()
    if now is None and _CACHE["v"] is not None and t - _CACHE["t"] < _TTL:
        return _CACHE["v"]
    try:
        on = (await _pget(KEY_ON)) == "1"
        if on:
            until = (await _pget(KEY_UNTIL)).strip()
            if until:
                try:
                    on = (now or datetime.utcnow()) < datetime.fromisoformat(until)
                except ValueError:
                    pass
    except Exception:
        logger.exception("halloween: خواندنِ وضعیت ناموفق بود (خاموش فرض شد)")
        return False
    if now is None:
        _CACHE.update(v=on, t=t)
    return on


async def set_on(on: bool, days: int = DEFAULT_DAYS, now: datetime | None = None) -> None:
    now = now or datetime.utcnow()
    await _pset(KEY_ON, "1" if on else "0")
    await _pset(KEY_UNTIL, (now + timedelta(days=days)).isoformat() if on else "")


async def theme_colors(bot_id: int, kind: str) -> str:
    """رنگِ تمِ هالوین برایِ مینی‌اپ (kind: bg|card)؛ خالی اگر خاموش است."""
    return (THEME_BG if kind == "bg" else THEME_CARD) if await is_on() else ""


# ───────────────────────── بازی ─────────────────────────

def _ukey(uid: int) -> str:
    return f"hw:u:{uid}"


def _parse(v: str) -> tuple[int, str, int]:
    try:
        c, d, n = (v or "0||0").split("|")
        return int(c), d, int(n)
    except ValueError:
        return 0, "", 0


def pick_outcome(rnd=None):
    total = sum(o[0] for o in OUTCOMES)
    x = (rnd.randrange(total) if rnd else secrets.randbelow(total))
    for o in OUTCOMES:
        x -= o[0]
        if x < 0:
            return o
    return OUTCOMES[0]


async def play(bot_id: int, uid: int, now: datetime | None = None, rnd=None) -> dict:
    """روزی یک بار. خروجی: {"played": bool, "candies": کل, "gain": n, "title", "text"}"""
    day = _tehran_day(now)
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == _ukey(uid)))
        c, d, n = _parse(row.value if row else "")
        if d == day:
            return {"played": False, "candies": c, "gain": 0}
        _w, gain, title, text = pick_outcome(rnd)
        val = f"{c + gain}|{day}|{n + 1}"
        if row:
            row.value = val
        else:
            s.add(Setting(bot_id=bot_id, key=_ukey(uid), value=val))
        await s.commit()
    return {"played": True, "candies": c + gain, "gain": gain, "title": title, "text": text}


async def leaderboard(bot_id: int, limit: int = 10) -> list[tuple[int, int, str]]:
    """[(uid, candies, name)] به ترتیبِ شیرینیِ بیشتر."""
    async with Session() as s:
        rows = (await s.scalars(select(Setting).where(Setting.bot_id == bot_id, Setting.key.like("hw:u:%")))).all()
        scored = []
        for r in rows:
            c, _d, _n = _parse(r.value)
            try:
                uid = int(r.key.split(":")[2])
            except (IndexError, ValueError):
                continue
            if c > 0:
                scored.append((uid, c))
        scored.sort(key=lambda x: -x[1])
        top = scored[:limit]
        names = {}
        if top:
            us = (await s.scalars(select(BotUser).where(BotUser.bot_id == bot_id,
                                                        BotUser.telegram_id.in_([u for u, _ in top])))).all()
            names = {u.telegram_id: (u.full_name or "") for u in us}
    return [(u, c, (names.get(u) or "ناشناس").split()[0][:14]) for u, c in top]


async def stats() -> dict:
    """آمارِ کلِ پلتفرم برایِ پنلِ مدیر."""
    async with Session() as s:
        rows = (await s.scalars(select(Setting).where(Setting.key.like("hw:u:%")))).all()
    players = len(rows)
    plays = sum(_parse(r.value)[2] for r in rows)
    bots = len({r.bot_id for r in rows})
    return {"players": players, "plays": plays, "bots": bots}


def _tot_kb(played: bool) -> InlineKeyboardMarkup:
    rows = []
    if not played:
        rows.append([InlineKeyboardButton(text="🎃 Trick or Treat!", callback_data="hw:tot", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="🏆 جدولِ شیرینی‌ها", callback_data="hw:top", style=ButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_hook(msg, bot, row) -> None:
    """بعد از /start: اگر تم روشن است و کاربر امروز بازی نکرده، دعوتِ «Trick or Treat». هرگز /start را خراب نمی‌کند."""
    try:
        if not await is_on():
            return
        async with Session() as s:
            r = await s.scalar(select(Setting).where(Setting.bot_id == row.id, Setting.key == _ukey(msg.from_user.id)))
        _c, d, _n = _parse(r.value if r else "")
        if d == _tehran_day():
            return
        await msg.answer("🎃🦇 <b>هالوین رسید!</b> 👻🕸\n\nهر روز یک بار «Trick or Treat» بزن و شیرینی جمع کن 🍬\n"
                         "ببین کدوی طلایی نصیبت می‌شه یا روحِ شیطون شیرینی‌هات رو می‌دزده!",
                         parse_mode="HTML", reply_markup=_tot_kb(False))
    except Exception:
        logger.exception("halloween start hook failed")


@halloween_router.callback_query(F.data == "hw:tot")
async def hw_play(cb: CallbackQuery, bot) -> None:
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if not await is_on():
        return await cb.answer("🎃 جشنِ هالوین تمام شد! سال بعد دوباره 👻", show_alert=True)
    r = await play(row.id, cb.from_user.id)
    if not r["played"]:
        await cb.answer("امروز بازی کردی 👻 فردا دوباره بیا!", show_alert=True)
        return
    await cb.answer()
    text = (f"{r['title']}\n\n{r['text']}\n\n🍬 شیرینی‌هایِ تو: <b>{r['candies']}</b>\n"
            "⏳ فردا دوباره بیا و شانستو امتحان کن!")
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_tot_kb(True))
    except Exception:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=_tot_kb(True))


@halloween_router.callback_query(F.data == "hw:top")
async def hw_top(cb: CallbackQuery, bot) -> None:
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    top = await leaderboard(row.id)
    medals = ["🥇", "🥈", "🥉"] + ["🔸"] * 7
    lines = ["🏆 <b>جدولِ شیرینی‌هایِ هالوین</b>\n"]
    if not top:
        lines.append("هنوز کسی شیرینی جمع نکرده؛ اولین نفر تو باش 🎃")
    for i, (_u, c, name) in enumerate(top):
        lines.append(f"{medals[i]} {name} — {c} 🍬")
    await cb.answer()
    text = "\n".join(lines)
    played = False
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == row.id, Setting.key == _ukey(cb.from_user.id)))
    played = _parse(r.value if r else "")[1] == _tehran_day()
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_tot_kb(played))
    except Exception:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=_tot_kb(played))
