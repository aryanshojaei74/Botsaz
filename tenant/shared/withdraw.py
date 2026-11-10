"""
withdraw.py — PATCH_WITHDRAW_V1

۱) درخواستِ برداشتِ وجه از کیف پول (پیشنهادِ #200): کاربر وقتی به حداقلِ مبلغ رسید درخواست می‌دهد؛ مبلغ همان لحظه
   از موجودی کسر و نگه داشته می‌شود؛ ادمین «پرداخت شد» یا «رد» (= برگشتِ مبلغ) می‌زند. پیش‌فرض خاموش.
   منبعِ قابلِ‌برداشت (wd_source): «referral» فقط پاداشِ زیرمجموعه‌گیری (پیش‌فرض) · «all» کلِ موجودی.
۲) متنِ زیرِ کیف پول («قابل برداشت نمی‌باشد…») قابلِ ویرایش/حذف توسطِ مالک (wallet_note_mode: default/custom/hidden).
همهٔ کسر/برگشت‌ها اتمیک‌اند (UPDATE شرطی) و در WalletTxn ثبت می‌شوند.
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
from models import BotUser, WalletTxn, WithdrawRequest

logger = _get_logger(__name__)

wd_router = Router(name="withdraw")

BTN_LABEL = "💸 درخواست برداشت وجه"
DEFAULT_MIN = 100_000
WALLET_NOTE_DEFAULT = "⚠️ <i>مبلغ کیف پول قابل برداشت نمی‌باشد و فقط برای خرید سرویس استفاده می‌شود.</i>"
NOTE_MODES = {"default": "پیش‌فرض", "custom": "متنِ دلخواه", "hidden": "حذف‌شده"}


class WdFSM(StatesGroup):
    amount = State()
    dest = State()
    a_min = State()
    a_note = State()
    a_label = State()


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


# ═══════════════════ تنظیمات ═══════════════════
async def _gs(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def label(bot_id: int) -> str:
    """نامِ دکمهٔ برداشت در کیف پول — قابلِ تغییر توسطِ مالک (wd_label)."""
    t = (await _gs(bot_id, "wd_label", "")).strip()
    return t[:60] if t else BTN_LABEL


async def enabled(bot_id: int) -> bool:
    return (await _gs(bot_id, "wd_enabled", "0")) == "1"


async def min_amount(bot_id: int) -> int:
    v = _fa(await _gs(bot_id, "wd_min", str(DEFAULT_MIN)))
    return int(v) if v.isdigit() and int(v) > 0 else DEFAULT_MIN


async def source(bot_id: int) -> str:
    return "all" if (await _gs(bot_id, "wd_source", "referral")) == "all" else "referral"


async def wallet_note(bot_id: int, default_text: str) -> str:
    """متنِ هشدارِ زیرِ کیف پول/پروفایل: پیش‌فرض، دلخواهِ مالک، یا حذف (رشتهٔ خالی)."""
    mode = await _gs(bot_id, "wallet_note_mode", "default")
    if mode == "hidden":
        return ""
    if mode == "custom":
        t = (await _gs(bot_id, "wallet_note_text", "")).strip()
        if t:
            return t
    if await enabled(bot_id):
        return (f"💸 با رسیدنِ مبلغِ قابلِ‌برداشت به {fmt(await min_amount(bot_id))} می‌توانی از دکمهٔ "
                f"«{_html.escape(await label(bot_id))}» درخواست بدهی.")
    return default_text


# ═══════════════════ منطقِ مالی (قابلِ تست) ═══════════════════
async def withdrawable(bot_id: int, user_id: int, balance: int) -> int:
    if await source(bot_id) == "all":
        return max(0, int(balance))
    async with Session() as s:
        earned = int(await s.scalar(select(func.coalesce(func.sum(WalletTxn.amount), 0)).where(
            WalletTxn.bot_id == bot_id, WalletTxn.user_id == user_id, WalletTxn.kind == "referral")) or 0)
        taken = int(await s.scalar(select(func.coalesce(func.sum(WithdrawRequest.amount), 0)).where(
            WithdrawRequest.bot_id == bot_id, WithdrawRequest.user_id == user_id,
            WithdrawRequest.status != "rejected")) or 0)
    return max(0, min(int(balance), earned - taken))


async def create_request(bot_id: int, user_id: int, amount: int, dest: str):
    """کسرِ اتمیک + ثبتِ درخواست. خروجی: ("ok", WithdrawRequest) | ("no_balance", None)."""
    async with Session() as s:
        r = await s.execute(update(BotUser).where(
            BotUser.id == user_id, BotUser.bot_id == bot_id, BotUser.balance >= amount
        ).values(balance=BotUser.balance - amount))
        if (r.rowcount or 0) != 1:
            await s.rollback()
            return "no_balance", None
        w = WithdrawRequest(bot_id=bot_id, user_id=user_id, amount=amount, dest=dest[:500])
        s.add(w)
        await s.flush()
        s.add(WalletTxn(bot_id=bot_id, user_id=user_id, amount=-amount, kind="withdraw_hold",
                        note=f"درخواست برداشت #{w.id}"))
        await s.commit()
        return "ok", w


async def decide(bot_id: int, wid: int, paid: bool):
    async with Session() as s:
        w = await s.get(WithdrawRequest, wid, with_for_update=True)
        if not w or w.bot_id != bot_id or w.status != "pending":
            return "already", w
        if paid:
            w.status = "paid"
        else:
            await s.execute(update(BotUser).where(BotUser.id == w.user_id, BotUser.bot_id == bot_id).values(
                balance=BotUser.balance + int(w.amount)))
            s.add(WalletTxn(bot_id=bot_id, user_id=w.user_id, amount=int(w.amount), kind="withdraw_refund",
                            note=f"بازگشتِ درخواستِ برداشتِ ردشده #{w.id}"))
            w.status = "rejected"
        w.decided_at = datetime.utcnow()
        await s.commit()
        return w.status, w


# ═══════════════════ رابطِ کاربر ═══════════════════
async def _row(bot: Bot):
    from tenant import get_bot_row
    return await get_bot_row(bot)


async def _me(bot_id: int, tg: int):
    async with Session() as s:
        return await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == tg))


def _cancel_kb():
    return _kb([[_btn("❌ انصراف", "wd:cancel", ButtonStyle.DANGER)]])


async def start(msg: Message, bot: Bot, state: FSMContext) -> None:
    """ورودی از کیف پول (کیبوردی)؛ نسخهٔ اینلاین: wd:start."""
    row = await _row(bot)
    if not row:
        return
    me = await _me(row.id, msg.from_user.id)
    if not row or not me:
        return
    ok, err = await _can_start(row.id, me)
    if not ok:
        return await msg.answer(err)
    await state.clear()
    await state.set_state(WdFSM.amount)
    avail = await withdrawable(row.id, me.id, int(me.balance or 0))
    await msg.answer(
        f"💸 <b>درخواست برداشت وجه</b>\n\n💵 موجودی: <b>{fmt(me.balance)}</b>\n"
        f"✅ قابلِ برداشت: <b>{fmt(avail)}</b>\n🔻 حداقل: <b>{fmt(await min_amount(row.id))}</b>\n\n"
        "مبلغِ برداشت را به تومان بفرست:", reply_markup=_cancel_kb())


async def _can_start(bot_id: int, me) -> tuple[bool, str]:
    if not await enabled(bot_id):
        return False, "💸 برداشتِ وجه در این ربات فعال نیست."
    mn = await min_amount(bot_id)
    avail = await withdrawable(bot_id, me.id, int(me.balance or 0))
    if avail < mn:
        return False, (f"❌ هنوز به حداقلِ برداشت نرسیده‌ای.\nقابلِ برداشت: {fmt(avail)}\nحداقل: {fmt(mn)}")
    return True, ""


@wd_router.callback_query(F.data == "wd:start")
async def wd_start_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    me = await _me(row.id, cb.from_user.id) if row else None
    if not me:
        return await cb.answer()
    ok, err = await _can_start(row.id, me)
    if not ok:
        return await cb.answer(err, show_alert=True)
    await cb.answer()
    await start(_Proxy(cb), bot, state)


class _Proxy:
    """شیمی برایِ فراخوانیِ start از callback (فقط answer و from_user لازم است)."""
    def __init__(self, cb):
        self.from_user = cb.from_user
        self._m = cb.message

    async def answer(self, *a, **k):
        return await self._m.answer(*a, **k)


@wd_router.callback_query(F.data == "wd:cancel")
async def wd_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("❎ لغو شد.")
    except Exception:
        await cb.message.answer("❎ لغو شد.")
    await cb.answer()


@wd_router.message(WdFSM.amount, F.text)
async def wd_amount(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    me = await _me(row.id, msg.from_user.id) if row else None
    if not me:
        return
    amt = parse_amount(msg.text)
    mn = await min_amount(row.id)
    avail = await withdrawable(row.id, me.id, int(me.balance or 0))
    if amt is None or amt < mn:
        return await msg.answer(f"❌ مبلغِ معتبر (حداقل {fmt(mn)}) بفرست.", reply_markup=_cancel_kb())
    if amt > avail:
        return await msg.answer(f"❌ حداکثرِ قابلِ برداشتِ تو {fmt(avail)} است.", reply_markup=_cancel_kb())
    await state.update_data(wd_amt=amt)
    await state.set_state(WdFSM.dest)
    await msg.answer("💳 شمارهٔ کارت (یا شبا) و نامِ صاحبِ حساب را بفرست (یا آدرسِ کیف پولِ ارزی + شبکه):",
                     reply_markup=_cancel_kb())


@wd_router.message(WdFSM.dest, F.text)
async def wd_dest(msg: Message, bot: Bot, state: FSMContext):
    dest = (msg.text or "").strip()
    if len(dest) < 6:
        return await msg.answer("❌ اطلاعاتِ واریز کامل نیست؛ دوباره بفرست.", reply_markup=_cancel_kb())
    d = await state.get_data()
    await state.update_data(wd_dest=dest[:500])
    await msg.answer(
        f"💸 <b>تأییدِ درخواستِ برداشت</b>\n\n💵 مبلغ: <b>{fmt(d.get('wd_amt', 0))}</b>\n"
        f"🏦 واریز به:\n<code>{_html.escape(dest)}</code>\n\n"
        "مبلغ تا نتیجه از موجودی‌ات کسر می‌شود؛ در صورتِ رد برمی‌گردد.",
        reply_markup=_kb([[_btn("✅ تأیید و ثبت", "wd:ok", ButtonStyle.SUCCESS)],
                          [_btn("❌ انصراف", "wd:cancel", ButtonStyle.DANGER)]]))


async def notify_admins_new(bot: Bot, row, me, w) -> None:
    """اعلانِ درخواستِ تازه به مالک/ادمین‌ها با دکمه‌هایِ واریز/رد (مشترکِ ربات و API)."""
    from tenant import _notify_admins
    fn = _html.escape(me.full_name or "") + (f" (@{me.username})" if me.username else "")
    await _notify_admins(
        bot, row,
        f"💸 <b>درخواستِ برداشت #{w.id}</b>\n\n👤 {fn} (<code>{me.telegram_id}</code>)\n"
        f"💵 مبلغ: <b>{fmt(w.amount)}</b>\n🏦 <code>{_html.escape(w.dest or '')}</code>",
        _kb([[_btn("✅ واریز شد", f"wd:ap:{w.id}", ButtonStyle.SUCCESS),
              _btn("❌ رد و برگشتِ مبلغ", f"wd:rj:{w.id}", ButtonStyle.DANGER)]]))


@wd_router.callback_query(F.data == "wd:ok")
async def wd_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    d = await state.get_data()
    await state.clear()
    amt, dest = int(d.get("wd_amt") or 0), d.get("wd_dest") or ""
    me = await _me(row.id, cb.from_user.id) if row else None
    if not row or not me or not amt or not dest:
        return await cb.answer("درخواست منقضی شده؛ دوباره شروع کن.", show_alert=True)
    ok, err = await _can_start(row.id, me)
    if not ok or amt > await withdrawable(row.id, me.id, int(me.balance or 0)):
        return await cb.answer("مبلغ دیگر قابلِ برداشت نیست.", show_alert=True)
    res, w = await create_request(row.id, me.id, amt, dest)
    if res != "ok":
        return await cb.answer("موجودی کافی نیست.", show_alert=True)
    try:
        await cb.message.edit_text(f"⏳ درخواستِ برداشتِ <b>{fmt(amt)}</b> ثبت شد (#{w.id}). "
                                   "بعد از واریز، خبر می‌دهیم.")
    except Exception:
        logger.info("wd: ویرایشِ پیامِ تأیید ممکن نشد (بی‌اهمیت)")
    await cb.answer()
    await notify_admins_new(bot, row, me, w)


async def _notify_user(bot: Bot, tg: int, text: str) -> None:
    if not tg:
        return
    try:
        await bot.send_message(tg, text)
    except Exception:
        logger.info("wd: پیام به کاربر %s نرسید (بی‌اهمیت)", tg)


@wd_router.callback_query(F.data.regexp(r"^wd:(ap|rj):\d+$"))
async def wd_decide(cb: CallbackQuery, bot: Bot):
    from tenant import is_owner_or_admin
    row = await _row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        return await cb.answer("فقط ادمین", show_alert=True)
    _, act, wid = cb.data.split(":")
    res, w = await decide(row.id, int(wid), act == "ap")
    if res == "already":
        return await cb.answer("این درخواست قبلاً بررسی شده.", show_alert=True)
    async with Session() as s:
        u = await s.get(BotUser, w.user_id)
    label = "✅ واریز شد" if res == "paid" else "❌ رد شد"
    try:
        await cb.message.edit_text((cb.message.html_text or cb.message.text or "") + f"\n\n{label} توسط {cb.from_user.id}",
                                   reply_markup=None)
    except Exception:
        logger.info("wd: ویرایشِ پیامِ ادمین ممکن نشد (بی‌اهمیت)")
    if res == "paid":
        await _notify_user(bot, u.telegram_id if u else 0, f"✅ برداشتِ {fmt(w.amount)} واریز شد. ممنون 🌹")
    else:
        await _notify_user(bot, u.telegram_id if u else 0,
                           f"❌ درخواستِ برداشتِ {fmt(w.amount)} رد شد و مبلغ به کیف پولت برگشت.")
    await cb.answer(label)


# ═══════════════════ پنلِ مالک (مالی و کیف پول) ═══════════════════
async def _admin_row(cb, bot):
    from tenant import is_owner_or_admin
    row = await _row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("فقط مالک/ادمین", show_alert=True)
        return None
    return row


async def _admin_view(bot_id: int):
    on, mn, src = await enabled(bot_id), await min_amount(bot_id), await source(bot_id)
    mode = await _gs(bot_id, "wallet_note_mode", "default")
    if mode not in NOTE_MODES:
        mode = "default"
    async with Session() as s:
        pend = await s.scalar(select(func.count()).select_from(WithdrawRequest).where(
            WithdrawRequest.bot_id == bot_id, WithdrawRequest.status == "pending")) or 0
    txt = ("💸 <b>برداشتِ وجه و متنِ کیف پول</b>\n\n"
           "کاربر وقتی به حداقلِ مبلغ رسید، از «کیف پول ← 💸 درخواست برداشت وجه» درخواست می‌دهد؛ مبلغ از موجودی‌اش نگه داشته "
           "می‌شود و بعد از واریزِ دستیِ شما و زدنِ «✅ واریز شد» نهایی می‌شود (با «رد» برمی‌گردد).\n\n"
           f"وضعیت: <b>{'🟢 روشن' if on else '🔴 خاموش'}</b>\n"
           f"حداقلِ برداشت: <b>{fmt(mn)}</b>\n"
           f"منبعِ قابلِ‌برداشت: <b>{'کلِ موجودی' if src == 'all' else 'فقط پاداشِ زیرمجموعه‌گیری'}</b>\n"
           f"⏳ درخواست‌هایِ منتظر: <b>{pend}</b>\n\n"
           f"📝 متنِ هشدارِ زیرِ کیف پول: <b>{NOTE_MODES[mode]}</b>")
    rows = [
        [_btn(("🟢 برداشت: روشن" if on else "🔴 برداشت: خاموش"), "wd:tog",
              ButtonStyle.SUCCESS if on else ButtonStyle.DANGER)],
        [_btn(f"🔻 حداقل برداشت: {mn:,}", "wd:setmin")],
        [_btn("منبع: " + ("کلِ موجودی ↔ فقط پاداش" if src == "all" else "فقط پاداش ↔ کلِ موجودی"), "wd:src")],
    ]
    if pend:
        rows.append([_btn(f"📋 درخواست‌هایِ منتظر ({pend})", "wd:pend", ButtonStyle.PRIMARY)])
    rows.append([_btn("🏷 تغییرِ نامِ دکمهٔ برداشت", "wd:label", ButtonStyle.PRIMARY)])
    rows.append([_btn("✏️ متنِ دلخواه زیرِ کیف پول", "wd:note:edit", ButtonStyle.PRIMARY)])
    rows.append([_btn(("👁 نمایشِ متنِ پیش‌فرض" if mode != "default" else "🙈 حذفِ متنِ هشدار"),
                      "wd:note:default" if mode != "default" else "wd:note:hide")])
    if mode == "custom":
        rows.append([_btn("🙈 حذفِ کاملِ متن", "wd:note:hide")])
    rows.append([_btn("⬅️ بازگشت", "tadm:sec:finance", ButtonStyle.DANGER)])
    return txt, _kb(rows)


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


async def _refresh(cb, row_id: int):
    txt, kb = await _admin_view(row_id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb)
    except Exception:
        logger.info("wd: ویرایشِ پنل ممکن نشد (بی‌اهمیت)")


@wd_router.callback_query(F.data == "wd:admin")
async def _admin_entry(cb: CallbackQuery, bot: Bot):
    await admin_menu(cb, bot)


@wd_router.callback_query(F.data == "wd:tog")
async def wd_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    row = await _admin_row(cb, bot)
    if not row:
        return
    new = "0" if await enabled(row.id) else "1"
    await set_setting(row.id, "wd_enabled", new)
    await _refresh(cb, row.id)
    await cb.answer("🟢 روشن شد" if new == "1" else "🔴 خاموش شد")


@wd_router.callback_query(F.data == "wd:src")
async def wd_source_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    row = await _admin_row(cb, bot)
    if not row:
        return
    new = "referral" if await source(row.id) == "all" else "all"
    await set_setting(row.id, "wd_source", new)
    await _refresh(cb, row.id)
    await cb.answer("کلِ موجودی" if new == "all" else "فقط پاداشِ زیرمجموعه‌گیری")


@wd_router.callback_query(F.data == "wd:setmin")
async def wd_setmin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _admin_row(cb, bot)
    if not row:
        return
    await state.set_state(WdFSM.a_min)
    await cb.message.answer("🔻 حداقلِ مبلغِ برداشت را به تومان بفرست (مثلاً 100000):", reply_markup=_cancel_kb())
    await cb.answer()


@wd_router.message(WdFSM.a_min, F.text)
async def wd_min_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting, is_owner_or_admin
    row = await _row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    amt = parse_amount(msg.text)
    if amt is None:
        return await msg.answer("❌ فقط عدد بفرست.", reply_markup=_cancel_kb())
    await set_setting(row.id, "wd_min", str(amt))
    await state.clear()
    txt, kb = await _admin_view(row.id)
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(txt, reply_markup=kb)


@wd_router.callback_query(F.data == "wd:label")
async def wd_label_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _admin_row(cb, bot)
    if not row:
        return
    await state.set_state(WdFSM.a_label)
    await cb.message.answer(
        f"🏷 نامِ فعلیِ دکمه: <b>{_html.escape(await label(row.id))}</b>\n\n"
        "نامِ جدید را بفرست (حداکثر ۶۰ حرف؛ ایموجی هم مجاز است). برایِ برگشت به نامِ پیش‌فرض «-» بفرست.",
        reply_markup=_cancel_kb())
    await cb.answer()


@wd_router.message(WdFSM.a_label, F.text)
async def wd_label_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting, is_owner_or_admin
    row = await _row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    t = (msg.text or "").strip()
    await set_setting(row.id, "wd_label", "" if t == "-" else t[:60])
    await state.clear()
    txt, kb = await _admin_view(row.id)
    await msg.answer("✅ نامِ دکمه ذخیره شد.")
    await msg.answer(txt, reply_markup=kb)


@wd_router.callback_query(F.data == "wd:note:edit")
async def wd_note_edit(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _admin_row(cb, bot)
    if not row:
        return
    await state.set_state(WdFSM.a_note)
    await cb.message.answer(
        "✏️ متنِ دلخواهی که زیرِ موجودیِ کیف پول (و پروفایل) نشان داده شود را بفرست.\n"
        "می‌توانی از HTML ساده (<b>، <i>) استفاده کنی.", reply_markup=_cancel_kb())
    await cb.answer()


@wd_router.message(WdFSM.a_note, F.text)
async def wd_note_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting, is_owner_or_admin
    row = await _row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    t = (msg.html_text or msg.text or "").strip()[:600]
    if not t:
        return await msg.answer("❌ متن خالی است.", reply_markup=_cancel_kb())
    await set_setting(row.id, "wallet_note_text", t)
    await set_setting(row.id, "wallet_note_mode", "custom")
    await state.clear()
    txt, kb = await _admin_view(row.id)
    await msg.answer("✅ متن ذخیره شد.")
    await msg.answer(txt, reply_markup=kb)


@wd_router.callback_query(F.data.regexp(r"^wd:note:(default|hide)$"))
async def wd_note_mode(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    row = await _admin_row(cb, bot)
    if not row:
        return
    mode = "default" if cb.data.endswith("default") else "hidden"
    await set_setting(row.id, "wallet_note_mode", mode)
    await _refresh(cb, row.id)
    await cb.answer("متنِ پیش‌فرض" if mode == "default" else "متن حذف شد")


@wd_router.callback_query(F.data == "wd:pend")
async def wd_pending(cb: CallbackQuery, bot: Bot):
    row = await _admin_row(cb, bot)
    if not row:
        return
    async with Session() as s:
        ws = (await s.scalars(select(WithdrawRequest).where(
            WithdrawRequest.bot_id == row.id, WithdrawRequest.status == "pending")
            .order_by(WithdrawRequest.id).limit(15))).all()
        users = {u.id: u for u in (await s.scalars(select(BotUser).where(
            BotUser.id.in_([w.user_id for w in ws] or [0])))).all()}
    if not ws:
        return await cb.answer("درخواستی نیست.", show_alert=True)
    for w in ws:
        u = users.get(w.user_id)
        fn = _html.escape((u.full_name or "") if u else "") + (f" (@{u.username})" if u and u.username else "")
        await cb.message.answer(
            f"💸 <b>درخواستِ برداشت #{w.id}</b>\n\n👤 {fn} (<code>{u.telegram_id if u else '-'}</code>)\n"
            f"💵 <b>{fmt(w.amount)}</b>\n🏦 <code>{_html.escape(w.dest or '')}</code>",
            reply_markup=_kb([[_btn("✅ واریز شد", f"wd:ap:{w.id}", ButtonStyle.SUCCESS),
                               _btn("❌ رد و برگشتِ مبلغ", f"wd:rj:{w.id}", ButtonStyle.DANGER)]]))
    await cb.answer()


# ═══════════════════ API (وب‌سرویس) ═══════════════════
async def api_info(bot_id: int, user) -> dict:
    avail = await withdrawable(bot_id, user.id, int(user.balance or 0))
    return {"enabled": await enabled(bot_id), "min_amount": await min_amount(bot_id), "source": await source(bot_id),
            "balance": int(user.balance or 0), "withdrawable": avail, "label": await label(bot_id)}


async def api_request(bot_id: int, user, amount: int | None, dest: str):
    """خروجی: ("ok", WithdrawRequest) یا (کدِ خطا, None)."""
    if not await enabled(bot_id):
        return "withdraw_disabled", None
    if not amount or amount <= 0:
        return "invalid_amount", None
    if amount < await min_amount(bot_id):
        return "below_minimum", None
    if len((dest or "").strip()) < 6:
        return "invalid_destination", None
    if amount > await withdrawable(bot_id, user.id, int(user.balance or 0)):
        return "insufficient_withdrawable", None
    res, w = await create_request(bot_id, user.id, amount, dest.strip())
    return ("ok", w) if res == "ok" else ("insufficient_balance", None)


async def api_list(bot_id: int, user_id: int) -> list[dict]:
    async with Session() as s:
        ws = (await s.scalars(select(WithdrawRequest).where(
            WithdrawRequest.bot_id == bot_id, WithdrawRequest.user_id == user_id)
            .order_by(WithdrawRequest.id.desc()).limit(50))).all()
    return [{"id": w.id, "amount": int(w.amount), "status": w.status, "destination": w.dest,
             "created_at": w.created_at.isoformat() if w.created_at else None,
             "decided_at": w.decided_at.isoformat() if w.decided_at else None} for w in ws]


# ═══════════════════ ثبت در «مالی و کیف پول» برایِ بقیهٔ انواعِ ربات ═══════════════════
# vpn_sales / virtual_service / panel_seller در settings.py خودشان ثبت می‌کنند؛ بقیه (فروشگاه، رزرو، تبلیغات، …) این‌جا،
# تا مالکِ هر رباتی که کیف پول دارد بتواند متنِ «قابلِ برداشت نیست» را عوض/حذف کند و برداشت را روشن کند (پیشنهادِ #201).
async def _admin_entry_handler(cb, bot):
    await admin_menu(cb, bot)


def _register_other_kinds() -> None:
    try:
        from tenant.settings_registry import SettingsItem, register_settings_items, section_key
        for _kind in ("digital_shop", "booking", "ad_bot", "escrow_bot", "giveaway", "funstat", "community_mgmt"):
            register_settings_items(section_key("finance", _kind), [
                SettingsItem(label="💸 برداشتِ وجه و متنِ کیف پول", callback="wd:admin", handler=_admin_entry_handler,
                             after=("btr:admin", "set:swap", "set:crypto", "set:kyc", "set:txlog", "tadm:receiptchannel")),
            ])
    except Exception:      # noqa: BLE001
        logger.exception("withdraw: ثبتِ آیتمِ مالی برایِ انواعِ دیگرِ ربات ناموفق بود")


_register_other_kinds()
