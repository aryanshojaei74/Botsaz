"""
bk_user.py — جریانِ مشتری در رباتِ رزروِ حرفه‌ای (PATCH_BOOKING_V2).

سرویس ← (پرسنل) ← روز ← ساعت ← نام ← شماره ← توضیح ← تأیید.  callbackها: bk:*
حالتِ FSM فقط برایِ ورودیِ متنی (نام/شماره/توضیح) استفاده می‌شود؛ بقیهٔ مراحل داخلِ callback_data است.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import re

from aiogram import Bot, F
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
)
from sqlalchemy import select

from config import Session
from models import BkAppt, BkService, BkStaff

from tenant.bots.booking import engine as E
from tenant.bots.booking.handlers import booking_router

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

STATUS_FA = {"hold": "🟠 منتظرِ پرداختِ بیعانه", "pending": "🟡 در انتظار تأیید", "confirmed": "🟢 تأییدشده", "cancelled": "⚪️ لغوشده",
             "rejected": "🔴 ردشده", "done": "✅ انجام‌شده", "noshow": "⚠️ عدم حضور"}


class BkFSM(StatesGroup):
    track = State()
    name = State()
    phone = State()
    note = State()


def _btn(text, cb, style=None):
    return InlineKeyboardButton(text=text, callback_data=cb, style=style)


def _esc(t) -> str:
    return _html.escape(str(t or ""))


async def _show(cb, text: str, rows: list[list[InlineKeyboardButton]]):
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


def _price_txt(p: int) -> str:
    return f"{int(p):,} تومان" if p else "توافقی"


def _dur_txt(m: int) -> str:
    h, mm = divmod(int(m), 60)
    if h and mm:
        return f"{h} ساعت و {mm} دقیقه"
    return f"{h} ساعت" if h else f"{mm} دقیقه"


async def _row(bot: Bot):
    from tenant import get_bot_row
    return await get_bot_row(bot)


def _back_home():
    return [_btn("🏠 منوی اصلی", "book:back", ButtonStyle.DANGER)]


# ───────────────────────── ۱) خدمات ─────────────────────────

async def bk_start(cb, bot: Bot):
    """ورودِ مشتری: فهرستِ خدمات (دکمهٔ «نوبت‌گیری»)."""
    row = await _row(bot)
    if not row:
        return await cb.answer()
    cfg = await E.get_cfg(row.id)
    svcs = await E.list_services(row.id)
    head = []
    if cfg["biz_name"]:
        head.append(f"🏢 <b>{_esc(cfg['biz_name'])}</b>")
    if cfg["welcome"]:
        head.append(_esc(cfg["welcome"]))
    if cfg["biz_address"]:
        head.append(f"📍 {_esc(cfg['biz_address'])}")
    if cfg["biz_phone"]:
        head.append(f"☎️ {_esc(cfg['biz_phone'])}")
    if not svcs:
        text = "\n".join(head + ["", "📭 این مجموعه هنوز خدماتی برای رزرو ثبت نکرده است."])
        await _show(cb, text.strip(), [_back_home()])
        return await cb.answer()
    lines = head + ["", "💈 <b>خدمات</b> — یکی را انتخاب کن:"]
    rows = []
    for sv in svcs:
        lines.append(f"• <b>{_esc(sv.name)}</b> — {_dur_txt(sv.duration_min)} — {_price_txt(sv.price)}"
                     + (f"\n   <i>{_esc(sv.description)}</i>" if sv.description else ""))
        rows.append([_btn(f"{sv.name} · {_price_txt(sv.price)}", f"bk:s:{sv.id}", ButtonStyle.PRIMARY)])
    rows.append([_btn("🗓 نوبت‌های من", "bk:my", ButtonStyle.SUCCESS)])
    rows.append(_back_home())
    await _show(cb, "\n".join(lines), rows)
    await cb.answer()


@booking_router.callback_query(F.data == "bk:home")
async def bk_home_cb(cb: CallbackQuery, bot: Bot):
    await bk_start(cb, bot)


async def _service(bot_id: int, sid: int) -> BkService | None:
    async with Session() as s:
        sv = await s.get(BkService, sid)
    return sv if sv and sv.bot_id == bot_id and sv.active else None


@booking_router.callback_query(F.data.startswith("bk:s:"))
async def bk_pick_service(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    sv = await _service(row.id, int(cb.data.split(":")[2]))
    if not sv:
        return await cb.answer("این خدمت دیگر موجود نیست.", show_alert=True)
    cfg = await E.get_cfg(row.id)
    if cfg["use_staff"]:
        staff = [st for st in await E.list_staff(row.id) if E.staff_does(st, sv.id)]
        if staff:
            rows = [[_btn("🎲 هر کدام (اولین نوبتِ خالی)", f"bk:t:{sv.id}:0", ButtonStyle.SUCCESS)]]
            rows += [[_btn(f"👤 {st.name}", f"bk:t:{sv.id}:{st.id}", ButtonStyle.PRIMARY)] for st in staff]
            rows.append([_btn("⬅️ بازگشت", "bk:home", ButtonStyle.DANGER)])
            await _show(cb, f"💈 <b>{_esc(sv.name)}</b>\nپرسنل را انتخاب کن:", rows)
            return await cb.answer()
    await _pick_day(cb, row, sv, 0, cfg)


@booking_router.callback_query(F.data.startswith("bk:t:"))
async def bk_pick_staff(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    _, _, sid, stf = cb.data.split(":")
    sv = await _service(row.id, int(sid))
    if not sv:
        return await cb.answer("این خدمت دیگر موجود نیست.", show_alert=True)
    await _pick_day(cb, row, sv, int(stf), await E.get_cfg(row.id))


# ───────────────────────── ۲) روز و ساعت ─────────────────────────

async def _pick_day(cb, row, sv: BkService, staff_id: int, cfg: dict):
    days = await E.open_days(row.id, sv, staff_id or None, cfg)
    back = f"bk:s:{sv.id}" if (cfg["use_staff"] and staff_id) else "bk:home"
    if not days:
        await _show(cb, f"💈 <b>{_esc(sv.name)}</b>\n\n😔 در {cfg['days_ahead']} روزِ آینده زمانِ خالی وجود ندارد.\n"
                        "لطفاً بعداً دوباره سر بزن یا با مجموعه تماس بگیر.",
                    [[_btn("⬅️ بازگشت", back, ButtonStyle.DANGER)]])
        return await cb.answer()
    rows, line = [], []
    for d in days:
        line.append(_btn(E.fa_date_long(d), f"bk:d:{sv.id}:{staff_id}:{d.strftime('%Y%m%d')}", ButtonStyle.PRIMARY))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([_btn("⬅️ بازگشت", back, ButtonStyle.DANGER)])
    await _show(cb, f"💈 <b>{_esc(sv.name)}</b> — {_dur_txt(sv.duration_min)} — {_price_txt(sv.price)}\n\n📅 روز را انتخاب کن:", rows)
    await cb.answer()


def _pd(s: str) -> _dt.date:
    return _dt.datetime.strptime(s, "%Y%m%d").date()


@booking_router.callback_query(F.data.startswith("bk:d:"))
async def bk_pick_time(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    _, _, sid, stf, ds = cb.data.split(":")
    sv = await _service(row.id, int(sid))
    if not sv:
        return await cb.answer("این خدمت دیگر موجود نیست.", show_alert=True)
    cfg = await E.get_cfg(row.id)
    d = _pd(ds)
    slots = await E.day_slots(row.id, sv, d, int(stf) or None, cfg)
    back = f"bk:t:{sv.id}:{stf}" if cfg["use_staff"] else f"bk:s:{sv.id}"
    if not slots:
        await _show(cb, "😔 برایِ این روز دیگر زمانِ خالی نمانده؛ روزِ دیگری را انتخاب کن.",
                    [[_btn("⬅️ بازگشت", back, ButtonStyle.DANGER)]])
        return await cb.answer()
    rows, line = [], []
    for st, _rid in slots:
        line.append(_btn(E.fmt_hm(st), f"bk:h:{sv.id}:{stf}:{ds}:{st}", ButtonStyle.SUCCESS))
        if len(line) == 3:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([_btn("⬅️ بازگشت", back, ButtonStyle.DANGER)])
    await _show(cb, f"💈 <b>{_esc(sv.name)}</b>\n📅 {E.fa_date_long(d)}\n\n⏰ ساعت را انتخاب کن:", rows)
    await cb.answer()


# ───────────────────────── ۳) مشخصاتِ مشتری ─────────────────────────

@booking_router.callback_query(F.data.startswith("bk:h:"))
async def bk_pick_slot(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    _, _, sid, stf, ds, st = cb.data.split(":")
    sv = await _service(row.id, int(sid))
    if not sv:
        return await cb.answer("این خدمت دیگر موجود نیست.", show_alert=True)
    cfg = await E.get_cfg(row.id)
    d, start = _pd(ds), int(st)
    if start not in dict(await E.day_slots(row.id, sv, d, int(stf) or None, cfg)):
        await cb.answer("این ساعت همین الان پر شد؛ ساعتِ دیگری را انتخاب کن.", show_alert=True)
        return await bk_pick_time(_Re(cb, f"bk:d:{sid}:{stf}:{ds}"), bot)
    prev = await _last_contact(row.id, cb.from_user.id)
    await state.clear()
    await state.update_data(bk_sid=sv.id, bk_stf=int(stf), bk_ds=ds, bk_st=start)
    if prev:
        await state.update_data(bk_prev=prev)
        await _show(cb, f"👤 مشخصاتِ قبلی را استفاده کنم؟\n\nنام: <b>{_esc(prev[0])}</b>\nشماره: <b>{_esc(prev[1])}</b>",
                    [[_btn("✅ بله، همین", "bk:useprev", ButtonStyle.SUCCESS)],
                     [_btn("✏️ مشخصاتِ جدید", "bk:newinfo", ButtonStyle.PRIMARY)],
                     [_btn("⬅️ بازگشت", f"bk:d:{sid}:{stf}:{ds}", ButtonStyle.DANGER)]])
        return await cb.answer()
    await _ask_name(cb, state)


class _Re:
    """cb با دادهٔ دیگر (برایِ برگشت به صفحهٔ ساعت‌ها بعد از «پر شد»)."""
    def __init__(self, cb, data):
        self.from_user, self.message, self.data = cb.from_user, cb.message, data

    async def answer(self, *a, **k):
        return None


async def _last_contact(bot_id: int, tg_id: int):
    async with Session() as s:
        ap = await s.scalar(select(BkAppt).where(BkAppt.bot_id == bot_id, BkAppt.tg_id == tg_id, BkAppt.phone != None)  # noqa: E711
                            .order_by(BkAppt.id.desc()).limit(1))
    return (ap.customer_name or "", ap.phone or "") if ap and ap.phone else None


async def _ask_name(cb, state: FSMContext):
    await state.set_state(BkFSM.name)
    await cb.message.answer("✏️ <b>نام و نام‌خانوادگی</b> را بفرست:", parse_mode="HTML")
    await cb.answer()


@booking_router.callback_query(F.data == "bk:newinfo")
async def bk_newinfo(cb: CallbackQuery, state: FSMContext):
    await _ask_name(cb, state)


@booking_router.callback_query(F.data == "bk:useprev")
async def bk_useprev(cb: CallbackQuery, bot: Bot, state: FSMContext):
    data = await state.get_data()
    prev = data.get("bk_prev")
    if not prev:
        return await _ask_name(cb, state)
    await state.update_data(bk_name=prev[0], bk_phone=prev[1])
    await _ask_note(cb.message, state)
    await cb.answer()


@booking_router.message(BkFSM.name, F.text)
async def bk_name_in(msg: Message, state: FSMContext):
    name = (msg.text or "").strip()
    if len(name) < 2 or len(name) > 60:
        return await msg.answer("نام را کامل‌تر بفرست (۲ تا ۶۰ نویسه).")
    await state.update_data(bk_name=name)
    await state.set_state(BkFSM.phone)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 ارسالِ شمارهٔ من", request_contact=True)]],
                             resize_keyboard=True, one_time_keyboard=True)
    await msg.answer("📱 <b>شمارهٔ موبایل</b> را بفرست (یا دکمهٔ زیر را بزن):", reply_markup=kb, parse_mode="HTML")


_PHONE_RE = re.compile(r"^(?:\+?98|0)?9\d{9}$")


def norm_phone(raw: str) -> str | None:
    t = E.to_en_digits(raw or "").replace(" ", "").replace("-", "")
    if not _PHONE_RE.match(t):
        return None
    t = t.lstrip("+")
    if t.startswith("98"):
        t = t[2:]
    return "0" + t.lstrip("0")


@booking_router.message(BkFSM.phone, F.contact)
async def bk_phone_contact(msg: Message, state: FSMContext):
    if msg.contact.user_id and msg.contact.user_id != msg.from_user.id:
        return await msg.answer("❗️ باید شمارهٔ خودت را بفرستی.")
    ph = norm_phone(msg.contact.phone_number or "")
    if not ph:
        return await msg.answer("❌ فقط شمارهٔ موبایلِ ایران (۰۹…) پذیرفته می‌شود.")
    await state.update_data(bk_phone=ph)
    await _ask_note(msg, state)


@booking_router.message(BkFSM.phone, F.text)
async def bk_phone_text(msg: Message, state: FSMContext):
    ph = norm_phone(msg.text or "")
    if not ph:
        return await msg.answer("❌ شماره معتبر نیست. مثل <code>09123456789</code> بفرست.", parse_mode="HTML")
    await state.update_data(bk_phone=ph)
    await _ask_note(msg, state)


async def _ask_note(msg, state: FSMContext):
    from aiogram.types import ReplyKeyboardRemove
    await state.set_state(BkFSM.note)
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("⏭ توضیحی ندارم", "bk:nonote", ButtonStyle.PRIMARY)]])
    await msg.answer("📝 اگر <b>توضیحی</b> برایِ مجموعه داری بنویس (اختیاری):", reply_markup=kb, parse_mode="HTML")
    try:
        r = await msg.answer("‎", reply_markup=ReplyKeyboardRemove())
        await r.delete()
    except Exception:      # noqa: BLE001
        logger.debug("bk: برداشتنِ کیبوردِ درخواستِ شماره ناموفق")


@booking_router.message(BkFSM.note, F.text)
async def bk_note_in(msg: Message, bot: Bot, state: FSMContext):
    await state.update_data(bk_note=(msg.text or "").strip()[:500])
    await _summary(msg, bot, state, msg.from_user.id)


@booking_router.callback_query(F.data == "bk:nonote")
async def bk_nonote(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.update_data(bk_note="")
    await _summary(cb.message, bot, state, cb.from_user.id)
    await cb.answer()


async def _summary(msg, bot: Bot, state: FSMContext, tg_id: int):
    row = await _row(bot)
    data = await state.get_data()
    sv = await _service(row.id, data.get("bk_sid", 0)) if row else None
    if not sv:
        await state.clear()
        return await msg.answer("این خدمت دیگر موجود نیست؛ از اول شروع کن.")
    d, st = _pd(data["bk_ds"]), int(data["bk_st"])
    staff_line = ""
    if data.get("bk_stf"):
        async with Session() as s:
            stf = await s.get(BkStaff, int(data["bk_stf"]))
        staff_line = f"\n👤 پرسنل: <b>{_esc(stf.name if stf else '')}</b>"
    cfg = await E.get_cfg(row.id)
    dep = E.deposit_for(sv.price, cfg)
    dep_line = ""
    if dep > 0:
        dep_line = (f"\n\n💳 <b>بیعانه: {dep:,} تومان</b> از کیف پولت برداشته می‌شود تا نوبت ثبت شود "
                    f"(مهلتِ پرداخت {cfg['hold_min']} دقیقه).\nبا لغوِ نوبت تا {cfg['refund_h']} ساعت قبل از شروع، بیعانه به کیف پولت برمی‌گردد.")
    text = (f"📋 <b>خلاصهٔ نوبت</b>\n\n💈 {_esc(sv.name)} ({_dur_txt(sv.duration_min)}){staff_line}\n"
            f"📅 {E.fa_date_long(d)}\n⏰ {E.fmt_hm(st)} تا {E.fmt_hm(st + sv.duration_min)}\n💰 {_price_txt(sv.price)}\n\n"
            f"👤 {_esc(data.get('bk_name'))}\n📱 {_esc(data.get('bk_phone'))}"
            + (f"\n📝 {_esc(data.get('bk_note'))}" if data.get("bk_note") else "") + dep_line)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ ثبتِ نهاییِ نوبت" if dep <= 0 else "✅ ادامه و پرداختِ بیعانه", "bk:confirm", ButtonStyle.SUCCESS)],
        [_btn("❌ انصراف", "bk:cancelflow", ButtonStyle.DANGER)]])
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@booking_router.callback_query(F.data == "bk:cancelflow")
async def bk_cancelflow(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await cb.answer("انصراف داده شد")
    await bk_start(cb, bot)


# ───────────────────────── ۴) ثبتِ نهایی ─────────────────────────

@booking_router.callback_query(F.data == "bk:confirm")
async def bk_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _row(bot)
    data = await state.get_data()
    if not row or not data.get("bk_sid") or not data.get("bk_phone"):
        await state.clear()
        return await cb.answer("نشست منقضی شد؛ از اول شروع کن.", show_alert=True)
    sv = await _service(row.id, int(data["bk_sid"]))
    if not sv:
        await state.clear()
        return await cb.answer("این خدمت دیگر موجود نیست.", show_alert=True)
    cfg = await E.get_cfg(row.id)
    d, st = _pd(data["bk_ds"]), int(data["bk_st"])
    ap, err = await E.book(row.id, cb.from_user.id, sv, d, st, int(data.get("bk_stf") or 0) or None,
                           data.get("bk_name", ""), data["bk_phone"], data.get("bk_note", ""), cfg)
    await state.clear()
    if err == "blocked":
        return await cb.answer("امکانِ رزرو برایِ حسابِ شما فعال نیست؛ لطفاً با مجموعه تماس بگیرید.", show_alert=True)
    if err or not ap:
        await cb.answer("این ساعت همین الان رزرو شد؛ لطفاً ساعتِ دیگری انتخاب کن.", show_alert=True)
        return await bk_start(cb, bot)
    if ap.status == "hold":                    # بیعانه لازم است ⇒ ساعت موقتاً نگه داشته شد؛ منتظرِ پرداخت
        await cb.answer("ساعت برایِ تو نگه داشته شد")
        return await _pay_screen(cb, row, ap, cfg)
    await cb.answer("ثبت شد ✅")
    done = ("✅ <b>نوبتت قطعی شد!</b>" if ap.status == "confirmed"
            else "✅ <b>نوبتت ثبت شد</b> و منتظرِ تأییدِ مجموعه است؛ به‌محضِ تأیید خبرت می‌کنیم.")
    await cb.message.answer(f"{done}\n\n{_appt_card(ap)}\n\nکدِ پیگیری: <code>#{E.appt_code(ap)}</code>", parse_mode="HTML")
    await _notify_owner_new(bot, row, ap, cb.from_user)
    try:
        from tenant import main_kb_or_remove
        await cb.message.answer("🏠", reply_markup=await main_kb_or_remove(row.id, row.owner_id, cb.from_user.id))
    except Exception:      # noqa: BLE001
        logger.debug("bk: بازگردانیِ کیبوردِ اصلی ناموفق")


def _appt_card(ap: BkAppt) -> str:
    d = _dt.date.fromisoformat(ap.date_str)
    lines = [f"💈 {_esc(ap.service_name)}"]
    if ap.staff_name:
        lines.append(f"👤 پرسنل: {_esc(ap.staff_name)}")
    lines += [f"📅 {E.fa_date_long(d)}", f"⏰ {E.fmt_hm(ap.start_min)} تا {E.fmt_hm(ap.end_min)}", f"💰 {_price_txt(ap.price)}"]
    if int(ap.deposit or 0) > 0:
        lines.append(f"💳 بیعانه: {int(ap.deposit):,} تومان")
    lines.append(f"وضعیت: {STATUS_FA.get(ap.status, ap.status)}")
    lines.append(f"🔖 کدِ پیگیری: <b>#{E.appt_code(ap)}</b>")
    return "\n".join(lines)


async def _notify_owner_new(bot: Bot, row, ap: BkAppt, user):
    """اطلاع به مالک/ادمین‌ها: مشخصاتِ مشتری + دکمه‌هایِ تأیید/رد (اگر تأیید خودکار نیست)."""
    try:
        from tenant import _notify_admins
        who = f"{_esc(ap.customer_name)} — <code>{_esc(ap.phone)}</code>"
        link = f'<a href="tg://user?id={ap.tg_id}">پروفایل تلگرام</a>'
        head = "🆕 نوبتِ جدیدِ قطعی" if ap.status == "confirmed" else "🆕 درخواستِ نوبتِ جدید"
        if int(ap.deposit or 0) > 0:
            head += f" (💳 بیعانهٔ {int(ap.deposit):,} تومان پرداخت شد)"
        text = f"{head} #{E.appt_code(ap)}\n\n{_appt_card(ap)}\n\n👤 {who}\n🔗 {link}"
        if ap.note:
            text += f"\n📝 {_esc(ap.note)}"
        rows = []
        if ap.status == "pending":
            rows.append([_btn("✅ تأیید", f"bka:ok:{ap.id}", ButtonStyle.SUCCESS), _btn("❌ رد", f"bka:no:{ap.id}", ButtonStyle.DANGER)])
        await _notify_admins(bot, row, text, InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    except Exception:      # noqa: BLE001
        logger.exception("bk: اطلاع به مالک ناموفق بود")


# ───────────────────────── ۵) نوبت‌هایِ من / لغو ─────────────────────────

async def bk_my(cb, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    today = E.iran_now().date().isoformat()
    async with Session() as s:
        aps = (await s.scalars(select(BkAppt).where(
            BkAppt.bot_id == row.id, BkAppt.tg_id == cb.from_user.id, BkAppt.date_str >= today,
            BkAppt.status.in_(("pending", "confirmed", "hold"))).order_by(BkAppt.date_str, BkAppt.start_min))).all()
        past = (await s.scalars(select(BkAppt).where(
            BkAppt.bot_id == row.id, BkAppt.tg_id == cb.from_user.id).order_by(BkAppt.id.desc()).limit(5))).all()
    if not aps:
        txt = "🗓 <b>نوبت‌هایِ من</b>\n\nنوبتِ فعالی نداری."
        if past:
            txt += "\n\n<b>آخرین نوبت‌ها:</b>\n" + "\n".join(
                f"• #{E.appt_code(a)} · {E.fa_date(_dt.date.fromisoformat(a.date_str))} {E.fmt_hm(a.start_min)} — {_esc(a.service_name)} — {STATUS_FA.get(a.status, a.status)}"
                for a in past)
        await _show(cb, txt, [[_btn("📜 سابقه و پیگیری", "bk:hist:0", ButtonStyle.PRIMARY), _btn("🔎 پیگیری با کد", "bk:trk", ButtonStyle.PRIMARY)],
                              [_btn("➕ نوبتِ جدید", "bk:home", ButtonStyle.SUCCESS)], _back_home()])
        return await cb.answer()
    rows = [[_btn(("🟠 " if a.status == "hold" else "") + f"#{E.appt_code(a)} · {E.fa_date(_dt.date.fromisoformat(a.date_str))} {E.fmt_hm(a.start_min)} · {a.service_name}",
                  f"bk:v:{a.id}", ButtonStyle.PRIMARY)] for a in aps]
    rows += [[_btn("📜 سابقه و پیگیری", "bk:hist:0", ButtonStyle.PRIMARY), _btn("🔎 پیگیری با کد", "bk:trk", ButtonStyle.PRIMARY)],
             [_btn("➕ نوبتِ جدید", "bk:home", ButtonStyle.SUCCESS)], _back_home()]
    await _show(cb, "🗓 <b>نوبت‌هایِ من</b>\nبرایِ جزئیات/لغو روی نوبت بزن:", rows)
    await cb.answer()


@booking_router.callback_query(F.data == "bk:my")
async def bk_my_cb(cb: CallbackQuery, bot: Bot):
    await bk_my(cb, bot)


PAY_FA = {"cash": "💵 نقد", "card": "💳 کارت", "online": "🌐 آنلاین", "wallet": "👛 کیف پول", "other": "➖ سایر"}


async def _view_payload(row, ap: BkAppt, cfg: dict, back: str = "bk:my"):
    """متن و دکمه‌هایِ صفحهٔ جزئیاتِ یک نوبت (برایِ «نوبت‌هایِ من»، «سابقه» و «پیگیری با کد») — شاملِ ریزِ پرداخت‌ها و بیعانه."""
    ok, why = E.can_cancel(ap, cfg)
    text = f"🗓 <b>نوبت #{E.appt_code(ap)}</b>\n\n{_appt_card(ap)}"
    pays = await E.payments_of(row.id, ap.id)
    if pays:
        lines = []
        for p in pays:
            d = E.fa_date((p.created_at + _dt.timedelta(hours=3, minutes=30)).date()) if p.created_at else ""
            lines.append(f"• {d} {PAY_FA.get(p.method, p.method)}: {'↩️ بازگشت ' if p.amount < 0 else ''}{abs(int(p.amount)):,} تومان"
                         + (f" — {_esc(p.note)}" if p.note else ""))
        net = sum(int(p.amount) for p in pays)
        text += "\n\n💵 <b>پرداخت‌ها:</b>\n" + "\n".join(lines) + f"\nخالصِ پرداخت‌شده: <b>{net:,}</b> از {int(ap.price or 0):,} تومان"
    if ap.status in ("cancelled", "rejected") and ap.cancel_reason:
        text += f"\n\n🗒 {_esc(ap.cancel_reason)}"
    rows = []
    if ap.status == "hold" and E.hold_left_min(ap, cfg) > 0:
        rows.append([_btn(f"👛 پرداختِ بیعانه ({int(ap.deposit):,})", f"bk:pay:{ap.id}", ButtonStyle.SUCCESS)])
    if ok:
        rows.append([_btn("❌ لغوِ این نوبت", f"bk:x:{ap.id}", ButtonStyle.DANGER)])
    elif ap.status in E.ACTIVE_STATUSES and why:
        text += f"\n\n<i>{_esc(why)}</i>"
        if cfg["biz_phone"]:
            text += f"\n☎️ {_esc(cfg['biz_phone'])}"
    rows.append([_btn("🔎 پیگیریِ نوبتِ دیگر", "bk:trk", ButtonStyle.PRIMARY)])
    rows.append([_btn("⬅️ بازگشت", back, ButtonStyle.DANGER)])
    return text, rows


@booking_router.callback_query(F.data.startswith("bk:v:"))
async def bk_view(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    async with Session() as s:
        ap = await s.get(BkAppt, int(cb.data.split(":")[2]))
    if not ap or ap.bot_id != row.id or ap.tg_id != cb.from_user.id:
        return await cb.answer("نوبت پیدا نشد.", show_alert=True)
    cfg = await E.get_cfg(row.id)
    if ap.status == "hold":
        if E.hold_left_min(ap, cfg) <= 0:
            return await cb.answer("مهلتِ پرداختِ این نوبت تمام شده است.", show_alert=True)
        return await _pay_screen(cb, row, ap, cfg)
    text, rows = await _view_payload(row, ap, cfg)
    await _show(cb, text, rows)
    await cb.answer()


# ───────────────────────── پیگیری با کد و سابقه ─────────────────────────

HIST_PAGE = 8


@booking_router.callback_query(F.data.startswith("bk:hist:"))
async def bk_history(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    page = int(cb.data.split(":")[2])
    async with Session() as s:
        from sqlalchemy import func as _f
        total = int(await s.scalar(select(_f.count()).select_from(BkAppt).where(BkAppt.bot_id == row.id, BkAppt.tg_id == cb.from_user.id)) or 0)
        aps = (await s.scalars(select(BkAppt).where(BkAppt.bot_id == row.id, BkAppt.tg_id == cb.from_user.id)
                               .order_by(BkAppt.id.desc()).offset(page * HIST_PAGE).limit(HIST_PAGE))).all()
    if not aps:
        await _show(cb, "📜 <b>سابقهٔ نوبت‌ها</b>\n\nهنوز نوبتی ثبت نکرده‌ای.", [[_btn("🔎 پیگیری با کد", "bk:trk", ButtonStyle.PRIMARY)], _back_home()])
        return await cb.answer()
    rows = [[_btn(f"#{E.appt_code(a)} · {E.fa_date(_dt.date.fromisoformat(a.date_str))} · {a.service_name} · {STATUS_FA.get(a.status, a.status)[:2]}",
                  f"bk:v:{a.id}", ButtonStyle.PRIMARY)] for a in aps]
    nav = []
    if page > 0:
        nav.append(_btn("⬅️ قبلی", f"bk:hist:{page - 1}"))
    if (page + 1) * HIST_PAGE < total:
        nav.append(_btn("بعدی ➡️", f"bk:hist:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("🔎 پیگیری با کد", "bk:trk", ButtonStyle.PRIMARY)])
    rows.append([_btn("⬅️ بازگشت", "bk:my", ButtonStyle.DANGER)])
    await _show(cb, f"📜 <b>سابقهٔ نوبت‌هایِ من</b> — {total} نوبت\nبرایِ جزئیات و پرداخت‌ها روی هر نوبت بزن:", rows)
    await cb.answer()


async def bk_track_start(cb, bot: Bot, state: FSMContext):
    """ورودِ «🔎 پیگیریِ نوبت» (دکمهٔ منو یا دکمهٔ داخلِ ربات): کدِ پیگیری را می‌گیرد."""
    await state.clear()
    await state.set_state(BkFSM.track)
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("📜 سابقهٔ من", "bk:hist:0", ButtonStyle.PRIMARY)], [_btn("❌ انصراف", "bk:trkx", ButtonStyle.DANGER)]])
    await cb.message.answer("🔎 <b>پیگیریِ نوبت</b>\n\n«کدِ پیگیری» نوبتت را بفرست (همان عددی که بعد از ثبتِ نوبت گرفتی؛ مثلاً <code>12</code>).",
                            reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@booking_router.callback_query(F.data == "bk:trk")
async def bk_track_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await bk_track_start(cb, bot, state)


@booking_router.callback_query(F.data == "bk:trkx")
async def bk_track_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await cb.answer("انصراف داده شد")
    await bk_my(cb, bot)


@booking_router.message(BkFSM.track, F.text)
async def bk_track_in(msg: Message, bot: Bot, state: FSMContext):
    row = await _row(bot)
    if not row:
        return
    code = E.parse_code(msg.text or "")
    if code is None:
        return await msg.answer("کدِ پیگیری فقط یک عدد است (مثلاً <code>12</code>). دوباره بفرست یا «❌ انصراف» را بزن.", parse_mode="HTML")
    ap = await E.find_by_code(row.id, code)
    # برایِ جلوگیریِ از لو رفتنِ نوبتِ دیگران، «وجود نداشتن» و «مالِ دیگری بودن» یک پیام دارند
    if not ap or ap.tg_id != msg.from_user.id:
        return await msg.answer("نوبتی با این کد برایِ شما پیدا نشد. کد را دوباره بررسی کن (در پیامِ ثبتِ نوبت و «🗓 نوبت‌هایِ من» هست).")
    await state.clear()
    cfg = await E.get_cfg(row.id)
    text, rows = await _view_payload(row, ap, cfg, back="bk:my")
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@booking_router.callback_query(F.data.startswith("bk:x:"))
async def bk_cancel_ask(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[2])
    policy = ""
    row = await _row(bot)
    if row:
        async with Session() as s:
            ap = await s.get(BkAppt, aid)
        if ap and ap.bot_id == row.id and ap.tg_id == cb.from_user.id:
            net = await E.net_deposit(row.id, aid)
            if net > 0:
                cfg = await E.get_cfg(row.id)
                back = (E.appt_start_dt(ap) - E.iran_now()) >= _dt.timedelta(hours=cfg["refund_h"])
                policy = (f"\n\n💳 بیعانهٔ {net:,} تومان " + ("با این لغو <b>به کیف پولت برمی‌گردد</b>." if back
                          else f"چون کمتر از {cfg['refund_h']} ساعت به نوبت مانده، <b>برگردانده نمی‌شود</b>."))
    await _show(cb, "❓ مطمئنی می‌خواهی این نوبت را <b>لغو</b> کنی؟" + policy,
                [[_btn("✅ بله، لغو شود", f"bk:xy:{aid}", ButtonStyle.DANGER)], [_btn("⬅️ نه", f"bk:v:{aid}", ButtonStyle.PRIMARY)]])
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bk:xy:"))
async def bk_cancel_do(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    aid = int(cb.data.split(":")[2])
    async with Session() as s:
        ap = await s.get(BkAppt, aid)
    if not ap or ap.bot_id != row.id or ap.tg_id != cb.from_user.id:
        return await cb.answer("نوبت پیدا نشد.", show_alert=True)
    cfg = await E.get_cfg(row.id)
    ok, why = E.can_cancel(ap, cfg)
    if not ok:
        return await cb.answer(why, show_alert=True)
    done = await E.set_status(row.id, aid, "cancelled", "لغو توسطِ مشتری", only_from=E.ACTIVE_STATUSES)
    if not done:                                  # هم‌زمان تعیین‌تکلیف شد (مثلاً مالک لغو کرد) ⇒ بازگشتِ بیعانه دوباره انجام نمی‌شود
        return await cb.answer("این نوبت قبلاً تعیین‌تکلیف شده است.", show_alert=True)
    refunded = await E.refund_deposit(row.id, aid, cfg, force_full=False)
    await cb.answer("نوبت لغو شد")
    if refunded:
        await cb.message.answer(f"💳 بیعانهٔ <b>{refunded:,}</b> تومان به کیف پولت برگشت.", parse_mode="HTML")
    elif int(ap.deposit or 0) > 0:
        await cb.message.answer("ℹ️ طبقِ قوانینِ مجموعه (لغو کمتر از مهلت) بیعانه برگردانده نمی‌شود.")
    try:
        from tenant import _notify_admins
        extra = (f"\n💳 بیعانه: {refunded:,} تومان به کیف پولِ مشتری برگشت." if refunded else
                 ("\n💳 بیعانه طبقِ قانون برگردانده نشد (نزدیکِ زمانِ نوبت)." if int(ap.deposit or 0) > 0 else ""))
        await _notify_admins(bot, row, f"❎ نوبت #{E.appt_code(ap)} توسطِ مشتری لغو شد.\n\n{_appt_card(ap)}\n👤 {_esc(ap.customer_name)} — <code>{_esc(ap.phone)}</code>{extra}")
    except Exception:      # noqa: BLE001
        logger.exception("bk: اطلاعِ لغو به مالک ناموفق")
    await bk_my(cb, bot)


# ───────────────────────── ۶) بیعانه ─────────────────────────

async def _pay_screen(cb, row, ap: BkAppt, cfg: dict, note: str = ""):
    async with Session() as s:
        from models import BotUser
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == ap.tg_id))
    bal = int(float(u.balance or 0)) if u else 0
    left = E.hold_left_min(ap, cfg)
    text = (f"{note}💳 <b>پرداختِ بیعانه</b>\n\n{_appt_card(ap)}\n\n"
            f"موجودیِ کیف پولت: <b>{bal:,}</b> تومان\nمبلغِ بیعانه: <b>{int(ap.deposit):,}</b> تومان\n"
            f"⏳ ساعت تا <b>{left}</b> دقیقهٔ دیگر برایِ تو نگه داشته می‌شود.")
    if bal < int(ap.deposit):
        text += f"\n\n⚠️ برایِ پرداخت <b>{int(ap.deposit) - bal:,}</b> تومان کم داری؛ کیف پولت را شارژ کن و بعد از «🗓 نوبت‌هایِ من» پرداخت را کامل کن."
    rows = [[_btn(f"👛 پرداخت از کیف پول ({int(ap.deposit):,})", f"bk:pay:{ap.id}", ButtonStyle.SUCCESS)]]
    if bal < int(ap.deposit):
        rows.append([_btn("💳 شارژ کیف پول", "w:back", ButtonStyle.PRIMARY)])
    rows.append([_btn("❌ انصراف (آزادکردنِ ساعت)", f"bk:xh:{ap.id}", ButtonStyle.DANGER)])
    await _show(cb, text, rows)


@booking_router.callback_query(F.data.startswith("bk:pay:"))
async def bk_pay(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    aid = int(cb.data.split(":")[2])
    cfg = await E.get_cfg(row.id)
    code, info = await E.pay_deposit(row.id, aid, cb.from_user.id, cfg)
    if code == "insufficient":
        await cb.answer("موجودیِ کیف پول کافی نیست.", show_alert=True)
        return await _pay_screen(cb, row, info["appt"], cfg)
    if code in ("expired",):
        await cb.answer("مهلتِ پرداخت تمام شد؛ دوباره نوبت بگیر.", show_alert=True)
        return await bk_start(cb, bot)
    if code in ("notfound", "nouser"):
        return await cb.answer("نوبت پیدا نشد.", show_alert=True)
    if code == "not_hold":
        await cb.answer("این نوبت قبلاً تعیین‌تکلیف شده است.", show_alert=True)
        return await bk_my(cb, bot)
    ap = info
    await cb.answer("پرداخت شد ✅")
    done = ("✅ <b>بیعانه پرداخت شد و نوبتت قطعی شد!</b>" if ap.status == "confirmed"
            else "✅ <b>بیعانه پرداخت شد و نوبتت ثبت شد</b>؛ منتظرِ تأییدِ مجموعه است و به‌محضِ تأیید خبرت می‌کنیم.")
    await cb.message.answer(f"{done}\n\n{_appt_card(ap)}\n\nکدِ پیگیری: <code>#{E.appt_code(ap)}</code>", parse_mode="HTML")
    await _notify_owner_new(bot, row, ap, cb.from_user)
    try:
        from tenant import main_kb_or_remove
        await cb.message.answer("🏠", reply_markup=await main_kb_or_remove(row.id, row.owner_id, cb.from_user.id))
    except Exception:      # noqa: BLE001
        logger.debug("bk: بازگردانیِ کیبوردِ اصلی ناموفق")


@booking_router.callback_query(F.data.startswith("bk:xh:"))
async def bk_release_hold(cb: CallbackQuery, bot: Bot):
    row = await _row(bot)
    if not row:
        return await cb.answer()
    aid = int(cb.data.split(":")[2])
    async with Session() as s:
        ap = await s.get(BkAppt, aid)
    if not ap or ap.bot_id != row.id or ap.tg_id != cb.from_user.id:
        return await cb.answer("نوبت پیدا نشد.", show_alert=True)
    await E.set_status(row.id, aid, "cancelled", "انصراف از پرداختِ بیعانه", only_from=("hold",))
    await cb.answer("ساعت آزاد شد")
    await bk_start(cb, bot)
