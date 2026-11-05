"""
bk_manual.py — ثبتِ نوبتِ دستی توسطِ مالک/ادمین برایِ مشتریِ حضوری یا تلفنی (PATCH_BOOKING_V2.2). callbackها: bka:m*

خدمت ← (پرسنل) ← روز ← ساعت ← مشتری (از لیست یا جدید) ← توضیح ← تأیید.
قواعد: نوبت مستقیم «تأییدشده» است؛ «حداقل فاصله تا نوبت» و «مسدودبودنِ مشتری» نادیده گرفته می‌شود ولی تداخل با نوبت‌ها/تعطیلی/ساعتِ کاری
همچنان کنترل می‌شود (E.book با by_owner=True). مشتریِ جدید شناسهٔ منفی می‌گیرد و پیام/یادآوری برایش فرستاده نمی‌شود.
"""
from __future__ import annotations

import datetime as _dt

from aiogram import Bot, F
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session
from models import BkCustomer, BkService

from tenant.bots.booking import engine as E
from tenant.bots.booking.bk_admin import BkAdm, _admin_card, _appt_actions, _btn, _esc, _guard, _guard_msg, _show
from tenant.bots.booking.bk_user import _dur_txt, _pd, _price_txt, norm_phone
from tenant.bots.booking.handlers import booking_router

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

BACK_AP = [_btn("⬅️ بازگشت", "bka:ap", ButtonStyle.DANGER)]


async def _cfg_owner(bot_id: int) -> dict:
    """تنظیماتِ رزرو با «حداقل فاصلهٔ رزرو» صفر (مالک می‌تواند همین حالا نوبت بدهد)."""
    return {**await E.get_cfg(bot_id), "lead": 0}


async def _svc(bot_id: int, sid: int) -> BkService | None:
    async with Session() as s:
        sv = await s.get(BkService, sid)
    return sv if sv and sv.bot_id == bot_id and sv.active else None


# ───────────────────────── ۱) خدمت / پرسنل ─────────────────────────

@booking_router.callback_query(F.data == "bka:m")
async def bka_m_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    svcs = await E.list_services(row.id)
    if not svcs:
        await _show(cb, "➕ <b>ثبتِ نوبتِ دستی</b>\n\nاول در «💈 خدمات و پرسنل» یک خدمت اضافه کن.", [BACK_AP])
        return await cb.answer()
    rows = [[_btn(f"{sv.name} · {_dur_txt(sv.duration_min)} · {_price_txt(sv.price)}", f"bka:m:s:{sv.id}", ButtonStyle.PRIMARY)] for sv in svcs]
    rows.append(BACK_AP)
    await _show(cb, "➕ <b>ثبتِ نوبتِ دستی</b>\nبرایِ کدام خدمت؟", rows)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:m:s:"))
async def bka_m_service(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    sv = await _svc(row.id, int(cb.data.split(":")[3]))
    if not sv:
        return await cb.answer("این خدمت دیگر موجود نیست.", show_alert=True)
    cfg = await E.get_cfg(row.id)
    if cfg["use_staff"]:
        staff = [st for st in await E.list_staff(row.id) if E.staff_does(st, sv.id)]
        if staff:
            rows = [[_btn("🎲 هر کدام (اولین نوبتِ خالی)", f"bka:m:t:{sv.id}:0", ButtonStyle.SUCCESS)]]
            rows += [[_btn(f"👤 {st.name}", f"bka:m:t:{sv.id}:{st.id}", ButtonStyle.PRIMARY)] for st in staff]
            rows.append([_btn("⬅️ بازگشت", "bka:m", ButtonStyle.DANGER)])
            await _show(cb, f"💈 <b>{_esc(sv.name)}</b>\nپرسنل؟", rows)
            return await cb.answer()
    await _days(cb, row, sv, 0)


@booking_router.callback_query(F.data.startswith("bka:m:t:"))
async def bka_m_staff(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, _, sid, stf = cb.data.split(":")
    sv = await _svc(row.id, int(sid))
    if not sv:
        return await cb.answer("این خدمت دیگر موجود نیست.", show_alert=True)
    await _days(cb, row, sv, int(stf))


# ───────────────────────── ۲) روز و ساعت ─────────────────────────

async def _days(cb, row, sv: BkService, stf: int):
    cfg = await _cfg_owner(row.id)
    days = await E.open_days(row.id, sv, stf or None, cfg)
    back = f"bka:m:s:{sv.id}" if (cfg["use_staff"] and stf) else "bka:m"
    if not days:
        await _show(cb, f"💈 <b>{_esc(sv.name)}</b>\n\n😔 در {cfg['days_ahead']} روزِ آینده زمانِ خالی نیست (ساعتِ کاری/تعطیلی را بررسی کن).",
                    [[_btn("⬅️ بازگشت", back, ButtonStyle.DANGER)]])
        return await cb.answer()
    rows, line = [], []
    for d in days:
        line.append(_btn(E.fa_date_long(d), f"bka:m:d:{sv.id}:{stf}:{d.strftime('%Y%m%d')}", ButtonStyle.PRIMARY))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([_btn("⬅️ بازگشت", back, ButtonStyle.DANGER)])
    await _show(cb, f"💈 <b>{_esc(sv.name)}</b> — {_dur_txt(sv.duration_min)}\n\n📅 روز؟", rows)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:m:d:"))
async def bka_m_day(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, _, sid, stf, ds = cb.data.split(":")
    sv = await _svc(row.id, int(sid))
    if not sv:
        return await cb.answer("این خدمت دیگر موجود نیست.", show_alert=True)
    cfg = await _cfg_owner(row.id)
    d = _pd(ds)
    slots = await E.day_slots(row.id, sv, d, int(stf) or None, cfg)
    back = f"bka:m:t:{sid}:{stf}" if cfg["use_staff"] else f"bka:m:s:{sid}"
    if not slots:
        await _show(cb, "😔 برایِ این روز زمانِ خالی نمانده؛ روزِ دیگری را انتخاب کن.", [[_btn("⬅️ بازگشت", back, ButtonStyle.DANGER)]])
        return await cb.answer()
    rows, line = [], []
    for st, _rid in slots:
        line.append(_btn(E.fmt_hm(st), f"bka:m:h:{sid}:{stf}:{ds}:{st}", ButtonStyle.SUCCESS))
        if len(line) == 3:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([_btn("⬅️ بازگشت", back, ButtonStyle.DANGER)])
    await _show(cb, f"💈 <b>{_esc(sv.name)}</b>\n📅 {E.fa_date_long(d)}\n\n⏰ ساعت؟", rows)
    await cb.answer()


# ───────────────────────── ۳) مشتری ─────────────────────────

@booking_router.callback_query(F.data.startswith("bka:m:h:"))
async def bka_m_time(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, _, sid, stf, ds, st = cb.data.split(":")
    sv = await _svc(row.id, int(sid))
    if not sv:
        return await cb.answer("این خدمت دیگر موجود نیست.", show_alert=True)
    cfg = await _cfg_owner(row.id)
    if int(st) not in dict(await E.day_slots(row.id, sv, _pd(ds), int(stf) or None, cfg)):
        return await cb.answer("این ساعت همین الان پر شد؛ ساعتِ دیگری را انتخاب کن.", show_alert=True)
    await state.clear()
    await state.update_data(m_sid=sv.id, m_stf=int(stf), m_ds=ds, m_st=int(st))
    async with Session() as s:
        recent = (await s.scalars(select(BkCustomer).where(BkCustomer.bot_id == row.id).order_by(BkCustomer.id.desc()).limit(6))).all()
    rows = [[_btn(f"{'🚫 ' if c.blocked else ''}{c.name or '—'} · {c.phone or '—'}", f"bka:m:c:{c.tg_id}", ButtonStyle.PRIMARY)] for c in recent]
    rows.append([_btn("➕ مشتریِ جدید (نام + شماره)", "bka:m:new", ButtonStyle.SUCCESS)])
    rows.append([_btn("⬅️ بازگشت", f"bka:m:d:{sid}:{stf}:{ds}", ButtonStyle.DANGER)])
    await _show(cb, f"👤 <b>مشتری</b>\n{_esc(sv.name)} — {E.fa_date_long(_pd(ds))} ساعت {E.fmt_hm(int(st))}\n\nیکی از مشتریانِ اخیر را انتخاب کن یا مشتریِ جدید بساز:", rows)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:m:c:"))
async def bka_m_pick_customer(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    if not (await state.get_data()).get("m_sid"):
        return await cb.answer("نشست منقضی شد؛ از اول شروع کن.", show_alert=True)
    tg = int(cb.data.split(":")[3])
    async with Session() as s:
        c = await s.scalar(select(BkCustomer).where(BkCustomer.bot_id == row.id, BkCustomer.tg_id == tg))
    if not c:
        return await cb.answer("پرونده پیدا نشد", show_alert=True)
    await state.update_data(m_tg=tg, m_name=c.name or "", m_phone=c.phone or "")
    await _ask_note(cb.message, state)
    await cb.answer()


@booking_router.callback_query(F.data == "bka:m:new")
async def bka_m_new(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    if not (await state.get_data()).get("m_sid"):
        return await cb.answer("نشست منقضی شد؛ از اول شروع کن.", show_alert=True)
    await state.update_data(m_tg=None)
    await state.set_state(BkAdm.m_name)
    await cb.message.answer("✏️ <b>نام و نام‌خانوادگیِ</b> مشتری را بفرست:", parse_mode="HTML")
    await cb.answer()


@booking_router.message(BkAdm.m_name, F.text)
async def bka_m_name(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    name = (msg.text or "").strip()
    if not (2 <= len(name) <= 60):
        return await msg.answer("نام ۲ تا ۶۰ نویسه باشد.")
    await state.update_data(m_name=name)
    await state.set_state(BkAdm.m_phone)
    await msg.answer("📱 <b>شمارهٔ موبایل</b> را بفرست (مثلاً <code>09123456789</code>) یا <code>-</code> اگر نداری:", parse_mode="HTML")


@booking_router.message(BkAdm.m_phone, F.text)
async def bka_m_phone(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    raw = (msg.text or "").strip()
    if raw in ("-", "—"):
        phone = ""
    else:
        phone = norm_phone(raw) or ""
        if not phone:
            return await msg.answer("❌ شماره معتبر نیست. مثل <code>09123456789</code> بفرست یا <code>-</code>.", parse_mode="HTML")
    await state.update_data(m_phone=phone)
    await _ask_note(msg, state)


async def _ask_note(msg, state: FSMContext):
    await state.set_state(BkAdm.m_note)
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("⏭ توضیحی ندارم", "bka:m:nonote", ButtonStyle.PRIMARY)]])
    await msg.answer("📝 توضیحِ نوبت (اختیاری) را بنویس:", reply_markup=kb)


@booking_router.message(BkAdm.m_note, F.text)
async def bka_m_note(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    await state.update_data(m_note=(msg.text or "").strip()[:500])
    await _summary(msg, bot, state)


@booking_router.callback_query(F.data == "bka:m:nonote")
async def bka_m_nonote(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await state.update_data(m_note="")
    await _summary(cb.message, bot, state)
    await cb.answer()


# ───────────────────────── ۴) خلاصه و ثبت ─────────────────────────

async def _summary(msg, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    data = await state.get_data()
    sv = await _svc(row.id, data.get("m_sid", 0)) if row else None
    if not sv:
        await state.clear()
        return await msg.answer("این خدمت دیگر موجود نیست؛ از اول شروع کن.")
    d, st = _pd(data["m_ds"]), int(data["m_st"])
    tg = data.get("m_tg")
    who = f"👤 {_esc(data.get('m_name') or '—')}\n📱 {_esc(data.get('m_phone') or 'بدونِ شماره')}"
    if tg is None or not E.has_telegram(tg):
        who += "\nℹ️ مشتری تلگرام ندارد؛ پیام/یادآوری برایش فرستاده نمی‌شود."
    text = (f"📋 <b>خلاصهٔ نوبتِ دستی</b>\n\n💈 {_esc(sv.name)} ({_dur_txt(sv.duration_min)})\n📅 {E.fa_date_long(d)}\n"
            f"⏰ {E.fmt_hm(st)} تا {E.fmt_hm(st + sv.duration_min)}\n💰 {_price_txt(sv.price)}\n\n{who}"
            + (f"\n📝 {_esc(data.get('m_note'))}" if data.get("m_note") else "")
            + "\n\nنوبت مستقیم «تأییدشده» ثبت می‌شود.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("✅ ثبتِ نوبت", "bka:m:ok", ButtonStyle.SUCCESS)], [_btn("❌ انصراف", "bka:m:x", ButtonStyle.DANGER)]])
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@booking_router.callback_query(F.data == "bka:m:x")
async def bka_m_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await state.clear()
    await cb.answer("انصراف داده شد")
    await _show(cb, "❌ ثبتِ نوبتِ دستی لغو شد.", [BACK_AP])


@booking_router.callback_query(F.data == "bka:m:ok")
async def bka_m_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    data = await state.get_data()
    if not data.get("m_sid") or not data.get("m_name"):
        await state.clear()
        return await cb.answer("نشست منقضی شد؛ از اول شروع کن.", show_alert=True)
    sv = await _svc(row.id, int(data["m_sid"]))
    if not sv:
        await state.clear()
        return await cb.answer("این خدمت دیگر موجود نیست.", show_alert=True)
    tg = data.get("m_tg")
    if tg is None:
        c = await E.find_or_create_manual_customer(row.id, data.get("m_name", ""), data.get("m_phone", ""))
        tg = c.tg_id
    cfg = await E.get_cfg(row.id)
    ap, err = await E.book(row.id, int(tg), sv, _pd(data["m_ds"]), int(data["m_st"]), int(data.get("m_stf") or 0) or None,
                           data.get("m_name", ""), data.get("m_phone", ""), data.get("m_note", ""), cfg, by_owner=True)
    await state.clear()
    if err or not ap:
        await cb.answer("این ساعت همین الان پر شد؛ دوباره انتخاب کن.", show_alert=True)
        return await bka_m_start(cb, bot, state)
    await cb.answer("ثبت شد ✅")
    # به مشتریِ دارای تلگرام خبر بده
    if E.has_telegram(ap.tg_id):
        try:
            from tenant.bots.booking.bk_user import _appt_card
            await bot.send_message(ap.tg_id, f"✅ <b>نوبتی برایِ شما ثبت شد</b>\n\n{_appt_card(ap)}\n\nکدِ پیگیری: <code>#{ap.id}</code>", parse_mode="HTML")
        except Exception:      # noqa: BLE001
            logger.info("booking: پیامِ نوبتِ دستی به مشتری نرسید (tg=%s)", ap.tg_id)
    await _show(cb, f"✅ <b>نوبت ثبت شد</b>\n\n{_admin_card(ap, 0)}", await _appt_actions(ap))
