"""
platform_dice.py — «🎲 بازی کن پول بگیر» روی خودِ رباتِ بات‌ساز (PATCH_DICE_GAME_V1).

همان منطقِ tenant/dice_game.py، ولی برای کاربرانِ خودِ بات‌ساز و کیف پولِ
PlatformUser. نتیجهٔ بازی را سرورِ تلگرام تعیین می‌کند (sendDice)، پس نه ادمین
و نه ربات نمی‌توانند در آن دست ببرند.

callbackها:
  ادمین (فقط سوپرادمین): padg:*
  کاربر: pdg:play:{id}
کاربر می‌تواند بازی را با زدنِ دکمه یا با نوشتنِ نامِ بازی شروع کند.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, StateFilter
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import func, select

from config import Session
from models import PlatformDiceGame, PlatformDicePlay, PlatformTxn, PlatformUser

platform_dice_router = Router(name="platform_dice")

# === PATCH_DICE_GAME_QUOTA_RACE_FIX_V1 ===
# همان مشکلِ tenant/dice_game.py اینجا هم بود: اگر کاربر چند پیامِ «تاس» را
# سریع پشتِ‌سرِهم بفرستد، چند run_play هم‌زمان اجرا می‌شدند و همه، قبل از اینکه
# نوبتِ قبلی commit شود، سهمیه را «خالی نشده» می‌دیدند — یعنی سهمیهٔ روزانه/
# هفتگی با ارسالِ سریع دور زده می‌شد. قفلِ per-user/per-game این را می‌بندد.
_play_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _get_play_lock(gid: int, telegram_id: int) -> asyncio.Lock:
    key = (gid, telegram_id)
    lock = _play_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _play_locks[key] = lock
    return lock

DICE_EMOJIS: dict[str, tuple[str, int, int]] = {
    "🎲": ("تاس", 1, 6),
    "🎯": ("دارت", 1, 6),
    "🎳": ("بولینگ", 1, 6),
    "🏀": ("بسکتبال", 1, 5),
    "⚽": ("فوتبال", 1, 5),
    "🎰": ("اسلات", 1, 64),
}
PERIOD_LABELS = {"day": "روزانه", "week": "هفتگی", "total": "در کل"}
MAX_FACES_FOR_PRIZES = 6

# اسلاتِ تلگرام از بین ۶۴ مقدارِ ممکن، فقط ۴ تا «بردِ واقعی» (سه‌تایی مشابه)
# دارد. طبقِ مستنداتِ Bot API مقادیرِ برنده همیشه همین‌ها هستند و ۶۴ همان
# «۷۷۷» (جکپاتِ اصلی) است. بقیهٔ ۶۰ حالت باخت‌اند و نباید جایزه‌ای داشته باشند.
SLOT_WIN_VALUES: dict[int, str] = {
    1: "🍋 🍋 🍋",
    22: "🍇 🍇 🍇",
    43: "🍫 🍫 🍫",
    64: "7️⃣ 7️⃣ 7️⃣  (۷۷۷)",
}


class PDgFSM(StatesGroup):
    prize = State()
    tries = State()


def _rial(n) -> str:
    return f"{int(n):,} تومان"


def faces_of(emoji: str) -> list[int]:
    if emoji == "🎰":
        return list(SLOT_WIN_VALUES.keys())
    _, lo, hi = DICE_EMOJIS.get(emoji, ("", 1, 6))
    if hi - lo + 1 > MAX_FACES_FOR_PRIZES:
        return []
    return list(range(lo, hi + 1))


def prizes_of(game: PlatformDiceGame) -> dict[str, int]:
    try:
        data = json.loads(game.prizes or "{}") or {}
    except Exception:
        data = {}
    return {str(k): int(v or 0) for k, v in data.items()}


def _period_start(period: str) -> datetime | None:
    now = datetime.utcnow()
    if period == "day":
        return now - timedelta(days=1)
    if period == "week":
        return now - timedelta(days=7)
    return None


def _period_duration(period: str) -> timedelta | None:
    if period == "day":
        return timedelta(days=1)
    if period == "week":
        return timedelta(days=7)
    return None


async def plays_used(game: PlatformDiceGame, telegram_id: int) -> int:
    async with Session() as s:
        q = select(func.count()).select_from(PlatformDicePlay).where(
            PlatformDicePlay.game_id == game.id,
            PlatformDicePlay.user_telegram_id == telegram_id)
        since = _period_start(game.tries_period)
        if since is not None:
            q = q.where(PlatformDicePlay.created_at >= since)
        return await s.scalar(q) or 0


# === PATCH_DICE_QUOTA_WAIT_MSG_V1 ===
# سهمیهٔ روزانه/هفتگی یک پنجرهٔ لغزان (rolling window) است، نه ریست‌شدن
# سرِ نیمه‌شب — یعنی وقتی سهمیه تمام می‌شود، اسلاتِ بعدی دقیقاً وقتی آزاد
# می‌شود که قدیمی‌ترین بازیِ داخلِ همان پنجره از بازه بیرون بیفتد. قبلاً
# فقط می‌گفت «بعداً امتحان کن»؛ حالا دقیقاً ساعت/دقیقهٔ باقی‌مانده (به
# وقتِ تهران) محاسبه و نمایش داده می‌شود.
try:
    IRAN_TZ = ZoneInfo("Asia/Tehran")
except Exception:
    IRAN_TZ = timezone(timedelta(hours=3, minutes=30))


async def _next_play_wait_text(game: PlatformDiceGame, telegram_id: int) -> str:
    """متنِ «چند ساعت/دقیقهٔ دیگر» تا اولین فرصتِ بعدیِ بازی، به وقتِ تهران."""
    duration = _period_duration(game.tries_period)
    if duration is None:
        # period == "total" → پنجره‌ای برای بازشدن وجود ندارد
        return "سهمیهٔ کلی‌ات تمام شده."

    since = _period_start(game.tries_period)
    async with Session() as s:
        oldest = await s.scalar(
            select(func.min(PlatformDicePlay.created_at)).where(
                PlatformDicePlay.game_id == game.id,
                PlatformDicePlay.user_telegram_id == telegram_id,
                PlatformDicePlay.created_at >= since))
    if oldest is None:
        return "بعداً امتحان کن."

    free_at_utc = oldest + duration
    remaining = free_at_utc - datetime.utcnow()
    if remaining.total_seconds() <= 0:
        return "همین الان دوباره امتحان کن."

    total_minutes = int(remaining.total_seconds() // 60) + 1  # گردکردنِ رو به بالا
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        left_txt = f"{hours} ساعت و {minutes} دقیقه"
    elif hours:
        left_txt = f"{hours} ساعت"
    else:
        left_txt = f"{minutes} دقیقه"

    free_at_tehran = free_at_utc.replace(tzinfo=timezone.utc).astimezone(IRAN_TZ)
    clock_txt = free_at_tehran.strftime("%H:%M")
    return f"{left_txt} دیگر (ساعت {clock_txt} به وقت تهران) می‌توانی دوباره بازی کنی."


async def _is_super(uid: int) -> bool:
    from config import PLATFORM_ADMIN_ID
    return uid == PLATFORM_ADMIN_ID


# ─────────────────────────── پنل مدیریت ───────────────────────────

@platform_dice_router.callback_query(F.data == "padg:menu")
async def padg_menu(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.clear()
    async with Session() as s:
        games = (await s.scalars(select(PlatformDiceGame).order_by(
            PlatformDiceGame.id))).all()
    rows = [[InlineKeyboardButton(
        text=("✅" if g.active else "⛔️") + f" {g.emoji} {g.title}",
        callback_data=f"padg:open:{g.id}")] for g in games]
    rows.append([InlineKeyboardButton(text="➕ افزودن بازی", callback_data="padg:add")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به پنل", callback_data="adm:back")])
    txt = ("🎲 <b>بازی کن پول بگیر — بات‌ساز</b>\n\n"
           "بازی‌ها از بازی‌های داخلیِ خودِ تلگرام استفاده می‌کنند، پس نتیجه را "
           "سرورِ تلگرام تعیین می‌کند و قابلِ دستکاری نیست.\n\n"
           "کاربر با زدنِ دکمهٔ «بازی کن پول بگیر» یا با نوشتنِ نامِ بازی "
           "(مثلاً «تاس») می‌تواند بازی کند و جایزه‌اش به کیف پولش اضافه می‌شود.\n\n"
           + (f"تعداد بازی‌های ثبت‌شده: <b>{len(games)}</b>" if games
              else "هنوز بازی‌ای ثبت نشده است."))
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_dice_router.callback_query(F.data == "padg:add")
async def padg_add(cb: CallbackQuery):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    async with Session() as s:
        used = set((await s.scalars(select(PlatformDiceGame.emoji))).all())
    rows = []
    for emoji, (name, lo, hi) in DICE_EMOJIS.items():
        if emoji in used:
            continue
        faces = hi - lo + 1
        note = f"{faces} حالت" if faces <= MAX_FACES_FOR_PRIZES else f"{faces} حالت — جایزهٔ یکسان"
        rows.append([InlineKeyboardButton(text=f"{emoji} {name} ({note})",
                                          callback_data=f"padg:new:{emoji}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="padg:menu")])
    if len(rows) == 1:
        await cb.answer("همهٔ بازی‌های تلگرام اضافه شده‌اند.", show_alert=True)
        return
    await cb.message.edit_text("➕ <b>افزودن بازی</b>\n\nکدام بازی؟",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                               parse_mode="HTML")
    await cb.answer()


@platform_dice_router.callback_query(F.data.startswith("padg:new:"))
async def padg_new(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    emoji = cb.data.split(":", 2)[2]
    if emoji not in DICE_EMOJIS:
        await cb.answer("بازی نامعتبر.", show_alert=True)
        return
    async with Session() as s:
        if await s.scalar(select(PlatformDiceGame).where(PlatformDiceGame.emoji == emoji)):
            await cb.answer("این بازی قبلاً ثبت شده.", show_alert=True)
            return
        g = PlatformDiceGame(emoji=emoji, title=DICE_EMOJIS[emoji][0], active=True,
                             prizes="{}", tries_limit=1, tries_period="day")
        s.add(g)
        await s.commit()
        gid = g.id
    await cb.answer("ثبت شد ✅")
    await _open(cb, gid, state)


async def _open(cb: CallbackQuery, gid: int, state: FSMContext):
    await state.clear()
    async with Session() as s:
        g = await s.get(PlatformDiceGame, gid)
        if not g:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        plays = await s.scalar(select(func.count()).select_from(PlatformDicePlay).where(
            PlatformDicePlay.game_id == gid)) or 0
        paid = await s.scalar(select(func.coalesce(func.sum(PlatformDicePlay.amount), 0)).where(
            PlatformDicePlay.game_id == gid)) or 0

    pr = prizes_of(g)
    faces = faces_of(g.emoji)
    if faces:
        lines = [f"  • {(SLOT_WIN_VALUES[v] if g.emoji == '🎰' else f'نتیجهٔ {v}')}: "
                 f"<b>{_rial(pr.get(str(v), 0))}</b>" for v in faces]
    else:
        lines = [f"  • هر نتیجه: <b>{_rial(pr.get('all', 0))}</b>"]
    limit_txt = ("نامحدود" if g.tries_limit == 0
                 else f"{g.tries_limit} بار {PERIOD_LABELS.get(g.tries_period, '')}")
    txt = (f"{g.emoji} <b>{g.title}</b>\n\n"
           f"وضعیت: {'✅ فعال' if g.active else '⛔️ غیرفعال'}\n"
           f"سهمیهٔ هر کاربر: <b>{limit_txt}</b>\n"
           f"بازی در گروه‌ها: {'✅ مجاز' if g.in_groups else '⛔️ ممنوع'}\n"
           + (f"گروهِ متصل: <b>{g.group_title or g.group_chat_id}</b>\n\n"
              if g.group_chat_id else "گروهِ متصل: — (در هر گروهی کار می‌کند)\n\n")
           + 
           "🎁 <b>جایزه‌ها</b>\n" + "\n".join(lines) + "\n\n"
           f"📊 تا حالا <b>{plays}</b> بار بازی شده و مجموعاً <b>{_rial(paid)}</b> پرداخت شده.")

    rows = []
    if faces:
        for i in range(0, len(faces), 3):
            rows.append([InlineKeyboardButton(
                text=f"{(SLOT_WIN_VALUES[v] if g.emoji == '🎰' else f'{v}️⃣')} {pr.get(str(v), 0):,}",
                callback_data=f"padg:prize:{gid}:{v}")
                for v in faces[i:i + 3]])
    else:
        rows.append([InlineKeyboardButton(text="🎁 جایزهٔ هر برد",
                                          callback_data=f"padg:prize:{gid}:all")])
    rows.append([InlineKeyboardButton(text=f"🔢 سهمیهٔ هر کاربر ({limit_txt})",
                                      callback_data=f"padg:tries:{gid}")])
    rows.append([InlineKeyboardButton(text=f"🔁 بازهٔ سهمیه ({PERIOD_LABELS.get(g.tries_period)})",
                                      callback_data=f"padg:period:{gid}")])
    rows.append([InlineKeyboardButton(
        text=("🌐 بازی در گروه‌ها: ✅ مجاز" if g.in_groups else "🌐 بازی در گروه‌ها: ⛔️ ممنوع"),
        callback_data=f"padg:groups:{gid}")])
    if g.group_chat_id:
        rows.append([InlineKeyboardButton(text="📢 ارسال دکمهٔ بازی در گروه",
                                          callback_data=f"padg:post:{gid}")])
        rows.append([InlineKeyboardButton(text="❌ قطعِ اتصال از گروه",
                                          callback_data=f"padg:unbind:{gid}")])
    else:
        rows.append([InlineKeyboardButton(text="🔗 اتصال به یک گروه",
                                          callback_data=f"padg:bind:{gid}")])
    rows.append([InlineKeyboardButton(
        text=("⛔️ غیرفعال کردن" if g.active else "✅ فعال کردن"),
        callback_data=f"padg:toggle:{gid}")])
    rows.append([InlineKeyboardButton(text="🗑 حذف این بازی", callback_data=f"padg:del:{gid}",
                                      style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="padg:menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_dice_router.callback_query(F.data.startswith("padg:open:"))
async def padg_open(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await _open(cb, int(cb.data.split(":")[2]), state)


@platform_dice_router.callback_query(F.data.startswith("padg:toggle:"))
async def padg_toggle(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(PlatformDiceGame, gid)
        if g:
            g.active = not g.active
            await s.commit()
    await _open(cb, gid, state)


# ───── اتصالِ بازی به یک گروهِ مشخص ─────

@platform_dice_router.callback_query(F.data.startswith("padg:bind:"))
async def padg_bind(cb: CallbackQuery, bot: Bot):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    gid = int(cb.data.split(":")[2])
    code = secrets.token_hex(3)
    async with Session() as s:
        g = await s.get(PlatformDiceGame, gid)
        if not g:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        g.bind_code = code
        await s.commit()
    me = await bot.get_me()
    await cb.message.answer(
        "🔗 <b>اتصال بازی به گروه</b>\n\n"
        f"۱) ربات <code>@{me.username}</code> را به گروه اضافه و ادمین کن.\n"
        f"۲) داخلِ همان گروه این را بفرست:\n\n<code>/dice {code}</code>\n\n"
        "بعد از آن ربات یک دکمهٔ «بازی» در گروه می‌فرستد که همه می‌توانند با آن بازی کنند.\n\n"
        "💡 برای اینکه <b>نوشتنِ اسمِ بازی</b> هم در گروه کار کند، Privacy Mode را از "
        "BotFather خاموش کن (Bot Settings ← Group Privacy ← Turn off). بدونِ آن تلگرام "
        "پیام‌های عادیِ گروه را به ربات نمی‌دهد و فقط دکمه و کامند کار می‌کند.",
        parse_mode="HTML")
    await cb.answer()


@platform_dice_router.message(Command("dice"))
async def pdg_bind_in_group(msg: Message, bot: Bot):
    if msg.chat.type not in ("group", "supergroup"):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        return
    if not await _is_super(msg.from_user.id):
        return
    code = parts[1].strip()
    async with Session() as s:
        g = await s.scalar(select(PlatformDiceGame).where(
            PlatformDiceGame.bind_code == code))
        if not g:
            await msg.reply("❌ کد نامعتبر یا منقضی است.")
            return
        g.group_chat_id = msg.chat.id
        g.group_title = (msg.chat.title or "")[:120]
        g.bind_code = None
        g.in_groups = True
        gid, emoji, title = g.id, g.emoji, g.title
        await s.commit()
    await msg.answer(
        f"✅ بازی <b>{emoji} {title}</b> به این گروه وصل شد.\n\n"
        "روی دکمهٔ زیر بزنید تا بازی کنید — جایزه به کیف پولتان اضافه می‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"{emoji} بازی کن پول بگیر",
                                 callback_data=f"pdg:play:{gid}")]]),
        parse_mode="HTML")


@platform_dice_router.callback_query(F.data.startswith("padg:post:"))
async def padg_post(cb: CallbackQuery, bot: Bot):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(PlatformDiceGame, gid)
        if not g or not g.group_chat_id:
            await cb.answer("این بازی به گروهی وصل نیست.", show_alert=True)
            return
        chat_id, emoji, title = g.group_chat_id, g.emoji, g.title
    try:
        await bot.send_message(
            chat_id,
            f"{emoji} <b>{title}</b>\n\nروی دکمه بزن، شانست را امتحان کن و "
            "جایزه‌ات را در کیف پولت بگیر!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"{emoji} بازی کن پول بگیر",
                                     callback_data=f"pdg:play:{gid}")]]),
            parse_mode="HTML")
        await cb.answer("ارسال شد ✅")
    except Exception as e:
        await cb.answer(f"ارسال نشد: {e}", show_alert=True)


@platform_dice_router.callback_query(F.data.startswith("padg:unbind:"))
async def padg_unbind(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(PlatformDiceGame, gid)
        if g:
            g.group_chat_id = None
            g.group_title = None
            g.bind_code = None
            await s.commit()
    await cb.answer("اتصال قطع شد.")
    await _open(cb, gid, state)


@platform_dice_router.callback_query(F.data.startswith("padg:groups:"))
async def padg_groups(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(PlatformDiceGame, gid)
        if g:
            g.in_groups = not g.in_groups
            newv = g.in_groups
            await s.commit()
        else:
            newv = False
    if newv:
        await cb.answer(
            "بازی در گروه‌ها مجاز شد.\n\n"
            "اگر در گروه جواب نداد، Privacy Mode ربات را از BotFather خاموش کن "
            "(Bot Settings → Group Privacy → Turn off) یا ربات را در گروه ادمین کن.",
            show_alert=True)
    await _open(cb, gid, state)


@platform_dice_router.callback_query(F.data.startswith("padg:period:"))
async def padg_period(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    gid = int(cb.data.split(":")[2])
    order = ["day", "week", "total"]
    async with Session() as s:
        g = await s.get(PlatformDiceGame, gid)
        if g:
            cur = g.tries_period if g.tries_period in order else "day"
            g.tries_period = order[(order.index(cur) + 1) % len(order)]
            await s.commit()
    await _open(cb, gid, state)


@platform_dice_router.callback_query(F.data.startswith("padg:del:"))
async def padg_del(cb: CallbackQuery):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    gid = int(cb.data.split(":")[2])
    await cb.message.edit_text(
        "🗑 <b>حذف بازی</b>\n\nتنظیمات جایزه‌ها و تاریخچهٔ بازی‌ها پاک می‌شود و "
        "برگشتی ندارد. مبالغی که قبلاً به کیف پول کاربران رفته دست‌نخورده می‌ماند.\n\nمطمئنی؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 بله، حذف کن", callback_data=f"padg:delgo:{gid}",
                                  style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="⬅️ منصرف شدم", callback_data=f"padg:open:{gid}")],
        ]), parse_mode="HTML")
    await cb.answer()


@platform_dice_router.callback_query(F.data.startswith("padg:delgo:"))
async def padg_delgo(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    from sqlalchemy import delete as _sa_delete
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        await s.execute(_sa_delete(PlatformDicePlay).where(PlatformDicePlay.game_id == gid))
        g = await s.get(PlatformDiceGame, gid)
        if g:
            await s.delete(g)
        await s.commit()
    await cb.answer("حذف شد.")
    await padg_menu(cb, state)


@platform_dice_router.callback_query(F.data.startswith("padg:prize:"))
async def padg_prize_start(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    _, _, gid, face = cb.data.split(":")
    await state.set_state(PDgFSM.prize)
    await state.update_data(pdg_id=int(gid), pdg_face=face)
    async with Session() as s:
        g = await s.get(PlatformDiceGame, int(gid))
        emoji = g.emoji if g else ""
    if face == "all":
        label = "هر برد"
    elif emoji == "🎰" and face.isdigit() and int(face) in SLOT_WIN_VALUES:
        label = SLOT_WIN_VALUES[int(face)]
    else:
        label = f"نتیجهٔ {face}"
    await cb.message.answer(
        f"🎁 جایزهٔ <b>{label}</b> را به تومان بفرست (0 = بدون جایزه):",
        parse_mode="HTML")
    await cb.answer()


@platform_dice_router.message(PDgFSM.prize, F.text)
async def padg_prize_save(msg: Message, state: FSMContext):
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    from platform_bot import fa_to_int
    val = fa_to_int(msg.text)
    if val is None or val < 0:
        await msg.answer("عدد نامعتبر. مبلغ را به تومان بفرست (مثلاً 5000):")
        return
    data = await state.get_data()
    gid, face = data.get("pdg_id"), data.get("pdg_face")
    async with Session() as s:
        g = await s.get(PlatformDiceGame, gid)
        if not g:
            await state.clear()
            await msg.answer("بازی پیدا نشد.")
            return
        pr = prizes_of(g)
        pr[str(face)] = val
        g.prizes = json.dumps(pr)
        await s.commit()
    await state.clear()
    await msg.answer(f"✅ جایزه روی <b>{_rial(val)}</b> تنظیم شد.",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                         [InlineKeyboardButton(text="⬅️ بازگشت به بازی",
                                               callback_data=f"padg:open:{gid}")]]),
                     parse_mode="HTML")


@platform_dice_router.callback_query(F.data.startswith("padg:tries:"))
async def padg_tries_start(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.set_state(PDgFSM.tries)
    await state.update_data(pdg_id=int(cb.data.split(":")[2]))
    await cb.message.answer(
        "🔢 هر کاربر چند بار اجازهٔ بازی داشته باشد؟ (0 = نامحدود)\n\n"
        "⚠️ سهمیهٔ نامحدود می‌تواند خیلی گران تمام شود.")
    await cb.answer()


@platform_dice_router.message(PDgFSM.tries, F.text)
async def padg_tries_save(msg: Message, state: FSMContext):
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    from platform_bot import fa_to_int
    val = fa_to_int(msg.text)
    if val is None or val < 0:
        await msg.answer("عدد نامعتبر. یک عدد بفرست (0 = نامحدود):")
        return
    gid = (await state.get_data()).get("pdg_id")
    async with Session() as s:
        g = await s.get(PlatformDiceGame, gid)
        if g:
            g.tries_limit = val
            await s.commit()
    await state.clear()
    await msg.answer("✅ سهمیه ذخیره شد.",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                         [InlineKeyboardButton(text="⬅️ بازگشت به بازی",
                                               callback_data=f"padg:open:{gid}")]]))


# ─────────────────────────── سمت کاربر ───────────────────────────

async def games_kb() -> InlineKeyboardMarkup | None:
    async with Session() as s:
        games = (await s.scalars(select(PlatformDiceGame).where(
            PlatformDiceGame.active.is_(True)).order_by(PlatformDiceGame.id))).all()
    if not games:
        return None
    # === PATCH_DICE_GAME_BACK_BUTTON_V1 ===
    # این صفحه از دکمهٔ صفحهٔ اصلی صدا زده می‌شه؛ تویِ حالتِ منویِ شیشه‌ای
    # (اینلاین) اگه دکمهٔ برگشت نباشه، کاربر همین‌جا گیر می‌کنه.
    rows = [[InlineKeyboardButton(text=f"{g.emoji} {g.title}",
                                  callback_data=f"pdg:play:{g.id}")] for g in games]
    rows.append([InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="hm:home_smart")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_games(msg: Message):
    """صفحهٔ «بازی کن پول بگیر» — از دکمهٔ صفحهٔ اصلیِ بات‌ساز صدا زده می‌شود."""
    kb = await games_kb()
    if kb is None:
        # === PATCH_DICE_GAME_BACK_BUTTON_V1 ===
        await msg.answer(
            "🎲 فعلاً هیچ بازی فعالی وجود ندارد. بعداً سر بزن!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="hm:home_smart")]]))
        return
    await msg.answer(
        "🎲 <b>بازی کن پول بگیر</b>\n\n"
        "یکی از بازی‌ها را انتخاب کن (یا فقط اسمش را بنویس). نتیجه را خودِ تلگرام "
        "تعیین می‌کند و جایزه‌اش به کیف پولت اضافه می‌شود.",
        reply_markup=kb, parse_mode="HTML")


async def run_play(bot: Bot, chat_id: int, telegram_id: int, gid: int,
                   player_name: str = "") -> bool:
    # === PATCH_DICE_GAME_QUOTA_RACE_FIX_V1 ===
    lock = _get_play_lock(gid, telegram_id)
    async with lock:
        return await _run_play_locked(bot, chat_id, telegram_id, gid, player_name)


async def _run_play_locked(bot: Bot, chat_id: int, telegram_id: int, gid: int,
                           player_name: str = "") -> bool:
    """بدنهٔ اصلیِ run_play؛ همیشه زیرِ قفلِ per-user/per-game فراخوانی می‌شود."""
    async with Session() as s:
        g = await s.get(PlatformDiceGame, gid)
        if not g or not g.active:
            await bot.send_message(chat_id, "این بازی در دسترس نیست.")
            return False
        emoji, title = g.emoji, g.title
        limit, period = g.tries_limit, g.tries_period
        game = g

    if limit:
        if await plays_used(game, telegram_id) >= limit:
            wait_txt = await _next_play_wait_text(game, telegram_id)
            await bot.send_message(
                chat_id,
                f"⏳ سهمیهٔ تو برای «{title}» تمام شده "
                f"({limit} بار {PERIOD_LABELS.get(period, '')}).\n{wait_txt}")
            return False

    sent = await bot.send_dice(chat_id, emoji=emoji)
    value = (sent.dice.value if sent and sent.dice else 0) or 0
    faces = faces_of(emoji)
    amount = int(prizes_of(game).get(str(value) if faces else "all", 0))

    async with Session() as s:
        s.add(PlatformDicePlay(game_id=gid, user_telegram_id=telegram_id,
                               value=value, amount=amount))
        if amount > 0:
            u = await s.scalar(select(PlatformUser).where(
                PlatformUser.telegram_id == telegram_id))
            if u:
                u.balance = float(u.balance or 0) + amount
                s.add(PlatformTxn(user_telegram_id=telegram_id, amount=amount,
                                  kind="game_prize",
                                  note=f"جایزهٔ بازی {title} (نتیجه {value})"))
        await s.commit()

    await asyncio.sleep(3.5)
    if amount > 0:
        text = f"{emoji} نتیجه: <b>{value}</b>\n\n🎉 بردی! <b>{_rial(amount)}</b> به کیف پولت اضافه شد."
    else:
        text = f"{emoji} نتیجه: <b>{value}</b>\n\n😔 این بار جایزه‌ای نبود. دوباره امتحان کن!"
    if limit:
        left = max(0, limit - await plays_used(game, telegram_id))
        text += f"\n\nسهمیهٔ باقی‌مانده: <b>{left}</b> بار {PERIOD_LABELS.get(period, '')}"
    if player_name:
        text = f"👤 {player_name}\n" + text
    await bot.send_message(chat_id, text,
                           reply_markup=(None if player_name else await games_kb()),
                           parse_mode="HTML")
    return True


@platform_dice_router.callback_query(F.data.startswith("pdg:play:"))
async def pdg_play(cb: CallbackQuery, bot: Bot):
    await cb.answer("بریم! 🎲")
    _grp = cb.message.chat.type in ("group", "supergroup")
    await run_play(bot, cb.message.chat.id, cb.from_user.id,
                   int(cb.data.split(":")[2]),
                   player_name=(cb.from_user.full_name if _grp else ""))


def _norm(t: str) -> str:
    t = (t or "").strip().replace("\u064a", "\u06cc").replace("\u0643", "\u06a9")
    return " ".join(t.replace("\u200c", " ").split()).lower()


async def try_play_by_name(msg: Message, bot: Bot) -> bool:
    """اگر متنِ پیام دقیقاً نامِ یکی از بازی‌های فعال باشد، آن را اجرا می‌کند."""
    text = _norm(msg.text or "")
    if not text or len(text) > 40:
        return False
    in_group = msg.chat.type in ("group", "supergroup")
    async with Session() as s:
        q = select(PlatformDiceGame).where(PlatformDiceGame.active.is_(True))
        if in_group:
            q = q.where(PlatformDiceGame.in_groups.is_(True)).where(
                (PlatformDiceGame.group_chat_id.is_(None))
                | (PlatformDiceGame.group_chat_id == msg.chat.id))
        games = (await s.scalars(q)).all()
    for g in games:
        if text in {_norm(g.title), _norm(g.emoji), _norm(f"{g.emoji} {g.title}")}:
            await run_play(bot, msg.chat.id, msg.from_user.id, g.id,
                           player_name=(msg.from_user.full_name if in_group else ""))
            return True
    return False


# ───── شروعِ بازی با نوشتنِ نامِ بازی ─────
# این هندلر عمداً آخرین چیز در زنجیره است (روترش آخر از همه include می‌شود) و
# اگر متن با هیچ بازی‌ای جور نبود با SkipHandler عبور می‌کند، تا هیچ‌وقت جلوی
# بقیهٔ امکاناتِ بات‌ساز را نگیرد.
@platform_dice_router.message(StateFilter(None), F.text)
async def pdg_text_entry(msg: Message, bot: Bot):
    try:
        if await try_play_by_name(msg, bot):
            return
    except Exception as e:
        print(f"[platform_dice_text_entry] {e}")
    raise SkipHandler
