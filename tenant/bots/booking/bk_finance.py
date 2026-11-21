"""
bk_finance.py — بخشِ «مالی» و «مشتریان و پرونده‌ها» در پنلِ رزرو (PATCH_BOOKING_V2). callbackها: bka:fin / bka:pay / bka:cu …

مالی: ثبتِ پرداختِ هر نوبت (نقد/کارت/آنلاین)، دریافتی‌ها (امروز/۷/۳۰ روز)، مطالباتِ انجام‌شده‌هایِ بی‌پرداخت، درآمدِ پیش‌رو، تفکیکِ
خدمت/پرسنل/روش. پرداختِ آنلاینِ خودکار در این نسخه نیست؛ مالک پولِ دریافتی را ثبت می‌کند.
مشتریان: پرونده (سابقه، مجموعِ پرداخت)، یادداشتِ داخلی، مسدودکردن، جستجو.
"""
from __future__ import annotations

import datetime as _dt

from aiogram import Bot, F
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, or_, select

from config import Session
from models import BkAppt, BkCustomer, BkPayment

from tenant.bots.booking import engine as E
from tenant.bots.booking.bk_admin import (
    BACK_HOME, BkAdm, STATUS_FA, _admin_card, _appt_actions, _btn, _esc, _guard, _guard_msg, _help_btn, _num, _show,
)
from tenant.bots.booking.handlers import booking_router

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

METHOD_FA = {"cash": "💵 نقد", "card": "💳 کارت", "online": "🌐 آنلاین", "wallet": "👛 کیف پول (بیعانه)", "other": "➖ سایر"}
BACK_FIN = [_btn("⬅️ بازگشت", "bka:fin", ButtonStyle.DANGER)]
PAGE = 8


def _t(n: int) -> str:
    return f"{int(n):,} تومان"


# ───────────────────────── ثبتِ پرداخت ─────────────────────────

@booking_router.callback_query(F.data.startswith("bka:pay:"))
async def bka_pay_start(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    aid = int(cb.data.split(":")[2])
    async with Session() as s:
        a = await s.get(BkAppt, aid)
    if not a or a.bot_id != row.id:
        return await cb.answer("پیدا نشد", show_alert=True)
    paid = await E.paid_of(row.id, aid)
    left = max(int(a.price or 0) - paid, 0)
    rows = [[_btn(f"{lbl}", f"bka:pm:{aid}:{k}", ButtonStyle.PRIMARY)] for k, lbl in METHOD_FA.items() if k != "wallet"]
    rows.append([_btn("⬅️ بازگشت", f"bka:a:{aid}:up:0", ButtonStyle.DANGER)])
    await _show(cb, f"💵 <b>ثبتِ پرداختِ نوبت #{E.appt_code(a)}</b>\nقیمت: {_t(a.price)} · پرداخت‌شده: {_t(paid)} · مانده: <b>{_t(left)}</b>\n\nروشِ پرداخت؟", rows)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:pm:"))
async def bka_pay_method(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, aid, method = cb.data.split(":")
    aid = int(aid)
    async with Session() as s:
        a = await s.get(BkAppt, aid)
    if not a or a.bot_id != row.id:
        return await cb.answer("پیدا نشد", show_alert=True)
    left = max(int(a.price or 0) - await E.paid_of(row.id, aid), 0)
    rows = []
    if left:
        rows.append([_btn(f"✅ کلِ مانده ({_t(left)})", f"bka:pa:{aid}:{method}:{left}", ButtonStyle.SUCCESS)])
    rows.append([_btn("✏️ مبلغِ دیگر", f"bka:pc:{aid}:{method}", ButtonStyle.PRIMARY)])
    rows.append([_btn("⬅️ بازگشت", f"bka:pay:{aid}", ButtonStyle.DANGER)])
    await _show(cb, f"💵 {METHOD_FA.get(method, method)} — مبلغ چقدر است؟", rows)
    await cb.answer()


async def _after_pay(cb_or_msg, row, aid: int, text: str):
    async with Session() as s:
        a = await s.get(BkAppt, aid)
    rows = await _appt_actions(a)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    body = f"{text}\n\n{_admin_card(a, await E.paid_of(row.id, aid))}"
    if hasattr(cb_or_msg, "message"):            # CallbackQuery ⇒ همان پیام ویرایش می‌شود
        await _show(cb_or_msg, body, rows)
    else:                                          # Message (بعد از ورودیِ متنی)
        await cb_or_msg.answer(body, reply_markup=kb, parse_mode="HTML")


@booking_router.callback_query(F.data.startswith("bka:pa:"))
async def bka_pay_all(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, aid, method, amount = cb.data.split(":")
    p = await E.add_payment(row.id, int(aid), int(amount), method)
    if not p:
        return await cb.answer("ثبت نشد", show_alert=True)
    await cb.answer("پرداخت ثبت شد ✅")
    await _after_pay(cb, row, int(aid), f"✅ {_t(p.amount)} ({METHOD_FA.get(p.method, p.method)}) ثبت شد.")


@booking_router.callback_query(F.data.startswith("bka:pc:"))
async def bka_pay_custom(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    _, _, aid, method = cb.data.split(":")
    await state.set_state(BkAdm.pay_amount)
    await state.update_data(pay_aid=int(aid), pay_method=method)
    await cb.message.answer("✏️ مبلغ را به <b>تومان</b> بفرست (فقط عدد):", parse_mode="HTML")
    await cb.answer()


@booking_router.message(BkAdm.pay_amount, F.text)
async def bka_pay_custom_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    n = _num(msg.text)
    if not n or n > 1_000_000_000:
        return await msg.answer("فقط یک عددِ مثبت (تومان) بفرست.")
    data = await state.get_data()
    await state.clear()
    p = await E.add_payment(row.id, int(data["pay_aid"]), n, data["pay_method"])
    if not p:
        return await msg.answer("ثبت نشد.")
    await _after_pay(msg, row, int(data["pay_aid"]), f"✅ {_t(p.amount)} ({METHOD_FA.get(p.method, p.method)}) ثبت شد.")


# ───────────────────────── مالی ─────────────────────────

@booking_router.callback_query(F.data == "bka:fin")
async def bka_finance(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    f = await E.finance_summary(row.id)
    text = ("💰 <b>مالی</b>\n\n"
            f"📥 <b>دریافتی</b>\nامروز: <b>{_t(f['today'])}</b>\n۷ روزِ اخیر: <b>{_t(f['d7'])}</b>\n۳۰ روزِ اخیر: <b>{_t(f['d30'])}</b>\n\n"
            f"🧾 <b>مطالبات</b> (انجام‌شده و پرداخت‌نشده): <b>{_t(f['owed_total'])}</b> در {len(f['owed'])} نوبت\n"
            f"📈 <b>درآمدِ پیش‌رو</b> (نوبت‌هایِ تأییدشدهٔ آینده): {_t(f['upcoming'])}")
    if f["by_method"]:
        text += "\n\n<b>روشِ دریافت (۳۰ روز):</b>\n" + "\n".join(f"• {METHOD_FA.get(k, k)}: {_t(v)}" for k, v in sorted(f["by_method"].items(), key=lambda x: -x[1]))
    rows = [[_btn(f"🧾 مطالبات ({len(f['owed'])})", "bka:fo:0", ButtonStyle.PRIMARY), _btn("📈 روزانهٔ ۷ روز", "bka:fd", ButtonStyle.PRIMARY)],
            [_btn("🏆 به‌تفکیکِ خدمت و پرسنل", "bka:fb", ButtonStyle.PRIMARY)],
            _help_btn("fin"), BACK_HOME]
    await _show(cb, text, rows)
    await cb.answer()


@booking_router.callback_query(F.data == "bka:fd")
async def bka_fin_daily(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    f = await E.finance_summary(row.id)
    today = E.iran_now().date()
    lines = ["📈 <b>دریافتیِ ۷ روزِ اخیر</b>\n"]
    mx = max([f["by_day"].get((today - _dt.timedelta(days=i)).isoformat(), 0) for i in range(7)] + [1])
    for i in range(6, -1, -1):
        d = today - _dt.timedelta(days=i)
        v = f["by_day"].get(d.isoformat(), 0)
        bar = "█" * max(0, round(10 * v / mx)) if v else "·"
        lines.append(f"{E.fa_date(d)} {bar} {_t(v)}")
    await _show(cb, "\n".join(lines), [BACK_FIN])
    await cb.answer()


@booking_router.callback_query(F.data == "bka:fb")
async def bka_fin_breakdown(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    f = await E.finance_summary(row.id)
    lines = ["🏆 <b>درآمدِ برآوردیِ ۳۰ روز</b> (نوبت‌هایِ «انجام‌شده»)\n"]
    lines.append("<b>به‌تفکیکِ خدمت:</b>")
    lines += [f"• {_esc(n)} — {c} نوبت — {_t(v)}" for n, c, v in f["by_service"]] or ["—"]
    if f["by_staff"]:
        lines += ["", "<b>به‌تفکیکِ پرسنل:</b>"] + [f"• {_esc(n)} — {c} نوبت — {_t(v)}" for n, c, v in f["by_staff"]]
    await _show(cb, "\n".join(lines), [BACK_FIN])
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:fo:"))
async def bka_fin_owed(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    page = int(cb.data.split(":")[2])
    f = await E.finance_summary(row.id)
    owed = sorted(f["owed"], key=lambda x: (x[0].date_str, x[0].start_min), reverse=True)
    chunk = owed[page * PAGE:(page + 1) * PAGE]
    if not chunk:
        await _show(cb, "🧾 <b>مطالبات</b>\n\n🎉 همهٔ نوبت‌هایِ انجام‌شده تسویه شده‌اند.", [BACK_FIN])
        return await cb.answer()
    rows = [[_btn(f"{E.fa_date(_dt.date.fromisoformat(a.date_str))} · {a.customer_name or '—'} · {_t(o)}", f"bka:a:{a.id}:up:0", ButtonStyle.PRIMARY)] for a, o in chunk]
    nav = []
    if page > 0:
        nav.append(_btn("⬅️ قبلی", f"bka:fo:{page - 1}"))
    if (page + 1) * PAGE < len(owed):
        nav.append(_btn("بعدی ➡️", f"bka:fo:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(BACK_FIN)
    await _show(cb, f"🧾 <b>مطالبات</b> — جمع: {_t(f['owed_total'])}\nروی هر مورد بزن و پرداخت را ثبت کن:", rows)
    await cb.answer()


# ───────────────────────── مشتریان و پرونده‌ها ─────────────────────────

BACK_CU = [_btn("⬅️ بازگشت", "bka:cu:0", ButtonStyle.DANGER)]


@booking_router.callback_query(F.data.startswith("bka:cu:"))
async def bka_customers(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    page = int(cb.data.split(":")[2])
    async with Session() as s:
        total = int(await s.scalar(select(func.count()).select_from(BkCustomer).where(BkCustomer.bot_id == row.id)) or 0)
        cus = (await s.scalars(select(BkCustomer).where(BkCustomer.bot_id == row.id).order_by(BkCustomer.id.desc())
                               .offset(page * PAGE).limit(PAGE))).all()
    rows = [[_btn(f"{'🚫 ' if c.blocked else ''}{c.name or '—'} · {c.phone or '—'}", f"bka:cuv:{c.tg_id}", ButtonStyle.PRIMARY)] for c in cus]
    nav = []
    if page > 0:
        nav.append(_btn("⬅️ قبلی", f"bka:cu:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(_btn("بعدی ➡️", f"bka:cu:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("🔎 جستجو (شماره/نام)", "bka:cus", ButtonStyle.SUCCESS)])
    rows.append(_help_btn("cu"))
    rows.append(BACK_HOME)
    await _show(cb, f"👤 <b>مشتریان و پرونده‌ها</b> — {total} مشتری\nروی هر مشتری بزن تا پرونده‌اش باز شود:" if cus
                else "👤 <b>مشتریان و پرونده‌ها</b>\n\nهنوز مشتری‌ای نوبت نگرفته است.", rows)
    await cb.answer()


@booking_router.callback_query(F.data == "bka:cus")
async def bka_customer_search(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await state.set_state(BkAdm.cu_search)
    await cb.message.answer("🔎 بخشی از <b>شماره</b> یا <b>نام</b> را بفرست:", parse_mode="HTML")
    await cb.answer()


@booking_router.message(BkAdm.cu_search, F.text)
async def bka_customer_search_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    await state.clear()
    q = E.to_en_digits((msg.text or "").strip())
    if len(q) < 2:
        return await msg.answer("حداقل ۲ نویسه بفرست.")
    async with Session() as s:
        cus = (await s.scalars(select(BkCustomer).where(
            BkCustomer.bot_id == row.id, or_(BkCustomer.phone.contains(q), BkCustomer.name.contains(q))).limit(15))).all()
    if not cus:
        return await msg.answer("چیزی پیدا نشد.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[BACK_CU]))
    rows = [[_btn(f"{'🚫 ' if c.blocked else ''}{c.name or '—'} · {c.phone or '—'}", f"bka:cuv:{c.tg_id}", ButtonStyle.PRIMARY)] for c in cus]
    rows.append(BACK_CU)
    await msg.answer(f"🔎 {len(cus)} نتیجه:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _customer_file(bot_id: int, tg: int):
    async with Session() as s:
        c = await s.scalar(select(BkCustomer).where(BkCustomer.bot_id == bot_id, BkCustomer.tg_id == tg))
        if not c:
            return None
        counts = dict((await s.execute(select(BkAppt.status, func.count()).where(BkAppt.bot_id == bot_id, BkAppt.tg_id == tg)
                                       .group_by(BkAppt.status))).all())
        paid = int(await s.scalar(select(func.coalesce(func.sum(BkPayment.amount), 0)).where(
            BkPayment.bot_id == bot_id, BkPayment.appt_id.in_(select(BkAppt.id).where(BkAppt.bot_id == bot_id, BkAppt.tg_id == tg)))) or 0)
        recent = (await s.scalars(select(BkAppt).where(BkAppt.bot_id == bot_id, BkAppt.tg_id == tg).order_by(BkAppt.id.desc()).limit(5))).all()
    return c, counts, paid, recent


@booking_router.callback_query(F.data.startswith("bka:cuv:"))
async def bka_customer_view(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    tg = int(cb.data.split(":")[2])
    got = await _customer_file(row.id, tg)
    if not got:
        return await cb.answer("پرونده پیدا نشد", show_alert=True)
    c, counts, paid, recent = got
    total = sum(counts.values())
    link = E.tg_link_html(tg)
    text = (f"👤 <b>{_esc(c.name or '—')}</b> {'🚫 <b>مسدود</b>' if c.blocked else ''}\n📱 <code>{_esc(c.phone)}</code>\n🔗 {link}\n\n"
            f"📊 کلِ نوبت‌ها: <b>{total}</b> · ✅ انجام‌شده: {counts.get('done', 0)} · 🟢 تأییدشده: {counts.get('confirmed', 0)} · 🟡 انتظار: {counts.get('pending', 0)}\n"
            f"⚪️ لغو: {counts.get('cancelled', 0)} · 🔴 رد: {counts.get('rejected', 0)} · ⚠️ عدم حضور: {counts.get('noshow', 0)}\n"
            f"💰 مجموعِ پرداخت: <b>{_t(paid)}</b>")
    if c.note:
        text += f"\n\n📝 <b>یادداشت:</b>\n{_esc(c.note)}"
    if recent:
        text += "\n\n<b>آخرین نوبت‌ها:</b>\n" + "\n".join(
            f"• #{E.appt_code(a)} · {E.fa_date(_dt.date.fromisoformat(a.date_str))} {E.fmt_hm(a.start_min)} — {_esc(a.service_name)} — {STATUS_FA.get(a.status, a.status)}" for a in recent)
    rows = [[_btn("📝 یادداشت", f"bka:cun:{tg}", ButtonStyle.PRIMARY),
             _btn("✅ رفعِ مسدودی" if c.blocked else "🚫 مسدودکردن", f"bka:cub:{tg}", ButtonStyle.SUCCESS if c.blocked else ButtonStyle.DANGER)],
            BACK_CU]
    await _show(cb, text, rows)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:cun:"))
async def bka_customer_note(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await state.set_state(BkAdm.cu_note)
    await state.update_data(cu_tg=int(cb.data.split(":")[2]))
    await cb.message.answer("📝 یادداشتِ داخلی را بفرست (مشتری نمی‌بیند). برایِ پاک‌کردن <code>-</code> بفرست:", parse_mode="HTML")
    await cb.answer()


@booking_router.message(BkAdm.cu_note, F.text)
async def bka_customer_note_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    await state.clear()
    txt = (msg.text or "").strip()
    async with Session() as s:
        c = await s.scalar(select(BkCustomer).where(BkCustomer.bot_id == row.id, BkCustomer.tg_id == int(data["cu_tg"])))
        if c:
            c.note = None if txt in ("-", "—") else txt[:1000]
            await s.commit()
    await msg.answer("✅ ذخیره شد.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("👤 پرونده", f"bka:cuv:{data['cu_tg']}", ButtonStyle.PRIMARY)]]))


@booking_router.callback_query(F.data.startswith("bka:cub:"))
async def bka_customer_block(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    tg = int(cb.data.split(":")[2])
    async with Session() as s:
        c = await s.scalar(select(BkCustomer).where(BkCustomer.bot_id == row.id, BkCustomer.tg_id == tg))
        if c:
            c.blocked = not c.blocked
            await s.commit()
            await cb.answer("مسدود شد 🚫" if c.blocked else "رفعِ مسدودی شد ✅")
    await bka_customer_view(cb, bot, state)
