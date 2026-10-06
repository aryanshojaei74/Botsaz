"""
platform_tickets.py — بخشِ «سیستم تیکت» جدا از platform_bot.py.

استخراج‌شده از platform_bot.py (~۶۰۰ خطِ سکشن + کلاسِ TicketFSM +
هندلر ticket_back_handler) به فایلِ مستقل تا platform_bot.py سبک‌تر بشه.
رفتار ۱۰۰٪ حفظ شده — همون هندلرها، همون callback_dataها، همون FSM.
"""
from __future__ import annotations


from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import (
    CallbackQuery, InlineKeyboardMarkup, Message,
    ReplyKeyboardRemove,
)
from sqlalchemy import select, func

from config import Session, PLATFORM_ADMIN_ID
from models import PlatformUser, Ticket, TicketReply
from premium_emoji import hx_button

from bot_utils import has_media  # === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

# PATCH_TICKET_BUILDER_IMPORT_V1
ticket_router = Router()


# ────────── lazy-import wrappers از platform_bot ──────────

async def smart_cancel_kb():
    from platform_bot import smart_cancel_kb as _fn
    return await _fn()


async def smart_home_kb(uid: int, show_admin: bool = False):
    from platform_bot import smart_home_kb as _fn
    return await _fn(uid, show_admin=show_admin)


# === PATCH_TICKETS_MISSING_HELPERS_V1 ===
# این دو helper توی خودِ platform_bot.py تعریف شدن ولی هنگام استخراج
# فراموش شدن که lazy wrapper بگیرن → NameError موقعِ کلیک روی «پاسخ به تیکت»
# (خطای ERR-164701 دقیقاً به همین دلیل بود).

async def smart_ticket_cancel_kb():
    """Lazy wrapper — کیبورد «لغو تیکت» رو از platform_bot می‌گیره."""
    from platform_bot import smart_ticket_cancel_kb as _fn
    return await _fn()


def to_jalali(dt):
    """Lazy wrapper — تبدیل به شمسی."""
    from platform_bot import to_jalali as _fn
    return _fn(dt)


def is_admin(uid: int) -> bool:
    from platform_bot import is_admin as _fn
    return _fn(uid)


async def get_sub_admin(s, uid: int):
    from platform_bot import get_sub_admin as _fn
    return await _fn(s, uid)


async def has_perm(uid: int, perm: str) -> bool:
    from platform_bot import has_perm as _fn
    return await _fn(uid, perm)


# === PATCH_TICKET_SEND_BTN_DELETE_V1 ===
# مثلِ بقیه‌ی wrapperهای بالا — این یکی هم موقعِ استخراج از
# platform_bot.py جا مونده بود و باعثِ NameError موقعِ ارسالِ پاسخِ
# ادمین به تیکت می‌شد.
async def _send_with_retry(bot, chat_id: int, text: str, retries: int = 3, **kwargs) -> bool:
    from platform_bot import _send_with_retry as _fn
    return await _fn(bot, chat_id, text, retries=retries, **kwargs)


BTN_CANCEL = "❌ لغو"
# PATCH_SPLIT_TICKETS_V1 — همون مقداری که توی platform_bot.py هست، باید کاملاً
# یکسان باشه چون هندلر با F.text == BTN_TICKET_BACK متن رو تطبیق می‌ده و اگه
# فرق داشته باشه، دکمه‌های ری‌پلای که از پلتفرم‌بات ارسال می‌شن، این هندلر رو
# صدا نمی‌زنن.
BTN_TICKET_BACK = "⬅️ بازگشت به تیکت‌ها"


class TicketFSM(StatesGroup):
    new_text = State()
    new_photo = State()   # منتظرِ عکس/اسکرین‌شاتِ ضمیمه (اختیاری)
    edit_text = State()
    user_reply = State()
    admin_reply = State()


@ticket_router.message(F.text == BTN_TICKET_BACK)
async def ticket_back_handler(msg: Message, state: FSMContext):
    current_state = await state.get_state()
    await state.clear()
    # اگر ادمین بود، به صفحه تیکت‌های ادمین برگردد
    if current_state == TicketFSM.admin_reply and (is_admin(msg.from_user.id)):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [hx_button("🟠 بی‌پاسخ", callback_data="atk:list:open")],
            [hx_button("🟢 پاسخ‌داده", callback_data="atk:list:answered")],
            [hx_button("🔒 بسته", callback_data="atk:list:closed")],
            [hx_button("⬅️ بازگشت به پنل", callback_data="adm:back", style=ButtonStyle.DANGER)],
        ])
        await msg.answer("🎫 تیکت‌ها — وضعیت را انتخاب کن:", reply_markup=ReplyKeyboardRemove())
        await msg.answer("🎫 تیکت‌ها — وضعیت را انتخاب کن:", reply_markup=kb)
    else:
        # کاربر عادی، برگشت به منوی تیکت
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [hx_button("➕ تیکت جدید", callback_data="tk:new", style=ButtonStyle.SUCCESS)],
            [hx_button("📨 تیکت‌های من", callback_data="tk:mine", style=ButtonStyle.PRIMARY)],
            [hx_button("⬅️ بازگشت", callback_data="tk:back_main", style=ButtonStyle.DANGER)],
        ])
        await msg.answer("🎫 <b>پشتیبانی</b>\nیکی را انتخاب کن:", reply_markup=ReplyKeyboardRemove())
        await msg.answer("🎫 <b>پشتیبانی</b>\nیکی را انتخاب کن:", reply_markup=kb)



# ======================= سیستم تیکت (کاربر + ادمین) =======================
TK_STATUS = {"open": "🟠 بی‌پاسخ", "answered": "🟢 پاسخ‌داده", "closed": "🔒 بسته"}


async def render_ticket(tid: int, viewer_admin: bool = False):
    async with Session() as s:
        t = await s.get(Ticket, tid)
        if not t:
            return None, None, None
        reps = (await s.scalars(
            select(TicketReply).where(TicketReply.ticket_id == tid).order_by(TicketReply.id))).all()
        owner, status, text, photo_file_id = t.user_telegram_id, t.status, t.text, t.photo_file_id
        created_at = t.created_at
        pu = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == owner))
    full_name = (pu.full_name if pu else None) or "بدون نام"
    when = to_jalali(created_at)
    # === PATCH_TICKET_SHOW_USERNAME_V1 ===
    # قبلاً فقط آیدیِ عددی می‌آمد و پشتیبانی برایِ پیام‌دادن به کاربر باید
    # جدا دنبالِ یوزرنیمش می‌گشت. اگر کاربر یوزرنیم نداشته باشد، همان خطِ
    # قبلی نمایش داده می‌شود.
    uname = (pu.username if pu else None) or ""
    uname_line = (f"\n🔗 یوزرنیم: @{uname}" if uname else "\n🔗 یوزرنیم: —")
    lines = [f"🎫 تیکت #{tid} — {TK_STATUS.get(status, status)}\n"]
    if viewer_admin:
        lines.append(f"👤 {full_name}\n🆔 آیدی: <code>{owner}</code>{uname_line}"
                     f"\n🗓 تاریخ ارسال: {when}\n")
    lines.append(f"📝 {text}")
    for r in reps:
        if r.sender == "admin":
            who = "🛠 پشتیبانی"
        else:
            who = "👤 کاربر" if viewer_admin else "👤 شما"
        photo_note = "\n📷 (عکس پیوست — در چت اصلی ارسال شده)" if getattr(r, "photo_file_id", None) else ""
        lines.append(f"\n{who}:\n{r.text}{photo_note}")
    return "\n".join(lines), (owner, status), photo_file_id


# ---- کاربر ----
@ticket_router.callback_query(F.data == "tk:new")
async def tk_new(cb: CallbackQuery, state: FSMContext):
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("📷 بله، عکس/اسکرین‌شات هم می‌فرستم", callback_data="tk:withphoto")],
        [hx_button("⏭ نه، فقط متن می‌نویسم", callback_data="tk:textonly")],
        [hx_button("⬅️ بازگشت", callback_data="tk:menu", style=ButtonStyle.DANGER)],
        [hx_button("🏠 بازگشت به خانه", callback_data="tk:back_main", style=ButtonStyle.DANGER)],
    ])
    await cb.message.answer(
        "🎫 <b>ثبت تیکت جدید</b>\n\nمی‌خوای همراهِ تیکتت یه عکس یا اسکرین‌شات هم بفرستی؟",
        reply_markup=kb)
    await cb.answer()


@ticket_router.callback_query(F.data == "tk:textonly")
async def tk_new_textonly(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TicketFSM.new_text)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("✍️ متن تیکت خود را بفرست:", reply_markup=await smart_ticket_cancel_kb())
    await cb.answer()


@ticket_router.callback_query(F.data == "tk:withphoto")
async def tk_new_withphoto(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TicketFSM.new_photo)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "📷 عکس/اسکرین‌شات رو بفرست.\n"
        "می‌تونی توضیحِ متنیِ تیکت رو هم زیرِ همون عکس (به‌عنوان caption) بنویسی — "
        "اگه ننویسی، بعداً ازت متن رو جدا می‌پرسم.",
        reply_markup=await smart_ticket_cancel_kb())
    await cb.answer()


@ticket_router.message(TicketFSM.new_photo, F.photo)
async def tk_photo_received(msg: Message, state: FSMContext, bot: Bot):
    file_id = msg.photo[-1].file_id
    caption = (msg.html_text or "").strip()
    if caption:
        await _save_new_platform_ticket(msg, state, bot, text=caption, photo_file_id=file_id)
        return
    await state.update_data(ticket_photo_id=file_id)
    await state.set_state(TicketFSM.new_text)
    await msg.answer("✍️ حالا متنِ توضیحِ تیکت رو هم بنویس:", reply_markup=await smart_ticket_cancel_kb())


@ticket_router.message(TicketFSM.new_photo, F.text)
async def tk_photo_expected_but_text(msg: Message, state: FSMContext):
    # === PATCH_TICKET_ADMIN_REPLY_EXIT_TEXT_V1 ===
    if (msg.text or "").strip() in (BTN_TICKET_BACK, BTN_CANCEL):
        await ticket_back_handler(msg, state)
        return
    await msg.answer("لطفاً یه عکس/اسکرین‌شات بفرست (یا برای لغو، «❌ لغو» رو بزن):",
                     reply_markup=await smart_ticket_cancel_kb())


async def _save_new_platform_ticket(msg: Message, state: FSMContext, bot: Bot, text: str,
                                     photo_file_id: str | None = None):
    await state.clear()
    if not text:
        await msg.answer("متن خالی بود.", reply_markup=await smart_home_kb(msg.from_user.id))
        return
    async with Session() as s:
        t = Ticket(user_telegram_id=msg.from_user.id, text=text, status="open",
                   photo_file_id=photo_file_id)
        s.add(t)
        await s.flush()
        tid = t.id
        await s.commit()
    await msg.answer(f"✅ تیکت #{tid} ثبت شد. پاسخ از همین‌جا برایت می‌آید.",
                     reply_markup=await smart_home_kb(msg.from_user.id))
    if PLATFORM_ADMIN_ID:
        try:
            kb_adm = InlineKeyboardMarkup(inline_keyboard=[[
                hx_button("↩️ پاسخ به تیکت",
                                     callback_data=f"tk:adm_reply:{tid}")
            ]])
            # === PATCH_TICKET_SHOW_USERNAME_V1 ===
            _un = msg.from_user.username
            _un_txt = f" (@{_un})" if _un else ""
            caption = (f"🎫 <b>تیکت جدید #{tid}</b>\n"
                      f"از کاربر: <code>{msg.from_user.id}</code>{_un_txt}\n"
                      f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n{text}")
            if photo_file_id:
                sent = await bot.send_photo(PLATFORM_ADMIN_ID, photo_file_id,
                                            caption=caption[:1000], reply_markup=kb_adm)
            else:
                sent = await bot.send_message(PLATFORM_ADMIN_ID, caption, reply_markup=kb_adm)
            # ذخیره message_id برای پاسخ از طریق reply
            async with Session() as s:
                t2 = await s.get(Ticket, tid)
                if t2:
                    t2.admin_message_id = sent.message_id
                    await s.commit()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@ticket_router.message(TicketFSM.new_text, F.text)
async def tk_new_save(msg: Message, state: FSMContext, bot: Bot):
    # === PATCH_TICKET_ADMIN_REPLY_EXIT_TEXT_V1 ===
    if (msg.text or "").strip() in (BTN_TICKET_BACK, BTN_CANCEL):
        await ticket_back_handler(msg, state)
        return
    text = msg.html_text.strip()  # حفظ قالب‌بندی تلگرام
    data = await state.get_data()
    photo_id = data.get("ticket_photo_id")
    await _save_new_platform_ticket(msg, state, bot, text=text, photo_file_id=photo_id)



@ticket_router.callback_query(F.data.startswith("tk:adm_reply:"))
async def tk_adm_reply_start(cb: CallbackQuery, state: FSMContext):
    """شروع پاسخ ادمین از طریق دکمه."""
    if not is_admin(cb.from_user.id):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    tid = int(cb.data.split(":")[2])
    await state.set_state(TicketFSM.admin_reply)
    await state.update_data(reply_tid=tid)
    await cb.message.answer(
        f"✏️ پاسخ به تیکت #{tid} را بنویس و ارسال کن (متن یا عکس، هر دو قابل قبوله):",
        reply_markup=await smart_ticket_cancel_kb())
    await cb.answer()


@ticket_router.callback_query(F.data == "tk:cancel_reply")
async def tk_cancel_reply(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("لغو شد")


# === PATCH_TICKET_TWO_WAY_REPLY_V1 ===
# گزارشِ مدیر: «کاربر تیکت می‌زنه، پاسخ می‌دم، دوباره پاسخ می‌ده — دکمهٔ
# پاسخ نمیاد که همین‌طور بتونیم صحبت کنیم».
#
# علت: دکمهٔ «↩️ پاسخ به تیکت» فقط رویِ *اولین* پیامِ تیکت گذاشته می‌شد.
# بعد از آن، هیچ‌کدام از شش نقطهٔ ارسال (پاسخِ ادمین متنی/عکسی، ریپلایِ
# مستقیم متنی/عکسی، و پاسخِ کاربر) هیچ کیبوردی نداشتند. نتیجه: گفتگو
# بعد از یک رفت‌وبرگشت به بن‌بست می‌خورد و هر دو طرف باید از منویِ اصلی
# دوباره واردِ تیکت می‌شدند.
#
# این دو تابع همان دکمه را به هر پیامِ گفتگو می‌چسبانند تا مکالمه پیوسته
# بماند — یکی برایِ کاربر، یکی برایِ ادمین.


def _kb_user_reply(tid: int) -> InlineKeyboardMarkup:
    """دکمهٔ پاسخ برایِ کاربر، زیرِ پیامِ پشتیبانی."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        hx_button("↩️ پاسخ به پشتیبانی", callback_data=f"tk:reply:{tid}")
    ]])


def _kb_admin_reply(tid: int) -> InlineKeyboardMarkup:
    """دکمهٔ پاسخ برایِ ادمین، زیرِ پیامِ کاربر."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        hx_button("↩️ پاسخ به تیکت", callback_data=f"tk:adm_reply:{tid}")
    ]])


@ticket_router.message(TicketFSM.admin_reply, F.text)
async def tk_adm_reply_send(msg: Message, bot: Bot, state: FSMContext):
    """ارسال پاسخ ادمین به کاربر (هم از روی دکمهٔ نوتیف، هم از پنل تیکت‌ها)."""
    # === PATCH_TICKET_ADMIN_REPLY_EXIT_TEXT_V1 ===
    # باگِ گزارش‌شده: ادمین وسطِ نوشتنِ پاسخ، به‌جایِ تایپِ متنِ پاسخ، روی
    # دکمهٔ ری‌پلای-کیبوردِ «⬅️ بازگشت به تیکت‌ها» یا «❌ لغو» می‌زد — و چون
    # این تابع هیچ‌وقت متنِ ورودی رو چک نمی‌کرد که خودِ یکی از این دکمه‌ها
    # نیست، متنِ دکمه عیناً به‌عنوانِ «پاسخِ پشتیبانی» برایِ مشتری فرستاده
    # می‌شد. این‌جا صریحاً همون رفتارِ «بازگشت»/«لغو» رو صدا می‌زنیم به‌جایِ
    # اینکه این متن رو یه پاسخِ واقعی حساب کنیم.
    if (msg.text or "").strip() in (BTN_TICKET_BACK, BTN_CANCEL):
        await ticket_back_handler(msg, state)
        return
    if not is_admin(msg.from_user.id) and not await has_perm(msg.from_user.id, "adm:tickets"):
        await state.clear()
        return
    data = await state.get_data()
    tid  = data.get("reply_tid")
    await state.clear()
    if not tid:
        return
    reply_text = msg.html_text  # حفظ قالب‌بندی تلگرام
    async with Session() as s:
        t = await s.get(Ticket, tid)
        if not t:
            await msg.answer("❌ تیکت یافت نشد.",
                             reply_markup=await smart_home_kb(msg.from_user.id))
            return
        uid = t.user_telegram_id
        t.status = "answered"
        s.add(TicketReply(ticket_id=tid, sender="admin", text=reply_text))
        await s.commit()
    try:
        await _send_with_retry(
            bot, uid,
            f"💬 <b>پاسخ پشتیبانی — تیکت #{tid}</b>\n\n{reply_text}",
            parse_mode="HTML", reply_markup=_kb_user_reply(tid))  # PATCH_TICKET_TWO_WAY_REPLY_V1
        await msg.answer(f"✅ پاسخ به تیکت #{tid} ارسال شد.",
                         reply_markup=await smart_home_kb(msg.from_user.id))
    except Exception as e:
        await msg.answer(f"❌ ارسال ناموفق: {e}",
                         reply_markup=await smart_home_kb(msg.from_user.id))


# PATCH_TICKET_ADMIN_PHOTO_MARKER
@ticket_router.message(TicketFSM.admin_reply, F.photo)
async def tk_adm_reply_send_photo(msg: Message, bot: Bot, state: FSMContext):
    """ارسال پاسخ ادمین به کاربر به‌همراه عکس (نسخهٔ عکس‌دارِ tk_adm_reply_send)."""
    if not is_admin(msg.from_user.id) and not await has_perm(msg.from_user.id, "adm:tickets"):
        await state.clear()
        return
    data = await state.get_data()
    tid = data.get("reply_tid")
    await state.clear()
    if not tid:
        return
    file_id = msg.photo[-1].file_id
    reply_text = msg.html_text if msg.caption else ""  # حفظ قالب‌بندی تلگرام در کپشن
    async with Session() as s:
        t = await s.get(Ticket, tid)
        if not t:
            await msg.answer("❌ تیکت یافت نشد.",
                             reply_markup=await smart_home_kb(msg.from_user.id))
            return
        uid = t.user_telegram_id
        t.status = "answered"
        s.add(TicketReply(ticket_id=tid, sender="admin", text=reply_text, photo_file_id=file_id))
        await s.commit()
    try:
        caption = f"💬 <b>پاسخ پشتیبانی — تیکت #{tid}</b>" + (f"\n\n{reply_text}" if reply_text else "")
        await bot.send_photo(uid, file_id, caption=caption, parse_mode="HTML",
                             reply_markup=_kb_user_reply(tid))  # PATCH_TICKET_TWO_WAY_REPLY_V1
        await msg.answer(f"✅ پاسخ به تیکت #{tid} ارسال شد.",
                         reply_markup=await smart_home_kb(msg.from_user.id))
    except Exception as e:
        await msg.answer(f"❌ ارسال ناموفق: {e}",
                         reply_markup=await smart_home_kb(msg.from_user.id))


@ticket_router.message(F.reply_to_message & F.from_user.id.in_([PLATFORM_ADMIN_ID]) & F.photo)
async def tk_inline_reply_photo(msg: Message, bot: Bot):
    """ادمین مستقیم روی پیام تیکت reply کنه (با عکس) → پاسخ به کاربر ارسال شه."""
    reply_msg_id = msg.reply_to_message.message_id
    async with Session() as s:
        t = await s.scalar(
            select(Ticket).where(Ticket.admin_message_id == reply_msg_id))
        if not t:
            return  # این reply مربوط به تیکت نیست
        uid = t.user_telegram_id
        tid = t.id
        t.status = "answered"
        file_id = msg.photo[-1].file_id
        reply_text = msg.html_text if msg.caption else ""
        s.add(TicketReply(ticket_id=tid, sender="admin", text=reply_text, photo_file_id=file_id))
        await s.commit()
    try:
        caption = f"💬 <b>پاسخ پشتیبانی — تیکت #{tid}</b>" + (f"\n\n{reply_text}" if reply_text else "")
        await bot.send_photo(uid, file_id, caption=caption, parse_mode="HTML",
                             reply_markup=_kb_user_reply(tid))  # PATCH_TICKET_TWO_WAY_REPLY_V1
        await msg.reply(f"✅ پاسخ به تیکت #{tid} برای کاربر ارسال شد.")
    except Exception as e:
        await msg.reply(f"❌ ارسال ناموفق: {e}")


@ticket_router.message(F.reply_to_message & F.from_user.id.in_([PLATFORM_ADMIN_ID]))
async def tk_inline_reply(msg: Message, bot: Bot):
    """ادمین مستقیم روی پیام تیکت reply کنه → پاسخ به کاربر ارسال شه."""
    reply_msg_id = msg.reply_to_message.message_id
    async with Session() as s:
        t = await s.scalar(
            select(Ticket).where(Ticket.admin_message_id == reply_msg_id))
        if not t:
            return  # این reply مربوط به تیکت نیست
        uid = t.user_telegram_id
        tid = t.id
        t.status = "answered"
        s.add(TicketReply(ticket_id=tid, sender="admin", text=msg.text or ""))
        await s.commit()
    try:
        await _send_with_retry(
            bot, uid,
            f"💬 <b>پاسخ پشتیبانی — تیکت #{tid}</b>\n\n{msg.text}",
            parse_mode="HTML", reply_markup=_kb_user_reply(tid))  # PATCH_TICKET_TWO_WAY_REPLY_V1
        await msg.reply(f"✅ پاسخ به تیکت #{tid} برای کاربر ارسال شد.")
    except Exception as e:
        await msg.reply(f"❌ ارسال ناموفق (بعد از چند تلاش): {e}")


@ticket_router.callback_query(F.data == "tk:mine")
async def tk_mine(cb: CallbackQuery):
    async with Session() as s:
        ts = (await s.scalars(
            select(Ticket).where(Ticket.user_telegram_id == cb.from_user.id)
            .order_by(Ticket.id.desc()).limit(15))).all()
    if not ts:
        try:
            await cb.message.edit_text("تیکتی نداری.")
        except Exception:
            try:
                await cb.message.delete()
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            await cb.message.answer("تیکتی نداری.")
        await cb.answer()
        return
    b = InlineKeyboardBuilder()
    for t in ts:
        mark = " 📷" if t.photo_file_id else ""
        b.button(text=f"#{t.id} — {TK_STATUS.get(t.status, t.status)}{mark}", callback_data=f"tk:v:{t.id}")
    b.button(text="⬅️ بازگشت", callback_data="tk:menu", style=ButtonStyle.DANGER)
    b.adjust(1)
    # پیامی که از آن اومدیم ممکنه پیامِ عکس‌دار باشه (وقتی از تیکتِ حاویِ عکس برمی‌گردیم)؛
    # روی پیام عکس‌دار نمی‌شه edit_text کرد، پس اگه خطا داد پیام قبلی رو پاک و پیام جدید می‌فرستیم.
    try:
        await cb.message.edit_text("📨 تیکت‌های تو:", reply_markup=b.as_markup())
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        await cb.message.answer("📨 تیکت‌های تو:", reply_markup=b.as_markup())
    await cb.answer()


@ticket_router.callback_query(F.data.startswith("tk:v:"))
async def tk_view(cb: CallbackQuery):
    tid = int(cb.data.split(":")[2])
    txt, meta, photo_file_id = await render_ticket(tid)
    if not txt:
        await cb.answer("یافت نشد", show_alert=True)
        return
    owner, status = meta
    if owner != cb.from_user.id:
        await cb.answer("این تیکت مال تو نیست", show_alert=True)
        return
    rows = []
    if status != "closed":
        rows.append([
            hx_button("✉️ پاسخ", callback_data=f"tk:reply:{tid}", style=ButtonStyle.PRIMARY),
            hx_button("✏️ ویرایش", callback_data=f"tk:edit:{tid}", style=ButtonStyle.PRIMARY),
        ])
    rows.append([
        hx_button("🗑 حذف", callback_data=f"tk:del:{tid}", style=ButtonStyle.DANGER),
        hx_button("⬅️ بازگشت", callback_data="tk:mine", style=ButtonStyle.DANGER),
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if photo_file_id:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        caption = txt if len(txt) <= 1000 else txt[:970] + "\n\n… (متن کامل کوتاه شد)"
        await cb.message.answer_photo(photo_file_id, caption=caption, reply_markup=kb)
        await cb.answer()
        return
    await cb.message.edit_text(txt, reply_markup=kb)
    await cb.answer()


@ticket_router.callback_query(F.data.startswith("tk:reply:"))
async def tk_reply(cb: CallbackQuery, state: FSMContext):
    tid = int(cb.data.split(":")[2])
    await state.set_state(TicketFSM.user_reply)
    await state.update_data(tid=tid)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("✍️ پاسخت را بفرست:", reply_markup=await smart_ticket_cancel_kb())
    await cb.answer()


@ticket_router.message(TicketFSM.user_reply, F.text)
async def tk_reply_save(msg: Message, state: FSMContext, bot: Bot):
    # === PATCH_TICKET_ADMIN_REPLY_EXIT_TEXT_V1 ===
    # همون کلاسِ باگ، سمتِ کاربر هم: اگه کاربر به‌جایِ تایپِ پاسخ، دکمهٔ
    # «بازگشت»/«لغو» رو بزنه، نباید متنِ دکمه به‌عنوانِ پیامِ واقعی برایِ
    # ادمین فوروارد بشه.
    if (msg.text or "").strip() in (BTN_TICKET_BACK, BTN_CANCEL):
        await ticket_back_handler(msg, state)
        return
    data = await state.get_data()
    tid = data.get("tid")
    text = (msg.text or "").strip()
    await state.clear()
    if tid and text:
        text = msg.html_text  # حفظ قالب‌بندی تلگرام
        async with Session() as s:
            t = await s.get(Ticket, tid)
            if t and t.user_telegram_id == msg.from_user.id:
                s.add(TicketReply(ticket_id=tid, sender="user", text=text))
                t.status = "open"
                await s.commit()
        if PLATFORM_ADMIN_ID:
            try:
                # PATCH_TICKET_TWO_WAY_REPLY_V1 — بدونِ این دکمه، ادمین
                # هیچ راهی برایِ ادامهٔ گفتگو نداشت.
                _un = msg.from_user.username
                await bot.send_message(
                    PLATFORM_ADMIN_ID,
                    f"💬 <b>پاسخ کاربر در تیکت #{tid}</b>\n"
                    f"از: <code>{msg.from_user.id}</code>"
                    + (f" (@{_un})" if _un else "") + f"\n\n{text}",
                    parse_mode="HTML", reply_markup=_kb_admin_reply(tid))
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await msg.answer("✅ پاسخت ثبت شد.", reply_markup=await smart_home_kb(msg.from_user.id))


# === PATCH_TICKET_TWO_WAY_REPLY_V1 ===
# سمتِ کاربر فقط F.text داشت: اگر کاربر در پاسخ عکس می‌فرستاد (رسید،
# اسکرین‌شاتِ خطا — یعنی همان چیزی که بیشتر لازم است) هیچ هندلری
# نمی‌گرفتش، پیام بی‌صدا گم می‌شد و کاربر فکر می‌کرد فرستاده.
# ادمین از همان اول هم متن و هم عکس را می‌توانست بفرستد؛ حالا کاربر هم.
@ticket_router.message(TicketFSM.user_reply, F.photo)
async def tk_reply_save_photo(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    tid = data.get("tid")
    await state.clear()
    if not tid:
        return
    caption = (msg.caption or "").strip()
    file_id = msg.photo[-1].file_id
    async with Session() as s:
        t = await s.get(Ticket, tid)
        if t and t.user_telegram_id == msg.from_user.id:
            s.add(TicketReply(ticket_id=tid, sender="user",
                              text=caption or "[عکس]"))
            t.status = "open"
            await s.commit()
        else:
            await msg.answer("❌ تیکت یافت نشد.")
            return
    if PLATFORM_ADMIN_ID:
        try:
            _un = msg.from_user.username
            await bot.send_photo(
                PLATFORM_ADMIN_ID, file_id,
                caption=(f"💬 <b>پاسخ کاربر در تیکت #{tid}</b>\n"
                         f"از: <code>{msg.from_user.id}</code>"
                         + (f" (@{_un})" if _un else "")
                         + (f"\n\n{caption}" if caption else "")),
                parse_mode="HTML", reply_markup=_kb_admin_reply(tid))
        except Exception:
            logger.exception("ارسالِ عکسِ پاسخِ کاربر به ادمین ناموفق بود")
    await msg.answer("✅ پاسخت ثبت شد.",
                     reply_markup=await smart_home_kb(msg.from_user.id))


@ticket_router.callback_query(F.data.startswith("tk:edit:"))
async def tk_edit(cb: CallbackQuery, state: FSMContext):
    tid = int(cb.data.split(":")[2])
    await state.set_state(TicketFSM.edit_text)
    await state.update_data(tid=tid)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("✏️ متن جدید تیکت را بفرست:", reply_markup=await smart_ticket_cancel_kb())
    await cb.answer()


@ticket_router.message(TicketFSM.edit_text, F.text)
async def tk_edit_save(msg: Message, state: FSMContext):
    # === PATCH_TICKET_ADMIN_REPLY_EXIT_TEXT_V1 ===
    if (msg.text or "").strip() in (BTN_TICKET_BACK, BTN_CANCEL):
        await ticket_back_handler(msg, state)
        return
    data = await state.get_data()
    tid = data.get("tid")
    text = (msg.text or "").strip()
    await state.clear()
    if tid and text:
        text = msg.html_text  # حفظ قالب‌بندی تلگرام
        async with Session() as s:
            t = await s.get(Ticket, tid)
            if t and t.user_telegram_id == msg.from_user.id:
                t.text = text
                await s.commit()
    await msg.answer("✅ تیکت ویرایش شد.", reply_markup=await smart_home_kb(msg.from_user.id))


@ticket_router.callback_query(F.data.startswith("tk:del:"))
async def tk_del(cb: CallbackQuery):
    tid = int(cb.data.split(":")[2])
    async with Session() as s:
        t = await s.get(Ticket, tid)
        if t and t.user_telegram_id == cb.from_user.id:
            reps = (await s.scalars(select(TicketReply).where(TicketReply.ticket_id == tid))).all()
            for r in reps:
                await s.delete(r)
            await s.delete(t)
            await s.commit()
    await cb.message.edit_text("🗑 تیکت حذف شد.")
    await cb.answer("حذف شد")


# ---- ادمین ----
@ticket_router.callback_query(F.data == "adm:tickets")
async def adm_tickets(cb: CallbackQuery):
    if not is_admin(cb.from_user.id) and not await has_perm(cb.from_user.id, "adm:tickets"):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("🟠 بی‌پاسخ", callback_data="atk:list:open")],
        [hx_button("🟢 پاسخ‌داده", callback_data="atk:list:answered")],
        [hx_button("🔒 بسته", callback_data="atk:list:closed")],
        [hx_button("⬅️ بازگشت به پنل", callback_data="adm:back", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text("🎫 تیکت‌ها — وضعیت را انتخاب کن:", reply_markup=kb)
    except Exception:
        try:
            await cb.message.answer("🎫 تیکت‌ها — وضعیت را انتخاب کن:", reply_markup=kb)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer()


# === PATCH_TICKET_LIST_PAGINATION_V1 ===
_ATK_PAGE_SIZE = 10


@ticket_router.callback_query(F.data.startswith("atk:list:"))
async def atk_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id) and not await has_perm(cb.from_user.id, "adm:tickets"):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    parts = cb.data.split(":")  # atk:list:{status}[:{page}]
    st = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 0
    async with Session() as s:
        total = await s.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.status == st)) or 0
        pages = max(1, (total + _ATK_PAGE_SIZE - 1) // _ATK_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        ts = (await s.scalars(
            select(Ticket).where(Ticket.status == st).order_by(Ticket.id.desc())
            .limit(_ATK_PAGE_SIZE).offset(page * _ATK_PAGE_SIZE))).all()
        owner_ids = {t.user_telegram_id for t in ts}
        user_map = {}
        for oid in owner_ids:
            user_map[oid] = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == oid))
    b = InlineKeyboardBuilder()
    for t in ts:
        mark = " 📷" if t.photo_file_id else ""
        pu = user_map.get(t.user_telegram_id)
        # === PATCH_TICKET_SHOW_USERNAME_V1 === یوزرنیم گویاتر از آیدیِ عددی است
        who = (pu.full_name if pu and pu.full_name else str(t.user_telegram_id))
        if pu and pu.username:
            who += f" @{pu.username}"
        when = to_jalali(t.created_at)
        b.button(text=f"#{t.id} — {who} ({t.user_telegram_id}) — {when}{mark}",
                 callback_data=f"atk:v:{t.id}")
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(hx_button("◀️ قبلی", callback_data=f"atk:list:{st}:{page-1}"))
    if page < pages - 1:
        nav.append(hx_button("بعدی ▶️", callback_data=f"atk:list:{st}:{page+1}"))
    if nav:
        b.row(*nav)
    b.row(hx_button("⬅️ بازگشت", callback_data="adm:tickets", style=ButtonStyle.DANGER))
    title = {"open": "بی‌پاسخ", "answered": "پاسخ‌داده", "closed": "بسته"}.get(st, st)
    list_text = (f"🎫 تیکت‌های {title} — مجموع {total} — صفحه {page+1}/{pages}"
                 + ("" if ts else "\n(خالی)"))
    try:
        await cb.message.edit_text(list_text, reply_markup=b.as_markup())
    except Exception:
        try:
            await cb.message.answer(list_text, reply_markup=b.as_markup())
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer()


@ticket_router.callback_query(F.data.startswith("atk:v:"))
async def atk_view(cb: CallbackQuery):
    if not is_admin(cb.from_user.id) and not await has_perm(cb.from_user.id, "adm:tickets"):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    tid = int(cb.data.split(":")[2])
    txt, meta, photo_file_id = await render_ticket(tid, viewer_admin=True)
    if not txt:
        await cb.answer("یافت نشد", show_alert=True)
        return
    owner, status = meta
    rows = [[hx_button("✉️ پاسخ", callback_data=f"atk:reply:{tid}", style=ButtonStyle.PRIMARY)]]
    rows.append([hx_button("👤 پروفایل کاربر", url=f"tg://user?id={owner}")])
    if status != "closed":
        rows.append([hx_button("🔒 بستن تیکت", callback_data=f"atk:close:{tid}", style=ButtonStyle.DANGER)])
    rows.append([hx_button("⬅️ بازگشت", callback_data="adm:tickets", style=ButtonStyle.DANGER)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    full_text = txt
    if photo_file_id:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        caption = full_text if len(full_text) <= 1000 else full_text[:970] + "\n\n… (متن کامل کوتاه شد)"
        try:
            await cb.message.answer_photo(photo_file_id, caption=caption, reply_markup=kb)
        except Exception:
            await cb.message.answer(caption + "\n\n⚠️ نمایش عکس ممکن نشد (فایل منقضی شده).",
                                    reply_markup=kb)
        await cb.answer()
        return
    try:
        await cb.message.edit_text(full_text, reply_markup=kb)
    except Exception:
        try:
            await cb.message.answer(full_text, reply_markup=kb)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer()


@ticket_router.callback_query(F.data.startswith("atk:reply:"))
async def atk_reply(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id) and not await has_perm(cb.from_user.id, "adm:tickets"):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    tid = int(cb.data.split(":")[2])
    await state.set_state(TicketFSM.admin_reply)
    await state.update_data(reply_tid=tid)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "✍️ پاسخ پشتیبانی را بفرست (متن یا عکس، هر دو قابل قبوله):",
        reply_markup=await smart_ticket_cancel_kb())
    await cb.answer()


# توجه: ارسال پاسخ از این جریان توسط tk_adm_reply_send / tk_adm_reply_send_photo (بالاتر) پردازش می‌شود؛
# هر دو مسیر (نوتیف تیکت و پنل «تیکت‌های کاربران») از همان استیت/کلید استفاده می‌کنند
# تا تنها یک هندلر پاسخ را دریافت و ارسال کند.


@ticket_router.callback_query(F.data.startswith("atk:close:"))
async def atk_close(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id) and not await has_perm(cb.from_user.id, "adm:tickets"):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    tid = int(cb.data.split(":")[2])
    owner = None
    async with Session() as s:
        t = await s.get(Ticket, tid)
        if t:
            t.status = "closed"
            owner = t.user_telegram_id
            await s.commit()
    if not owner:
        await cb.answer("⛔️ تیکت پیدا نشد.", show_alert=True)
        return
    try:
        await bot.send_message(owner, f"🔒 تیکت #{tid} توسط پشتیبانی بسته شد.")
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    if has_media(cb.message):  # === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
        try:
            base_cap = cb.message.caption or ""
            suffix = "\n\n🔒 این تیکت بسته شد."
            if len(base_cap) + len(suffix) > 1024:
                base_cap = base_cap[:1024 - len(suffix)]
            await cb.message.edit_caption(caption=base_cap + suffix, reply_markup=None)
        except Exception:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            try:
                await cb.message.answer(f"🔒 تیکت #{tid} بسته شد.")
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    else:
        try:
            await cb.message.edit_text(f"🔒 تیکت #{tid} بسته شد.")
        except Exception:
            try:
                await cb.message.answer(f"🔒 تیکت #{tid} بسته شد.")
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("بسته شد")


