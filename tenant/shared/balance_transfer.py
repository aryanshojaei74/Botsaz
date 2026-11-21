"""
balance_transfer.py — PATCH_BALANCE_TRANSFER_V1

انتقالِ موجودیِ کیف پول از یک کاربر به کاربرِ دیگرِ *همان ربات* (به آیدیِ عددی یا @یوزرنیم).

تنظیمِ مالک (مالی و کیف پول ← 🔁 انتقال موجودی)، کلیدِ btr_mode:
  off      خاموش (پیش‌فرض) — دکمه‌ای به کاربر نشان داده نمی‌شود
  approve  با تأییدِ ادمین — مبلغ همان لحظه از فرستنده کسر و نگه داشته می‌شود؛ با تأیید به مقصد می‌رسد
           و با رد به فرستنده برمی‌گردد
  auto     خودکار — فوری و بدونِ دخالتِ ادمین

همهٔ جابه‌جایی‌ها اتمیک‌اند (UPDATE شرطی روی balance)، پس با دو درخواستِ هم‌زمان موجودی منفی نمی‌شود و
پولی گم یا دوبرابر نمی‌شود. هر دو سرِ تراکنش در WalletTxn ثبت می‌شود.
"""
from __future__ import annotations

import html as _html
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select, update

from config import Session
from logging_setup import get_logger as _get_logger
from models import BalanceTransfer, Bot as BotRow, BotUser, WalletTxn

logger = _get_logger(__name__)

btr_router = Router(name="balance_transfer")

MODES = {"off": "⛔️ خاموش", "approve": "✋ با تأییدِ ادمین", "auto": "⚡️ خودکار"}
MIN_AMOUNT = 1_000
BTN_LABEL = "🔁 انتقال موجودی"


class BtrFSM(StatesGroup):
    target = State()
    amount = State()


def _fa(t: str) -> str:
    return (t or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).strip()


def fmt(n) -> str:
    return f"{int(n):,} تومان"


def parse_amount(txt: str) -> int | None:
    t = _fa(txt).replace(",", "").replace("٬", "").replace(" ", "")
    return int(t) if t.isdigit() and 0 < int(t) < 10 ** 11 else None


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text, cb, style=None):
    kw = {"text": text, "callback_data": cb}
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


# ═══════════════════ منطقِ مالی (قابلِ تست) ═══════════════════
async def get_mode(bot_id: int) -> str:
    from tenant import get_setting
    m = await get_setting(bot_id, "btr_mode", "off")
    return m if m in MODES else "off"


async def _debit(s, bot_id: int, uid: int, amount: int) -> bool:
    """کسرِ اتمیک؛ False اگر موجودی کافی نباشد."""
    r = await s.execute(update(BotUser).where(
        BotUser.id == uid, BotUser.bot_id == bot_id, BotUser.balance >= amount
    ).values(balance=BotUser.balance - amount))
    return (r.rowcount or 0) == 1


async def _credit(s, bot_id: int, uid: int, amount: int) -> None:
    await s.execute(update(BotUser).where(BotUser.id == uid, BotUser.bot_id == bot_id).values(
        balance=BotUser.balance + amount))


def _txn(s, bot_id: int, uid: int, amount: int, kind: str, note: str) -> None:
    s.add(WalletTxn(bot_id=bot_id, user_id=uid, amount=amount, kind=kind, note=note[:200]))


async def do_transfer(bot_id: int, from_id: int, to_id: int, amount: int, mode: str):
    """انتقال را انجام می‌دهد. خروجی: ("done"|"pending"|"no_balance"|"invalid", BalanceTransfer|None)."""
    if from_id == to_id or amount < MIN_AMOUNT:
        return "invalid", None
    async with Session() as s:
        if not await _debit(s, bot_id, from_id, amount):
            await s.rollback()
            return "no_balance", None
        t = BalanceTransfer(bot_id=bot_id, from_user_id=from_id, to_user_id=to_id, amount=amount)
        s.add(t)
        await s.flush()
        if mode == "auto":
            await _credit(s, bot_id, to_id, amount)
            t.status, t.decided_at = "done", datetime.utcnow()
            _txn(s, bot_id, from_id, -amount, "transfer_out", f"انتقال به کاربر #{to_id} (#{t.id})")
            _txn(s, bot_id, to_id, amount, "transfer_in", f"دریافت از کاربر #{from_id} (#{t.id})")
        else:
            _txn(s, bot_id, from_id, -amount, "transfer_hold", f"انتقالِ منتظرِ تأیید #{t.id}")
        await s.commit()
        return t.status, t


async def decide(bot_id: int, tid: int, approve: bool):
    """تأیید/ردِ انتقالِ منتظر. خروجی: (وضعیتِ جدید | "already", BalanceTransfer|None)."""
    async with Session() as s:
        t = await s.get(BalanceTransfer, tid, with_for_update=True)
        if not t or t.bot_id != bot_id or t.status != "pending":
            return "already", t
        amt = int(t.amount)
        if approve:
            await _credit(s, bot_id, t.to_user_id, amt)
            _txn(s, bot_id, t.to_user_id, amt, "transfer_in", f"دریافت از کاربر #{t.from_user_id} (#{t.id})")
            t.status = "done"
        else:
            await _credit(s, bot_id, t.from_user_id, amt)
            _txn(s, bot_id, t.from_user_id, amt, "transfer_refund", f"بازگشتِ انتقالِ ردشده #{t.id}")
            t.status = "rejected"
        t.decided_at = datetime.utcnow()
        await s.commit()
        return t.status, t


async def find_user(bot_id: int, text: str):
    """آیدیِ عددی یا @یوزرنیم → BotUser همین ربات (یا None)."""
    t = _fa(text)
    async with Session() as s:
        if t.isdigit():
            return await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == int(t)))
        name = t.lstrip("@").strip().lower()
        if not name or not all(c.isalnum() or c == "_" for c in name):
            return None
        return await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, func.lower(BotUser.username) == name))


# ═══════════════════ رابطِ کاربر ═══════════════════
async def _row(bot: Bot):
    from tenant import get_bot_row
    return await get_bot_row(bot)


async def _me(bot_id: int, tg: int):
    async with Session() as s:
        return await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == tg))


def _cancel_kb():
    return _kb([[_btn("❌ انصراف", "btr:cancel", ButtonStyle.DANGER)]])


async def start(msg: Message, bot: Bot, state: FSMContext) -> None:
    """ورودی از کیف پول (هم اینلاین هم کیبوردی)."""
    row = await _row(bot)
    if not row:
        return
    mode = await get_mode(row.id)
    if mode == "off":
        return await msg.answer("🔁 انتقالِ موجودی در این ربات فعال نیست.")
    me = await _me(row.id, msg.from_user.id)
    if not me:
        return
    if int(me.balance or 0) < MIN_AMOUNT:
        return await msg.answer(f"❌ موجودیِ شما برای انتقال کافی نیست (حداقل {fmt(MIN_AMOUNT)}).")
    await state.clear()
    await state.set_state(BtrFSM.target)
    how = "پس از تأییدِ ادمین به مقصد می‌رسد." if mode == "approve" else "فوری انجام می‌شود."
    await msg.answer(
        f"🔁 <b>انتقال موجودی</b>\n\n💵 موجودیِ شما: <b>{fmt(me.balance)}</b>\n"
        f"انتقال {how}\n\n👤 <b>آیدیِ عددی یا @یوزرنیمِ</b> گیرنده را بفرستید "
        "(گیرنده باید قبلاً همین ربات را استارت کرده باشد):", reply_markup=_cancel_kb())


@btr_router.callback_query(F.data == "btr:start")
async def btr_start_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _start_from_cb(cb, bot, state)


async def _start_from_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    mode = await get_mode(row.id)
    if mode == "off":
        return await cb.answer("انتقالِ موجودی فعال نیست.", show_alert=True)
    me = await _me(row.id, cb.from_user.id)
    if not me or int(me.balance or 0) < MIN_AMOUNT:
        return await cb.answer(f"موجودیِ شما برای انتقال کافی نیست (حداقل {fmt(MIN_AMOUNT)}).", show_alert=True)
    await state.clear()
    await state.set_state(BtrFSM.target)
    how = "پس از تأییدِ ادمین به مقصد می‌رسد." if mode == "approve" else "فوری انجام می‌شود."
    await cb.message.answer(
        f"🔁 <b>انتقال موجودی</b>\n\n💵 موجودیِ شما: <b>{fmt(me.balance)}</b>\n"
        f"انتقال {how}\n\n👤 <b>آیدیِ عددی یا @یوزرنیمِ</b> گیرنده را بفرستید "
        "(گیرنده باید قبلاً همین ربات را استارت کرده باشد):", reply_markup=_cancel_kb())
    await cb.answer()


@btr_router.callback_query(F.data == "btr:cancel")
async def btr_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("❎ انتقال لغو شد.")
    except Exception:
        await cb.message.answer("❎ انتقال لغو شد.")
    await cb.answer()


@btr_router.message(BtrFSM.target, F.text)
async def btr_target(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    to = await find_user(row.id, msg.text)
    if not to:
        return await msg.answer("❌ کاربری با این مشخصات در این ربات پیدا نشد (باید ربات را استارت کرده باشد).",
                                reply_markup=_cancel_kb())
    if to.telegram_id == msg.from_user.id:
        return await msg.answer("❌ نمی‌توانید به خودتان منتقل کنید.", reply_markup=_cancel_kb())
    await state.update_data(btr_to=to.id)
    await state.set_state(BtrFSM.amount)
    name = _html.escape(to.full_name or "") + (f" (@{to.username})" if to.username else "")
    await msg.answer(f"👤 گیرنده: <b>{name.strip() or to.telegram_id}</b>\n\n💵 مبلغ را به تومان بفرستید "
                     f"(حداقل {fmt(MIN_AMOUNT)}):", reply_markup=_cancel_kb())


@btr_router.message(BtrFSM.amount, F.text)
async def btr_amount(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    amt = parse_amount(msg.text)
    if amt is None or amt < MIN_AMOUNT:
        return await msg.answer(f"❌ مبلغ معتبر (حداقل {fmt(MIN_AMOUNT)}) بفرستید.", reply_markup=_cancel_kb())
    me = await _me(row.id, msg.from_user.id)
    if not me or int(me.balance or 0) < amt:
        return await msg.answer(f"❌ موجودیِ شما ({fmt(me.balance if me else 0)}) کافی نیست.",
                                reply_markup=_cancel_kb())
    d = await state.get_data()
    await state.update_data(btr_amt=amt)
    async with Session() as s:
        to = await s.get(BotUser, int(d.get("btr_to") or 0))
    name = (_html.escape(to.full_name or "") + (f" (@{to.username})" if to and to.username else "")).strip() \
        if to else "—"
    await msg.answer(
        f"🔁 <b>تأییدِ انتقال</b>\n\n👤 گیرنده: <b>{name}</b>\n💵 مبلغ: <b>{fmt(amt)}</b>\n\n"
        "پس از تأیید قابلِ بازگشت نیست.",
        reply_markup=_kb([[_btn("✅ تأیید و انتقال", "btr:ok", ButtonStyle.SUCCESS)],
                          [_btn("❌ انصراف", "btr:cancel", ButtonStyle.DANGER)]]))


@btr_router.callback_query(F.data == "btr:ok")
async def btr_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    d = await state.get_data()
    await state.clear()
    to_id, amt = int(d.get("btr_to") or 0), int(d.get("btr_amt") or 0)
    if not row or not to_id or not amt:
        return await cb.answer("درخواست منقضی شده؛ دوباره شروع کنید.", show_alert=True)
    mode = await get_mode(row.id)
    me = await _me(row.id, cb.from_user.id)
    if mode == "off" or not me:
        return await cb.answer("انتقالِ موجودی فعال نیست.", show_alert=True)
    res, t = await do_transfer(row.id, me.id, to_id, amt, mode)
    if res == "no_balance":
        return await cb.answer("موجودی کافی نیست.", show_alert=True)
    if res == "invalid" or t is None:
        return await cb.answer("انتقال ممکن نیست.", show_alert=True)
    async with Session() as s:
        to = await s.get(BotUser, to_id)
    try:
        await cb.message.edit_text(
            f"✅ <b>{fmt(amt)}</b> منتقل شد." if res == "done"
            else f"⏳ درخواستِ انتقالِ <b>{fmt(amt)}</b> ثبت شد و پس از تأییدِ ادمین انجام می‌شود.\n"
                 "مبلغ تا تعیینِ نتیجه از موجودیِ شما کسر شده است و در صورتِ رد برمی‌گردد.")
    except Exception:
        logger.info("btr: ویرایشِ پیامِ تأیید ممکن نشد (بی‌اهمیت)")
    await cb.answer()
    if res == "done":
        await _notify_user(bot, to.telegram_id if to else 0,
                           f"💰 <b>{fmt(amt)}</b> از طرفِ کاربرِ دیگری به کیف پولتان منتقل شد.")
    else:
        from tenant import _notify_admins
        fn = _html.escape(me.full_name or "") + (f" (@{me.username})" if me.username else "")
        tn = _html.escape((to.full_name or "") + (f" (@{to.username})" if to and to.username else "")) if to else "—"
        await _notify_admins(
            bot, row,
            f"🔁 <b>درخواستِ انتقالِ موجودی #{t.id}</b>\n\n👤 از: {fn} (<code>{me.telegram_id}</code>)\n"
            f"👤 به: {tn} (<code>{to.telegram_id if to else '-'}</code>)\n💵 مبلغ: <b>{fmt(amt)}</b>",
            _kb([[_btn("✅ تأیید", f"btr:ap:{t.id}", ButtonStyle.SUCCESS),
                  _btn("❌ رد", f"btr:rj:{t.id}", ButtonStyle.DANGER)]]))


async def _notify_user(bot: Bot, tg: int, text: str) -> None:
    if not tg:
        return
    try:
        await bot.send_message(tg, text)
    except Exception:
        logger.info("btr: پیام به کاربر %s نرسید (بی‌اهمیت)", tg)


@btr_router.callback_query(F.data.regexp(r"^btr:(ap|rj):\d+$"))
async def btr_decide(cb: CallbackQuery, bot: Bot):
    from tenant import is_owner_or_admin
    row = await _row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        return await cb.answer("فقط ادمین", show_alert=True)
    _, act, tid = cb.data.split(":")
    res, t = await decide(row.id, int(tid), act == "ap")
    if res == "already":
        return await cb.answer("این درخواست قبلاً بررسی شده.", show_alert=True)
    async with Session() as s:
        fu = await s.get(BotUser, t.from_user_id)
        tu = await s.get(BotUser, t.to_user_id)
    label = "✅ تأیید شد" if res == "done" else "❌ رد شد"
    try:
        await cb.message.edit_text((cb.message.html_text or cb.message.text or "") + f"\n\n{label} توسط {cb.from_user.id}",
                                   reply_markup=None)
    except Exception:
        logger.info("btr: ویرایشِ پیامِ ادمین ممکن نشد (بی‌اهمیت)")
    if res == "done":
        await _notify_user(bot, fu.telegram_id if fu else 0, f"✅ انتقالِ {fmt(t.amount)} شما انجام شد.")
        await _notify_user(bot, tu.telegram_id if tu else 0, f"💰 <b>{fmt(t.amount)}</b> به کیف پولتان منتقل شد.")
    else:
        await _notify_user(bot, fu.telegram_id if fu else 0,
                           f"❌ انتقالِ {fmt(t.amount)} رد شد و مبلغ به کیف پولتان برگشت.")
    await cb.answer(label)


# ═══════════════════ پنلِ مالک (مالی و کیف پول) ═══════════════════
async def _admin_view(bot_id: int):
    mode = await get_mode(bot_id)
    async with Session() as s:
        pend = await s.scalar(select(func.count()).select_from(BalanceTransfer).where(
            BalanceTransfer.bot_id == bot_id, BalanceTransfer.status == "pending")) or 0
    txt = ("🔁 <b>انتقال موجودی بینِ کاربران</b>\n\n"
           "کاربر می‌تواند از «کیف پول ← 🔁 انتقال موجودی» بخشی از موجودیِ خودش را به کاربرِ دیگرِ همین ربات بدهد.\n\n"
           f"وضعیتِ فعلی: <b>{MODES[mode]}</b>\n"
           f"⏳ درخواست‌هایِ منتظرِ تأیید: <b>{pend}</b>\n\n"
           "• <b>با تأییدِ ادمین:</b> مبلغ از فرستنده نگه داشته می‌شود و با تأییدِ شما به مقصد می‌رسد (با رد برمی‌گردد).\n"
           "• <b>خودکار:</b> فوری و بدونِ دخالتِ شما.")
    rows = []
    for k, lbl in (("approve", "✋ با تأییدِ ادمین"), ("auto", "⚡️ خودکار"), ("off", "⛔️ خاموش")):
        rows.append([_btn(("✅ " if k == mode else "") + lbl, f"btr:mode:{k}",
                          ButtonStyle.SUCCESS if k == mode else (ButtonStyle.DANGER if k == "off" else None))])
    rows.append([_btn("⬅️ بازگشت", "tadm:sec:finance", ButtonStyle.DANGER)])
    return txt, _kb(rows)


async def _admin_row(cb, bot):
    from tenant import is_owner_or_admin
    row = await _row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("فقط مالک/ادمین", show_alert=True)
        return None
    return row


async def admin_menu(cb, bot: Bot):
    row = await _admin_row(cb, bot)
    if not row:
        return
    txt, kb = await _admin_view(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        await cb.message.answer(txt, reply_markup=kb)
    await cb.answer()


@btr_router.callback_query(F.data == "btr:admin")
async def _admin_entry(cb: CallbackQuery, bot: Bot):
    await admin_menu(cb, bot)


@btr_router.callback_query(F.data.regexp(r"^btr:mode:(off|approve|auto)$"))
async def btr_set_mode(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    row = await _admin_row(cb, bot)
    if not row:
        return
    mode = cb.data.split(":")[2]
    await set_setting(row.id, "btr_mode", mode)
    txt, kb = await _admin_view(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        logger.info("btr: ویرایشِ پنل ممکن نشد (بی‌اهمیت)")
    await cb.answer(MODES[mode])
