"""
bk_admin.py — پنلِ مدیریتِ مالک/ادمین در رباتِ رزروِ حرفه‌ای (PATCH_BOOKING_V2). callbackها: bka:*

داشبورد، لیستِ نوبت‌ها (انتظار/امروز/فردا/آینده)، تأیید/رد/انجام/عدم‌حضور/لغو، خدمات، پرسنل، ساعتِ کاری (+تنظیمِ سریع)،
روزهایِ تعطیل، تنظیمات و آمار. همهٔ مراحلِ متنی با FSM.
"""
from __future__ import annotations

import datetime as _dt

from aiogram import Bot, F
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import Session
from models import BkAppt, BkHours, BkOff, BkService, BkStaff, MainButton

from tenant.bots.booking import engine as E
from tenant.bots.booking.bk_user import STATUS_FA, _appt_card, _btn, _dur_txt, _esc, _price_txt, _show
from tenant.bots.booking.handlers import booking_router

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

PAGE = 8


class BkAdm(StatesGroup):
    sv_name = State()
    sv_dur = State()
    sv_price = State()
    sv_desc = State()
    sv_edit = State()
    st_name = State()
    hr_start = State()
    hr_end = State()
    q_start = State()
    q_end = State()
    of_date = State()
    of_reason = State()
    cfg_val = State()
    pay_amount = State()
    m_name = State()
    m_phone = State()
    m_note = State()
    cu_note = State()
    cu_search = State()


async def _guard(cb, bot: Bot):
    from tenant import _owner_ok
    return await _owner_ok(cb, bot)


async def _guard_msg(msg: Message, bot: Bot):
    from tenant import _owner_ok_msg
    return await _owner_ok_msg(msg, bot)


BACK_HOME = [_btn("⬅️ بازگشت", "bka:home", ButtonStyle.DANGER)]
BACK_AP = [_btn("⬅️ بازگشت", "bka:ap", ButtonStyle.DANGER)]
BACK_CAT = [_btn("⬅️ بازگشت", "bka:cat", ButtonStyle.DANGER)]
BACK_TM = [_btn("⬅️ بازگشت", "bka:tm", ButtonStyle.DANGER)]
PANEL_HOME = [_btn("🏠 پنل مدیریت", "tadm:menu", ButtonStyle.DANGER)]


def _help_btn(key: str):
    return [_btn("❓ راهنمایِ این بخش", f"bka:h:{key}", ButtonStyle.PRIMARY)]


# ───────────────────────── خانه و زیرمنوها ─────────────────────────

async def build_home(row) -> tuple[str, InlineKeyboardMarkup]:
    """صفحهٔ اصلیِ پنلِ اختصاصیِ رزرو (متن + کیبورد). از /admin و «پنل مدیریت» هم صدا زده می‌شود."""
    from tenant.bots.booking.loop import worker_alive
    cfg = await E.get_cfg(row.id)
    now = E.iran_now()
    today, tmr = now.date().isoformat(), (now.date() + _dt.timedelta(days=1)).isoformat()
    async with Session() as s:
        async def cnt(*conds):
            return int(await s.scalar(select(func.count()).select_from(BkAppt).where(BkAppt.bot_id == row.id, *conds)) or 0)
        pending = await cnt(BkAppt.status == "pending", BkAppt.date_str >= today)
        n_today = await cnt(BkAppt.date_str == today, BkAppt.status.in_(E.ACTIVE_STATUSES))
        n_tmr = await cnt(BkAppt.date_str == tmr, BkAppt.status.in_(E.ACTIVE_STATUSES))
        n_svc = int(await s.scalar(select(func.count()).select_from(BkService).where(BkService.bot_id == row.id, BkService.active.is_(True))) or 0)
        n_hours = int(await s.scalar(select(func.count()).select_from(BkHours).where(BkHours.bot_id == row.id)) or 0)
        n_btn = int(await s.scalar(select(func.count()).select_from(MainButton).where(
            MainButton.bot_id == row.id, MainButton.action.in_(("book_show", "book_my")))) or 0)
    title = f"📅 <b>{_esc(cfg['biz_name'])}</b>" if cfg["biz_name"] else "📅 <b>پنل رزرو و نوبت‌دهی</b>"
    text = (f"{title}\n\n🟡 در انتظار تأیید: <b>{pending}</b>\n📅 امروز: <b>{n_today}</b> نوبت · فردا: <b>{n_tmr}</b>\n"
            f"💈 خدماتِ فعال: <b>{n_svc}</b>")
    todo = []
    if not n_svc:
        todo.append("➕ یک «خدمت» اضافه کن (💈 خدمات و پرسنل)")
    if not n_hours:
        todo.append("🕘 «ساعت کاری» را تنظیم کن (🕘 زمان‌بندی)")
    if not n_btn:
        todo.append("🧩 دکمهٔ «نوبت‌گیری» را به منویِ ربات اضافه کن (دکمهٔ پایین)")
    if todo:
        text += "\n\n⚠️ <b>برایِ شروع:</b>\n" + "\n".join(todo)
    if (cfg["remind1"] or cfg["remind2"]) and not await worker_alive():
        text += "\n\n🔕 <b>یادآوریِ خودکار موقتاً غیرفعال است</b> (سرویسِ یادآوری بالا نیست). به پشتیبانیِ بات‌ساز خبر بده."
    rows = [
        [_btn(f"📋 نوبت‌ها ({pending} در انتظار)" if pending else "📋 نوبت‌ها", "bka:ap", ButtonStyle.SUCCESS if pending else ButtonStyle.PRIMARY),
         _btn("👤 مشتریان و پرونده‌ها", "bka:cu:0", ButtonStyle.PRIMARY)],
        [_btn("💈 خدمات و پرسنل", "bka:cat", ButtonStyle.PRIMARY), _btn("🕘 زمان‌بندی", "bka:tm", ButtonStyle.PRIMARY)],
        [_btn("💰 مالی", "bka:fin", ButtonStyle.PRIMARY), _btn("📊 گزارش‌ها", "bka:stats", ButtonStyle.PRIMARY)],
        [_btn("⚙️ تنظیمات", "bka:cfg", ButtonStyle.PRIMARY), _btn("🧰 ابزارهایِ عمومیِ ربات", "bka:gen", ButtonStyle.PRIMARY)],
    ]
    if not n_btn:
        rows.append([_btn("🧩 افزودنِ دکمه‌هایِ رزرو به منویِ ربات", "bka:seedbtn", ButtonStyle.SUCCESS)])
    rows.append(_help_btn("home"))
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def home_kb(row) -> InlineKeyboardMarkup:
    return (await build_home(row))[1]


async def bk_dashboard(cb, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    text, kb = await build_home(row)
    await _show(cb, text, kb.inline_keyboard)
    await cb.answer()


@booking_router.callback_query(F.data == "bka:home")
async def bka_home(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await bk_dashboard(cb, bot)


@booking_router.callback_query(F.data.startswith("bka:h:"))
async def bka_help(cb: CallbackQuery, bot: Bot):
    if not await _guard(cb, bot):
        return
    from tenant.bots.booking.bk_help import help_text
    key = cb.data.split(":")[2]
    back = {"home": "bka:home", "ap": "bka:ap", "cat": "bka:cat", "tm": "bka:tm", "cu": "bka:cu:0", "fin": "bka:fin",
            "cfg": "bka:cfg", "rep": "bka:stats"}.get(key, "bka:home")
    await _show(cb, help_text(key), [[_btn("⬅️ بازگشت", back, ButtonStyle.DANGER)]])
    await cb.answer()


@booking_router.callback_query(F.data == "bka:ap")
async def bka_hub_appts(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await state.clear()
    rows = [[_btn("➕ ثبتِ نوبتِ دستی (حضوری/تلفنی)", "bka:m", ButtonStyle.SUCCESS)],
            [_btn("🟡 در انتظار تأیید", "bka:l:pend:0", ButtonStyle.SUCCESS)],
            [_btn("📅 امروز", "bka:l:today:0", ButtonStyle.PRIMARY), _btn("🗓 فردا", "bka:l:tmr:0", ButtonStyle.PRIMARY)],
            [_btn("🔜 همهٔ نوبت‌هایِ آینده", "bka:l:up:0", ButtonStyle.PRIMARY)],
            _help_btn("ap"), BACK_HOME]
    await _show(cb, "📋 <b>نوبت‌ها</b>\nکدام لیست را ببینی؟", rows)
    await cb.answer()


@booking_router.callback_query(F.data == "bka:cat")
async def bka_hub_catalog(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await state.clear()
    rows = [[_btn("💈 خدمات", "bka:sv", ButtonStyle.PRIMARY), _btn("👥 پرسنل", "bka:st", ButtonStyle.PRIMARY)],
            _help_btn("cat"), BACK_HOME]
    await _show(cb, "💈 <b>خدمات و پرسنل</b>\nچه کاری و توسطِ چه کسی انجام می‌شود:", rows)
    await cb.answer()


@booking_router.callback_query(F.data == "bka:tm")
async def bka_hub_time(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await state.clear()
    rows = [[_btn("🕘 ساعتِ کاری", "bka:hr", ButtonStyle.PRIMARY), _btn("🚫 روزهایِ تعطیل", "bka:of", ButtonStyle.PRIMARY)],
            _help_btn("tm"), BACK_HOME]
    await _show(cb, "🕘 <b>زمان‌بندی</b>\nچه روز و ساعتی نوبت می‌گیری:", rows)
    await cb.answer()


@booking_router.callback_query(F.data == "bka:gen")
async def bka_generic_tools(cb: CallbackQuery, bot: Bot):
    """ابزارهایِ عمومیِ همهٔ ربات‌هایِ بات‌ساز (ادمین‌ها، تیکت، کیف‌پول، آنتی‌اسپم، …) — جدا از پنلِ رزرو."""
    row = await _guard(cb, bot)
    if not row:
        return
    from tenant import tadmin_kb
    kb = await tadmin_kb(row.id, row.field, row.plan, True)
    rows = [list(r) for r in kb.inline_keyboard] if hasattr(kb, "inline_keyboard") else []
    rows.append([_btn("📅 بازگشت به پنلِ رزرو", "bka:home", ButtonStyle.SUCCESS)])
    await _show(cb, "🧰 <b>ابزارهایِ عمومیِ ربات</b>\nاین بخش‌ها مشترک بینِ همهٔ ربات‌هایِ بات‌ساز است (ادمین‌ها، تیکت‌ها، کیف‌پول، …). "
                    "کارهایِ مخصوصِ رزرو را از «پنلِ رزرو» انجام بده.", rows)
    await cb.answer()


@booking_router.callback_query(F.data == "bka:seedbtn")
async def bka_seed_buttons(cb: CallbackQuery, bot: Bot):
    """دو دکمهٔ «📅 نوبت‌گیری» و «🗓 نوبت‌های من» را (اگر نیستند) به منویِ اصلی اضافه می‌کند."""
    row = await _guard(cb, bot)
    if not row:
        return
    async with Session() as s:
        have = {b.action for b in (await s.scalars(select(MainButton).where(MainButton.bot_id == row.id))).all()}
        top = int(await s.scalar(select(func.coalesce(func.max(MainButton.row), -1)).where(MainButton.bot_id == row.id)) or -1)
        added = 0
        for action, label, color in (("book_show", "📅 نوبت‌گیری", "success"), ("book_my", "🗓 نوبت‌های من", "primary")):
            if action in have:
                continue
            added += 1
            s.add(MainButton(bot_id=row.id, row=top + 1, col=added - 1, label=label, action=action, color=color))
        await s.commit()
    await cb.answer("دکمه‌ها به منو اضافه شد ✅" if added else "دکمه‌ها از قبل هست.", show_alert=True)
    await bk_dashboard(cb, bot)


# ───────────────────────── نوبت‌ها ─────────────────────────

LIST_TITLES = {"pend": "🟡 در انتظار تأیید", "today": "📅 نوبت‌هایِ امروز", "tmr": "🗓 نوبت‌هایِ فردا", "up": "🔜 نوبت‌هایِ آینده"}


@booking_router.callback_query(F.data.startswith("bka:l:"))
async def bka_list(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, kind, page = cb.data.split(":")
    page = int(page)
    now = E.iran_now()
    today, tmr = now.date().isoformat(), (now.date() + _dt.timedelta(days=1)).isoformat()
    q = select(BkAppt).where(BkAppt.bot_id == row.id)
    if kind == "pend":
        q = q.where(BkAppt.status == "pending", BkAppt.date_str >= today)
    elif kind == "today":
        q = q.where(BkAppt.date_str == today, BkAppt.status.in_(E.ACTIVE_STATUSES + ("done", "noshow")))
    elif kind == "tmr":
        q = q.where(BkAppt.date_str == tmr, BkAppt.status.in_(E.ACTIVE_STATUSES))
    else:
        q = q.where(BkAppt.date_str >= today, BkAppt.status.in_(E.ACTIVE_STATUSES))
    async with Session() as s:
        total = int(await s.scalar(select(func.count()).select_from(q.subquery())) or 0)
        aps = (await s.scalars(q.order_by(BkAppt.date_str, BkAppt.start_min).offset(page * PAGE).limit(PAGE))).all()
    if not aps:
        await _show(cb, f"{LIST_TITLES.get(kind, '')}\n\n📭 موردی نیست.", [BACK_AP])
        return await cb.answer()
    rows = []
    for a in aps:
        d = _dt.date.fromisoformat(a.date_str)
        icon = STATUS_FA.get(a.status, "")[:2]
        rows.append([_btn(f"{icon} {E.fa_date(d)} {E.fmt_hm(a.start_min)} · {a.customer_name or '—'} · {a.service_name}",
                          f"bka:a:{a.id}:{kind}:{page}", ButtonStyle.PRIMARY)])
    nav = []
    if page > 0:
        nav.append(_btn("⬅️ قبلی", f"bka:l:{kind}:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(_btn("بعدی ➡️", f"bka:l:{kind}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(BACK_AP)
    await _show(cb, f"{LIST_TITLES.get(kind, '')} — {total} مورد", rows)
    await cb.answer()


def _admin_card(a: BkAppt, paid: int = 0) -> str:
    link = E.tg_link_html(a.tg_id)
    t = f"📌 <b>نوبت #{a.id}</b>\n\n{_appt_card(a)}\n\n👤 {_esc(a.customer_name)}\n📱 <code>{_esc(a.phone)}</code>\n🔗 {link}"
    if a.price or paid:
        left = max(int(a.price or 0) - int(paid), 0)
        t += f"\n💵 پرداخت‌شده: <b>{int(paid):,}</b> از {int(a.price or 0):,} تومان" + (f" — مانده: <b>{left:,}</b>" if left else " ✅")
    if a.note:
        t += f"\n📝 {_esc(a.note)}"
    if a.cancel_reason:
        t += f"\n🗒 {_esc(a.cancel_reason)}"
    return t


async def _appt_actions(a: BkAppt, kind: str = "up", page: int = 0) -> list[list]:
    rows = []
    if a.status == "pending":
        rows.append([_btn("✅ تأیید", f"bka:ok:{a.id}", ButtonStyle.SUCCESS), _btn("❌ رد", f"bka:no:{a.id}", ButtonStyle.DANGER)])
    if a.status == "confirmed":
        rows.append([_btn("✔️ انجام شد", f"bka:done:{a.id}", ButtonStyle.SUCCESS), _btn("⚠️ نیامد", f"bka:ns:{a.id}", ButtonStyle.DANGER)])
    if a.status in ("confirmed", "done"):
        rows.append([_btn("💵 ثبتِ پرداخت", f"bka:pay:{a.id}", ButtonStyle.PRIMARY)])
    if a.status in E.ACTIVE_STATUSES:
        rows.append([_btn("🚫 لغوِ نوبت (توسطِ مجموعه)", f"bka:cx:{a.id}", ButtonStyle.DANGER)])
    rows.append([_btn("⬅️ بازگشت", f"bka:l:{kind}:{page}", ButtonStyle.DANGER)])
    return rows


@booking_router.callback_query(F.data.startswith("bka:a:"))
async def bka_view(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    parts = cb.data.split(":")
    async with Session() as s:
        a = await s.get(BkAppt, int(parts[2]))
    if not a or a.bot_id != row.id:
        return await cb.answer("پیدا نشد", show_alert=True)
    await _show(cb, _admin_card(a, await E.paid_of(row.id, a.id)), await _appt_actions(a, parts[3] if len(parts) > 3 else "up", int(parts[4]) if len(parts) > 4 else 0))
    await cb.answer()


async def _tell_customer(bot: Bot, a: BkAppt, text: str):
    if not E.has_telegram(a.tg_id):          # مشتریِ ثبتِ دستی: تلگرام ندارد
        return
    try:
        await bot.send_message(a.tg_id, text, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        logger.info("bk: پیام به مشتری نرسید (tg=%s)", a.tg_id)


_ACTIONS = {
    "ok": ("confirmed", ("pending",), "✅ نوبت تأیید شد.",
           "✅ <b>نوبتت تأیید شد!</b>\n\n{card}"),
    "no": ("rejected", ("pending",), "❌ نوبت رد شد.",
           "😔 متأسفانه نوبتت <b>تأیید نشد</b>. لطفاً زمانِ دیگری انتخاب کن.\n\n{card}"),
    "done": ("done", ("confirmed",), "✔️ ثبت شد: انجام‌شده.", None),
    "ns": ("noshow", ("confirmed",), "⚠️ ثبت شد: عدم حضور.", None),
    "cx": ("cancelled", E.ACTIVE_STATUSES, "🚫 نوبت لغو شد.",
           "ℹ️ نوبتِ شما توسطِ مجموعه <b>لغو</b> شد؛ برایِ هماهنگی با مجموعه تماس بگیرید.\n\n{card}"),
}


@booking_router.callback_query(F.data.regexp(r"^bka:(ok|no|done|ns|cx):\d+$"))
async def bka_act(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    _, act, aid = cb.data.split(":")
    status, only_from, toast, note = _ACTIONS[act]
    reason = {"cx": "لغو توسطِ مجموعه", "no": "ردشده توسطِ مجموعه"}.get(act)
    a = await E.set_status(row.id, int(aid), status, reason, only_from=only_from)
    if not a:
        return await cb.answer("این نوبت قبلاً تعیین‌تکلیف شده یا پیدا نشد.", show_alert=True)
    if note:
        await _tell_customer(bot, a, note.format(card=_appt_card(a)))
    await cb.answer(toast)
    await _show(cb, _admin_card(a, await E.paid_of(row.id, a.id)), await _appt_actions(a))


# ───────────────────────── خدمات ─────────────────────────

@booking_router.callback_query(F.data == "bka:sv")
async def bka_services(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    svcs = await E.list_services(row.id, only_active=False)
    rows = [[_btn(f"{'🟢' if sv.active else '⚪️'} {sv.name} · {_dur_txt(sv.duration_min)} · {_price_txt(sv.price)}", f"bka:sv:{sv.id}", ButtonStyle.PRIMARY)]
            for sv in svcs]
    rows.append([_btn("➕ خدمتِ جدید", "bka:sva", ButtonStyle.SUCCESS)])
    rows.append(_help_btn("cat"))
    rows.append(BACK_CAT)
    await _show(cb, "💈 <b>خدمات</b>\nروی هر خدمت بزن تا ویرایش کنی:" if svcs else "💈 <b>خدمات</b>\n\nهنوز خدمتی ثبت نشده.", rows)
    await cb.answer()


@booking_router.callback_query(F.data == "bka:sva")
async def bka_sv_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await state.set_state(BkAdm.sv_name)
    await cb.message.answer("💈 <b>نامِ خدمت</b> را بفرست (مثلاً «کوتاهی مو» یا «ویزیتِ عمومی»):", parse_mode="HTML")
    await cb.answer()


@booking_router.message(BkAdm.sv_name, F.text)
async def bka_sv_name(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    name = (msg.text or "").strip()
    if not (2 <= len(name) <= 100):
        return await msg.answer("نام ۲ تا ۱۰۰ نویسه باشد.")
    await state.update_data(sv_name=name)
    await state.set_state(BkAdm.sv_dur)
    await msg.answer("⏱ <b>مدتِ خدمت</b> چند دقیقه است؟ (مثلاً <code>30</code> یا <code>90</code>)", parse_mode="HTML")


def _num(msg_text: str) -> int | None:
    t = E.to_en_digits(msg_text or "").replace(",", "").replace("٬", "").strip()
    return int(t) if t.isdigit() else None


@booking_router.message(BkAdm.sv_dur, F.text)
async def bka_sv_dur(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    n = _num(msg.text)
    if not n or not (5 <= n <= 600):
        return await msg.answer("عددِ دقیقه (۵ تا ۶۰۰) بفرست.")
    await state.update_data(sv_dur=n)
    await state.set_state(BkAdm.sv_price)
    await msg.answer("💰 <b>قیمت</b> به تومان؟ (<code>0</code> = توافقی)", parse_mode="HTML")


@booking_router.message(BkAdm.sv_price, F.text)
async def bka_sv_price(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    n = _num(msg.text)
    if n is None or n > 1_000_000_000:
        return await msg.answer("فقط عدد (تومان) بفرست؛ ۰ = توافقی.")
    await state.update_data(sv_price=n)
    await state.set_state(BkAdm.sv_desc)
    await msg.answer("📝 یک <b>توضیحِ کوتاه</b> برایِ خدمت بنویس (یا <code>-</code> برایِ بدونِ توضیح):", parse_mode="HTML")


@booking_router.message(BkAdm.sv_desc, F.text)
async def bka_sv_desc(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    await state.clear()
    desc = (msg.text or "").strip()
    async with Session() as s:
        sv = BkService(bot_id=row.id, name=data["sv_name"], duration_min=int(data["sv_dur"]), price=int(data["sv_price"]),
                       description=None if desc in ("-", "—", "") else desc[:300])
        s.add(sv)
        await s.commit()
    await msg.answer(f"✅ خدمتِ «{_esc(sv.name)}» اضافه شد.", parse_mode="HTML",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("💈 خدمات", "bka:sv", ButtonStyle.PRIMARY)], BACK_CAT]))


@booking_router.callback_query(F.data.regexp(r"^bka:sv:\d+$"))
async def bka_sv_view(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    async with Session() as s:
        sv = await s.get(BkService, int(cb.data.split(":")[2]))
    if not sv or sv.bot_id != row.id:
        return await cb.answer("پیدا نشد", show_alert=True)
    text = (f"💈 <b>{_esc(sv.name)}</b>\n⏱ {_dur_txt(sv.duration_min)}\n💰 {_price_txt(sv.price)}\n"
            f"{'🟢 فعال' if sv.active else '⚪️ غیرفعال'}" + (f"\n📝 {_esc(sv.description)}" if sv.description else ""))
    rows = [[_btn("✏️ نام", f"bka:sve:{sv.id}:name"), _btn("⏱ مدت", f"bka:sve:{sv.id}:dur")],
            [_btn("💰 قیمت", f"bka:sve:{sv.id}:price"), _btn("📝 توضیح", f"bka:sve:{sv.id}:desc")],
            [_btn("⚪️ غیرفعال‌کردن" if sv.active else "🟢 فعال‌کردن", f"bka:svt:{sv.id}", ButtonStyle.PRIMARY),
             _btn("🗑 حذف", f"bka:svd:{sv.id}", ButtonStyle.DANGER)],
            [_btn("⬅️ بازگشت", "bka:sv", ButtonStyle.DANGER)]]
    await _show(cb, text, rows)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:svt:"))
async def bka_sv_toggle(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    async with Session() as s:
        sv = await s.get(BkService, int(cb.data.split(":")[2]))
        if sv and sv.bot_id == row.id:
            sv.active = not sv.active
            await s.commit()
    await bka_sv_view(cb, bot)


@booking_router.callback_query(F.data.startswith("bka:svd:"))
async def bka_sv_del(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    parts = cb.data.split(":")
    sid = int(parts[2])
    if len(parts) == 3:
        return await _show(cb, "❓ این خدمت حذف شود؟ (نوبت‌هایِ قبلی می‌مانند)",
                           [[_btn("✅ بله، حذف", f"bka:svd:{sid}:y", ButtonStyle.DANGER)], [_btn("⬅️ نه", f"bka:sv:{sid}", ButtonStyle.PRIMARY)]])
    async with Session() as s:
        sv = await s.get(BkService, sid)
        if sv and sv.bot_id == row.id:
            await s.delete(sv)
            await s.commit()
    await cb.answer("حذف شد")
    await _services_after_delete(cb, bot)


async def _services_after_delete(cb, bot):
    row = await _guard(cb, bot)
    svcs = await E.list_services(row.id, only_active=False)
    rows = [[_btn(f"{'🟢' if sv.active else '⚪️'} {sv.name} · {_dur_txt(sv.duration_min)}", f"bka:sv:{sv.id}", ButtonStyle.PRIMARY)] for sv in svcs]
    rows += [[_btn("➕ خدمتِ جدید", "bka:sva", ButtonStyle.SUCCESS)], BACK_CAT]
    await _show(cb, "💈 <b>خدمات</b>", rows)


_EDIT_PROMPT = {"name": "✏️ نامِ جدید را بفرست:", "dur": "⏱ مدتِ جدید (دقیقه):", "price": "💰 قیمتِ جدید (تومان؛ ۰ = توافقی):",
                "desc": "📝 توضیحِ جدید (یا <code>-</code> برایِ حذف):"}


@booking_router.callback_query(F.data.startswith("bka:sve:"))
async def bka_sv_edit(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    _, _, sid, fld = cb.data.split(":")
    await state.set_state(BkAdm.sv_edit)
    await state.update_data(sid=int(sid), fld=fld)
    await cb.message.answer(_EDIT_PROMPT[fld], parse_mode="HTML")
    await cb.answer()


@booking_router.message(BkAdm.sv_edit, F.text)
async def bka_sv_edit_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    fld, txt = data["fld"], (msg.text or "").strip()
    async with Session() as s:
        sv = await s.get(BkService, int(data["sid"]))
        if not sv or sv.bot_id != row.id:
            await state.clear()
            return await msg.answer("پیدا نشد.")
        if fld == "name":
            if not (2 <= len(txt) <= 100):
                return await msg.answer("نام ۲ تا ۱۰۰ نویسه باشد.")
            sv.name = txt
        elif fld == "dur":
            n = _num(txt)
            if not n or not (5 <= n <= 600):
                return await msg.answer("عددِ دقیقه (۵ تا ۶۰۰) بفرست.")
            sv.duration_min = n
        elif fld == "price":
            n = _num(txt)
            if n is None or n > 1_000_000_000:
                return await msg.answer("فقط عدد بفرست.")
            sv.price = n
        else:
            sv.description = None if txt in ("-", "—") else txt[:300]
        await s.commit()
    await state.clear()
    await msg.answer("✅ ذخیره شد.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("💈 خدمات", "bka:sv", ButtonStyle.PRIMARY)], BACK_CAT]))


# ───────────────────────── پرسنل ─────────────────────────

@booking_router.callback_query(F.data == "bka:st")
async def bka_staff(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    cfg = await E.get_cfg(row.id)
    staff = await E.list_staff(row.id, only_active=False)
    rows = [[_btn(f"{'🟢' if st.active else '⚪️'} {st.name}", f"bka:st:{st.id}", ButtonStyle.PRIMARY)] for st in staff]
    rows.append([_btn("➕ پرسنلِ جدید", "bka:sta", ButtonStyle.SUCCESS)])
    rows.append([_btn("✅ انتخابِ پرسنل توسطِ مشتری: روشن (زدن = خاموش)" if cfg["use_staff"]
                      else "⬜️ انتخابِ پرسنل توسطِ مشتری: خاموش (زدن = روشن)", "bka:cfgt:bk_use_staff", ButtonStyle.PRIMARY)])
    rows.append(_help_btn("cat"))
    rows.append(BACK_CAT)
    await _show(cb, "👥 <b>پرسنل</b>\n\nاگر مجموعهٔ تو چند متخصص دارد، آن‌ها را اضافه کن و «انتخابِ پرسنل» را روشن کن؛ "
                    "هر نفر می‌تواند ساعتِ کاریِ اختصاصی و خدماتِ مخصوصِ خودش را داشته باشد. اگر تنها هستی، لازم نیست.", rows)
    await cb.answer()


@booking_router.callback_query(F.data == "bka:sta")
async def bka_staff_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await state.set_state(BkAdm.st_name)
    await cb.message.answer("👤 نامِ پرسنل را بفرست:")
    await cb.answer()


@booking_router.message(BkAdm.st_name, F.text)
async def bka_staff_name(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    name = (msg.text or "").strip()
    if not (2 <= len(name) <= 60):
        return await msg.answer("نام ۲ تا ۶۰ نویسه باشد.")
    await state.clear()
    async with Session() as s:
        st = BkStaff(bot_id=row.id, name=name)
        s.add(st)
        await s.commit()
    await msg.answer(f"✅ «{_esc(name)}» اضافه شد. حالا می‌توانی خدماتش را مشخص کنی.", parse_mode="HTML",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("👥 پرسنل", "bka:st", ButtonStyle.PRIMARY)]]))


@booking_router.callback_query(F.data.regexp(r"^bka:st:\d+$"))
async def bka_staff_view(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    sid = int(cb.data.split(":")[2])
    async with Session() as s:
        st = await s.get(BkStaff, sid)
    if not st or st.bot_id != row.id:
        return await cb.answer("پیدا نشد", show_alert=True)
    svcs = await E.list_services(row.id)
    rows = []
    for sv in svcs:
        on = E.staff_does(st, sv.id) and bool(st.service_ids)
        rows.append([_btn(f"{'✅' if on else '⬜️'} {sv.name}", f"bka:stsv:{sid}:{sv.id}")])
    rows.append([_btn("🔁 همهٔ خدمات (پیش‌فرض)", f"bka:stsv:{sid}:0")])
    rows.append([_btn("🕘 ساعتِ کاریِ اختصاصی", f"bka:hrs:{sid}", ButtonStyle.PRIMARY)])
    rows.append([_btn("⚪️ غیرفعال" if st.active else "🟢 فعال", f"bka:stt:{sid}", ButtonStyle.PRIMARY), _btn("🗑 حذف", f"bka:std:{sid}", ButtonStyle.DANGER)])
    rows.append([_btn("⬅️ بازگشت", "bka:st", ButtonStyle.DANGER)])
    await _show(cb, f"👤 <b>{_esc(st.name)}</b>\nخدماتی که انجام می‌دهد (خالی = همه):", rows)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:stsv:"))
async def bka_staff_svc(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, sid, vid = cb.data.split(":")
    async with Session() as s:
        st = await s.get(BkStaff, int(sid))
        if st and st.bot_id == row.id:
            if int(vid) == 0:
                st.service_ids = None
            else:
                ids = {int(x) for x in (st.service_ids or "").split(",") if x.strip().isdigit()}
                ids ^= {int(vid)}
                st.service_ids = ",".join(str(i) for i in sorted(ids)) or None
            await s.commit()
    await bka_staff_view(cb, bot)


@booking_router.callback_query(F.data.startswith("bka:stt:"))
async def bka_staff_toggle(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    async with Session() as s:
        st = await s.get(BkStaff, int(cb.data.split(":")[2]))
        if st and st.bot_id == row.id:
            st.active = not st.active
            await s.commit()
    await bka_staff_view(cb, bot)


@booking_router.callback_query(F.data.startswith("bka:std:"))
async def bka_staff_del(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    parts = cb.data.split(":")
    sid = int(parts[2])
    if len(parts) == 3:
        return await _show(cb, "❓ این پرسنل حذف شود؟",
                           [[_btn("✅ بله", f"bka:std:{sid}:y", ButtonStyle.DANGER)], [_btn("⬅️ نه", f"bka:st:{sid}", ButtonStyle.PRIMARY)]])
    async with Session() as s:
        st = await s.get(BkStaff, sid)
        if st and st.bot_id == row.id:
            await s.delete(st)
            for h in (await s.scalars(select(BkHours).where(BkHours.bot_id == row.id, BkHours.staff_id == sid))).all():
                await s.delete(h)
            await s.commit()
    await cb.answer("حذف شد")
    await _show(cb, "👥 پرسنل حذف شد.", [[_btn("👥 پرسنل", "bka:st", ButtonStyle.PRIMARY)], BACK_CAT])


# ───────────────────────── ساعتِ کاری ─────────────────────────
# staff_id=0 ⇒ کلِ مجموعه؛ bka:hr (مجموعه) / bka:hrs:{staff} (پرسنل)

async def _hours_text(bot_id: int, staff_id: int) -> str:
    async with Session() as s:
        hs = (await s.scalars(select(BkHours).where(BkHours.bot_id == bot_id, BkHours.staff_id == staff_id)
                              .order_by(BkHours.weekday, BkHours.start_min))).all()
    by: dict[int, list[str]] = {}
    for h in hs:
        by.setdefault(h.weekday, []).append(f"{E.fmt_hm(h.start_min)}–{E.fmt_hm(h.end_min)}")
    lines = [f"{E.WEEKDAY_FA[i]}: " + ("، ".join(by[i]) if i in by else "تعطیل") for i in range(7)]
    return "\n".join(lines)


async def _hours_home(cb, staff_id: int, bot_id: int):
    who = "کلِ مجموعه" if not staff_id else "این پرسنل"
    text = f"🕘 <b>ساعتِ کاری — {who}</b>\n\n{await _hours_text(bot_id, staff_id)}\n\nبرایِ ویرایشِ یک روز روی آن بزن:"
    rows = [[_btn(f"⚡️ تنظیمِ سریع (چند روز با هم)", f"bka:hq:{staff_id}", ButtonStyle.SUCCESS)]]
    line = []
    for i in range(7):
        line.append(_btn(E.WEEKDAY_FA[i], f"bka:hd:{staff_id}:{i}", ButtonStyle.PRIMARY))
        if len(line) == 3:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append(_help_btn("tm"))
    rows.append([_btn("⬅️ بازگشت", f"bka:st:{staff_id}" if staff_id else "bka:tm", ButtonStyle.DANGER)])
    await _show(cb, text, rows)


@booking_router.callback_query(F.data == "bka:hr")
async def bka_hours(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    await _hours_home(cb, 0, row.id)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:hrs:"))
async def bka_hours_staff(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    await _hours_home(cb, int(cb.data.split(":")[2]), row.id)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:hd:"))
async def bka_hours_day(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    _, _, stf, wd = cb.data.split(":")
    stf, wd = int(stf), int(wd)
    async with Session() as s:
        hs = (await s.scalars(select(BkHours).where(BkHours.bot_id == row.id, BkHours.staff_id == stf, BkHours.weekday == wd)
                              .order_by(BkHours.start_min))).all()
    rows = [[_btn(f"🗑 {E.fmt_hm(h.start_min)}–{E.fmt_hm(h.end_min)}", f"bka:hx:{h.id}:{stf}:{wd}", ButtonStyle.DANGER)] for h in hs]
    rows.append([_btn("➕ افزودنِ بازه (برایِ استراحت دو بازه بگذار)", f"bka:ha:{stf}:{wd}", ButtonStyle.SUCCESS)])
    if hs:
        rows.append([_btn("🧹 تعطیل‌کردنِ این روز", f"bka:hc:{stf}:{wd}", ButtonStyle.DANGER)])
    rows.append([_btn("⬅️ بازگشت", f"bka:hrs:{stf}" if stf else "bka:hr", ButtonStyle.DANGER)])
    await _show(cb, f"🕘 <b>{E.WEEKDAY_FA[wd]}</b>\n" + ("\n".join(f"{E.fmt_hm(h.start_min)}–{E.fmt_hm(h.end_min)}" for h in hs) or "تعطیل"), rows)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:hx:"))
async def bka_hours_del(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, hid, stf, wd = cb.data.split(":")
    async with Session() as s:
        h = await s.get(BkHours, int(hid))
        if h and h.bot_id == row.id:
            await s.delete(h)
            await s.commit()
    await bka_hours_day(_CbData(cb, f"bka:hd:{stf}:{wd}"), bot, state)


class _CbData:
    def __init__(self, cb, data):
        self.from_user, self.message, self.data = cb.from_user, cb.message, data
        self._cb = cb

    async def answer(self, *a, **k):
        return await self._cb.answer(*a, **k)


@booking_router.callback_query(F.data.startswith("bka:hc:"))
async def bka_hours_clear(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, stf, wd = cb.data.split(":")
    async with Session() as s:
        for h in (await s.scalars(select(BkHours).where(BkHours.bot_id == row.id, BkHours.staff_id == int(stf), BkHours.weekday == int(wd)))).all():
            await s.delete(h)
        await s.commit()
    await bka_hours_day(_CbData(cb, f"bka:hd:{stf}:{wd}"), bot, state)


@booking_router.callback_query(F.data.startswith("bka:ha:"))
async def bka_hours_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    _, _, stf, wd = cb.data.split(":")
    await state.set_state(BkAdm.hr_start)
    await state.update_data(stf=int(stf), wd=int(wd))
    await cb.message.answer("🕘 <b>ساعتِ شروع</b> را بفرست (مثلاً <code>09:00</code>):", parse_mode="HTML")
    await cb.answer()


@booking_router.message(BkAdm.hr_start, F.text)
async def bka_hours_start(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    m = E.parse_hm(msg.text)
    if m is None or m >= 24 * 60:
        return await msg.answer("ساعت نامعتبر؛ مثل <code>09:00</code> بفرست.", parse_mode="HTML")
    await state.update_data(hs=m)
    await state.set_state(BkAdm.hr_end)
    await msg.answer("🕔 <b>ساعتِ پایان</b> را بفرست (مثلاً <code>17:00</code>):", parse_mode="HTML")


@booking_router.message(BkAdm.hr_end, F.text)
async def bka_hours_end(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    m = E.parse_hm(msg.text)
    if m is None or m <= int(data["hs"]):
        return await msg.answer("پایان باید بعد از شروع باشد؛ دوباره بفرست.")
    async with Session() as s:
        s.add(BkHours(bot_id=row.id, staff_id=int(data["stf"]), weekday=int(data["wd"]), start_min=int(data["hs"]), end_min=m))
        await s.commit()
    await state.clear()
    stf, wd = int(data["stf"]), int(data["wd"])
    await msg.answer(f"✅ {E.WEEKDAY_FA[wd]}: {E.fmt_hm(int(data['hs']))}–{E.fmt_hm(m)} اضافه شد.",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🕘 ساعت کاری", f"bka:hrs:{stf}" if stf else "bka:hr", ButtonStyle.PRIMARY)]]))


# تنظیمِ سریع: چند روز با یک بازه
QUICK = {"a": ("شنبه تا چهارشنبه", [0, 1, 2, 3, 4]), "b": ("شنبه تا پنج‌شنبه", [0, 1, 2, 3, 4, 5]), "c": ("همهٔ روزها", [0, 1, 2, 3, 4, 5, 6])}


@booking_router.callback_query(F.data.startswith("bka:hq:"))
async def bka_hours_quick(cb: CallbackQuery, bot: Bot):
    if not await _guard(cb, bot):
        return
    stf = cb.data.split(":")[2]
    rows = [[_btn(f"{lbl}", f"bka:hqs:{stf}:{k}", ButtonStyle.PRIMARY)] for k, (lbl, _d) in QUICK.items()]
    rows.append([_btn("⬅️ بازگشت", f"bka:hrs:{stf}" if int(stf) else "bka:hr", ButtonStyle.DANGER)])
    await _show(cb, "⚡️ <b>تنظیمِ سریع</b>\nروزهایِ کاری کدام‌اند؟ (برنامهٔ قبلیِ همین روزها جایگزین می‌شود)", rows)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:hqs:"))
async def bka_hours_quick_days(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    _, _, stf, k = cb.data.split(":")
    await state.set_state(BkAdm.q_start)
    await state.update_data(stf=int(stf), qk=k)
    await cb.message.answer(f"🕘 {QUICK[k][0]} — <b>ساعتِ شروع</b> (مثلاً <code>09:00</code>):", parse_mode="HTML")
    await cb.answer()


@booking_router.message(BkAdm.q_start, F.text)
async def bka_quick_start(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    m = E.parse_hm(msg.text)
    if m is None or m >= 24 * 60:
        return await msg.answer("ساعت نامعتبر؛ مثل <code>09:00</code> بفرست.", parse_mode="HTML")
    await state.update_data(hs=m)
    await state.set_state(BkAdm.q_end)
    await msg.answer("🕔 <b>ساعتِ پایان</b> (مثلاً <code>17:00</code>):", parse_mode="HTML")


@booking_router.message(BkAdm.q_end, F.text)
async def bka_quick_end(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    m = E.parse_hm(msg.text)
    if m is None or m <= int(data["hs"]):
        return await msg.answer("پایان باید بعد از شروع باشد؛ دوباره بفرست.")
    days = QUICK[data["qk"]][1]
    stf = int(data["stf"])
    async with Session() as s:
        # «ویرایشِ درجا» به‌جایِ حذف و ساختِ دوباره: ردیفِ موجودِ هر روز به‌روز می‌شود، ردیفِ اضافه (استراحت) حذف و فقط
        # برایِ روزهایِ بدونِ ردیف ردیفِ تازه ساخته می‌شود (سریع‌تر و بدونِ تغییرِ شناسه‌ها).
        existing: dict[int, list] = {}
        for h in (await s.scalars(select(BkHours).where(BkHours.bot_id == row.id, BkHours.staff_id == stf, BkHours.weekday.in_(days))
                                  .order_by(BkHours.weekday, BkHours.id))).all():
            existing.setdefault(h.weekday, []).append(h)
        for wd in days:
            rows_ = existing.get(wd, [])
            if rows_:
                rows_[0].start_min, rows_[0].end_min = int(data["hs"]), m
                for extra in rows_[1:]:
                    await s.delete(extra)
            else:
                s.add(BkHours(bot_id=row.id, staff_id=stf, weekday=wd, start_min=int(data["hs"]), end_min=m))
        await s.commit()
    await state.clear()
    await msg.answer(f"✅ {QUICK[data['qk']][0]}: {E.fmt_hm(int(data['hs']))}–{E.fmt_hm(m)} تنظیم شد.\n"
                     "برایِ استراحتِ بینِ ساعت (مثلاً ناهار) روزِ مورد نظر را باز کن و دو بازه بگذار.",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🕘 ساعت کاری", f"bka:hrs:{stf}" if stf else "bka:hr", ButtonStyle.PRIMARY)]]))


# ───────────────────────── روزهایِ تعطیل ─────────────────────────

@booking_router.callback_query(F.data == "bka:of")
async def bka_offs(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    today = E.iran_now().date().isoformat()
    async with Session() as s:
        offs = (await s.scalars(select(BkOff).where(BkOff.bot_id == row.id, BkOff.staff_id == 0, BkOff.date_str >= today)
                                .order_by(BkOff.date_str).limit(20))).all()
    rows = [[_btn(f"🗑 {E.fa_date(_dt.date.fromisoformat(o.date_str))}" + (f" — {o.reason}" if o.reason else ""), f"bka:ofx:{o.id}", ButtonStyle.DANGER)]
            for o in offs]
    rows.append([_btn("➕ تعطیلیِ جدید", "bka:ofa", ButtonStyle.SUCCESS)])
    rows.append(_help_btn("tm"))
    rows.append(BACK_TM)
    await _show(cb, "🚫 <b>روزهایِ تعطیل</b>\nدر این روزها هیچ نوبتی گرفته نمی‌شود. برایِ حذف روی هر مورد بزن:" if offs
                else "🚫 <b>روزهایِ تعطیل</b>\n\nتعطیلیِ ثبت‌شده‌ای نیست.", rows)
    await cb.answer()


@booking_router.callback_query(F.data == "bka:ofa")
async def bka_off_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    await state.set_state(BkAdm.of_date)
    await cb.message.answer("📅 <b>تاریخِ شمسی</b> را بفرست (مثلاً <code>1405/09/01</code> یا <code>09/01</code>):", parse_mode="HTML")
    await cb.answer()


@booking_router.message(BkAdm.of_date, F.text)
async def bka_off_date(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard_msg(msg, bot):
        return
    d = E.parse_fa_date(msg.text)
    if not d or d < E.iran_now().date():
        return await msg.answer("تاریخ نامعتبر یا گذشته است؛ مثل <code>1405/09/01</code> بفرست.", parse_mode="HTML")
    await state.update_data(od=d.isoformat())
    await state.set_state(BkAdm.of_reason)
    await msg.answer("📝 دلیل (اختیاری، مثلاً «تعطیلِ رسمی») یا <code>-</code>:", parse_mode="HTML")


@booking_router.message(BkAdm.of_reason, F.text)
async def bka_off_reason(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    data = await state.get_data()
    reason = (msg.text or "").strip()
    async with Session() as s:
        s.add(BkOff(bot_id=row.id, staff_id=0, date_str=data["od"], reason=None if reason in ("-", "—") else reason[:100]))
        await s.commit()
    await state.clear()
    # هشدار اگر نوبتِ فعال در آن روز هست
    async with Session() as s:
        n = int(await s.scalar(select(func.count()).select_from(BkAppt).where(
            BkAppt.bot_id == row.id, BkAppt.date_str == data["od"], BkAppt.status.in_(E.ACTIVE_STATUSES))) or 0)
    warn = f"\n\n⚠️ <b>{n}</b> نوبتِ فعال در این روز هست؛ آن‌ها خودکار لغو نمی‌شوند، دستی با مشتری هماهنگ کن." if n else ""
    await msg.answer(f"✅ {E.fa_date(_dt.date.fromisoformat(data['od']))} تعطیل شد.{warn}", parse_mode="HTML",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("🚫 روزهایِ تعطیل", "bka:of", ButtonStyle.PRIMARY)]]))


@booking_router.callback_query(F.data.startswith("bka:ofx:"))
async def bka_off_del(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    async with Session() as s:
        o = await s.get(BkOff, int(cb.data.split(":")[2]))
        if o and o.bot_id == row.id:
            await s.delete(o)
            await s.commit()
    await bka_offs(cb, bot, state)


# ───────────────────────── تنظیمات ─────────────────────────

_TOGGLES = {"bk_auto_confirm": ("تأییدِ خودکارِ نوبت‌ها", "نوبت‌ها بدونِ تأییدِ شما قطعی می‌شوند", "هر نوبت باید توسطِ شما تأیید شود"),
            "bk_use_staff": ("انتخابِ پرسنل توسطِ مشتری", "مشتری پرسنل انتخاب می‌کند", "بدونِ پرسنل (یک مجموعه)")}
_NUMS = {"bk_slot_step": ("فاصلهٔ شروعِ نوبت‌ها (دقیقه)", "مثلاً 30"),
         "bk_lead_min": ("حداقل فاصلهٔ رزرو تا شروعِ نوبت (دقیقه)", "مثلاً 60"),
         "bk_days_ahead": ("تا چند روزِ آینده قابلِ رزرو باشد", "مثلاً 14"),
         "bk_cancel_hours": ("مهلتِ لغو توسطِ مشتری (ساعت قبل از نوبت)", "مثلاً 12"),
         "bk_remind1_h": ("یادآوریِ اول (ساعت قبل؛ ۰ = خاموش)", "مثلاً 24"),
         "bk_remind2_h": ("یادآوریِ دوم (ساعت قبل؛ ۰ = خاموش)", "مثلاً 2")}
_TEXTS = {"bk_biz_name": "نامِ مجموعه", "bk_biz_address": "آدرس", "bk_biz_phone": "تلفنِ تماس", "bk_welcome": "پیامِ خوش‌آمد (بالایِ لیستِ خدمات)"}


@booking_router.callback_query(F.data == "bka:cfg")
async def bka_cfg(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    cfg = await E.get_cfg(row.id)
    rows = [[_btn(("✅ " if cfg["auto_confirm"] else "⬜️ ") + "تأییدِ خودکارِ نوبت‌ها", "bka:cfgt:bk_auto_confirm", ButtonStyle.PRIMARY)],
            [_btn(("✅ " if cfg["use_staff"] else "⬜️ ") + "انتخابِ پرسنل توسطِ مشتری", "bka:cfgt:bk_use_staff", ButtonStyle.PRIMARY)]]
    cur = {"bk_slot_step": cfg["step"], "bk_lead_min": cfg["lead"], "bk_days_ahead": cfg["days_ahead"], "bk_cancel_hours": cfg["cancel_h"],
           "bk_remind1_h": cfg["remind1"], "bk_remind2_h": cfg["remind2"]}
    for k, (lbl, _ex) in _NUMS.items():
        rows.append([_btn(f"{lbl}: {cur[k]}", f"bka:cfgv:{k}")])
    for k, lbl in _TEXTS.items():
        have = {"bk_biz_name": cfg["biz_name"], "bk_biz_address": cfg["biz_address"], "bk_biz_phone": cfg["biz_phone"], "bk_welcome": cfg["welcome"]}[k]
        rows.append([_btn(f"{'✅' if have else '➕'} {lbl}", f"bka:cfgv:{k}")])
    rows.append(_help_btn("cfg"))
    rows.append(BACK_HOME)
    await _show(cb, "⚙️ <b>تنظیمات رزرو</b>\nروی هر مورد بزن تا تغییر دهی:", rows)
    await cb.answer()


@booking_router.callback_query(F.data.startswith("bka:cfgt:"))
async def bka_cfg_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    from tenant import get_setting, set_setting
    key = cb.data.split(":")[2]
    if key not in _TOGGLES:
        return await cb.answer()
    cur = await get_setting(row.id, key, E.DEFAULTS[key])
    await set_setting(row.id, key, "0" if cur == "1" else "1")
    await cb.answer("تغییر کرد")
    if key == "bk_use_staff":
        return await bka_staff(cb, bot, state)
    await bka_cfg(cb, bot, state)


@booking_router.callback_query(F.data.startswith("bka:cfgv:"))
async def bka_cfg_value(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not await _guard(cb, bot):
        return
    key = cb.data.split(":")[2]
    if key in _NUMS:
        lbl, ex = _NUMS[key]
        await cb.message.answer(f"✏️ {lbl}\nعدد را بفرست ({ex}):")
    elif key in _TEXTS:
        await cb.message.answer(f"✏️ {_TEXTS[key]} را بفرست (یا <code>-</code> برایِ پاک‌کردن):", parse_mode="HTML")
    else:
        return await cb.answer()
    await state.set_state(BkAdm.cfg_val)
    await state.update_data(key=key)
    await cb.answer()


@booking_router.message(BkAdm.cfg_val, F.text)
async def bka_cfg_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard_msg(msg, bot)
    if not row:
        return
    from tenant import set_setting
    key = (await state.get_data())["key"]
    txt = (msg.text or "").strip()
    if key in _NUMS:
        n = _num(txt)
        lim = {"bk_slot_step": (5, 240), "bk_lead_min": (0, 10080), "bk_days_ahead": (1, 60), "bk_cancel_hours": (0, 336),
               "bk_remind1_h": (0, 168), "bk_remind2_h": (0, 168)}[key]
        if n is None or not (lim[0] <= n <= lim[1]):
            return await msg.answer(f"عددی بینِ {lim[0]} و {lim[1]} بفرست.")
        val = str(n)
    else:
        val = "" if txt in ("-", "—") else txt[:300]
    await set_setting(row.id, key, val)
    await state.clear()
    await msg.answer("✅ ذخیره شد.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⚙️ تنظیمات", "bka:cfg", ButtonStyle.PRIMARY)], BACK_HOME]))


# ───────────────────────── آمار ─────────────────────────

@booking_router.callback_query(F.data == "bka:stats")
async def bka_stats(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    now = E.iran_now().date()
    month_ago = (now - _dt.timedelta(days=30)).isoformat()
    async with Session() as s:
        by = dict((await s.execute(select(BkAppt.status, func.count()).where(
            BkAppt.bot_id == row.id, BkAppt.date_str >= month_ago).group_by(BkAppt.status))).all())
        rev = int(await s.scalar(select(func.coalesce(func.sum(BkAppt.price), 0)).where(
            BkAppt.bot_id == row.id, BkAppt.date_str >= month_ago, BkAppt.status.in_(("done", "confirmed")))) or 0)
        top = (await s.execute(select(BkAppt.service_name, func.count()).where(
            BkAppt.bot_id == row.id, BkAppt.date_str >= month_ago, BkAppt.status.in_(("done", "confirmed", "pending")))
            .group_by(BkAppt.service_name).order_by(func.count().desc()).limit(5))).all()
        customers = int(await s.scalar(select(func.count(func.distinct(BkAppt.tg_id))).where(BkAppt.bot_id == row.id, BkAppt.date_str >= month_ago)) or 0)
    total = sum(by.values())
    noshow = by.get("noshow", 0)
    lines = ["📊 <b>آمارِ ۳۰ روزِ اخیر</b>", "",
             f"کلِ نوبت‌ها: <b>{total}</b> · مشتریِ یکتا: <b>{customers}</b>",
             f"🟢 تأییدشده: {by.get('confirmed', 0)} · ✅ انجام‌شده: {by.get('done', 0)} · 🟡 انتظار: {by.get('pending', 0)}",
             f"⚪️ لغو: {by.get('cancelled', 0)} · 🔴 رد: {by.get('rejected', 0)} · ⚠️ عدم حضور: {noshow}",
             f"💰 درآمدِ برآوردی (تأییدشده+انجام‌شده): <b>{rev:,}</b> تومان"]
    if total:
        lines.append(f"نرخِ عدم حضور: {round(100 * noshow / total)}٪")
    if top:
        lines += ["", "🏆 پرطرفدارترین خدمات:"] + [f"• {_esc(n)} — {c}" for n, c in top]
    await _show(cb, "\n".join(lines), [_help_btn("rep"), BACK_HOME])
    await cb.answer()
