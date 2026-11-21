"""
wallet_codes.py — «🎟 کدِ شارژِ کیف‌پول» (کد هدیه) (PATCH_WALLET_CODES_V1).

• مالک (مالی ← 🎟 کدهای شارژ کیف‌پول): کدِ تکی یا گروهی می‌سازد — مبلغ، سقفِ دفعاتِ کلِ کد، سقفِ هر کاربر، انقضا؛
  فهرست/غیرفعال/حذف.
• مشتری: در صفحهٔ کیف‌پول، اگر مالک حداقل یک کدِ فعال داشته باشد، «🎟 وارد کردنِ کدِ شارژ» نشان داده می‌شود.
• ضدِ حدس: ۵ کدِ اشتباه ⇒ ۱۵ دقیقه قفل. کسرِ سقف و واریز در یک تراکنش.
callbackها: wcode:* · wcode:redeem
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import Session
from models import BotUser, WalletCode, WalletCodeUse, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

wallet_codes_router = Router(name="wallet_codes")

LABEL = "🎟 وارد کردنِ کدِ شارژ"
PAGE = 8
LOCK_SECS = 900
MAX_FAILS = 5
_FAILS: dict[tuple[int, int], list[float]] = {}
_AR = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_ALPHA = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"        # بدونِ حرف‌هایِ شبیهِ هم (O/0، I/1/L)


class WcFSM(StatesGroup):
    redeem = State()
    amount = State()
    uses = State()
    per_user = State()
    days = State()
    code = State()
    count = State()


def gen_code() -> str:
    return "-".join("".join(secrets.choice(_ALPHA) for _ in range(4)) for _ in range(2))


def norm_code(t: str | None) -> str:
    return (t or "").translate(_AR).strip().upper().replace(" ", "")


def _num(t: str | None) -> int | None:
    s = (t or "").translate(_AR).replace(",", "").replace("٬", "").strip()
    return int(s) if s.isdigit() else None


def _fmt(n) -> str:
    return f"{int(n or 0):,}"


# ───────────────────────── منطقِ مشتری ─────────────────────────

def _locked(bot_id: int, uid: int, now: float | None = None) -> int:
    now = now or time.time()
    fl = [t for t in _FAILS.get((bot_id, uid), []) if now - t < LOCK_SECS]
    _FAILS[(bot_id, uid)] = fl
    return int(LOCK_SECS - (now - fl[0])) if len(fl) >= MAX_FAILS else 0


def _fail(bot_id: int, uid: int, now: float | None = None) -> None:
    _FAILS.setdefault((bot_id, uid), []).append(now or time.time())


async def redeem(bot_id: int, uid: int, raw: str, now: _dt.datetime | None = None) -> dict:
    """status: ok | locked | invalid | inactive | expired | used_up | already | no_user (+ amount, balance)."""
    wait = _locked(bot_id, uid)
    if wait:
        return {"status": "locked", "wait": wait}
    code = norm_code(raw)
    now = now or _dt.datetime.utcnow()
    async with Session() as s:
        c = await s.scalar(select(WalletCode).where(WalletCode.bot_id == bot_id, WalletCode.code == code).with_for_update()) if code else None
        if c is None:
            _fail(bot_id, uid)
            return {"status": "invalid"}
        if not c.active:
            return {"status": "inactive"}
        if c.expires_at and c.expires_at <= now:
            return {"status": "expired"}
        if c.max_uses and c.used_count >= c.max_uses:
            return {"status": "used_up"}
        if c.per_user:
            mine = int(await s.scalar(select(func.count(WalletCodeUse.id)).where(
                WalletCodeUse.code_id == c.id, WalletCodeUse.user_tg == uid)) or 0)
            if mine >= c.per_user:
                return {"status": "already"}
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == uid).with_for_update())
        if u is None:
            return {"status": "no_user"}
        c.used_count += 1
        u.balance = float(u.balance or 0) + c.amount_toman
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=c.amount_toman, kind="charge", note=f"کدِ شارژ {c.code}"))
        s.add(WalletCodeUse(bot_id=bot_id, code_id=c.id, user_tg=uid, amount_toman=c.amount_toman))
        await s.commit()
        return {"status": "ok", "amount": c.amount_toman, "balance": int(u.balance), "code": c.code, "user_id": u.id}


async def any_active(bot_id: int, now: _dt.datetime | None = None) -> bool:
    now = now or _dt.datetime.utcnow()
    async with Session() as s:
        n = await s.scalar(select(func.count(WalletCode.id)).where(
            WalletCode.bot_id == bot_id, WalletCode.active == True,   # noqa: E712
            (WalletCode.expires_at.is_(None)) | (WalletCode.expires_at > now),
            (WalletCode.max_uses == 0) | (WalletCode.used_count < WalletCode.max_uses)))
    return bool(n)


_MSG = {"invalid": "❌ کد نامعتبر است.", "inactive": "⛔️ این کد غیرفعال شده است.", "expired": "⌛️ مهلتِ این کد تمام شده است.",
        "used_up": "😕 ظرفیتِ این کد تمام شده است.", "already": "ℹ️ این کد را قبلاً استفاده کرده‌ای.",
        "no_user": "ابتدا ربات را /start کن."}


@wallet_codes_router.callback_query(F.data == "wcode:redeem")
async def ask_redeem(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.set_state(WcFSM.redeem)
    await cb.message.answer("🎟 کدِ شارژت را بفرست:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ انصراف", callback_data="wcode:x", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@wallet_codes_router.callback_query(F.data == "wcode:x")
async def cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """انصراف ⇒ پیامِ «کد را بفرست» پاک می‌شود و دوباره صفحهٔ کیف‌پول نشان داده می‌شود (نه فقط یک نوتیف)."""
    await state.clear()
    await cb.answer("انصراف داده شد.")
    try:
        await cb.message.delete()
    except Exception:      # noqa: BLE001
        logger.info("wallet_codes: پاک‌کردنِ پیامِ پرسشِ کد ممکن نشد")
    try:
        from tenant import get_bot_row, get_or_create_user, show_wallet
        row = await get_bot_row(bot)
        if row:
            u = await get_or_create_user(row.id, cb.from_user.id, cb.from_user.full_name or "", cb.from_user.username or "")
            await show_wallet(cb.message, row.id, u)
    except Exception:      # noqa: BLE001
        logger.exception("wallet_codes: بازگشت به کیف‌پول ناموفق بود")


@wallet_codes_router.message(WcFSM.redeem, F.text)
async def got_code(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    r = await redeem(row.id, msg.from_user.id, msg.text)
    if r["status"] == "locked":
        await state.clear()
        return await msg.answer(f"⛔️ تلاشِ اشتباهِ زیاد؛ {r['wait'] // 60 + 1} دقیقهٔ دیگر دوباره امتحان کن.")
    if r["status"] != "ok":
        if r["status"] != "invalid":
            await state.clear()
        return await msg.answer(_MSG.get(r["status"], "انجام نشد.") + ("\nدوباره بفرست یا انصراف بزن." if r["status"] == "invalid" else ""))
    await state.clear()
    await msg.answer(f"✅ <b>{_fmt(r['amount'])} تومان</b> به کیف‌پولت اضافه شد.\n💰 موجودیِ جدید: {_fmt(r['balance'])} تومان",
                     parse_mode="HTML")
    try:
        from tenant.shared.tx_channel import tx_log
        async with Session() as s:
            u = await s.get(BotUser, r["user_id"])
        await tx_log(bot, row.id, buyer=u, amount=r["amount"], gateway="gift_code", new_balance=r["balance"], ref=r["code"])
    except Exception:      # noqa: BLE001
        logger.exception("wallet_codes: لاگِ تراکنش ناموفق بود")


# ───────────────────────── مالک ─────────────────────────

async def _owner(cb, bot):
    from tenant import _owner_ok
    return await _owner_ok(cb, bot)


def _kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def _b(text, cb, style=None):
    return InlineKeyboardButton(text=text, callback_data=cb, style=style)


async def _edit(cb: CallbackQuery, text: str, kb):
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@wallet_codes_router.callback_query(F.data == "wcode:menu")
async def menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.clear()
    async with Session() as s:
        total = int(await s.scalar(select(func.count(WalletCode.id)).where(WalletCode.bot_id == row.id)) or 0)
        used = int(await s.scalar(select(func.count(WalletCodeUse.id)).where(WalletCodeUse.bot_id == row.id)) or 0)
        paid = int(await s.scalar(select(func.coalesce(func.sum(WalletCodeUse.amount_toman), 0)).where(WalletCodeUse.bot_id == row.id)) or 0)
    await _edit(cb, "🎟 <b>کدهایِ شارژ کیف‌پول</b>\n\nمشتری کد را در «کیف پول» وارد می‌کند و مبلغ به موجودی‌اش اضافه می‌شود. "
                    "دکمهٔ ورود فقط وقتی دیده می‌شود که حداقل یک کدِ فعال داشته باشی.\n\n"
                    f"📦 کدها: <b>{_fmt(total)}</b> · ✅ استفاده: <b>{_fmt(used)}</b> بار · 💸 مجموعِ پرداخت‌شده: <b>{_fmt(paid)}</b> تومان",
                _kb([_b("➕ ساخت کدِ تکی", "wcode:new:1", ButtonStyle.SUCCESS), _b("🎲 ساخت گروهی", "wcode:new:n", ButtonStyle.SUCCESS)],
                    [_b("📋 فهرستِ کدها", "wcode:ls:0", ButtonStyle.PRIMARY)],
                    [_b("⬅️ بازگشت", "tadm:sec:finance", ButtonStyle.DANGER)]))
    await cb.answer()


@wallet_codes_router.callback_query(F.data.regexp(r"^wcode:new:(1|n)$"))
async def new_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.clear()
    await state.set_state(WcFSM.amount)
    await state.update_data(wc_batch=cb.data.endswith(":n"))
    await cb.message.answer("💰 مبلغِ هر کد را به <b>تومان</b> بفرست:", parse_mode="HTML",
                            reply_markup=_kb([_b("✖️ لغو", "wcode:menu", ButtonStyle.DANGER)]))
    await cb.answer()


async def _guard_msg(msg: Message, bot: Bot):
    from tenant import _owner_ok_msg
    return await _owner_ok_msg(msg, bot)


@wallet_codes_router.message(WcFSM.amount, F.text)
async def s_amount(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    v = _num(msg.text)
    if not v or v > 100_000_000:
        return await msg.answer("یک مبلغِ معتبر (۱ تا ۱۰۰ میلیون تومان) بفرست.")
    await state.update_data(wc_amount=v)
    await state.set_state(WcFSM.uses)
    await msg.answer("🔢 هر کد در مجموع چند بار قابلِ استفاده باشد؟ (۰ = نامحدود، ۱ = یک‌بار مصرف)")


@wallet_codes_router.message(WcFSM.uses, F.text)
async def s_uses(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    v = _num(msg.text)
    if v is None or v > 1_000_000:
        return await msg.answer("یک عددِ صحیح بفرست (۰ = نامحدود).")
    await state.update_data(wc_uses=v)
    await state.set_state(WcFSM.per_user)
    await msg.answer("👤 هر کاربر حداکثر چند بار بتواند از هر کد استفاده کند؟ (۱ پیشنهادی، ۰ = نامحدود)")


@wallet_codes_router.message(WcFSM.per_user, F.text)
async def s_per_user(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    v = _num(msg.text)
    if v is None or v > 1000:
        return await msg.answer("یک عددِ صحیح بفرست.")
    await state.update_data(wc_per=v)
    await state.set_state(WcFSM.days)
    await msg.answer("📅 چند روز اعتبار داشته باشد؟ (۰ = بدونِ انقضا)")


@wallet_codes_router.message(WcFSM.days, F.text)
async def s_days(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    v = _num(msg.text)
    if v is None or v > 3650:
        return await msg.answer("یک عددِ صحیح بین ۰ تا ۳۶۵۰ بفرست.")
    await state.update_data(wc_days=v)
    d = await state.get_data()
    if d.get("wc_batch"):
        await state.set_state(WcFSM.count)
        return await msg.answer("🎲 چند کد ساخته شود؟ (۱ تا ۲۰۰)")
    await state.set_state(WcFSM.code)
    await msg.answer("✏️ خودِ کد را بفرست (حرف/عدد، مثل <code>NOWRUZ</code>) یا <code>-</code> برایِ ساختِ تصادفی:", parse_mode="HTML")


async def _create(bot_id: int, d: dict, codes: list[str]) -> tuple[list[str], list[str]]:
    exp = _dt.datetime.utcnow() + _dt.timedelta(days=int(d["wc_days"])) if d.get("wc_days") else None
    made, dup = [], []
    async with Session() as s:
        for c in codes:
            if await s.scalar(select(WalletCode.id).where(WalletCode.bot_id == bot_id, WalletCode.code == c)):
                dup.append(c)
                continue
            s.add(WalletCode(bot_id=bot_id, code=c, amount_toman=int(d["wc_amount"]), max_uses=int(d["wc_uses"]),
                             per_user=int(d["wc_per"]), expires_at=exp, active=True))
            made.append(c)
        await s.commit()
    return made, dup


@wallet_codes_router.message(WcFSM.code, F.text)
async def s_code(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    raw = (msg.text or "").strip()
    code = gen_code() if raw == "-" else norm_code(raw)
    if not (3 <= len(code) <= 40) or not all(ch.isalnum() or ch in "-_" for ch in code):
        return await msg.answer("کد باید ۳ تا ۴۰ حرفِ انگلیسی/عدد (یا - و _) باشد.")
    d = await state.get_data()
    made, dup = await _create(row.id, d, [code])
    await state.clear()
    if dup:
        return await msg.answer("❌ این کد قبلاً وجود دارد.")
    await msg.answer(f"✅ کد ساخته شد:\n<code>{_html.escape(code)}</code>\n💰 {_fmt(d['wc_amount'])} تومان", parse_mode="HTML",
                     reply_markup=_kb([_b("🎟 کدها", "wcode:menu", ButtonStyle.PRIMARY)]))


@wallet_codes_router.message(WcFSM.count, F.text)
async def s_count(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    n = _num(msg.text)
    if not n or n > 200:
        return await msg.answer("یک عددِ ۱ تا ۲۰۰ بفرست.")
    d = await state.get_data()
    codes: list[str] = []
    while len(codes) < n:
        c = gen_code()
        if c not in codes:
            codes.append(c)
    made, _dup = await _create(row.id, d, codes)
    await state.clear()
    body = "\n".join(made)
    await msg.answer(f"✅ {len(made)} کد ساخته شد ({_fmt(d['wc_amount'])} تومان برایِ هر کدام). فایلِ کدها پایین است.")
    await msg.answer_document(BufferedInputFile(body.encode(), filename="wallet-codes.txt"),
                              reply_markup=_kb([_b("🎟 کدها", "wcode:menu", ButtonStyle.PRIMARY)]))


@wallet_codes_router.callback_query(F.data.regexp(r"^wcode:ls:\d+$"))
async def listing(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    page = int(cb.data.split(":")[2])
    async with Session() as s:
        cs = (await s.scalars(select(WalletCode).where(WalletCode.bot_id == row.id).order_by(WalletCode.id.desc())
                              .offset(page * PAGE).limit(PAGE + 1))).all()
    more, cs = len(cs) > PAGE, cs[:PAGE]
    rows = [[_b(("🟢 " if c.active else "⚪️ ") + f"{c.code} — {_fmt(c.amount_toman)} ({c.used_count}/{c.max_uses or '∞'})", f"wcode:v:{c.id}")]
            for c in cs]
    nav = ([_b("⬅️ قبلی", f"wcode:ls:{page - 1}")] if page else []) + ([_b("بعدی ➡️", f"wcode:ls:{page + 1}")] if more else [])
    await _edit(cb, "📋 <b>فهرستِ کدها</b>" + ("" if cs else "\n\nهنوز کدی نساخته‌ای."),
                _kb(*rows, nav, [_b("⬅️ بازگشت", "wcode:menu", ButtonStyle.DANGER)]))
    await cb.answer()


@wallet_codes_router.callback_query(F.data.regexp(r"^wcode:(v|t|d|dok):\d+$"))
async def detail(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    act, cid = cb.data.split(":")[1], int(cb.data.split(":")[2])
    async with Session() as s:
        c = await s.get(WalletCode, cid)
        if not c or c.bot_id != row.id:
            return await cb.answer("پیدا نشد.", show_alert=True)
        if act == "t":
            c.active = not c.active
            await s.commit()
        elif act == "dok":
            await s.execute(WalletCodeUse.__table__.delete().where(WalletCodeUse.code_id == cid))
            await s.delete(c)
            await s.commit()
            await cb.answer("حذف شد.")
            cb2 = cb.model_copy(update={"data": "wcode:ls:0"})
            return await listing(cb2, bot)
    if act == "d":
        return await _edit(cb, f"🗑 کدِ <code>{_html.escape(c.code)}</code> و سابقهٔ استفاده‌اش حذف شود؟",
                           _kb([_b("✅ بله، حذف", f"wcode:dok:{cid}", ButtonStyle.DANGER), _b("⬅️ نه", f"wcode:v:{cid}")]))
    exp = c.expires_at.strftime("%Y-%m-%d") if c.expires_at else "بدونِ انقضا"
    await _edit(cb, f"🎟 <code>{_html.escape(c.code)}</code>\n💰 {_fmt(c.amount_toman)} تومان\n"
                    f"🔢 استفاده: {c.used_count} از {c.max_uses or 'نامحدود'} · هر کاربر: {c.per_user or 'نامحدود'}\n"
                    f"📅 انقضا: {exp}\nوضعیت: {'🟢 فعال' if c.active else '⚪️ غیرفعال'}",
                _kb([_b("⚪️ غیرفعال‌کردن" if c.active else "🟢 فعال‌کردن", f"wcode:t:{cid}", ButtonStyle.PRIMARY)],
                    [_b("🗑 حذف", f"wcode:d:{cid}", ButtonStyle.DANGER)], [_b("⬅️ بازگشت", "wcode:ls:0", ButtonStyle.DANGER)]))
    if act != "v":
        await cb.answer("انجام شد.")
    else:
        await cb.answer()
