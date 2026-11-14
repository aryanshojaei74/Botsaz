"""wager_rank.py — PATCH_WAGER_RANK_V1 (درخواستِ سینا)

۱) «برترین بازیکنان»: آمارِ بازی/برد/باخت + مبلغ‌ها + کارمزدِ پرداختی (از جدولِ platform_wagers؛ ربات اصلی bot_id=0 یا ربات مستأجر).
۲) کارمزدِ پلکانی: مالک پله‌ها را می‌گذارد («از N بازی/برد به بعد، کارمزد P٪»)؛ هنگامِ تسویه، کارمزدِ *برنده* بر اساس رتبهٔ خودش
   (تعدادِ بازی یا تعدادِ برد، پیش از همین بازی) محاسبه می‌شود. پله نگذاری ⇒ همان کارمزدِ عادی.
ذخیره: تنظیماتِ همان ربات با نام‌هایِ fee_basis (games|wins) و fee_tiers («حداقل:درصد» با «؛»).
هیچ import سنگینی (tenant) در سطحِ ماژول نیست تا هر دو پروسه بتوانند استفاده کنند.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

MAX_TIERS = 8
KEY_BASIS = "fee_basis"
KEY_TIERS = "fee_tiers"
_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹٫٬", "0123456789..")


def parse_tiers(text: str | None) -> list[tuple[int, Decimal]]:
    """هر خط «حداقل درصد» یا «حداقل:درصد». خروجی بر اساسِ حداقل مرتب. خطا ⇒ ValueError با پیامِ فارسی."""
    out: dict[int, Decimal] = {}
    raw = (text or "").translate(_FA).replace("؛", "\n").replace(";", "\n").replace("،", "\n")
    for line in raw.splitlines():
        line = line.strip().replace(":", " ").replace("=", " ").replace("٪", " ").replace("%", " ")
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"خط نامعتبر: «{line}» — فرمت: «حداقل درصد»، مثلاً «20 4»")
        try:
            mn, pct = int(parts[0]), Decimal(parts[1])
        except Exception:  # noqa: BLE001
            raise ValueError(f"عدد نامعتبر در «{line}»")
        if mn < 0 or mn > 1_000_000:
            raise ValueError("حداقل باید بین ۰ و ۱٬۰۰۰٬۰۰۰ باشد.")
        if pct < 0 or pct > 50:
            raise ValueError("درصد کارمزد باید بین ۰ و ۵۰ باشد.")
        out[mn] = pct
    if len(out) > MAX_TIERS:
        raise ValueError(f"حداکثر {MAX_TIERS} پله مجاز است.")
    return sorted(out.items())


def dump_tiers(tiers: list[tuple[int, Decimal]]) -> str:
    return ";".join(f"{mn}:{pct:g}" for mn, pct in tiers)


def load_tiers_text(raw: str | None) -> list[tuple[int, Decimal]]:
    try:
        return parse_tiers(raw)
    except ValueError:
        return []


def pct_for(tiers: list[tuple[int, Decimal]], count: int) -> Decimal | None:
    """بالاترین پله‌ای که count به آن رسیده؛ None اگر پله‌ای نیست یا به هیچ‌کدام نرسیده."""
    best = None
    for mn, pct in tiers:
        if count >= mn:
            best = pct
    return best


def tiers_text_fa(tiers: list[tuple[int, Decimal]], basis: str) -> str:
    unit = "برد" if basis == "wins" else "بازی"
    if not tiers:
        return "پله‌ای تنظیم نشده (همان کارمزد عادی)"
    return "\n".join(f"• از {mn} {unit} به بعد: {pct:g}٪" for mn, pct in tiers)


# ───────────────────────── خواندن/نوشتنِ تنظیمات (روی همان session) ─────────────────────────

async def _read(s, bot_id: int, key: str, default: str = "") -> str:
    from models import Setting          # ربات اصلی هم در همین جدول با bot_id=0 ذخیره می‌شود (platform_bot.get_pset)
    name = f"wager_game_{key}" if bot_id else f"platform_wager_{key}"
    row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == name))
    return (row.value if row and row.value is not None else default)


async def load_config(bot_id: int = 0) -> tuple[str, list[tuple[int, Decimal]]]:
    from config import Session
    async with Session() as s:
        basis = (await _read(s, bot_id, KEY_BASIS, "games")).strip() or "games"
        tiers = load_tiers_text(await _read(s, bot_id, KEY_TIERS, ""))
    return ("wins" if basis == "wins" else "games"), tiers


async def save_config(bot_id: int, *, basis: str | None = None, tiers_text: str | None = None) -> None:
    import platform_wager as core
    if bot_id:
        from tenant import set_setting
        if basis is not None:
            await set_setting(bot_id, f"wager_game_{KEY_BASIS}", basis)
        if tiers_text is not None:
            await set_setting(bot_id, f"wager_game_{KEY_TIERS}", tiers_text)
    else:
        if basis is not None:
            await core._set(f"platform_wager_{KEY_BASIS}", basis)
        if tiers_text is not None:
            await core._set(f"platform_wager_{KEY_TIERS}", tiers_text)


async def save_kv(bot_id: int, key: str, value: str) -> None:
    import platform_wager as core
    if bot_id:
        from tenant import set_setting
        await set_setting(bot_id, f"wager_game_{key}", value)
    else:
        await core._set(f"platform_wager_{key}", value)


async def free_cfg(bot_id: int = 0) -> dict:
    """PATCH_FREE_GAME_V1: {on, limit (سقفِ روزانهٔ هر نفر؛ ۰=نامحدود)، fee (هزینهٔ ورودِ هر بازیکن به تومان)}. پیش‌فرض: خاموش، ۵ بازی، بدونِ هزینه."""
    from config import Session
    async with Session() as s:
        on = (await _read(s, bot_id, "free_on", "0")).strip() == "1"
        lim = (await _read(s, bot_id, "free_limit", "5")).strip()
        fee = (await _read(s, bot_id, "free_fee", "0")).strip()

    def _i(v, d):
        try:
            return max(0, int(str(v).translate(_FA).replace(",", "")))
        except ValueError:
            return d
    return {"on": on, "limit": min(_i(lim, 5), 1000), "fee": min(_i(fee, 0), 10_000_000)}


# ───────────────────────── کارمزدِ پلکانی (هستهٔ تسویه صدا می‌زند) ─────────────────────────

async def rank_count(s, bot_id: int, uid: int, basis: str, exclude_wid: int | None = None) -> int:
    from sqlalchemy import func, or_
    from models import PlatformWager as W
    q = select(func.count(W.id)).where(
        func.coalesce(W.bot_id, 0) == bot_id, W.status == "settled", W.winner_id.is_not(None), W.amount > 0)   # بازیِ رایگان رتبه نمی‌سازد
    if exclude_wid:
        q = q.where(W.id != exclude_wid)
    if basis == "wins":
        q = q.where(W.winner_id == uid)
    else:
        q = q.where(or_(W.creator_id == uid, W.opponent_id == uid))
    return int(await s.scalar(q) or 0)


async def apply_rank_fee(s, w, winner: int) -> None:
    """پیش از پرداختِ جایزه: اگر پله‌ایِ فعال هست، کارمزد/جایزهٔ همین بازی را بر اساسِ رتبهٔ برنده بازمحاسبه می‌کند."""
    bot_id = int(w.bot_id or 0)
    basis = (await _read(s, bot_id, KEY_BASIS, "games")).strip()
    basis = "wins" if basis == "wins" else "games"
    tiers = load_tiers_text(await _read(s, bot_id, KEY_TIERS, ""))
    if not tiers:
        return
    cnt = await rank_count(s, bot_id, winner, basis, exclude_wid=w.id)
    pct = pct_for(tiers, cnt)
    if pct is None:
        return                                   # هنوز به اولین پله نرسیده ⇒ کارمزدِ عادی
    amount = int(w.amount)
    fee = int((Decimal(amount * 2) * pct / Decimal("100")).quantize(Decimal("1")))
    if amount * 2 - fee <= 0:
        return
    w.fee_pct, w.fee_amount, w.payout_amount = pct, fee, amount * 2 - fee


# ───────────────────────── آمارِ بازیکنان ─────────────────────────

async def player_stats(bot_id: int = 0, limit: int = 15, order: str = "wins") -> list[dict]:
    from config import Session
    from models import PlatformWager as W
    from sqlalchemy import func
    async with Session() as s:
        rows = (await s.execute(select(
            W.creator_id, W.creator_name, W.creator_username, W.opponent_id, W.opponent_name, W.opponent_username,
            W.winner_id, W.amount, W.fee_amount).where(
            func.coalesce(W.bot_id, 0) == bot_id, W.status == "settled", W.winner_id.is_not(None),
            W.opponent_id.is_not(None), W.amount > 0).order_by(W.id.desc()).limit(20000))).all()
    st: dict[int, dict] = {}

    def slot(uid, name, uname):
        d = st.setdefault(uid, {"uid": uid, "name": name or "", "uname": uname or "", "games": 0, "games_amt": 0,
                                "wins": 0, "wins_amt": 0, "losses": 0, "losses_amt": 0, "fees": 0})
        return d
    for cid, cn, cu, oid, on, ou, win, amt, fee in rows:
        amt, fee = int(amt or 0), int(fee or 0)
        for uid, nm, un in ((cid, cn, cu), (oid, on, ou)):
            d = slot(uid, nm, un)
            d["games"] += 1
            d["games_amt"] += amt
            if uid == win:
                d["wins"] += 1
                d["wins_amt"] += amt
                d["fees"] += fee                 # کارمزد از جایزهٔ برنده کم می‌شود
            else:
                d["losses"] += 1
                d["losses_amt"] += amt
    key = (lambda d: (-d["games"], -d["wins"])) if order == "games" else (lambda d: (-d["wins"], d["losses"], -d["games"]))
    return sorted(st.values(), key=key)[:limit]


def _nm(d: dict) -> str:
    import html
    n = (d["name"] or "").strip() or (f"@{d['uname']}" if d["uname"] else str(d["uid"]))
    return html.escape(n[:24])


def _t(n: int) -> str:
    return f"{int(n):,}"


def public_board(stats: list[dict], tiers=None, basis="games") -> str:
    if not stats:
        return "🏆 هنوز بازیکنی در جدول نیست؛ اولین بازی را شروع کن!"
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>برترین بازیکنان</b>", ""]
    for i, d in enumerate(stats[:10]):
        pre = medals[i] if i < 3 else f"{i + 1}."
        cnt = d["wins"] if basis == "wins" else d["games"]
        pct = pct_for(tiers or [], cnt)
        fee = f" · کارمزد {pct:g}٪" if pct is not None else ""
        lines.append(f"{pre} <b>{_nm(d)}</b> — {d['games']} بازی · {d['wins']} برد · {d['losses']} باخت{fee}")
    return "\n".join(lines)


def admin_board(stats: list[dict]) -> str:
    if not stats:
        return "📊 هنوز بازیِ تمام‌شده‌ای ثبت نشده."
    lines = ["📊 <b>آمار برترین بازیکنان</b> (بر اساس برد)", ""]
    for i, d in enumerate(stats):
        lines += [f"{i + 1}. <b>{_nm(d)}</b> (<code>{d['uid']}</code>)",
                  f"   بازی: {d['games']} به مبلغ {_t(d['games_amt'])} تومان",
                  f"   برد: {d['wins']} به مبلغ {_t(d['wins_amt'])} · باخت: {d['losses']} به مبلغ {_t(d['losses_amt'])}",
                  f"   کارمزدِ پرداختی (از جایزه‌ها): {_t(d['fees'])} تومان"]
    return "\n".join(lines)[:3900]


# ═══════════════════════════ رابط (ربات اصلی + ربات‌هایِ مستأجر) ═══════════════════════════
import re  # noqa: E402

from aiogram import Bot, F, Router  # noqa: E402
from aiogram.dispatcher.event.bases import SkipHandler  # noqa: E402
from aiogram.enums import ButtonStyle, ChatType  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.state import State, StatesGroup  # noqa: E402
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message  # noqa: E402

from logging_setup import get_logger as _get_logger  # noqa: E402
logger = _get_logger(__name__)

platform_rank_router = Router(name="platform_wager_rank")     # callbackهایِ pgrk:*  (ربات اصلی)
tenant_rank_router = Router(name="tenant_wager_rank")         # callbackهایِ twrk:*  (ربات مستأجر)

_BOARD_RX = re.compile(r"^\s*(?:برترین[\s‌]*(?:بازیکنان|بازیکن‌ها|ها|های[\s‌]*بازی)|لیدربورد|جدول[\s‌]*برترین‌?ها)\s*$")


class RankFSM(StatesGroup):
    tiers = State()
    free_limit = State()     # PATCH_FREE_GAME_V1
    free_fee = State()


HELP_TIERS = (
    "✏️ <b>پله‌هایِ کارمزد</b>\n\n"
    "هر خط: «حداقل  درصد». یعنی بازیکنی که به این تعداد (بازی یا برد، طبقِ «مبنا») رسیده، وقتی <b>برنده</b> شد کارمزدش این درصدِ "
    "کلِ مبلغِ دو نفر است.\n\n"
    "مثال:\n<code>0 5\n20 4\n50 3\n100 2</code>\n"
    "یعنی از ۰ بازی: ۵٪ · از ۲۰ بازی: ۴٪ · از ۵۰: ۳٪ · از ۱۰۰: ۲٪.\n"
    f"حداکثر {MAX_TIERS} پله، درصد ۰ تا ۵۰. برایِ حذفِ همهٔ پله‌ها: /clear · برایِ انصراف: /cancel"
)


async def _menu_view(bot_id: int, prefix: str, back_cb: str):
    basis, tiers = await load_config(bot_id)
    unit = "تعدادِ برد" if basis == "wins" else "تعدادِ بازی"
    text = ("🏅 <b>کارمزدِ پلکانی و آمارِ بازیکنان</b>\n\n"
            "کارمزدِ هر بازی (از جایزهٔ برنده) بر اساسِ رتبهٔ برنده کم/زیاد می‌شود.\n\n"
            f"📏 مبنا: <b>{unit}</b>\n{tiers_text_fa(tiers, basis)}\n\n"
            "بازیکنی که به هیچ پله‌ای نرسیده باشد، کارمزدِ عادی را می‌دهد.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📏 مبنا: {'تعدادِ برد' if basis == 'wins' else 'تعدادِ بازی'} (تغییر)",
                              callback_data=f"{prefix}:basis")],
        [InlineKeyboardButton(text="✏️ تنظیمِ پله‌ها", callback_data=f"{prefix}:set", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📊 آمارِ برترین بازیکنان", callback_data=f"{prefix}:stats", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🆓 بازیِ رایگان (سقف روزانه و هزینهٔ ورود)", callback_data=f"{prefix}:free")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb, style=ButtonStyle.DANGER)]])
    return text, kb


async def _free_view(bot_id: int, prefix: str):
    fc = await free_cfg(bot_id)
    lim = f"{fc['limit']} بازی" if fc["limit"] else "نامحدود"
    fee = f"{fc['fee']:,} تومان از هر بازیکن" if fc["fee"] else "ندارد"
    text = ("🆓 <b>بازیِ رایگان</b>\n\n"
            "وقتی کسی فقط اسمِ بازی را بنویسد (مثلاً «سرعت» یا «رندوم»، بدونِ مبلغ)، بازیِ رایگان ساخته می‌شود: "
            "جایزهٔ پولی ندارد، فقط برنده اعلام می‌شود.\n\n"
            f"وضعیت: <b>{'✅ روشن' if fc['on'] else '❌ خاموش'}</b>\n"
            f"📅 سقفِ روزانهٔ هر نفر: <b>{lim}</b>\n"
            f"🎟 هزینهٔ ورودِ هر بازی (کارمزدِ بازی): <b>{fee}</b>\n\n"
            "هزینهٔ ورود بعد از پیوستنِ حریف از کیف پولِ هر دو بازیکن کم می‌شود و برنمی‌گردد. "
            "بازی‌هایِ رایگان در آمار و رتبه‌بندی حساب نمی‌شوند.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("🔴 خاموش‌کردن" if fc["on"] else "🟢 روشن‌کردن"), callback_data=f"{prefix}:fon",
                              style=ButtonStyle.DANGER if fc["on"] else ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="📅 سقفِ روزانه", callback_data=f"{prefix}:flim"),
         InlineKeyboardButton(text="🎟 هزینهٔ ورود", callback_data=f"{prefix}:ffee")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"{prefix}:menu", style=ButtonStyle.DANGER)]])
    return text, kb


async def _free_action(cb: CallbackQuery, state: FSMContext, bot_id: int, act: str, prefix: str) -> bool:
    """act ∈ free|fon|flim|ffee؛ True یعنی پیام را فرستادیم/صفحه را عوض کردیم."""
    if act == "fon":
        fc = await free_cfg(bot_id)
        await save_kv(bot_id, "free_on", "0" if fc["on"] else "1")
    elif act in ("flim", "ffee"):
        await state.set_state(RankFSM.free_limit if act == "flim" else RankFSM.free_fee)
        await state.update_data(rank_bot=bot_id)
        await cb.message.answer(
            "📅 سقفِ بازیِ رایگانِ هر نفر در روز را بفرست (عدد؛ ۰ = نامحدود). برایِ انصراف: /cancel" if act == "flim"
            else "🎟 هزینهٔ ورودِ هر بازیکن در هر بازیِ رایگان را به تومان بفرست (۰ = رایگانِ کامل). برایِ انصراف: /cancel")
        await cb.answer()
        return True
    text, kb = await _free_view(bot_id, prefix)
    await _show(cb, text, kb)
    await cb.answer()
    return True


async def _free_input(msg: Message, state: FSMContext, bot_id: int, which: str, prefix: str) -> None:
    raw = (msg.text or "").strip()
    if raw not in ("/cancel", "لغو", "انصراف"):
        try:
            val = int(raw.translate(_FA).replace(",", ""))
            if val < 0 or val > (1000 if which == "free_limit" else 10_000_000):
                raise ValueError
        except ValueError:
            await msg.answer("⚠️ یک عددِ معتبر بفرست (یا /cancel).")
            return
        await save_kv(bot_id, which, str(val))
    await state.clear()
    text, kb = await _free_view(bot_id, prefix)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


async def _show(cb_or_msg, text, kb):
    try:
        if hasattr(cb_or_msg, "message") and cb_or_msg.message and hasattr(cb_or_msg, "data"):
            await cb_or_msg.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await cb_or_msg.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        if hasattr(cb_or_msg, "message") and cb_or_msg.message:
            await cb_or_msg.message.answer(text, reply_markup=kb, parse_mode="HTML")


# ─────────── ربات اصلی ───────────
def _main_admin(uid: int) -> bool:
    from config import PLATFORM_ADMIN_ID
    return uid == PLATFORM_ADMIN_ID


@platform_rank_router.callback_query(F.data.in_({"pgrk:menu", "pgrk:basis", "pgrk:set", "pgrk:stats", "pgrk:back",
                                                  "pgrk:free", "pgrk:fon", "pgrk:flim", "pgrk:ffee"}))
async def main_rank_cb(cb: CallbackQuery, state: FSMContext):
    if not _main_admin(cb.from_user.id):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    act = cb.data.split(":")[1]
    if act == "back":
        from platform_group_money import _edit_settings
        await _edit_settings(cb, "bet")
        return
    if act in ("free", "fon", "flim", "ffee"):
        await _free_action(cb, state, 0, act, "pgrk")
        return
    if act == "basis":
        basis, _ = await load_config(0)
        await save_config(0, basis=("games" if basis == "wins" else "wins"))
    elif act == "set":
        await state.set_state(RankFSM.tiers)
        await state.update_data(rank_bot=0)
        await cb.message.answer(HELP_TIERS, parse_mode="HTML")
        await cb.answer()
        return
    elif act == "stats":
        text = admin_board(await player_stats(0, 15))
        await cb.message.answer(text, parse_mode="HTML")
        await cb.answer()
        return
    text, kb = await _menu_view(0, "pgrk", "pgrk:back")
    await _show(cb, text, kb)
    await cb.answer()


@platform_rank_router.message(RankFSM.free_limit, F.text)
async def main_free_limit_input(msg: Message, state: FSMContext):
    if msg.from_user and _main_admin(msg.from_user.id):
        await _free_input(msg, state, 0, "free_limit", "pgrk")


@platform_rank_router.message(RankFSM.free_fee, F.text)
async def main_free_fee_input(msg: Message, state: FSMContext):
    if msg.from_user and _main_admin(msg.from_user.id):
        await _free_input(msg, state, 0, "free_fee", "pgrk")


@platform_rank_router.message(RankFSM.tiers, F.text)
async def main_rank_tiers_input(msg: Message, state: FSMContext):
    if not msg.from_user or not _main_admin(msg.from_user.id):
        return
    await _tiers_input(msg, state, 0, "pgrk", "pgrk:back")


@platform_rank_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(lambda m: bool(m.text and _BOARD_RX.match(m.text))))
async def main_board(msg: Message):
    from platform_group_money import _group_allowed
    import platform_wager_race as R
    if not msg.from_user or msg.from_user.is_bot or not await _group_allowed(msg.chat.id) or not await R.enabled():
        raise SkipHandler
    basis, tiers = await load_config(0)
    await msg.reply(public_board(await player_stats(0, 10), tiers, basis), parse_mode="HTML")


async def _tiers_input(msg: Message, state: FSMContext, bot_id: int, prefix: str, back_cb: str):
    raw = (msg.text or "").strip()
    if raw in ("/cancel", "لغو", "انصراف"):
        await state.clear()
        text, kb = await _menu_view(bot_id, prefix, back_cb)
        await msg.answer("لغو شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        return
    if raw == "/clear":
        await save_config(bot_id, tiers_text="")
        await state.clear()
        text, kb = await _menu_view(bot_id, prefix, back_cb)
        await msg.answer("✅ همهٔ پله‌ها حذف شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        return
    try:
        tiers = parse_tiers(raw)
    except ValueError as e:
        await msg.answer(f"⚠️ {e}\nدوباره بفرست (یا /cancel).")
        return
    await save_config(bot_id, tiers_text=dump_tiers(tiers))
    await state.clear()
    text, kb = await _menu_view(bot_id, prefix, back_cb)
    await msg.answer("✅ ذخیره شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")


# ─────────── ربات مستأجر ───────────
async def _tenant_row(bot: Bot, uid: int, need_admin: bool = True):
    from bot_addons import WAGER_ADDON, addon_active, wager_eligible
    from tenant.shared.wager_games import _row_and_admin
    row, is_admin = await _row_and_admin(bot, uid)
    if not row or not wager_eligible(row.field, row.plan) or not await addon_active(row.id, WAGER_ADDON):
        return None
    if need_admin and not is_admin:
        return None
    return row


@tenant_rank_router.callback_query(F.data.in_({"twrk:menu", "twrk:basis", "twrk:set", "twrk:stats",
                                                "twrk:free", "twrk:fon", "twrk:flim", "twrk:ffee"}))
async def tenant_rank_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _tenant_row(bot, cb.from_user.id)
    if not row:
        await cb.answer("فقط مالک/ادمینِ ربات", show_alert=True)
        return
    act = cb.data.split(":")[1]
    if act in ("free", "fon", "flim", "ffee"):
        await _free_action(cb, state, row.id, act, "twrk")
        return
    if act == "basis":
        basis, _ = await load_config(row.id)
        await save_config(row.id, basis=("games" if basis == "wins" else "wins"))
    elif act == "set":
        await state.set_state(RankFSM.tiers)
        await state.update_data(rank_bot=row.id)
        await cb.message.answer(HELP_TIERS, parse_mode="HTML")
        await cb.answer()
        return
    elif act == "stats":
        await cb.message.answer(admin_board(await player_stats(row.id, 15)), parse_mode="HTML")
        await cb.answer()
        return
    text, kb = await _menu_view(row.id, "twrk", "gset:betmenu")
    await _show(cb, text, kb)
    await cb.answer()


@tenant_rank_router.message(RankFSM.free_limit, F.text)
async def tenant_free_limit_input(msg: Message, bot: Bot, state: FSMContext):
    row = await _tenant_row(bot, msg.from_user.id) if msg.from_user else None
    if row:
        await _free_input(msg, state, row.id, "free_limit", "twrk")


@tenant_rank_router.message(RankFSM.free_fee, F.text)
async def tenant_free_fee_input(msg: Message, bot: Bot, state: FSMContext):
    row = await _tenant_row(bot, msg.from_user.id) if msg.from_user else None
    if row:
        await _free_input(msg, state, row.id, "free_fee", "twrk")


@tenant_rank_router.message(RankFSM.tiers, F.text)
async def tenant_rank_tiers_input(msg: Message, bot: Bot, state: FSMContext):
    if not msg.from_user:
        return
    row = await _tenant_row(bot, msg.from_user.id)
    if not row:
        return
    await _tiers_input(msg, state, row.id, "twrk", "gset:betmenu")


@tenant_rank_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(lambda m: bool(m.text and _BOARD_RX.match(m.text))))
async def tenant_board(msg: Message, bot: Bot):
    from bot_addons import WAGER_ADDON, addon_active, wager_eligible
    from tenant.shared.wager_games import _row_and_admin
    import platform_wager as core
    if not msg.from_user or msg.from_user.is_bot:
        raise SkipHandler
    row, _adm = await _row_and_admin(bot, msg.from_user.id)
    if (not row or not wager_eligible(row.field, row.plan) or not await addon_active(row.id, WAGER_ADDON)
            or not await core.master_enabled(row.id)):
        raise SkipHandler
    basis, tiers = await load_config(row.id)
    await msg.reply(public_board(await player_stats(row.id, 10), tiers, basis), parse_mode="HTML")
