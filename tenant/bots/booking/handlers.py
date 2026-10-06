"""
ماژول رزرو نوبت — برای ربات‌های booking

قابلیت‌ها:
  • نمایش اسلات‌های زمانی آزاد
  • رزرو نوبت با تایید ادمین
  • لغو نوبت
  • مشاهده نوبت‌های کاربر
  • پنل ادمین مدیریت نوبت‌ها
"""
import datetime

from aiogram.enums import ButtonStyle  # PATCH_TENANT_ADMIN_COLORS_V1
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)
from sqlalchemy import func, select

from config import Session
from models import (
    Appointment, AppointmentSlot, Bot as BotRow, BotUser, Setting,
)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

booking_router = Router()

WEEKDAY_FA = ["شنبه", "یک‌شنبه", "دو‌شنبه", "سه‌شنبه", "چهار‌شنبه", "پنج‌شنبه", "جمعه"]


async def get_bot_row_by_token(token: str) -> BotRow | None:
    async with Session() as s:
        return await s.scalar(select(BotRow).where(BotRow.token == token))


class BookFSM(StatesGroup):
    choose_slot = State()
    enter_note = State()
    add_slot_weekday = State()
    add_slot_time = State()
    add_slot_capacity = State()


# ─────────────────── نمایش اسلات‌ها ───────────────────

def slots_kb(slots, today_weekday: int) -> InlineKeyboardMarkup:
    rows = []
    for sl in slots:
        wd = WEEKDAY_FA[sl.weekday]
        rows.append([InlineKeyboardButton(
            text=f"📅 {wd} — {sl.time_str} (ظرفیت: {sl.capacity})",
            callback_data=f"book:slot:{sl.id}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="book:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@booking_router.callback_query(F.data == "book:back")
async def book_back(cb: CallbackQuery, bot: Bot):
    """بازگشت از صفحات رزرو (لیست اسلات‌ها/نوبت‌های من) به منوی اصلیِ ربات."""
    from tenant import get_bot_row, main_inline_kb, main_kb_or_remove, get_setting
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    if await get_setting(row.id, "menu_mode", "reply") == "inline":
        ikb = await main_inline_kb(row.id, row.owner_id, cb.from_user.id)
        if ikb:
            try:
                await cb.message.edit_text("🏠 منوی اصلی:", reply_markup=ikb)
            except Exception:
                await cb.message.answer("🏠 منوی اصلی:", reply_markup=ikb)
    else:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        kb = await main_kb_or_remove(row.id, row.owner_id, cb.from_user.id)
        await cb.message.answer("🏠 منوی اصلی:", reply_markup=kb)
    await cb.answer()


@booking_router.callback_query(F.data == "book:show")
async def book_show_slots(cb: CallbackQuery, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await cb.answer()
        return
    async with Session() as s:
        slots = (await s.scalars(
            select(AppointmentSlot).where(
                AppointmentSlot.bot_id == bot_row.id,
                AppointmentSlot.is_active == True
            ).order_by(AppointmentSlot.weekday, AppointmentSlot.time_str)
        )).all()
    if not slots:
        await cb.message.edit_text(
            "📭 هیچ اسلات زمانی‌ای تعریف نشده.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="book:back")]
            ])
        )
        return
    today = datetime.date.today().weekday()
    await cb.message.edit_text(
        "📅 <b>زمان‌های موجود برای رزرو</b>\nروز و ساعت دلخواه را انتخاب کن:",
        reply_markup=slots_kb(slots, today)
    )
    await cb.answer()


@booking_router.callback_query(F.data.startswith("book:slot:"))
async def book_select_slot(cb: CallbackQuery, state: FSMContext, bot: Bot):
    sid = int(cb.data.split(":")[2])
    async with Session() as s:
        sl = await s.get(AppointmentSlot, sid)
    if not sl:
        await cb.answer()
        return
    await state.update_data(slot_id=sid,
                             slot_wd=WEEKDAY_FA[sl.weekday],
                             slot_time=sl.time_str)
    await state.set_state(BookFSM.enter_note)
    await cb.message.answer(
        f"📅 نوبت: <b>{WEEKDAY_FA[sl.weekday]} ساعت {sl.time_str}</b>\n\n"
        f"یادداشت یا توضیحی برای ادمین داری؟ (یا /skip):"
    )
    await cb.answer()


@booking_router.message(BookFSM.enter_note, F.text)
async def book_save(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    note = "" if msg.text.strip() == "/skip" else msg.html_text  # حفظ قالب‌بندی تلگرام
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await state.clear()
        return
    async with Session() as s:
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_row.id,
            BotUser.telegram_id == msg.from_user.id))
    today = datetime.date.today()
    # پیدا کردن تاریخ بعدی با این روز هفته
    slot_wd = data["slot_wd"]
    capacity_full = False
    appt_id = None
    async with Session() as s:
        sl = await s.get(AppointmentSlot, data["slot_id"])
        if not sl or not user:
            await state.clear()
            await msg.answer("این اسلات دیگر در دسترس نیست.")
            return
        # ── رعایت ظرفیت: تعداد نوبت‌های فعالِ همین اسلات (همان روز و ساعت) را می‌شماریم
        #    و اگر به ظرفیت رسیده باشد، اجازهٔ رزرو نمی‌دهیم (جلوگیری از over-booking) ──
        taken = await s.scalar(
            select(func.count()).select_from(Appointment).where(
                Appointment.bot_id == bot_row.id,
                Appointment.time_str == sl.time_str,
                Appointment.date_str == str(today),
                Appointment.status.in_(("pending", "confirmed")),
            )
        ) or 0
        if taken >= sl.capacity:
            capacity_full = True
        else:
            appt = Appointment(
                bot_id=bot_row.id,
                user_id=user.id,
                service_name=f"نوبت {slot_wd} {sl.time_str}",
                date_str=str(today),
                time_str=sl.time_str,
                note=note,
                status="pending"
            )
            s.add(appt)
            await s.commit()
            appt_id = appt.id
    if capacity_full:
        await state.clear()
        from tenant import inline_back_kb
        await msg.answer(
            f"❌ ظرفیت این نوبت (<b>{slot_wd} — {data['slot_time']}</b>) تکمیل شده است.\n"
            "لطفاً یک زمان دیگر انتخاب کن.",
            reply_markup=await inline_back_kb(bot_row.id),
        )
        return
    # اطلاع به ادمین
    admin_text = (
        f"📅 <b>رزرو جدید #{appt_id}</b>\n"
        f"کاربر: {msg.from_user.full_name} (<code>{msg.from_user.id}</code>)\n"
        f"زمان: {slot_wd} — {data['slot_time']}\n"
        f"یادداشت: {note or '—'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید", callback_data=f"book:confirm:{appt_id}"),
        InlineKeyboardButton(text="❌ رد", callback_data=f"book:reject:{appt_id}"),
    ]])
    await bot.send_message(bot_row.owner_id, admin_text, reply_markup=kb)
    await state.clear()
    from tenant import inline_back_kb
    await msg.answer(
        f"✅ درخواست رزرو ثبت شد!\n"
        f"زمان: <b>{slot_wd} — {data['slot_time']}</b>\n"
        f"بعد از تایید ادمین اطلاع‌رسانی می‌شود.",
        reply_markup=await inline_back_kb(bot_row.id),
    )


@booking_router.callback_query(F.data.startswith("book:confirm:"))
async def book_confirm(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[2])
    async with Session() as s:
        appt = await s.get(Appointment, aid)
        if not appt:
            await cb.answer()
            return
        user = await s.get(BotUser, appt.user_id)
        appt.status = "confirmed"
        await s.commit()
    await cb.message.edit_reply_markup()
    await cb.message.answer(f"✅ نوبت #{aid} تایید شد.")
    await bot.send_message(
        user.telegram_id,
        f"✅ نوبت شما تایید شد!\n⏰ {appt.service_name}"
    )
    await cb.answer()


@booking_router.callback_query(F.data.startswith("book:reject:"))
async def book_reject(cb: CallbackQuery, bot: Bot):
    aid = int(cb.data.split(":")[2])
    async with Session() as s:
        appt = await s.get(Appointment, aid)
        if not appt:
            await cb.answer()
            return
        user = await s.get(BotUser, appt.user_id)
        appt.status = "cancelled"
        await s.commit()
    await cb.message.edit_reply_markup()
    await cb.message.answer(f"❌ نوبت #{aid} رد شد.")
    await bot.send_message(user.telegram_id, "❌ متأسفانه نوبت درخواستی شما تایید نشد.")
    await cb.answer()


@booking_router.callback_query(F.data == "book:my")
async def book_my_appointments(cb: CallbackQuery, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await cb.answer()
        return
    async with Session() as s:
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_row.id,
            BotUser.telegram_id == cb.from_user.id))
        if not user:
            await cb.answer()
            return
        appts = (await s.scalars(
            select(Appointment).where(
                Appointment.bot_id == bot_row.id,
                Appointment.user_id == user.id
            ).order_by(Appointment.created_at.desc()).limit(10)
        )).all()
    if not appts:
        await cb.message.edit_text(
            "📭 هنوز نوبتی رزرو نکردی.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="book:back")]
            ])
        )
        return
    status_map = {
        "pending": "⏳ در انتظار تایید",
        "confirmed": "✅ تایید شده",
        "cancelled": "❌ لغو شده",
        "done": "☑️ انجام شده",
    }
    lines = []
    for a in appts:
        lines.append(f"• {a.service_name} — {status_map.get(a.status, a.status)}")
    await cb.message.edit_text(
        "📅 <b>نوبت‌های من</b>\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="book:back")]
        ])
    )
    await cb.answer()


# ─────────────────── پنل ادمین رزرو ───────────────────

def booking_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 نوبت‌های در انتظار", callback_data="badm:pending", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="📅 مدیریت اسلات‌ها", callback_data="badm:slots", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:products", style=ButtonStyle.DANGER),
         InlineKeyboardButton(text="🏠 پنل مدیریت", callback_data="tadm:menu", style=ButtonStyle.DANGER)],
    ])


@booking_router.callback_query(F.data == "tadm:booking")
async def badm_menu(cb: CallbackQuery, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or cb.from_user.id != bot_row.owner_id:
        await cb.answer()
        return
    await cb.message.edit_text("📅 <b>مدیریت رزرو</b>", reply_markup=booking_admin_kb())
    await cb.answer()


@booking_router.callback_query(F.data == "badm:pending")
async def badm_pending(cb: CallbackQuery, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or cb.from_user.id != bot_row.owner_id:
        await cb.answer()
        return
    async with Session() as s:
        appts = (await s.scalars(
            select(Appointment).where(
                Appointment.bot_id == bot_row.id,
                Appointment.status == "pending"
            ).order_by(Appointment.created_at)
        )).all()
    if not appts:
        await cb.message.edit_text(
            "✅ هیچ نوبت در انتظاری وجود ندارد.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:booking", style=ButtonStyle.DANGER)]
            ])
        )
        return
    rows = []
    for a in appts:
        rows.append([
            InlineKeyboardButton(text=f"✅ #{a.id} {a.service_name}", callback_data=f"book:confirm:{a.id}"),
            InlineKeyboardButton(text="❌", callback_data=f"book:reject:{a.id}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:booking", style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        f"⏳ <b>نوبت‌های در انتظار ({len(appts)})</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()


@booking_router.callback_query(F.data == "badm:slots")
async def badm_slots(cb: CallbackQuery, bot: Bot):
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row or cb.from_user.id != bot_row.owner_id:
        await cb.answer()
        return
    async with Session() as s:
        slots = (await s.scalars(
            select(AppointmentSlot).where(AppointmentSlot.bot_id == bot_row.id)
            .order_by(AppointmentSlot.weekday, AppointmentSlot.time_str)
        )).all()
    rows = []
    for sl in slots:
        st = "🟢" if sl.is_active else "🔴"
        rows.append([InlineKeyboardButton(
            text=f"{st} {WEEKDAY_FA[sl.weekday]} {sl.time_str} (ظ:{sl.capacity})",
            callback_data=f"badm:slot:toggle:{sl.id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="➕ اسلات جدید", callback_data="badm:slot:add", style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:booking", style=ButtonStyle.DANGER)])
    await cb.message.edit_text(
        "📅 <b>اسلات‌های زمانی</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()


@booking_router.callback_query(F.data.startswith("badm:slot:toggle:"))
async def badm_slot_toggle(cb: CallbackQuery, bot: Bot):
    sid = int(cb.data.split(":")[3])
    async with Session() as s:
        sl = await s.get(AppointmentSlot, sid)
        if sl:
            sl.is_active = not sl.is_active
            await s.commit()
    await badm_slots(cb, bot)


@booking_router.callback_query(F.data == "badm:slot:add")
async def badm_slot_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.add_slot_weekday)
    wd_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=d, callback_data=f"badm:slot:wd:{i}", style=ButtonStyle.PRIMARY)]
        for i, d in enumerate(WEEKDAY_FA)
    ])
    await cb.message.answer("روز هفته را انتخاب کن:", reply_markup=wd_kb)
    await cb.answer()


@booking_router.callback_query(BookFSM.add_slot_weekday, F.data.startswith("badm:slot:wd:"))
async def badm_slot_wd(cb: CallbackQuery, state: FSMContext):
    wd = int(cb.data.split(":")[3])
    await state.update_data(slot_wd=wd)
    await state.set_state(BookFSM.add_slot_time)
    await cb.message.answer("ساعت را وارد کن (مثال: 14:30):")
    await cb.answer()


@booking_router.message(BookFSM.add_slot_time, F.text)
async def badm_slot_time(msg: Message, state: FSMContext):
    await state.update_data(slot_time=msg.text.strip())
    await state.set_state(BookFSM.add_slot_capacity)
    await msg.answer("ظرفیت هر اسلات را وارد کن (پیش‌فرض: 1):")


@booking_router.message(BookFSM.add_slot_capacity, F.text)
async def badm_slot_save(msg: Message, state: FSMContext, bot: Bot):
    try:
        cap = int(msg.text.strip())
    except ValueError:
        cap = 1
    data = await state.get_data()
    bot_row = await get_bot_row_by_token(bot.token)
    if not bot_row:
        await state.clear()
        return
    async with Session() as s:
        s.add(AppointmentSlot(
            bot_id=bot_row.id,
            weekday=data["slot_wd"],
            time_str=data["slot_time"],
            capacity=cap
        ))
        await s.commit()
    await state.clear()
    await msg.answer(f"✅ اسلات {WEEKDAY_FA[data['slot_wd']]} {data['slot_time']} اضافه شد.")
