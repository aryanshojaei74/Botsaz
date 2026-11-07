"""
dice_game.py — قابلیت «🎲 بازی کن و جایزه بگیر» (PATCH_DICE_GAME_V1).

ایده:
  مالکِ ربات یک یا چند «بازی» ثبت می‌کند که از بازی‌های داخلیِ خودِ تلگرام
  استفاده می‌کنند (متد sendDice). نتیجه را سرورِ تلگرام تعیین می‌کند، نه ربات —
  یعنی نه مالک و نه ما نمی‌توانیم در نتیجه دست ببریم و بازی کاملاً منصفانه است.

  مالک برای هر نتیجهٔ ممکن (مثلاً ۱ تا ۶ برای تاس) یک جایزه به تومان تعیین
  می‌کند، و تعیین می‌کند هر کاربر چند بار (روزانه/هفتگی/در کل) اجازهٔ بازی
  دارد. کاربر روی دکمه می‌زند، تاس می‌چرخد، و هر عددی که آمد همان مبلغ به
  کیف پولش اضافه می‌شود.

نکتهٔ مهمِ طراحی: بازی هیچ هزینه‌ای از کاربر نمی‌گیرد؛ این یک ابزارِ
هدیه/تشویق است، نه شرط‌بندی. برای همین هم سهمیهٔ تعداد دفعات وجود دارد تا
هزینهٔ مالک کنترل‌شده بماند.

ذخیره‌سازی: جدول‌های dice_games و dice_plays (models.py).

callbackها:
  ادمین: dga:*   — لیست بازی‌ها، افزودن، تنظیم جایزه‌ها، سهمیه، فعال/غیرفعال
  کاربر: dg:play:{game_id}
"""
import asyncio
import json
import secrets
import html as _html
from datetime import datetime, timedelta

# === PATCH_DICE_GAME_QUOTA_RACE_FIX_V1 ===
# اگر کاربر چند پیامِ «تاس»/دکمهٔ بازی را خیلی سریع پشتِ‌سرِهم بفرستد، چند
# نمونه از run_play هم‌زمان اجرا می‌شدند: همه‌شان plays_used را قبل از اینکه
# بازیِ قبلی commit شود می‌خواندند، همه سهمیه را «خالی نشده» می‌دیدند، و همه
# اجازهٔ بازی می‌گرفتند — یعنی سهمیهٔ روزانه/هفتگی دور زده می‌شد. این دیکشنری
# به‌ازای هر (بات، بازی، کاربر) یک قفل می‌سازد تا چکِ سهمیه + ثبتِ نتیجه به‌صورتِ
# اتمیک انجام شود و درخواست‌های هم‌زمانِ همان کاربر صف بشوند نه موازی اجرا شوند.
_play_locks: dict[tuple[int, int, int], asyncio.Lock] = {}


def _get_play_lock(bot_id: int, game_id: int, telegram_id: int) -> asyncio.Lock:
    key = (bot_id, game_id, telegram_id)
    lock = _play_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _play_locks[key] = lock
    return lock

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import func, select

from config import Session
from models import Bot as BotRow, BotUser, DiceGame, DicePlay, WalletTxn, VpnPlan, VpnPanel, PanelAccount

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

dice_game_router = Router(name="dice_game")

# بازی‌های تلگرام و بازهٔ نتیجهٔ هرکدام. (min, max) طبق مستندات Bot API.
DICE_EMOJIS: dict[str, tuple[str, int, int]] = {
    "🎲": ("تاس", 1, 6),
    "🎯": ("دارت", 1, 6),
    "🎳": ("بولینگ", 1, 6),
    "🏀": ("بسکتبال", 1, 5),
    "⚽": ("فوتبال", 1, 5),
    "🎰": ("اسلات", 1, 64),
}

PERIOD_LABELS = {"day": "روزانه", "week": "هفتگی", "total": "در کل"}

# برای اسلات (۶۴ حالت) تعیینِ جایزه به‌ازای تک‌تکِ نتایج غیرعملی است، پس فقط
# بازی‌هایی که ≤ ۶ حالت دارند اجازهٔ تنظیمِ جایزهٔ تفکیکی دارند.
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


class DgFSM(StatesGroup):
    prize = State()
    tries = State()


def faces_of(emoji: str) -> list[int]:
    if emoji == "🎰":
        # فقط ترکیب‌های واقعاً برنده؛ ۶۰ حالتِ دیگر همیشه باخت‌اند.
        return list(SLOT_WIN_VALUES.keys())
    _, lo, hi = DICE_EMOJIS.get(emoji, ("", 1, 6))
    if hi - lo + 1 > MAX_FACES_FOR_PRIZES:
        return []
    return list(range(lo, hi + 1))


# DicePlay.amount ستونِ INTEGER (int32) است. قبلاً مبلغِ جایزه هیچ سقفی نداشت و
# با جایزهٔ ۵۰۰ میلیارد تومانی هر بار بازی با «value out of int32 range»
# می‌ترکید (error.log). جایزهٔ بالاتر از این سقف نامعتبر حساب می‌شود.
MAX_PRIZE = 1_000_000_000


def prizes_of(game: DiceGame) -> dict[str, int]:
    try:
        data = json.loads(game.prizes or "{}") or {}
    except Exception:
        data = {}
    out = {}
    for k, v in data.items():
        try:
            n = int(v or 0)
        except (TypeError, ValueError):
            n = 0
        out[str(k)] = n if 0 <= n <= MAX_PRIZE else 0
    return out


def prize_for(game: DiceGame, value: int) -> int:
    return prizes_of(game).get(str(value), 0)


# === PATCH_DICE_GAME_PLAN_PRIZE_V1 ===
def plan_prizes_of(game: DiceGame) -> dict[str, int]:
    try:
        data = json.loads(game.plan_prizes or "{}") or {}
    except Exception:
        data = {}
    return {str(k): int(v) for k, v in data.items() if v}


def plan_id_for(game: DiceGame, value: int) -> int | None:
    return plan_prizes_of(game).get(str(value))


def _period_start(period: str) -> datetime | None:
    now = datetime.utcnow()
    if period == "day":
        return now - timedelta(days=1)
    if period == "week":
        return now - timedelta(days=7)
    return None  # total


async def plays_used(bot_id: int, game: DiceGame, telegram_id: int) -> int:
    """تعداد دفعاتی که این کاربر در بازهٔ جاری بازی کرده است."""
    async with Session() as s:
        q = select(func.count()).select_from(DicePlay).where(
            DicePlay.bot_id == bot_id,
            DicePlay.game_id == game.id,
            DicePlay.user_telegram_id == telegram_id)
        since = _period_start(game.tries_period)
        if since is not None:
            q = q.where(DicePlay.created_at >= since)
        return await s.scalar(q) or 0


# === PATCH_DICE_QUOTA_COUNTDOWN_V1 ===
# «سهمیه‌ات تمام شد» بدونِ گفتنِ «کِی دوباره؟» باعثِ پیامِ تکراری به
# پشتیبانی می‌شد. بازه‌ها کشویی‌اند (۲۴ ساعت از خودِ بازی، نه نیمه‌شب)،
# پس زمانِ آزادشدن = قدیمی‌ترین بازیِ داخلِ بازه + طولِ بازه.
_PERIOD_LEN = {"day": timedelta(days=1), "week": timedelta(days=7)}


async def next_free_at(bot_id: int, game: DiceGame, telegram_id: int):
    """اولین لحظه‌ای (UTC) که این کاربر دوباره سهمیه پیدا می‌کند.

    None یعنی هرگز (بازهٔ «total») یا اصلاً محدودیتی نیست.
    """
    limit = int(game.tries_limit or 0)
    span = _PERIOD_LEN.get(game.tries_period)
    if not limit or span is None:
        return None
    since = _period_start(game.tries_period)
    async with Session() as s:
        q = (select(DicePlay.created_at)
             .where(DicePlay.bot_id == bot_id,
                    DicePlay.game_id == game.id,
                    DicePlay.user_telegram_id == telegram_id)
             .order_by(DicePlay.created_at.desc()))
        if since is not None:
            q = q.where(DicePlay.created_at >= since)
        # بازیِ شمارهٔ «limit» از آخر — با خارج‌شدنِ همان از بازه، یک
        # سهمیه آزاد می‌شود.
        rows = (await s.scalars(q.limit(limit))).all()
    if len(rows) < limit:
        return None
    return rows[-1] + span


def _fmt_delta_fa(delta) -> str:
    """«۸ ساعت و ۴۱ دقیقه» — به فارسیِ خوانا، بدونِ ثانیه."""
    total = int(max(0, delta.total_seconds()))
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d} روز")
    if h:
        parts.append(f"{h} ساعت")
    if m and not d:
        parts.append(f"{m} دقیقه")
    if not parts:
        return "کمتر از یک دقیقه"
    return " و ".join(parts)


async def quota_reset_kw(bot_id: int, game: DiceGame, telegram_id: int) -> dict:
    """متغیرهایِ {reset_in} / {reset_at} برایِ متنِ پایانِ سهمیه.

    اگر بازه «total» باشد (سهمیهٔ همیشگی، بدونِ تجدید) هر دو خالی می‌مانند
    تا متن جملهٔ بی‌معنیِ «۰ دقیقهٔ دیگر» ندهد.
    """
    try:
        nxt = await next_free_at(bot_id, game, telegram_id)
    except Exception:
        logger.exception("محاسبهٔ زمانِ تجدیدِ سهمیهٔ بازی ناموفق بود")
        nxt = None
    if nxt is None:
        return {"reset_in": "", "reset_at": "", "reset_line": ""}
    now = datetime.utcnow()
    local = nxt + timedelta(hours=3, minutes=30)   # تهران
    reset_in = _fmt_delta_fa(nxt - now)
    reset_at = local.strftime("%H:%M")
    return {
        "reset_in": reset_in,
        "reset_at": reset_at,
        "reset_line": f"{reset_in} دیگر (ساعت {reset_at}) "
                      "می‌توانی دوباره بازی کنی.",
    }


# ─────────────────────────── پنل مدیریت ───────────────────────────

# === PATCH_DICE_GATE_V1 ===
# شرطِ بازی‌کردن: none = آزاد | topup = اولین افزایشِ موجودی | order = اولین سفارش (خرید)
_GATE_LABELS = {"none": "🔓 آزاد (بدونِ شرط)", "topup": "💳 بعد از اولین افزایشِ موجودی",
                "order": "🛒 بعد از اولین سفارش"}
_GATE_ORDER = ("none", "topup", "order")
_NON_DEPOSIT_KINDS = ("refund", "smm_refund", "bet_refund", "admin", "referral", "welcome_bonus",
                      "prize", "revert", "stake")
_PURCHASE_KINDS = ("purchase", "smm_order", "smm_order_api", "renew", "sublink", "wireguard",
                   "openvpn", "sold", "panel", "rc")


async def _get_gate(bot_id: int) -> str:
    from tenant import get_setting
    v = await get_setting(bot_id, "dice_gate", "none")
    return v if v in _GATE_LABELS else "none"


async def gate_check(bot_id: int, telegram_id: int) -> tuple[bool, str]:
    """(مجاز؟, پیامِ خطا). فقط «حداقل یک‌بار» مهم است، نه مبلغ."""
    gate = await _get_gate(bot_id)
    if gate == "none":
        return True, ""
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        if not u:
            ok = False
        elif gate == "topup":
            from tenant.shared.kyc import deposit_txn_filter      # PATCH_KYC_DEPOSIT_WHITELIST_V1
            n = await s.scalar(select(func.count(WalletTxn.id)).where(
                WalletTxn.bot_id == bot_id, WalletTxn.user_id == u.id, deposit_txn_filter()))
            ok = bool(n)
        else:
            n = await s.scalar(select(func.count(WalletTxn.id)).where(
                WalletTxn.bot_id == bot_id, WalletTxn.user_id == u.id,
                WalletTxn.amount < 0, WalletTxn.kind.in_(_PURCHASE_KINDS)))
            if not n:
                from models import SmmOrder
                n = await s.scalar(select(func.count(SmmOrder.id)).where(
                    SmmOrder.bot_id == bot_id, SmmOrder.user_telegram_id == telegram_id))
            ok = bool(n)
    if ok:
        return True, ""
    if gate == "topup":
        return False, ("🔒 برای فعال‌شدنِ این قابلیت، اولین افزایشِ موجودیِ خودت را انجام بده.\n"
                       "(مبلغ مهم نیست؛ فقط یک‌بار کیف پولت را شارژ کن.)")
    return False, ("🔒 برای فعال‌شدنِ این قابلیت، اولین سفارشِ خودت را ثبت کن.\n"
                   "(بعد از اولین خرید، بازی برایت باز می‌شود.)")


def _games_kb(games: list[DiceGame], plan: str = "", gate: str = "none") -> InlineKeyboardMarkup:
    rows = []
    for g in games:
        mark = "✅" if g.active else "⛔️"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {g.emoji} {g.title}", callback_data=f"dga:open:{g.id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="➕ افزودن بازی", callback_data="dga:add", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text=f"🔒 شرطِ بازی: {_GATE_LABELS.get(gate, gate)}",
                                      callback_data="dga:gate", style=ButtonStyle.PRIMARY)])
    # === PATCH_PARTNER_SETTINGS_BACK_FIX_V1 ===
    # برایِ رباتِ همکار این صفحه از داخلِ «تنظیمات عمومی ربات»
    # (set:hub_general) باز می‌شود. «tadm:menu» عمداً از فهرستِ سفیدِ
    # partner_menu.py بیرونه (چون معمولاً یعنی «منویِ اصلیِ ربات»)، پس
    # برایِ همکار کاربر رو یک‌راست به خانهٔ پنل می‌فرستاد، نه یک قدم عقب.
    # === PATCH_DICE_CUSTOM_TEXTS_V1 ===
    rows.append([InlineKeyboardButton(
        text="✍️ متن‌هایِ بازی", callback_data="dga:texts")])
    _back_cb = "set:hub_general" if plan == "partner" else "tadm:menu"
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=_back_cb, style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dice_game_router.callback_query(F.data == "dga:menu")
async def dga_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    async with Session() as s:
        games = (await s.scalars(select(DiceGame).where(
            DiceGame.bot_id == row.id).order_by(DiceGame.id))).all()
    txt = (
        "🎲 <b>بازی کن و جایزه بگیر</b>\n\n"
        "بازی‌هایی که اینجا ثبت می‌کنی از بازی‌های داخلیِ خودِ تلگرام استفاده "
        "می‌کنند، پس نتیجه را سرورِ تلگرام تعیین می‌کند و کاملاً منصفانه است.\n\n"
        "برای هر نتیجه یک جایزه به تومان تعیین می‌کنی؛ کاربر دکمه را می‌زند، "
        "بازی می‌چرخد و همان مبلغ به کیف پولش اضافه می‌شود.\n\n"
    )
    if not games:
        txt += "هنوز بازی‌ای ثبت نکرده‌ای."
    else:
        txt += f"تعداد بازی‌های ثبت‌شده: <b>{len(games)}</b>"
    _gate = await _get_gate(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=_games_kb(games, row.plan, _gate), parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=_games_kb(games, row.plan, _gate), parse_mode="HTML")
    await cb.answer()


@dice_game_router.callback_query(F.data == "dga:gate")
async def dga_gate_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """چرخشِ شرطِ بازی: آزاد ← اولین افزایشِ موجودی ← اولین سفارش."""
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await _get_gate(row.id)
    nxt = _GATE_ORDER[(_GATE_ORDER.index(cur) + 1) % len(_GATE_ORDER)]
    await set_setting(row.id, "dice_gate", nxt)
    await cb.answer("✅ " + _GATE_LABELS[nxt])
    await dga_menu(cb, bot, state)


@dice_game_router.callback_query(F.data == "dga:add")
async def dga_add(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    async with Session() as s:
        used = set((await s.scalars(select(DiceGame.emoji).where(
            DiceGame.bot_id == row.id))).all())
    rows = []
    for emoji, (name, lo, hi) in DICE_EMOJIS.items():
        if emoji in used:
            continue
        faces = hi - lo + 1
        note = f"{faces} حالت" if faces <= MAX_FACES_FOR_PRIZES else f"{faces} حالت — جایزهٔ یکسان"
        rows.append([InlineKeyboardButton(
            text=f"{emoji} {name} ({note})", callback_data=f"dga:new:{emoji}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="dga:menu", style=ButtonStyle.DANGER)])
    if len(rows) == 1:
        await cb.answer("همهٔ بازی‌های تلگرام را قبلاً اضافه کرده‌ای.", show_alert=True)
        return
    await cb.message.edit_text(
        "➕ <b>افزودن بازی</b>\n\nکدام بازیِ تلگرام را می‌خواهی اضافه کنی؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@dice_game_router.callback_query(F.data.startswith("dga:new:"))
async def dga_new(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    emoji = cb.data.split(":", 2)[2]
    if emoji not in DICE_EMOJIS:
        await cb.answer("بازی نامعتبر.", show_alert=True)
        return
    title = DICE_EMOJIS[emoji][0]
    async with Session() as s:
        dup = await s.scalar(select(DiceGame).where(
            DiceGame.bot_id == row.id, DiceGame.emoji == emoji))
        if dup:
            await cb.answer("این بازی قبلاً ثبت شده.", show_alert=True)
            return
        g = DiceGame(bot_id=row.id, emoji=emoji, title=title, active=True,
                     prizes="{}", tries_limit=1, tries_period="day")
        s.add(g)
        await s.commit()
        gid = g.id
    await cb.answer("بازی ثبت شد ✅")
    await _open_game(cb, bot, gid, state)


async def _open_game(cb: CallbackQuery, bot: Bot, gid: int, state: FSMContext):
    from tenant import _owner_ok, rial
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    async with Session() as s:
        g = await s.get(DiceGame, gid)
        if not g or g.bot_id != row.id:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        plays = await s.scalar(select(func.count()).select_from(DicePlay).where(
            DicePlay.game_id == g.id)) or 0
        paid = await s.scalar(select(func.coalesce(func.sum(DicePlay.amount), 0)).where(
            DicePlay.game_id == g.id)) or 0

    pr = prizes_of(g)
    plp = plan_prizes_of(g)
    async with Session() as s:
        plans = (await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == row.id, VpnPlan.active.is_(True)).order_by(VpnPlan.sort_order, VpnPlan.id))).all()
    plan_titles = {p.id: p.title for p in plans}
    faces = faces_of(g.emoji)
    lines = []
    if faces:
        for v in faces:
            label = SLOT_WIN_VALUES[v] if g.emoji == "🎰" else f"نتیجهٔ {v}"
            pid = plp.get(str(v))
            plan_txt = f" + 🎁 {plan_titles[pid]}" if pid in plan_titles else (
                " + 🎁 (پلن حذف‌شده)" if pid else "")
            lines.append(f"  • {label}: <b>{rial(pr.get(str(v), 0))}</b>{plan_txt}")
    else:
        lines.append(f"  • هر نتیجه: <b>{rial(pr.get('all', 0))}</b>")

    limit_txt = ("نامحدود" if g.tries_limit == 0
                 else f"{g.tries_limit} بار {PERIOD_LABELS.get(g.tries_period, '')}")
    txt = (
        f"{g.emoji} <b>{g.title}</b>\n\n"
        f"وضعیت: {'✅ فعال' if g.active else '⛔️ غیرفعال'}\n"
        f"سهمیهٔ هر کاربر: <b>{limit_txt}</b>\n"
        f"بازی در گروه‌ها: {'✅ مجاز' if g.in_groups else '⛔️ ممنوع'}\n"
        + (f"گروهِ متصل: <b>{g.group_title or g.group_chat_id}</b>\n\n"
           if g.group_chat_id else "گروهِ متصل: — (در هر گروهی کار می‌کند)\n\n")
        + "🎁 <b>جایزه‌ها</b>\n" + "\n".join(lines) + "\n\n"
        + 
        f"📊 تا حالا: <b>{plays}</b> بار بازی، مجموعاً <b>{rial(paid)}</b> پرداخت شده.\n\n"
        "برای اینکه کاربران این بازی را ببینند، در «مدیریت دکمه‌های صفحه اصلی» "
        "یک دکمه بساز و عملکردش را روی «🎲 بازی کن و جایزه بگیر» بگذار."
    )

    rows = []
    if faces:
        for i in range(0, len(faces), 3):
            rows.append([InlineKeyboardButton(
                text=f"{(SLOT_WIN_VALUES[v] if g.emoji == '🎰' else f'{v}️⃣')} {pr.get(str(v), 0):,}",
                callback_data=f"dga:prize:{g.id}:{v}")
                for v in faces[i:i + 3]])
        # === PATCH_DICE_GAME_PLAN_PRIZE_V1 ===
        for v in faces:
            pid = plp.get(str(v))
            plan_label = plan_titles.get(pid, "بدون پلن") if pid else "بدون پلن"
            face_label = SLOT_WIN_VALUES[v] if g.emoji == "🎰" else str(v)
            rows.append([InlineKeyboardButton(
                text=f"🔗 پلنِ {face_label}: {plan_label}",
                callback_data=f"dga:plan:{g.id}:{v}", style=ButtonStyle.PRIMARY)])
    else:
        rows.append([InlineKeyboardButton(text="🎁 جایزهٔ هر برد",
                                          callback_data=f"dga:prize:{g.id}:all", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text=f"🔢 سهمیهٔ هر کاربر ({limit_txt})",
                                      callback_data=f"dga:tries:{g.id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text=f"🔁 بازهٔ سهمیه ({PERIOD_LABELS.get(g.tries_period)})",
                                      callback_data=f"dga:period:{g.id}")])
    rows.append([InlineKeyboardButton(
        text=("🌐 بازی در گروه‌ها: ✅ مجاز" if g.in_groups else "🌐 بازی در گروه‌ها: ⛔️ ممنوع"),
        callback_data=f"dga:groups:{g.id}", style=ButtonStyle.SUCCESS)])
    # === PATCH_DICE_WINNER_NOTIFY_V1 ===
    rows.append([InlineKeyboardButton(
        text=("📣 اعلام برنده به مدیر: ✅ روشن" if g.notify_owner_on_win
              else "📣 اعلام برنده به مدیر: ⛔️ خاموش"),
        callback_data=f"dga:winnotify:{g.id}", style=ButtonStyle.PRIMARY)])
    if g.group_chat_id:
        rows.append([InlineKeyboardButton(text="📢 ارسال دکمهٔ بازی در گروه",
                                          callback_data=f"dga:post:{g.id}", style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(text="❌ قطعِ اتصال از گروه",
                                          callback_data=f"dga:unbind:{g.id}", style=ButtonStyle.DANGER)])
    else:
        rows.append([InlineKeyboardButton(text="🔗 اتصال به یک گروه",
                                          callback_data=f"dga:bind:{g.id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text=("⛔️ غیرفعال کردن بازی" if g.active else "✅ فعال کردن بازی"),
        callback_data=f"dga:toggle:{g.id}", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="🗑 حذف این بازی",
                                      callback_data=f"dga:del:{g.id}",
                                      style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="dga:menu", style=ButtonStyle.DANGER)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dice_game_router.callback_query(F.data.startswith("dga:open:"))
async def dga_open(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _open_game(cb, bot, int(cb.data.split(":")[2]), state)


@dice_game_router.callback_query(F.data.startswith("dga:toggle:"))
async def dga_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(DiceGame, gid)
        if not g or g.bot_id != row.id:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        g.active = not g.active
        await s.commit()
    await _open_game(cb, bot, gid, state)


# ───── اتصالِ بازی به یک گروهِ مشخص ─────

@dice_game_router.callback_query(F.data.startswith("dga:bind:"))
async def dga_bind(cb: CallbackQuery, bot: Bot):
    """کدِ یک‌بارمصرف می‌سازد؛ مالک همان کد را داخلِ گروه می‌فرستد تا گروه ثبت شود.

    چرا با «کامند در گروه» و نه با گرفتنِ آیدیِ گروه؟ چون این روش هم‌زمان ثابت
    می‌کند که ربات واقعاً داخلِ آن گروه هست و پیام‌هایش را دریافت می‌کند.
    """
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[2])
    code = secrets.token_hex(3)
    async with Session() as s:
        g = await s.get(DiceGame, gid)
        if not g or g.bot_id != row.id:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        g.bind_code = code
        await s.commit()
    me = await bot.get_me()
    await cb.message.answer(
        "🔗 <b>اتصال بازی به گروه</b>\n\n"
        f"۱) ربات <code>@{me.username}</code> را به گروه اضافه کن و ترجیحاً ادمینش کن.\n"
        f"۲) داخلِ همان گروه این را بفرست:\n\n"
        f"<code>/dice {code}</code>\n\n"
        "بعد از آن، بازی به همان گروه وصل می‌شود و ربات یک دکمهٔ «بازی» در گروه "
        "می‌فرستد که همه می‌توانند با آن بازی کنند.\n\n"
        "💡 اگر می‌خواهی کاربران بتوانند فقط با <b>نوشتنِ اسمِ بازی</b> هم بازی کنند، "
        "باید Privacy Mode ربات را از BotFather خاموش کنی "
        "(Bot Settings ← Group Privacy ← Turn off). بدونِ آن، تلگرام پیام‌های "
        "عادیِ گروه را اصلاً به ربات نمی‌دهد و فقط دکمه و کامند کار می‌کند — "
        "دلیلِ اینکه نوشتنِ «تاس» در گروه جواب نمی‌داد همین است.",
        parse_mode="HTML")
    await cb.answer()


@dice_game_router.message(Command("dice"))
async def dg_bind_in_group(msg: Message, bot: Bot):
    """داخلِ گروه: /dice <code> → گروه را به بازی وصل می‌کند."""
    if msg.chat.type not in ("group", "supergroup"):
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        return
    code = parts[1].strip()
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        if not row:
            return
        g = await s.scalar(select(DiceGame).where(
            DiceGame.bot_id == row.id, DiceGame.bind_code == code))
        if not g:
            await msg.reply("❌ کد نامعتبر یا منقضی است.")
            return
        if msg.from_user.id != row.owner_id:
            await msg.reply("❌ فقط مالکِ ربات می‌تواند گروه را وصل کند.")
            return
        g.group_chat_id = msg.chat.id
        g.group_title = (msg.chat.title or "")[:120]
        g.bind_code = None
        g.in_groups = True
        gid, emoji, title = g.id, g.emoji, g.title
        await s.commit()
    await msg.answer(
        f"✅ بازی <b>{emoji} {title}</b> به این گروه وصل شد.\n\n"
        "روی دکمهٔ زیر بزنید تا بازی کنید — جایزه به کیف پولتان در ربات اضافه می‌شود.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"{emoji} بازی کن و جایزه بگیر",
                                 callback_data=f"dg:play:{gid}")]]),
        parse_mode="HTML")


@dice_game_router.callback_query(F.data.startswith("dga:post:"))
async def dga_post(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(DiceGame, gid)
        if not g or g.bot_id != row.id or not g.group_chat_id:
            await cb.answer("این بازی به گروهی وصل نیست.", show_alert=True)
            return
        chat_id, emoji, title = g.group_chat_id, g.emoji, g.title
    try:
        await bot.send_message(
            chat_id,
            f"{emoji} <b>{title}</b>\n\nروی دکمه بزن، شانست را امتحان کن و "
            "جایزه‌ات را در کیف پولت بگیر!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"{emoji} بازی کن و جایزه بگیر",
                                     callback_data=f"dg:play:{gid}")]]),
            parse_mode="HTML")
        await cb.answer("پیام در گروه ارسال شد ✅")
    except Exception as e:
        await cb.answer(f"ارسال نشد: {e}", show_alert=True)


@dice_game_router.callback_query(F.data.startswith("dga:unbind:"))
async def dga_unbind(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(DiceGame, gid)
        if g and g.bot_id == row.id:
            g.group_chat_id = None
            g.group_title = None
            g.bind_code = None
            await s.commit()
    await cb.answer("اتصال قطع شد.")
    await _open_game(cb, bot, gid, state)


@dice_game_router.callback_query(F.data.startswith("dga:groups:"))
async def dga_groups(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """اجازه/منعِ بازی در گروه‌ها.

    ⚠️ برای اینکه ربات در گروه اصلاً متنِ «تاس» را دریافت کند، باید Privacy Mode
    آن از BotFather خاموش باشد یا ربات در گروه ادمین باشد.
    """
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(DiceGame, gid)
        if not g or g.bot_id != row.id:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        g.in_groups = not g.in_groups
        newv = g.in_groups
        await s.commit()
    if newv:
        await cb.answer(
            "بازی در گروه‌ها مجاز شد.\n\n"
            "اگر در گروه جواب نداد، Privacy Mode ربات را از BotFather خاموش کن "
            "(Bot Settings → Group Privacy → Turn off) یا ربات را در گروه ادمین کن.",
            show_alert=True)
    await _open_game(cb, bot, gid, state)


# === PATCH_DICE_WINNER_NOTIFY_V1 ===
@dice_game_router.callback_query(F.data.startswith("dga:winnotify:"))
async def dga_winnotify(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """روشن/خاموش کردنِ اعلامِ «برندهٔ جدید» به مدیرِ ربات برای این بازی."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        g = await s.get(DiceGame, gid)
        if not g or g.bot_id != row.id:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        g.notify_owner_on_win = not g.notify_owner_on_win
        newv = g.notify_owner_on_win
        await s.commit()
    if newv:
        await cb.answer(
            "روشن شد ✅\nاز این پس هر وقت کاربری در این بازی برنده شود، "
            "مشخصاتش و جایزه‌اش برایت ارسال می‌شود.", show_alert=True)
    else:
        await cb.answer("خاموش شد ⛔️", show_alert=True)
    await _open_game(cb, bot, gid, state)


@dice_game_router.callback_query(F.data.startswith("dga:period:"))
async def dga_period(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[2])
    order = ["day", "week", "total"]
    async with Session() as s:
        g = await s.get(DiceGame, gid)
        if not g or g.bot_id != row.id:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        cur = g.tries_period if g.tries_period in order else "day"
        g.tries_period = order[(order.index(cur) + 1) % len(order)]
        await s.commit()
    await _open_game(cb, bot, gid, state)


@dice_game_router.callback_query(F.data.startswith("dga:del:"))
async def dga_del_confirm(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[2])
    await cb.message.edit_text(
        "🗑 <b>حذف بازی</b>\n\nبا حذف بازی، تنظیمات جایزه‌ها و تاریخچهٔ بازی‌های "
        "کاربران هم پاک می‌شود و برگشتی ندارد. مبالغی که قبلاً به کیف پول کاربران "
        "اضافه شده دست‌نخورده باقی می‌ماند.\n\nمطمئنی؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 بله، حذف کن",
                                  callback_data=f"dga:delgo:{gid}",
                                  style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="⬅️ نه، منصرف شدم", callback_data=f"dga:open:{gid}", style=ButtonStyle.DANGER)],
        ]), parse_mode="HTML")
    await cb.answer()


@dice_game_router.callback_query(F.data.startswith("dga:delgo:"))
async def dga_del_exec(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[2])
    from sqlalchemy import delete as _sa_delete
    async with Session() as s:
        g = await s.get(DiceGame, gid)
        if not g or g.bot_id != row.id:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        await s.execute(_sa_delete(DicePlay).where(DicePlay.game_id == gid))
        await s.delete(g)
        await s.commit()
    await cb.answer("حذف شد.")
    await dga_menu(cb, bot, state)


# ───── تنظیم جایزهٔ هر نتیجه ─────

@dice_game_router.callback_query(F.data.startswith("dga:prize:"))
async def dga_prize_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, _, gid, face = cb.data.split(":")
    async with Session() as s:
        g = await s.get(DiceGame, int(gid))
        if not g or g.bot_id != row.id:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        emoji = g.emoji
    await state.set_state(DgFSM.prize)
    await state.update_data(dg_id=int(gid), dg_face=face)
    if face == "all":
        label = "هر برد"
    elif emoji == "🎰" and face.isdigit() and int(face) in SLOT_WIN_VALUES:
        label = SLOT_WIN_VALUES[int(face)]
    else:
        label = f"نتیجهٔ {face}"
    await cb.message.answer(
        f"🎁 جایزهٔ <b>{label}</b> در بازی {emoji} را به تومان بفرست.\n"
        "برای «بدون جایزه» عدد <code>0</code> را بفرست.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@dice_game_router.message(DgFSM.prize, F.text)
async def dga_prize_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import _owner_ok_msg, fa_to_int, rial
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    val = fa_to_int(msg.text)
    if val is None or val < 0 or val > MAX_PRIZE:
        await msg.answer("عدد نامعتبر است. مبلغ را به تومان بفرست (مثلاً 5000؛ "
                         f"حداکثر {MAX_PRIZE:,}):")
        return
    data = await state.get_data()
    gid, face = data.get("dg_id"), data.get("dg_face")
    async with Session() as s:
        g = await s.get(DiceGame, gid)
        if not g or g.bot_id != row.id:
            await state.clear()
            await msg.answer("بازی پیدا نشد.")
            return
        pr = prizes_of(g)
        pr[str(face)] = val
        g.prizes = json.dumps(pr)
        await s.commit()
    await state.clear()
    await msg.answer(
        f"✅ جایزه روی <b>{rial(val)}</b> تنظیم شد.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ بازگشت به بازی", callback_data=f"dga:open:{gid}", style=ButtonStyle.DANGER)]]),
        parse_mode="HTML")


# === PATCH_DICE_GAME_PLAN_PRIZE_V1 ===
# ───── اتصالِ جایزهٔ هر نتیجه به یک پلنِ فروشِ VPN ─────

@dice_game_router.callback_query(F.data.startswith("dga:plan:"))
async def dga_plan_pick(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, _, gid, face = cb.data.split(":")
    async with Session() as s:
        g = await s.get(DiceGame, int(gid))
        if not g or g.bot_id != row.id:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        plans = (await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == row.id, VpnPlan.active.is_(True)).order_by(VpnPlan.sort_order, VpnPlan.id))).all()
    if not plans:
        await cb.answer(
            "هنوز هیچ پلنِ فعالی برای فروشِ VPN تعریف نکرده‌ای. اول از منویِ پنلِ متصل، "
            "یک پلن (حجم/روز/قیمت) بساز، بعد اینجا وصلش کن.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(
        text=f"{p.title} — {p.volume_gb or '∞'} گیگ / {p.days or '∞'} روز",
        callback_data=f"dga:planset:{gid}:{face}:{p.id}", style=ButtonStyle.PRIMARY)] for p in plans]
    rows.append([InlineKeyboardButton(text="❌ بدون پلن (فقط جایزهٔ نقدی)",
                                      callback_data=f"dga:planset:{gid}:{face}:0", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"dga:open:{gid}", style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        "🔗 <b>اتصال جایزه به پلنِ فروشِ VPN</b>\n\n"
        "اگر کاربر همین نتیجه را بیاورد، علاوه بر جایزهٔ نقدی (در صورت تنظیم)، "
        "یک سرویسِ VPN رایگان از پلنِ انتخابی برایش ساخته می‌شود.\n\n"
        "کدام پلن؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@dice_game_router.callback_query(F.data.startswith("dga:planset:"))
async def dga_plan_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, _, gid, face, pid = cb.data.split(":")
    async with Session() as s:
        g = await s.get(DiceGame, int(gid))
        if not g or g.bot_id != row.id:
            await cb.answer("بازی پیدا نشد.", show_alert=True)
            return
        plp = plan_prizes_of(g)
        if pid == "0":
            plp.pop(face, None)
        else:
            plp[face] = int(pid)
        g.plan_prizes = json.dumps(plp)
        await s.commit()
    await cb.answer("ذخیره شد ✅" if pid != "0" else "پلن قطع شد.")
    await _open_game(cb, bot, int(gid), state)


# ───── سهمیهٔ تعداد دفعات ─────

@dice_game_router.callback_query(F.data.startswith("dga:tries:"))
async def dga_tries_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    gid = int(cb.data.split(":")[2])
    await state.set_state(DgFSM.tries)
    await state.update_data(dg_id=gid)
    await cb.message.answer(
        "🔢 هر کاربر چند بار اجازهٔ بازی داشته باشد؟\n"
        "یک عدد بفرست. <code>0</code> یعنی نامحدود.\n\n"
        "⚠️ حواست باشد هر بازی از کیف پولِ تو خرج می‌کند؛ سهمیهٔ نامحدود "
        "می‌تواند خیلی گران تمام شود.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@dice_game_router.message(DgFSM.tries, F.text)
async def dga_tries_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import _owner_ok_msg, fa_to_int
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    val = fa_to_int(msg.text)
    if val is None or val < 0 or val > 1_000_000:
        await msg.answer("عدد نامعتبر است. یک عدد بفرست (0 = نامحدود):")
        return
    gid = (await state.get_data()).get("dg_id")
    async with Session() as s:
        g = await s.get(DiceGame, gid)
        if not g or g.bot_id != row.id:
            await state.clear()
            await msg.answer("بازی پیدا نشد.")
            return
        g.tries_limit = val
        await s.commit()
    await state.clear()
    await msg.answer(
        "✅ سهمیه ذخیره شد.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ بازگشت به بازی", callback_data=f"dga:open:{gid}", style=ButtonStyle.DANGER)]]))


# ─────────────────────────── سمت کاربر ───────────────────────────

async def dice_games_kb(bot_id: int) -> InlineKeyboardMarkup | None:
    async with Session() as s:
        games = (await s.scalars(select(DiceGame).where(
            DiceGame.bot_id == bot_id, DiceGame.active.is_(True)
        ).order_by(DiceGame.id))).all()
    if not games:
        return None
    # === PATCH_DICE_GAME_BACK_BUTTON_V1 ===
    # این صفحه از دکمهٔ صفحهٔ اصلی صدا زده می‌شه؛ تویِ حالتِ منویِ شیشه‌ای
    # اگه دکمهٔ برگشت نباشه، کاربر همین‌جا گیر می‌کنه.
    from tenant import _inline_back_btn
    rows = [[InlineKeyboardButton(text=f"{g.emoji} {g.title}",
                                  callback_data=f"dg:play:{g.id}")] for g in games]
    rows.append([_inline_back_btn()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_dice_games(msg: Message, bot_id: int, header: str = ""):
    """صفحهٔ «بازی کن و جایزه بگیر» برای کاربر — از عملکردِ دکمهٔ صفحهٔ اصلی صدا زده می‌شود."""
    kb = await dice_games_kb(bot_id)
    if kb is None:
        # === PATCH_DICE_GAME_BACK_BUTTON_V1 ===
        from tenant import _inline_back_btn
        await msg.answer(
            "🎲 فعلاً هیچ بازی فعالی وجود ندارد. بعداً سر بزن!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_inline_back_btn()]]))
        return
    text = (header or "") + (
        "🎲 <b>بازی کن و جایزه بگیر</b>\n\n"
        "یکی از بازی‌ها را انتخاب کن. نتیجه را خودِ تلگرام تعیین می‌کند و "
        "هر چه بیاید، جایزه‌اش به کیف پولت اضافه می‌شود.")
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


# === PATCH_DICE_GAME_PLAN_PRIZE_V1 ===
async def _deliver_plan_prize(bot: Bot, telegram_id: int, bot_id: int, plan_id: int) -> str:
    """پلنِ VPنِ رایگانِ متصل به این جایزه را می‌سازد و پاره‌متنی برای اضافه‌شدن
    به پیامِ نتیجه برمی‌گرداند. کاربر هیچ پولی پرداخت نمی‌کند — این عیناً مثلِ
    خریدِ معمولیِ VPN است با این تفاوت که به‌جایِ کیفِ پول، بازی هزینه را
    «پرداخت» کرده. اگر ساخت با خطا مواجه شود، کاربر بی‌جواب نمی‌ماند و به
    مالکِ ربات ارجاع داده می‌شود."""
    from tenant.shared.panel_clients.vpn_panel import gen_uuid, gen_sub_id
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client_from_fields
    from tenant.bots.vpn.vpn_user import _make_config_email, new_svc_code, _expires_at_from_ms

    async with Session() as s:
        plan = await s.get(VpnPlan, plan_id)
        if not plan or plan.bot_id != bot_id or not plan.active:
            return "\n\n⚠️ پلنِ جایزه دیگر در دسترس نیست؛ به پشتیبانیِ ربات پیام بده تا دستی برایت بسازد."
        panel = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == bot_id, VpnPanel.panel_key == plan.panel_key,
            VpnPanel.enabled.is_(True)))
        if not panel:
            return "\n\n⚠️ پنلِ این پلن دیگر متصل نیست؛ به پشتیبانیِ ربات پیام بده تا دستی برایت بسازد."
        panel_data = (panel.base_url, panel.username, panel.password,
                      panel.sub_base or "", panel.inbound_ids, panel.panel_key, panel.api_key)
        p_gb, p_days, p_title, p_limit = plan.volume_gb, plan.days, plan.title, plan.limit_ip
        # === PATCH_DICE_PRIZE_SRVNAME_V1 ===
        # نامِ کانفیگ (نام سرور) باید دقیقاً مثلِ خریدِ عادی از الگویِ «نام
        # سرورها»ی همین پنل ساخته شود. قبلاً bot_username و tg_username پاس داده
        # نمی‌شد، پس اگر ادمین نوعِ شناسه را «یوزرنیم» گذاشته بود، به‌جای یوزرنیم
        # به آیدیِ عددی برمی‌گشت (باگِ «...7527059770ve5»). این‌جا یوزرنیمِ کاربر
        # و یوزرنیمِ ربات را از دیتابیس می‌خوانیم و پاس می‌دهیم.
        _bu = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        tg_username = _bu.username if _bu else None
        _brow = await s.get(BotRow, bot_id)
        bot_username = _brow.username if _brow else None

    email = await _make_config_email(
        bot_id, panel_data[5], bot_username, telegram_id, tg_username)
    client = make_panel_client_from_fields(
        panel_data[5], panel_data[0], panel_data[1], panel_data[2],
        sub_base=panel_data[3], inbound_ids_str=panel_data[4], api_key=panel_data[6])
    try:
        acc = await client.create_account(
            email, gb=p_gb, days=p_days, limit_ip=p_limit,
            uuid_str=gen_uuid(), sub_id=gen_sub_id())
    except Exception as e:
        try:
            await client.close()
        except Exception:
            logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")
        return (f"\n\n⚠️ جایزهٔ سرویسِ VPN «{p_title}» ساخته نشد ({e}). "
                "به پشتیبانیِ ربات پیام بده تا دستی برایت بسازد.")

    sub_link = acc.get("sub_link") or ""
    async with Session() as s:
        s.add(PanelAccount(
            bot_id=bot_id, user_telegram_id=telegram_id, email=email,
            panel_key=panel_data[5], uuid=acc.get("uuid"), sub_id=acc.get("sub_id"),
            sub_link=sub_link, volume_gb=p_gb, days=p_days,
            plan_title=f"🎁 جایزهٔ بازی: {p_title}", status="active",
            svc_code=await new_svc_code(s, bot_id),
            expires_at=_expires_at_from_ms(acc.get("expiry_ms")), first_conn_notified=False))
        await s.commit()
    try:
        await client.close()
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")

    return (f"\n\n🎁 سرویسِ VPN «<b>{p_title}</b>» ({p_gb or '∞'} گیگ / {p_days or '∞'} روز) "
            "هم برات ساخته شد — از «سرویس‌های من» لینکش رو بردار.")


async def run_play(bot: Bot, chat_id: int, telegram_id: int,
                   bot_id: int, game_id: int, player_name: str = "") -> bool:
    """اجرای یک نوبتِ بازی: چکِ سهمیه → انداختنِ تاسِ تلگرام → شارژِ کیف پول.

    هم از دکمهٔ اینلاین و هم از «نوشتنِ نامِ بازی» صدا زده می‌شود، تا منطق
    فقط یک‌جا نوشته شده باشد. خروجی: آیا نوبت اجرا شد یا نه.
    """
    import asyncio
    from tenant import rial

    # === PATCH_DICE_GAME_QUOTA_RACE_FIX_V1 ===
    # قفلِ per-user/per-game: تا وقتی یک نوبتِ این کاربر برای همین بازی کامل
    # نشده (چکِ سهمیه → انداختنِ تاس → ثبتِ نتیجه)، نوبتِ بعدیِ همان کاربر برای
    # همان بازی معطل می‌ماند، نه اینکه موازی و بدونِ دیدنِ آخرین سهمیه اجرا شود.
    lock = _get_play_lock(bot_id, game_id, telegram_id)
    async with lock:
        return await _run_play_locked(bot, chat_id, telegram_id, bot_id, game_id, player_name)


async def _run_play_locked(bot: Bot, chat_id: int, telegram_id: int,
                            bot_id: int, game_id: int, player_name: str = "") -> bool:
    """بدنهٔ اصلیِ run_play؛ همیشه زیرِ قفلِ per-user/per-game فراخوانی می‌شود."""
    from tenant import rial

    async with Session() as s:
        g = await s.get(DiceGame, game_id)
        if not g or g.bot_id != bot_id or not g.active:
            await bot.send_message(chat_id, "این بازی در دسترس نیست.")
            return False
        emoji, title = g.emoji, g.title
        limit, period = g.tries_limit, g.tries_period
        notify_win = bool(g.notify_owner_on_win)  # === PATCH_DICE_WINNER_NOTIFY_V1 ===
        game = g

    # === PATCH_DICE_GATE_V1 === شرطِ «اولین افزایشِ موجودی/سفارش» پیش از سهمیه
    _gok, _gmsg = await gate_check(bot_id, telegram_id)
    if not _gok:
        await bot.send_message(chat_id, _gmsg)
        return False

    if limit:
        used = await plays_used(bot_id, game, telegram_id)
        if used >= limit:
            # PATCH_DICE_CUSTOM_TEXTS_V1
            from tenant.shared.dice_texts import get_text as _dtxt
            # === PATCH_DICE_QUOTA_COUNTDOWN_V1 ===
            _rk = await quota_reset_kw(bot_id, game, telegram_id)
            await bot.send_message(chat_id, await _dtxt(
                bot_id, "quota", name=player_name, game=title, emoji=emoji,
                value="", prize="", left=0, limit=limit,
                period=PERIOD_LABELS.get(period, ""), **_rk), parse_mode="HTML")
            return False

    # نتیجه را سرورِ تلگرام تعیین می‌کند — انیمیشن هم برای کاربر پخش می‌شود.
    sent = await bot.send_dice(chat_id, emoji=emoji)
    value = (sent.dice.value if sent and sent.dice else 0) or 0

    faces = faces_of(emoji)
    amount = prize_for(game, value) if faces else prizes_of(game).get("all", 0)
    plan_id = plan_id_for(game, value)  # === PATCH_DICE_GAME_PLAN_PRIZE_V1 ===

    async with Session() as s:
        s.add(DicePlay(bot_id=bot_id, game_id=game_id,
                       user_telegram_id=telegram_id,
                       value=value, amount=int(amount)))
        # === PATCH_DICE_WINNER_NOTIFY_V1 === مشخصاتِ کاربر و مالک را برای
        # اعلامِ احتمالیِ «برندهٔ جدید» جمع می‌کنیم (چه بردِ نقدی، چه پلن).
        _win_user = None
        _owner_id = None
        _new_balance = None
        # FOR UPDATE: بدونِ قفل، خریدِ هم‌زمانِ همین کاربر (که موجودی را کم
        # می‌کند) با نوشتنِ «موجودیِ قدیم + جایزه» پاک می‌شد (lost update).
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id).with_for_update())
        if amount > 0:
            if u:
                u.balance = float(u.balance or 0) + int(amount)
                s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=int(amount),
                                kind="charge", note=f"جایزهٔ بازی {title} (نتیجه {value})"))
        if u:
            _win_user = {
                "full_name": u.full_name or "",
                "username": u.username or "",
                "phone": u.phone_number or "",
                "telegram_id": u.telegram_id,
            }
            _new_balance = float(u.balance or 0)
        _brow = await s.get(BotRow, bot_id)
        _owner_id = _brow.owner_id if _brow else None
        await s.commit()

    plan_text = ""
    if plan_id:
        plan_text = await _deliver_plan_prize(bot, telegram_id, bot_id, plan_id)

    # کمی مکث تا انیمیشنِ تلگرام تمام شود، بعد نتیجه اعلام شود
    await asyncio.sleep(3.5)

    # === PATCH_DICE_CUSTOM_TEXTS_V1 ===
    from tenant.shared.dice_texts import get_text as _dtxt
    _key = "win_cash" if amount > 0 else ("win_plan" if plan_id else "lose")
    _left = 0
    if limit:
        _left = max(0, limit - (await plays_used(bot_id, game, telegram_id)))
    text = await _dtxt(bot_id, _key, name=player_name, game=title, emoji=emoji,
                       value=value, prize=rial(amount), left=_left,
                       limit=limit or 0,
                       period=PERIOD_LABELS.get(period, ""))
    text += plan_text

    if limit:
        left = max(0, limit - (await plays_used(bot_id, game, telegram_id)))
        text += f"\n\nسهمیهٔ باقی‌مانده: <b>{left}</b> بار {PERIOD_LABELS.get(period, '')}"

    if player_name:
        text = f"👤 {player_name}\n" + text
    kb = None if player_name else await dice_games_kb(bot_id)
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

    # === PATCH_DICE_WINNER_NOTIFY_V1 ===
    # اگر مالک این قابلیت را روشن کرده و کاربر واقعاً چیزی برده (نقدی یا پلن)،
    # یک پیامِ حرفه‌ای با مشخصاتِ برنده و جایزه برای مالک می‌فرستیم.
    if notify_win and _owner_id and (amount > 0 or plan_id):
        try:
            await _notify_owner_winner(
                bot, _owner_id, emoji=emoji, title=title, value=value,
                user=_win_user, amount=int(amount), new_balance=_new_balance,
                plan_id=plan_id)
        except Exception:
            logger.exception("اعلامِ برنده به مالک ناموفق بود (نادیده گرفته شد)")
    return True


# === PATCH_DICE_WINNER_NOTIFY_V1 ===
async def _notify_owner_winner(bot: Bot, owner_id: int, *, emoji: str, title: str,
                               value: int, user: dict | None, amount: int,
                               new_balance: float | None, plan_id: int | None) -> None:
    """پیامِ حرفه‌ایِ «برندهٔ جدید» را برای مالکِ ربات می‌فرستد."""
    from tenant import rial
    from datetime import datetime as _dtnow

    user = user or {}
    full_name = _html.escape(str(user.get("full_name") or "بدون‌نام"))
    uname = user.get("username") or ""
    uname_line = f"@{uname}" if uname else "—"
    phone = user.get("phone") or "—"
    tg_id = user.get("telegram_id") or "—"

    # عنوانِ پلنِ VPN اگر جایزهٔ پلن بوده
    plan_line = ""
    if plan_id:
        try:
            async with Session() as s:
                _pl = await s.get(VpnPlan, int(plan_id))
                if _pl:
                    plan_line = (f"\n🎁 سرویس VPN: <b>{_html.escape(_pl.title or '—')}</b>"
                                 f" ({_pl.volume_gb or '∞'} گیگ / {_pl.days or '∞'} روز)")
        except Exception:
            logger.exception("خواندنِ عنوانِ پلنِ جایزه ناموفق بود (نادیده گرفته شد)")

    prize_lines = ""
    if amount > 0:
        prize_lines += f"\n💰 جایزهٔ نقدی: <b>{rial(amount)}</b>"
    prize_lines += plan_line
    if not prize_lines:
        prize_lines = "\n🎁 جایزه: —"

    bal_line = ""
    if new_balance is not None:
        bal_line = f"\n👛 موجودی فعلی کاربر: <b>{rial(int(new_balance))}</b>"

    now = _dtnow.now()
    when = now.strftime("%Y/%m/%d – %H:%M")

    msg = (
        "🏆 <b>برندهٔ جدید در بازی!</b>\n"
        "┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🎮 بازی: <b>{_html.escape(title)}</b> {emoji}\n"
        f"🎯 نتیجه: <b>{value}</b>\n"
        "┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        "👤 <b>مشخصات کاربر</b>\n"
        f"• نام: <b>{full_name}</b>\n"
        f"• یوزرنیم: {uname_line}\n"
        f"• شناسه: <code>{tg_id}</code>\n"
        f"• شماره: {phone}\n"
        "┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🎉 <b>جایزه</b>{prize_lines}"
        f"{bal_line}\n"
        "┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🕒 {when}"
    )
    await bot.send_message(owner_id, msg, parse_mode="HTML")


@dice_game_router.callback_query(F.data.startswith("dg:play:"))
async def dg_play(cb: CallbackQuery, bot: Bot):
    gid = int(cb.data.split(":")[2])
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
    if not row:
        await cb.answer()
        return
    await cb.answer("بریم! 🎲")
    _grp = cb.message.chat.type in ("group", "supergroup")
    await run_play(bot, cb.message.chat.id, cb.from_user.id, row.id, gid,
                   player_name=(cb.from_user.full_name if _grp else ""))


# ───── شروعِ بازی با نوشتنِ نامِ بازی ─────

def _norm(t: str) -> str:
    """نرمال‌سازیِ متن برای تطبیقِ نام: حذفِ فاصله‌های اضافه/اموجی‌های دوطرف
    و یکسان‌سازیِ «ی/ک»ِ عربی با فارسی."""
    t = (t or "").strip().replace("\u064a", "\u06cc").replace("\u0643", "\u06a9")
    t = t.replace("\u200c", " ")
    return " ".join(t.split()).lower()


async def try_play_by_name(msg: Message, bot: Bot, bot_id: int) -> bool:
    """اگر متنِ پیام دقیقاً نامِ (یا اموجیِ) یکی از بازی‌های فعال باشد، همان بازی
    را اجرا می‌کند. خروجی True یعنی پیام مصرف شد و نباید ادامه پردازش شود.

    عمداً «تطبیقِ دقیق» است نه «شامل‌بودن»، تا پیام‌های عادیِ کاربر که اتفاقی
    کلمهٔ «تاس» دارند بازی را راه نیندازند.
    """
    text = _norm(msg.text or "")
    if not text or len(text) > 40:
        return False
    in_group = msg.chat.type in ("group", "supergroup")
    async with Session() as s:
        q = select(DiceGame).where(DiceGame.bot_id == bot_id, DiceGame.active.is_(True))
        if in_group:
            # بازیِ متصل به یک گروهِ خاص فقط در همان گروه کار می‌کند
            q = q.where(DiceGame.in_groups.is_(True)).where(
                (DiceGame.group_chat_id.is_(None)) | (DiceGame.group_chat_id == msg.chat.id))
        games = (await s.scalars(q)).all()
    for g in games:
        names = {_norm(g.title), _norm(g.emoji), _norm(f"{g.emoji} {g.title}")}
        if text in names:
            await run_play(bot, msg.chat.id, msg.from_user.id, bot_id, g.id,
                           player_name=(msg.from_user.full_name if in_group else ""))
            return True
    return False
