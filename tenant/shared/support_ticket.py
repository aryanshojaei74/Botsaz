"""
support_ticket.py — سیستم تیکتِ پشتیبانیِ ربات‌های مستأجر (کاربر + ادمین).

این ماژول کاملِ قابلیتِ «تیکت» را دربر می‌گیرد:
  • سمتِ کاربر: ثبتِ تیکتِ جدید (با/بدونِ عکس) + فهرستِ «تیکت‌های من» + مشاهدهٔ هر تیکت
    (پیشوندِ callback: ticket:)
  • سمتِ ادمین: فهرستِ تیکت‌ها با فیلترِ وضعیت + مشاهده/پاسخ/بستن/بازگشاییِ هر تیکت
    (پیشوندِ callback: tkt: و tadm:tickets)

قبلاً این ~۵۳۰ خط پراکنده در دو نقطهٔ دورِ tenant/__init__.py (که آن‌موقع
~۱۷۴۰۰ خط بود) زندگی می‌کردند؛ چون پیشوندهای callback‌شان کاملاً مجزا و
خودکفا بودند (هیچ بخشِ دیگری از پروژه به این کد وابسته نبود)، این یک مرزِ
طبیعی و کم‌ریسک برای جداسازی به فایلِ مستقل بود.

الگوی وارد‌کردنِ کمک‌تابع‌های مشترک (get_bot_row، smart_cancel_kb، ...) از
tenant/__init__.py عمداً *محلی* (داخلِ خودِ هر تابع) است، نه در سطحِ ماژول؛
چون tenant/__init__.py در زمانِ import خودش این فایل را import می‌کند
(برای ثبتِ ticket_router) — وارد‌کردنِ سطحِ‌ماژولِ برعکس، importِ حلقوی
(circular import) می‌ساخت. همین الگو در tenant/vpn_user.py و بقیهٔ
زیرماژول‌های موجودِ پروژه هم استفاده شده.
"""
from __future__ import annotations

from aiogram.enums import ButtonStyle  # PATCH_TENANT_ADMIN_COLORS_V1
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from sqlalchemy import func, select

from config import Session
from models import BotUser, TenantTicket, TenantTicketReply

from bot_utils import has_media  # === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

ticket_router = Router()


class TicketFSM(StatesGroup):
    new_text = State()
    new_photo = State()   # منتظرِ عکس/اسکرین‌شاتِ ضمیمه (اختیاری)
    reply = State()        # ادمین در حال پاسخ
    # === PATCH_SUPPORT_DIRECT_CONTACT_V1 ===
    set_contact = State()   # ادمین آیدی/یوزرنیمِ تماسِ مستقیم را می‌نویسد
    # === PATCH_TICKET_USER_FOLLOWUP_V1 ===
    user_reply = State()    # کاربر در حالِ ارسالِ پیامِ جدید برایِ تیکتِ بازِ خودش
    # === PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
    new_subject = State()   # منتظرِ موضوعِ کوتاهِ تیکت (بعدِ انتخابِ دسته، قبلِ متنِ اصلی)
    order_code = State()    # PATCH_ORDER_TICKET_FLOW_V3 — منتظرِ کدِ پیگیریِ سفارش (دستهٔ «پیگیریِ سفارش»)


# === PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
# دسته‌بندیِ تیکت — عمداً یه لیستِ ثابتِ کوچیکه (نه قابلِ‌تنظیم توسطِ هر
# مالکِ ربات)، چون این‌جا نه UI مدیریتی برایِ سفارشی‌کردنش هست نه دلیلِ
# روشنی که هر ربات دسته‌هایِ متفاوتی بخواد — همین چهارتا اکثرِ زمینه‌هایِ
# فعلِ ربات‌سازها (فروشِ VPN، خدماتِ مجازی، تبلیغاتی) رو پوشش می‌ده.
TICKET_CATEGORIES: list[tuple[str, str]] = [
    ("billing", "💰 مالی و پرداخت"),
    ("technical", "🛠 فنی / مشکلِ سرویس"),
    ("order", "📦 پیگیریِ سفارش"),
    ("other", "❓ سایر"),
]
TICKET_CATEGORY_LABEL = dict(TICKET_CATEGORIES)


# === PATCH_TICKET_RATING_V1 ===
def _rating_kb(tid: int) -> InlineKeyboardMarkup:
    """کیبوردِ ۱ تا ۵ ستاره برایِ امتیازدهی به یک تیکتِ بسته‌شده."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{n}⭐️", callback_data=f"tkt:rate:{tid}:{n}")
        for n in (1, 2, 3, 4, 5)
    ]])


# ═══════════════════════════ سمتِ کاربر ═══════════════════════════

@ticket_router.message(TicketFSM.new_text, F.text)
async def ticket_new(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, main_kb_or_remove
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
        await msg.answer("لغو شد.", reply_markup=kb or ReplyKeyboardRemove())
        return
    data = await state.get_data()
    photo_id = data.get("ticket_photo_id")
    await _save_new_ticket(msg, state, bot, text=raw, photo_file_id=photo_id)


# === PATCH_TICKET_REPLY_ACCEPT_MEDIA_V1 ===
# همان باگ در سمتِ کاربر: وقتی ربات «متنِ تیکت» را خواسته و کاربر یک
# اسکرین‌شات می‌فرستد، فیلترِ F.text نمی‌گرفت و فالبک با «عملیات قبلی لغو
# شد» تیکتِ نیمه‌کاره را دور می‌ریخت. حالا عکس هم پذیرفته می‌شود.
@ticket_router.message(TicketFSM.new_text, F.photo)
async def ticket_new_photo_instead_of_text(msg: Message, state: FSMContext, bot: Bot):
    from tenant import smart_cancel_kb
    file_id = msg.photo[-1].file_id
    caption = (msg.caption or "").strip()
    if caption:
        await _save_new_ticket(msg, state, bot, text=caption, photo_file_id=file_id)
        return
    await state.update_data(ticket_photo_id=file_id)
    await msg.answer("✍️ عکس ثبت شد. حالا توضیحِ تیکت را هم بنویس:",
                     reply_markup=await smart_cancel_kb(bot))


# ─────────── منوی تیکت: «ثبت تیکت جدید» / «تیکت‌های من» ───────────

async def _ticket_menu_content(bot_id: int) -> tuple[str, "InlineKeyboardMarkup | ReplyKeyboardMarkup"]:
    """متن و کیبوردِ منوی تیکت («ثبت تیکت جدید» / «تیکت‌های من») — مشترک بینِ
    ورودِ اول (زدنِ دکمه‌ی تیکت) و برگشتن به این صفحه از مراحلِ بعدی."""
    from tenant import get_setting, inline_back_row, BTN_BACK_MENU
    opts = [("📝 ثبت تیکت جدید", "ticket:new"), ("🎫 تیکت‌های من", "ticket:mine")]
    # === PATCH_SUPPORT_DIRECT_CONTACT_V1 ===
    direct_contact = await get_setting(bot_id, "support_direct_contact", "")
    direct_url = _support_contact_url(direct_contact)
    if await get_setting(bot_id, "menu_mode", "reply") != "inline":
        rows = [[KeyboardButton(text=lbl)] for lbl, _cbd in opts]
        if direct_url:
            # === PATCH_SUPPORT_DIRECT_CONTACT_V1 ===
            # کیبوردِ ریپلای نمی‌تواند مستقیم لینک باز کند؛ برایِ همین این
            # گزینه به یک متنِ عادی تبدیل می‌شود که با زدنش، ربات خودش
            # لینکِ قابل‌کلیک را می‌فرستد (تابعِ ticket_direct_contact پایین‌تر).
            rows.append([KeyboardButton(text="💬 پیامِ مستقیم به پشتیبانی")])
        rows.append([KeyboardButton(text=BTN_BACK_MENU)])
        tkt_kb = ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
    else:
        rows = [[InlineKeyboardButton(text=lbl, callback_data=cbd)] for lbl, cbd in opts]
        if direct_url:
            rows.append([InlineKeyboardButton(text="💬 پیامِ مستقیم به پشتیبانی", url=direct_url)])
        _br = await inline_back_row(bot_id)
        if _br:
            rows.append(_br)
        tkt_kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return "🎫 <b>پشتیبانی و تیکت</b>\n\nیکی از گزینه‌ها را انتخاب کن:", tkt_kb


# === PATCH_SUPPORT_DIRECT_CONTACT_V1 ===
def _support_contact_url(raw: str) -> str | None:
    """آیدی/یوزرنیمِ ذخیره‌شده را به یک لینکِ قابل‌کلیک تبدیل می‌کند.
    یوزرنیم قابل‌اعتمادتر است؛ آیدیِ عددیِ خام با tg://user?id= کار می‌کند
    ولی فقط اگر آن شخص یوزرنیمِ عمومی داشته باشد یا مقصد قبلاً او را دیده
    باشد — این محدودیتِ خودِ تلگرام است، نه ربات."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.lstrip("@").isdigit():
        return f"tg://user?id={raw.lstrip('@')}"
    return f"https://t.me/{raw.lstrip('@')}"


@ticket_router.message(F.text == "💬 پیامِ مستقیم به پشتیبانی")
async def ticket_direct_contact_keyboard(msg: Message, bot: Bot):
    """معادلِ کیبوردیِ دکمهٔ اینلاینِ «پیامِ مستقیم» — چون دکمهٔ ریپلای
    نمی‌تواند مستقیم لینک باز کند، همین‌جا خودمان لینک را می‌فرستیم."""
    from tenant import get_bot_row, get_setting
    row = await get_bot_row(bot)
    if not row:
        return
    raw = await get_setting(row.id, "support_direct_contact", "")
    url = _support_contact_url(raw)
    if not url:
        await msg.answer("فعلاً تماسِ مستقیم تنظیم نشده.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 باز کردنِ چت", url=url)]])
    await msg.answer("برای پیامِ مستقیم، این دکمه را بزن:", reply_markup=kb)


# ─────────────────── تنظیمِ تماسِ مستقیم — طرفِ ادمین ───────────────────

@ticket_router.callback_query(F.data == "supp:contact")
async def supp_contact_menu(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, get_setting
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    cur = await get_setting(row.id, "support_direct_contact", "")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ تنظیم/تغییر", callback_data="supp:contact:set")],
    ] + ([[InlineKeyboardButton(text="🗑 حذف (خاموش‌کردنِ این دکمه)",
                                callback_data="supp:contact:clear", style=ButtonStyle.DANGER)]]
        if cur else [])
      + [[InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:admins", style=ButtonStyle.DANGER)]])
    txt = (
        "💬 <b>پیامِ مستقیم به پشتیبانی</b>\n\n"
        "اگر می‌خواهی کاربرها بتوانند علاوه‌بر تیکت، مستقیم به آیدی/یوزرنیمِ "
        "خودت پیام بدهند، اینجا تنظیمش کن — یک دکمه در منویِ پشتیبانی "
        "اضافه می‌شود.\n\n"
        "⚠️ یوزرنیم (مثلاً @something) خیلی مطمئن‌تر از آیدیِ عددی است — "
        "آیدیِ عددی فقط اگر خودت یوزرنیمِ عمومی داشته باشی به‌درستی باز می‌شود.\n\n"
        f"وضعیتِ فعلی: {('<code>' + cur + '</code>') if cur else 'تنظیم نشده'}"
    )
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@ticket_router.callback_query(F.data == "supp:contact:set")
async def supp_contact_set_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_ok_local(cb, bot)
    if not row:
        return
    from tenant import smart_cancel_kb
    await state.set_state(TicketFSM.set_contact)
    await cb.message.answer(
        "✏️ یوزرنیمِ خودت را بفرست (مثلاً @something)، یا اگر یوزرنیمِ "
        "عمومی نداری، آیدیِ عددیِ تلگرامت را بفرست:",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@ticket_router.message(TicketFSM.set_contact, F.text)
async def supp_contact_set_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, set_setting
    await state.clear()
    row = await get_bot_row(bot)
    if not row:
        return
    val = (msg.text or "").strip()
    if not _support_contact_url(val):
        await msg.answer("❌ این ورودی معتبر نیست. دوباره از منو امتحان کن.")
        return
    await set_setting(row.id, "support_direct_contact", val)
    await msg.answer(f"✅ ذخیره شد. حالا دکمهٔ «💬 پیامِ مستقیم به پشتیبانی» در منویِ پشتیبانی نشان داده می‌شود.")


@ticket_router.callback_query(F.data == "supp:contact:clear")
async def supp_contact_clear(cb: CallbackQuery, bot: Bot):
    row = await _owner_ok_local(cb, bot)
    if not row:
        return
    from tenant import set_setting
    await set_setting(row.id, "support_direct_contact", "")
    await cb.answer("حذف شد")
    await supp_contact_menu(cb, bot)


async def _owner_ok_local(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return None
    if not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("فقط ادمین‌ها", show_alert=True)
        return None
    return row


@ticket_router.callback_query(F.data == "ticket:menu")
async def ticket_menu_back(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    text, kb = await _ticket_menu_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@ticket_router.callback_query(F.data == "ticket:new")
async def ticket_new_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """=== PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
    اولین قدمِ ثبتِ تیکت الان انتخابِ دسته است (مثلِ سایت‌های حرفه‌ای)،
    نه مستقیم پرسیدنِ عکس. مسیرِ عکس/متن دست‌نخورده مونده، فقط بعدِ
    دسته+موضوع میاد."""
    from tenant import get_bot_row, inline_back_row
    row = await get_bot_row(bot)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    kb_rows = [[InlineKeyboardButton(text=lbl, callback_data=f"ticket:cat:{key}")]
               for key, lbl in TICKET_CATEGORIES]
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ticket:menu")])
    if row:
        _hbr = await inline_back_row(row.id)
        if _hbr:
            kb_rows.append(_hbr)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await cb.message.answer(
        "🎫 <b>ثبت تیکت جدید</b>\n\nاین تیکت دربارهٔ کدوم بخشه؟",
        reply_markup=kb)
    await cb.answer()


# === PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
@ticket_router.callback_query(F.data.startswith("ticket:cat:"))
async def ticket_new_category_pick(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import smart_cancel_kb
    key = cb.data.split(":", 2)[2]
    if key not in TICKET_CATEGORY_LABEL:
        await cb.answer()
        return
    await state.update_data(ticket_category=key, ticket_order_code=None)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # === PATCH_ORDER_TICKET_FLOW_V3 === (Fadayi) «پیگیریِ سفارش»: اول کدِ پیگیری ← بعد متنِ پیام ← تیکت با مشخصاتِ سفارش
    if key == "order":
        from tenant import get_bot_row
        _r = await get_bot_row(bot)
        if _r and (_r.field or "") == "virtual_service":
            await state.set_state(TicketFSM.order_code)
            await cb.message.answer(
                "🔖 <b>کدِ پیگیریِ سفارش</b> را بفرست.\n"
                "کد در پیامِ ثبتِ سفارش یا در بخشِ «سفارش‌های من» هست.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⏭ کدِ پیگیری ندارم", callback_data="ticket:nocode")],
                    [InlineKeyboardButton(text="❌ انصراف", callback_data="ticket:menu", style=ButtonStyle.DANGER)]]))
            await cb.answer()
            return
    await state.set_state(TicketFSM.new_subject)
    await cb.message.answer(
        "✏️ موضوعِ تیکت را در یک جمله کوتاه بنویس (مثلاً «واریز انجام شد ولی سرویس فعال نشد»):",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


def _norm_order_code(raw: str) -> str:
    fa = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return (raw or "").translate(fa).strip().lstrip("#").replace(" ", "").upper()


@ticket_router.message(TicketFSM.order_code, F.text)
async def ticket_order_code_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, main_kb_or_remove, smart_cancel_kb
    row = await get_bot_row(bot)
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id) if row else None
        await msg.answer("لغو شد.", reply_markup=kb or ReplyKeyboardRemove())
        return
    code = _norm_order_code(raw)
    if not row or not code:
        await msg.answer("کدِ پیگیری را بفرست (یا «⏭ کدِ پیگیری ندارم» را بزن).")
        return
    from tenant.bots.virtual_service.smm import _order_details_html
    if not await _order_details_html(row.id, code):
        await msg.answer(
            "❌ سفارشی با این کدِ پیگیری پیدا نشد. کد را دوباره چک کن و بفرست، یا «⏭ کدِ پیگیری ندارم» را بزن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ کدِ پیگیری ندارم", callback_data="ticket:nocode")]]))
        return
    await state.update_data(ticket_order_code=code, ticket_subject=f"پیگیریِ سفارش {code}")
    await state.set_state(TicketFSM.new_text)
    await msg.answer(f"✅ سفارشِ <code>{code}</code> پیدا شد.\n\n✍️ حالا پیام یا سؤالت درباره‌یِ این سفارش را بنویس:",
                     parse_mode="HTML", reply_markup=await smart_cancel_kb(bot))


@ticket_router.callback_query(F.data == "ticket:nocode")
async def ticket_order_nocode(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """کدِ پیگیری ندارد: مسیرِ قبلی (موضوع ← متن)؛ مشخصاتِ آخرین سفارش‌هایش خودکار اضافه می‌شود."""
    from tenant import smart_cancel_kb
    await state.update_data(ticket_order_code=None)
    await state.set_state(TicketFSM.new_subject)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("✏️ موضوعِ تیکت را در یک جمله کوتاه بنویس:", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


# === PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
@ticket_router.message(TicketFSM.new_subject, F.text)
async def ticket_new_subject_in(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, main_kb_or_remove, inline_back_row
    row = await get_bot_row(bot)
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id) if row else None
        await msg.answer("لغو شد.", reply_markup=kb or ReplyKeyboardRemove())
        return
    if not raw:
        await msg.answer("لطفاً یه موضوعِ کوتاه بنویس (یا برای لغو «❌ لغو» رو بزن):")
        return
    await state.update_data(ticket_subject=raw[:100])
    kb_rows = [
        [InlineKeyboardButton(text="📷 بله، عکس/اسکرین‌شات هم می‌فرستم", callback_data="ticket:withphoto")],
        [InlineKeyboardButton(text="⏭ نه، فقط متن می‌نویسم", callback_data="ticket:textonly")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ticket:menu")],
    ]
    if row:
        _hbr = await inline_back_row(row.id)
        if _hbr:
            kb_rows.append(_hbr)
    await msg.answer(
        "می‌خوای همراهِ تیکتت یه عکس یا اسکرین‌شات هم بفرستی؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@ticket_router.callback_query(F.data == "ticket:textonly")
async def ticket_new_textonly(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import smart_cancel_kb
    await state.set_state(TicketFSM.new_text)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("✍️ متن تیکت خود را بنویسید:", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@ticket_router.callback_query(F.data == "ticket:withphoto")
async def ticket_new_withphoto(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import smart_cancel_kb
    await state.set_state(TicketFSM.new_photo)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        "📷 عکس/اسکرین‌شات رو بفرست.\n"
        "می‌تونی توضیحِ متنیِ تیکت رو هم زیرِ همون عکس (به‌عنوان caption) بنویسی — "
        "اگه ننویسی، بعداً ازت متن رو جدا می‌پرسم.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@ticket_router.message(TicketFSM.new_photo, F.photo)
async def ticket_photo_received(msg: Message, state: FSMContext, bot: Bot):
    from tenant import smart_cancel_kb
    file_id = msg.photo[-1].file_id
    caption = (msg.caption or "").strip()
    if caption:
        await _save_new_ticket(msg, state, bot, text=caption, photo_file_id=file_id)
        return
    await state.update_data(ticket_photo_id=file_id)
    await state.set_state(TicketFSM.new_text)
    await msg.answer("✍️ حالا متنِ توضیحِ تیکت رو هم بنویس:", reply_markup=await smart_cancel_kb(bot))


@ticket_router.message(TicketFSM.new_photo, F.text)
async def ticket_photo_expected_but_text(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, main_kb_or_remove, smart_cancel_kb
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        row = await get_bot_row(bot)
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id) if row else None
        await msg.answer("لغو شد.", reply_markup=kb or ReplyKeyboardRemove())
        return
    await msg.answer("لطفاً یه عکس/اسکرین‌شات بفرست (یا برای لغو، «❌ لغو» رو بزن):",
                     reply_markup=await smart_cancel_kb(bot))


async def _save_new_ticket(msg: Message, state: FSMContext, bot: Bot, text: str,
                            photo_file_id: str | None = None):
    from tenant import get_bot_row, main_kb_or_remove, _notify_admins
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    # === PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
    # باید قبل از state.clear() خونده بشه.
    _data = await state.get_data()
    category = _data.get("ticket_category")
    subject = (_data.get("ticket_subject") or "").strip() or None
    # === PATCH_ORDER_TICKET_FLOW_V3 === کدِ پیگیریِ گرفته‌شده → همان قالبِ تیکتِ دکمهٔ سفارش (مشخصاتِ کاملِ سفارش)
    _code = _data.get("ticket_order_code")
    if category == "order" and _code and (row.field or "") == "virtual_service":
        try:
            from tenant.bots.virtual_service.smm import build_order_ticket_text
            text = await build_order_ticket_text(row.id, _code, text)
        except Exception:
            logger.exception("ساختِ متنِ تیکتِ پیگیریِ سفارش ناموفق بود")
    # === PATCH_ORDER_TICKET_FLOW_V2 === تیکتِ «پیگیریِ سفارش» از منویِ تیکت (بدونِ کدِ پیگیری): مشخصاتِ آخرین سفارش‌ها اضافه شود
    if category == "order" and (row.field or "") == "virtual_service" and "مشخصاتِ سفارش" not in (text or ""):
        try:
            from tenant.bots.virtual_service.smm import recent_orders_ticket_block
            _blk = await recent_orders_ticket_block(row.id, msg.from_user.id)
            if _blk:
                text = f"{text}\n\n{_blk}"
        except Exception:
            logger.exception("افزودنِ مشخصاتِ سفارش به تیکت ناموفق بود")
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == msg.from_user.id))
        if not u:
            await state.clear()
            return
        t = TenantTicket(bot_id=row.id, user_id=u.id, text=text, photo_file_id=photo_file_id,
                         category=category, subject=subject)
        s.add(t)
        await s.flush()
        tid = t.id
        await s.commit()
    await state.clear()
    kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
    await msg.answer(f"✅ تیکت #{tid} ثبت شد. به زودی پاسخ می‌گیرید.", reply_markup=kb)
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 پاسخ", callback_data=f"tkt:reply:{tid}:{msg.from_user.id}")
    ]])
    # === PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
    _head = f"🎫 تیکت جدید #{tid}\nکاربر: <code>{msg.from_user.id}</code>"
    if category:
        _head += f"\nدسته: {TICKET_CATEGORY_LABEL.get(category, category)}"
    if subject:
        _head += f"\nموضوع: <b>{subject}</b>"
    admin_caption = f"{_head}\n\n{text}"
    await _notify_admins(bot, row, admin_caption, admin_kb, photo_file_id=photo_file_id)


async def _my_tickets_content(bot_id: int, telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """متن و کیبوردِ فهرستِ تیکت‌های کاربر — مشترک بینِ حالتِ شیشه‌ای و کیبوردی."""
    from tenant import inline_back_row, _jalali_date_time
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        tickets = []
        if u:
            tickets = (await s.scalars(
                select(TenantTicket).where(TenantTicket.user_id == u.id)
                .order_by(TenantTicket.id.desc()).limit(15)
            )).all()
    status_lbl = {"open": "🟡 در انتظار پاسخ", "answered": "🟢 پاسخ داده‌شده", "closed": "⚪️ بسته‌شده"}
    rows = []
    for t in tickets:
        d, _tm = _jalali_date_time(t.created_at)
        photo_mark = " 📷" if t.photo_file_id else ""
        # === PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
        # تیکت‌هایِ قدیمی موضوع ندارن، پس برچسبِ قدیمی برایِ اونا حفظ می‌شه.
        if t.subject:
            _subj = t.subject if len(t.subject) <= 28 else t.subject[:27] + "…"
            lbl = f"#{t.id} · {_subj} — {status_lbl.get(t.status, t.status)}{photo_mark}"
        else:
            lbl = f"#{t.id} — {status_lbl.get(t.status, t.status)} — {d}{photo_mark}"
        rows.append([InlineKeyboardButton(text=lbl, callback_data=f"ticket:view:{t.id}")])
    _br = await inline_back_row(bot_id)
    if _br:
        rows.append(_br)
    if not tickets:
        text = "🎫 <b>تیکت‌های من</b>\n\nهنوز تیکتی ثبت نکرده‌اید."
    else:
        text = "🎫 <b>تیکت‌های من</b>\n\nروی هر تیکت بزن تا کامل ببینیش:"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@ticket_router.callback_query(F.data == "ticket:mine")
async def ticket_mine(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    text, kb = await _my_tickets_content(row.id, cb.from_user.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@ticket_router.callback_query(F.data.startswith("ticket:view:"))
async def ticket_view(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, inline_back_row, _jalali_date_time
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    tid = int(cb.data.split(":")[2])
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        t = await s.get(TenantTicket, tid)
        if not t or not u or t.user_id != u.id or t.bot_id != row.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        replies = (await s.scalars(
            select(TenantTicketReply).where(TenantTicketReply.ticket_id == tid)
            .order_by(TenantTicketReply.id))).all()
    status_lbl = {"open": "🟡 در انتظار پاسخ", "answered": "🟢 پاسخ داده‌شده", "closed": "⚪️ بسته‌شده"}
    d, tm = _jalali_date_time(t.created_at)
    lines = [f"🎫 <b>تیکت #{t.id}</b>  —  {status_lbl.get(t.status, t.status)}"]
    # === PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
    if t.subject:
        lines.append(f"📌 <b>{t.subject}</b>")
    if t.category:
        lines.append(f"🏷 {TICKET_CATEGORY_LABEL.get(t.category, t.category)}")
    lines += [f"🗓 {d}  🕒 {tm}", "",
              f"👤 <b>شما:</b>\n{t.text}"]
    for r in replies:
        who = "👮 پشتیبانی" if r.sender == "admin" else "👤 شما"
        lines.append(f"\n{who}:\n{r.text}")
    # === PATCH_TICKET_USER_FOLLOWUP_V1 ===
    # قبلاً کاربر از این صفحه هیچ راهی برای ادامه‌دادنِ گفتگو نداشت — فقط
    # می‌تونست بره «بازگشت به تیکت‌های من» یا یه تیکتِ کاملاً تازه بسازه.
    # حالا اگه تیکت بسته نشده، دکمهٔ ارسالِ پیامِ جدید هم اضافه می‌شه.
    kb_rows: list[list[InlineKeyboardButton]] = []
    if t.status != "closed":
        kb_rows.append([InlineKeyboardButton(
            text="✍️ ارسال پیام جدید", callback_data=f"ticket:reply:{t.id}",
            style=ButtonStyle.PRIMARY)])
    else:
        lines.append("\n<i>این تیکت بسته شده. برای موضوعِ جدید، تیکتِ تازه ثبت کن.</i>")
        # === PATCH_TICKET_RATING_V1 ===
        # اگه از پیامِ خودکارِ لحظه‌یِ بسته‌شدن امتیاز نداده (یا اون پیام
        # از دست رفته)، از همین صفحه هم می‌تونه امتیاز بده.
        if t.rating:
            lines.append(f"\n⭐️ امتیازِ شما به این تیکت: {t.rating}/5")
        else:
            lines.append("\n<i>هنوز به این تیکت امتیاز ندادی.</i>")
            kb_rows.append([InlineKeyboardButton(
                text=f"{n}⭐️", callback_data=f"tkt:rate:{t.id}:{n}") for n in (1, 2, 3, 4, 5)])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت به تیکت‌های من", callback_data="ticket:mine")])
    _br = await inline_back_row(row.id)
    if _br:
        kb_rows.append(_br)
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if t.photo_file_id:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        caption = text if len(text) <= 1000 else text[:970] + "\n\n… (متن کامل کوتاه شد)"
        await cb.message.answer_photo(t.photo_file_id, caption=caption, reply_markup=kb)
        await cb.answer()
        return
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


# === PATCH_TICKET_USER_FOLLOWUP_V1 ===
@ticket_router.callback_query(F.data.startswith("ticket:reply:"))
async def ticket_user_reply_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """کاربر می‌خواد برایِ تیکتِ خودش (که هنوز بسته نشده) یه پیامِ جدید بفرسته."""
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    tid = int(cb.data.split(":")[2])
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        t = await s.get(TenantTicket, tid)
        if not t or not u or t.user_id != u.id or t.bot_id != row.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        if t.status == "closed":
            await cb.answer("این تیکت بسته شده — برای موضوعِ جدید، تیکتِ تازه ثبت کن.", show_alert=True)
            return
    await state.set_state(TicketFSM.user_reply)
    await state.update_data(ur_tid=tid, ur_bot_id=row.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer(
        f"✍️ پیامِ جدیدت را برایِ تیکت #{tid} بنویس (متن یا عکس با توضیح):",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


# === PATCH_TICKET_USER_FOLLOWUP_V1 ===
# دقیقاً همون تحمل‌پذیریِ رسانه‌ای که برایِ پاسخِ ادمین (tkt_reply_send)
# قبلاً لازم شده بود: متن، عکس‌با‌کپشن یا عکسِ‌بی‌کپشن، همه پذیرفته می‌شن؛
# نوعِ پشتیبانی‌نشده با راهنما رد می‌شه (بدونِ پاک‌کردنِ state).
@ticket_router.message(TicketFSM.user_reply)
async def ticket_user_reply_send(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, main_kb_or_remove, smart_cancel_kb, _notify_admins
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    tid = data.get("ur_tid")

    text = (msg.text or msg.caption or "").strip()
    photo_id = msg.photo[-1].file_id if msg.photo else None

    if text in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
        await msg.answer("لغو شد.", reply_markup=kb or ReplyKeyboardRemove())
        return

    if not tid:
        await state.clear()
        await msg.answer("❌ تیکت مشخص نیست. دوباره از «🎫 تیکت‌های من» وارد شو.")
        return

    if not text and not photo_id:
        await msg.answer(
            "لطفاً پیام را به‌صورت <b>متن</b> یا <b>عکس (با توضیح)</b> بفرست.\n"
            "برای انصراف «❌ لغو» را بزن.",
            reply_markup=await smart_cancel_kb(bot))
        return

    await state.clear()

    async with Session() as s:
        t = await s.get(TenantTicket, tid)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == msg.from_user.id))
        if not t or not u or t.user_id != u.id or t.bot_id != row.id:
            await msg.answer("❌ تیکت یافت نشد.")
            return
        if t.status == "closed":
            await msg.answer("این تیکت بسته شده — برای موضوعِ جدید، تیکتِ تازه ثبت کن.")
            return
        # پیامِ تازهٔ کاربر یعنی دوباره منتظرِ بررسیِ پشتیبانیه.
        t.status = "open"
        t_subject = t.subject  # PATCH_TICKET_CATEGORY_SUBJECT_V1 — قبل از بستنِ سشن خونده می‌شه
        s.add(TenantTicketReply(ticket_id=tid, sender="user",
                                text=text or "🖼 (تصویر)"))
        await s.commit()

    kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
    await msg.answer(f"✅ پیامت برایِ تیکت #{tid} ثبت شد.", reply_markup=kb or ReplyKeyboardRemove())

    # اطلاعِ ادمین — دقیقاً همون دکمهٔ «💬 پاسخ»یِ تیکتِ تازه، تا بتونه
    # مستقیم از همون‌جا جواب بده.
    try:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💬 پاسخ", callback_data=f"tkt:reply:{tid}:{msg.from_user.id}")
        ]])
        _subj_line = f"\nموضوع: <b>{t_subject}</b>" if t_subject else ""  # PATCH_TICKET_CATEGORY_SUBJECT_V1
        admin_caption = f"🔔 پیامِ جدید برایِ تیکت #{tid}{_subj_line}\nکاربر: <code>{msg.from_user.id}</code>\n\n{text}"
        await _notify_admins(bot, row, admin_caption, admin_kb, photo_file_id=photo_id)
    except Exception:
        logger.exception("اطلاعِ ادمین از پیامِ جدیدِ تیکت ناموفق بود")


# === PATCH_TICKET_RATING_V1 ===
@ticket_router.callback_query(F.data.startswith("tkt:rate:"))
async def ticket_rate(cb: CallbackQuery, bot: Bot):
    """کاربر به پشتیبانیِ یک تیکتِ بسته‌شده امتیاز می‌ده (۱ تا ۵)."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    parts = cb.data.split(":")
    tid, n = int(parts[2]), int(parts[3])
    if n not in (1, 2, 3, 4, 5):
        await cb.answer()
        return
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        t = await s.get(TenantTicket, tid)
        if not t or not u or t.user_id != u.id or t.bot_id != row.id:
            await cb.answer("یافت نشد.", show_alert=True)
            return
        if t.status != "closed":
            # نظریِ نادر: بینِ فرستادنِ دکمه‌ها و زدنِ کاربر، تیکت دوباره باز شده.
            await cb.answer("این تیکت الان بازه — امتیازدهی فقط برایِ تیکتِ بسته‌شده‌ست.", show_alert=True)
            return
        t.rating = n
        await s.commit()
    await cb.answer("🙏 ممنون از بازخوردت!", show_alert=True)
    try:
        thanks = f"🙏 ممنون از امتیازت به تیکت #{tid}: {n}⭐️"
        if has_media(cb.message):
            await cb.message.edit_caption(caption=thanks, reply_markup=None)
        else:
            await cb.message.edit_text(thanks, reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# ═══════════════════════════ سمتِ ادمین ═══════════════════════════

@ticket_router.callback_query(F.data == "tadm:tickets")
async def tadm_tickets(cb: CallbackQuery, bot: Bot):
    await _show_tickets(cb, bot, status_filter="open", page=0)


# === PATCH_TICKET_ORDER_SETTINGS_V1 ===
_AUTO_CYCLE = (0, 12, 24, 48, 72)


@ticket_router.callback_query(F.data.startswith("tkt:cfg:"))
async def tkt_cfg(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    what = cb.data.split(":")[2]
    if what == "ordbtn":
        cur = (await get_setting(row.id, "order_support_btn", "1")) != "0"
        await set_setting(row.id, "order_support_btn", "0" if cur else "1")
        await cb.answer("⛔️ دکمه از زیرِ پیام‌هایِ سفارش برداشته شد" if cur else "✅ دکمه دوباره زیرِ پیام‌هایِ سفارش می‌آید",
                        show_alert=True)
    elif what == "ordask":
        cur = (await get_setting(row.id, "order_ticket_ask_text", "1")) != "0"
        await set_setting(row.id, "order_ticket_ask_text", "0" if cur else "1")
        await cb.answer("⛔️ تیکت فوری ثبت می‌شود (بدونِ پرسیدنِ متن)" if cur else "✅ اول متنِ پیامِ مشتری پرسیده می‌شود")
    elif what == "auto":
        cur = (await get_setting(row.id, "ticket_autoclose_hours", "0")) or "0"
        cur = int(cur) if str(cur).isdigit() else 0
        nxt = _AUTO_CYCLE[(_AUTO_CYCLE.index(cur) + 1) % len(_AUTO_CYCLE)] if cur in _AUTO_CYCLE else 24
        await set_setting(row.id, "ticket_autoclose_hours", str(nxt))
        await cb.answer(f"✅ تیکتِ جواب‌داده‌شده بعد از {nxt} ساعتِ بی‌پاسخیِ کاربر بسته می‌شود" if nxt else "⛔️ بستنِ خودکار خاموش شد",
                        show_alert=True)
    await _show_tickets(cb, bot, status_filter="open", page=0)


async def auto_close_answered_tickets_once() -> int:
    """تیکت‌هایی که آخرین پاسخ مالِ ادمین است و کاربر در N ساعت جواب نداده‌اند را می‌بندد (فقط رباتی که N>0 دارد)."""
    import datetime as _dt
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from models import Bot as BotRow, Setting
    closed = 0
    async with Session() as s:
        cfgs = (await s.execute(select(Setting.bot_id, Setting.value).where(
            Setting.key == "ticket_autoclose_hours"))).all()
    for bot_id, val in cfgs:
        hours = int(val) if str(val or "").isdigit() else 0
        if hours <= 0:
            continue
        cutoff = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
        to_notify: list[tuple[int, int]] = []
        async with Session() as s:
            tks = (await s.scalars(select(TenantTicket).where(
                TenantTicket.bot_id == bot_id, TenantTicket.status == "answered"))).all()
            for t in tks:
                last = await s.scalar(select(TenantTicketReply).where(
                    TenantTicketReply.ticket_id == t.id).order_by(TenantTicketReply.id.desc()).limit(1))
                if last is None or last.sender != "admin" or (last.created_at and last.created_at > cutoff):
                    continue
                t.status = "closed"
                bu = await s.get(BotUser, t.user_id)
                if bu:
                    to_notify.append((t.id, bu.telegram_id))
                closed += 1
            await s.commit()
        if to_notify:
            async with Session() as s:
                brow = await s.get(BotRow, bot_id)
            if brow and getattr(brow, "token", None):
                tb = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                try:
                    for tid, tg in to_notify:
                        try:
                            await tb.send_message(
                                tg, f"🎫 تیکت #{tid} چون بیش از {hours} ساعت جوابی نداشت بسته شد.\n"
                                    "اگر هنوز سؤالی داری، یک تیکتِ تازه ثبت کن 🌹")
                        except Exception:
                            logger.info("ticket auto-close: اطلاع به کاربر ممکن نشد (بی‌اهمیت)")
                finally:
                    await tb.session.close()
    return closed


async def ticket_autoclose_loop() -> None:
    import asyncio
    while True:
        try:
            await auto_close_answered_tickets_once()
        except Exception:
            logger.exception("ticket auto-close loop failed")
        await asyncio.sleep(600)


@ticket_router.callback_query(F.data.startswith("tkt:filter:"))
async def tkt_filter(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")  # tkt:filter:{status}[:{page}]
    status_filter = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 0
    await _show_tickets(cb, bot, status_filter=status_filter, page=page)


# === PATCH_TICKET_LIST_PAGINATION_V1 ===
_TKT_PAGE_SIZE = 10


async def _show_tickets(cb: CallbackQuery, bot: Bot, status_filter: str = "open", page: int = 0):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return

    # برچسب‌های فیلتر
    FILTER_LABELS = {
        "open":     "🟡 باز",
        "answered": "💬 بی‌پاسخ کاربر",
        "closed":   "✅ بسته",
    }

    async with Session() as s:
        # شمارش هر دسته
        counts = {}
        for st in ("open", "answered", "closed"):
            counts[st] = await s.scalar(
                select(func.count()).select_from(TenantTicket).where(
                    TenantTicket.bot_id == row.id,
                    TenantTicket.status == st
                )
            ) or 0

        total_in_filter = counts.get(status_filter, 0)
        pages = max(1, (total_in_filter + _TKT_PAGE_SIZE - 1) // _TKT_PAGE_SIZE)
        page = max(0, min(page, pages - 1))

        # تیکت‌های فیلتر شده (صفحه‌بندی‌شده — هر صفحه ۱۰ تا)
        tickets = (await s.scalars(
            select(TenantTicket).where(
                TenantTicket.bot_id == row.id,
                TenantTicket.status == status_filter
            ).order_by(TenantTicket.id.desc())
            .limit(_TKT_PAGE_SIZE).offset(page * _TKT_PAGE_SIZE)
        )).all()

        user_map = {t.user_id: None for t in tickets}
        for uid in user_map:
            user_map[uid] = await s.get(BotUser, uid)

    # دکمه‌های فیلتر — هر کدام در ردیف جداگانه تا تعداد خوانا باشد
    b_rows = []
    for st, label in FILTER_LABELS.items():
        marker = "▶️ " if st == status_filter else ""
        b_rows.append([InlineKeyboardButton(
            text=f"{marker}{label} ({counts[st]})",
            callback_data=f"tkt:filter:{st}:0"
        )])

    if not tickets:
        b_rows.append([InlineKeyboardButton(text="— تیکتی در این دسته وجود ندارد —", callback_data="noop")])
    else:
        for t in tickets:
            bu = user_map.get(t.user_id)
            tg_id = bu.telegram_id if bu else 0
            if bu:
                who = f"@{bu.username}" if bu.username else (bu.full_name or str(bu.telegram_id))
            else:
                who = "؟"
            # === PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
            # اگه موضوع ثبت شده، به‌جایِ ۲۰ حرفِ اولِ متن (که ممکنه وسطِ
            # جمله بریده بشه)، همون موضوعِ کوتاه نشون داده می‌شه.
            if t.subject:
                preview = t.subject[:24] + ("…" if len(t.subject) > 24 else "")
            else:
                preview = t.text[:20] + ("..." if len(t.text) > 20 else "")
            b_rows.append([InlineKeyboardButton(
                text=f"#{t.id} — {who} ({tg_id}) — {preview}",
                callback_data=f"tkt:view:{t.id}:{tg_id}"
            )])

    # ناوبریِ صفحه‌بندی (قبلی/بعدی)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"tkt:filter:{status_filter}:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"tkt:filter:{status_filter}:{page+1}"))
    if nav:
        b_rows.append(nav)

    # === PATCH_TICKET_ORDER_SETTINGS_V1 === (فدایی) کلیدهایِ پیگیریِ سفارش و بستنِ خودکار
    from tenant import get_setting as _gs
    _ob = (await _gs(row.id, "order_support_btn", "1")) != "0"
    _oa = (await _gs(row.id, "order_ticket_ask_text", "1")) != "0"
    _ah = (await _gs(row.id, "ticket_autoclose_hours", "0")) or "0"
    _ah = int(_ah) if str(_ah).isdigit() else 0
    b_rows.append([InlineKeyboardButton(
        text=("📞 دکمهٔ «پشتیبانی (پیگیریِ سفارش)» زیرِ پیام‌هایِ سفارش: ✅ روشن" if _ob
              else "📞 دکمهٔ «پشتیبانی (پیگیریِ سفارش)» زیرِ پیام‌هایِ سفارش: ⛔️ خاموش"),
        callback_data="tkt:cfg:ordbtn", style=ButtonStyle.PRIMARY)])
    if _ob:
        b_rows.append([InlineKeyboardButton(
            text=("💬 گرفتنِ متنِ پیام قبل از ثبتِ تیکتِ سفارش: ✅ روشن" if _oa
                  else "💬 گرفتنِ متنِ پیام قبل از ثبتِ تیکتِ سفارش: ⛔️ خاموش (فوری ثبت می‌شود)"),
            callback_data="tkt:cfg:ordask", style=ButtonStyle.PRIMARY)])
    b_rows.append([InlineKeyboardButton(
        text=(f"⏳ بستنِ خودکارِ تیکتِ جواب‌داده‌شده و بی‌پاسخِ کاربر: ✅ بعد از {_ah} ساعت" if _ah
              else "⏳ بستنِ خودکارِ تیکتِ جواب‌داده‌شده و بی‌پاسخِ کاربر: ⛔️ خاموش"),
        callback_data="tkt:cfg:auto", style=ButtonStyle.PRIMARY)])
    # === PATCH_PARTNER_SETTINGS_BACK_FIX_V1 ===
    _tkt_back_cb = "pp:settings" if row.plan == "partner" else "tadm:sec:admins"
    b_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=_tkt_back_cb, style=ButtonStyle.DANGER),
                   InlineKeyboardButton(text="🏠 پنل مدیریت", callback_data="tadm:menu", style=ButtonStyle.DANGER)])

    label_fa = FILTER_LABELS.get(status_filter, status_filter)
    txt = (f"🎫 <b>تیکت‌ها</b> — {label_fa}\n"
           f"مجموع: {total_in_filter} تیکت — صفحه {page+1}/{pages}")

    try:
        await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=b_rows))
    except Exception:
        await cb.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=b_rows))
    await cb.answer()


@ticket_router.callback_query(F.data.startswith("tkt:view:"))
async def tkt_view(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, _, tid, tg_id = cb.data.split(":")
    await _tkt_view_render(cb, int(tid), tg_id)


async def _tkt_view_render(cb: CallbackQuery, tid: int, tg_id) -> None:
    """رندرِ صفحهٔ نمایشِ تیکت — بدونِ خواندن از cb.data (نگاه کن به
    PATCH_CB_DATA_FROZEN_FIX_V1 در tenant/smm.py برایِ توضیحِ کاملِ باگ)."""
    from tenant import _jalali_date_time
    async with Session() as s:
        t = await s.get(TenantTicket, tid)
        if not t:
            await cb.answer("یافت نشد", show_alert=True)
            return
        replies = (await s.scalars(
            select(TenantTicketReply).where(TenantTicketReply.ticket_id == tid)
            .order_by(TenantTicketReply.id)
        )).all()
        # اطلاعات کاربر
        bu = await s.get(BotUser, t.user_id)

    # ساخت بخش مشخصات کاربر
    d, tm = _jalali_date_time(t.created_at)
    if bu:
        uname_part = f"@{bu.username}" if bu.username else "—"
        user_info = (
            f"👤 <b>{bu.full_name or 'بدون نام'}</b>\n"
            f"🔗 یوزرنیم: {uname_part}\n"
            f"🆔 آیدی: <code>{bu.telegram_id}</code>\n"
            f"🗓 تاریخ ارسال: {d}  🕒 {tm}"
        )
    else:
        user_info = f"🆔 آیدی: <code>{tg_id}</code>\n🗓 تاریخ ارسال: {d}  🕒 {tm}"

    thread = f"💬 پیام اول:\n{t.text}"
    for r in replies:
        who = "👤 کاربر" if r.sender == "user" else "👮 ادمین"
        thread += f"\n\n{who}:\n{r.text}"

    status_emoji = {"open": "🟡", "answered": "💬", "closed": "✅"}.get(t.status, "❓")

    kb_rows = []
    if t.status != "closed":
        kb_rows.append([InlineKeyboardButton(text="💬 پاسخ", callback_data=f"tkt:reply:{tid}:{tg_id}")])
        kb_rows.append([InlineKeyboardButton(text="✅ بستن تیکت", callback_data=f"tkt:close:{tid}")])
    else:
        kb_rows.append([InlineKeyboardButton(text="🔓 بازگشایی تیکت", callback_data=f"tkt:reopen:{tid}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:tickets", style=ButtonStyle.DANGER)])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    header = (
        f"🎫 <b>تیکت #{tid}</b>  {status_emoji}\n"
        f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"{user_info}\n"
    )
    # === PATCH_TICKET_CATEGORY_SUBJECT_V1 ===
    if t.subject:
        header += f"📌 موضوع: <b>{t.subject}</b>\n"
    if t.category:
        header += f"🏷 دسته: {TICKET_CATEGORY_LABEL.get(t.category, t.category)}\n"
    if t.rating:  # PATCH_TICKET_RATING_V1
        header += f"⭐️ امتیازِ کاربر: {t.rating}/5\n"
    header += f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n\n"
    if t.photo_file_id:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        full_text = header + thread
        caption = full_text if len(full_text) <= 1000 else full_text[:970] + "\n\n… (متن کامل کوتاه شد)"
        try:
            await cb.message.answer_photo(t.photo_file_id, caption=caption, reply_markup=kb)
        except Exception:
            # اگر عکس/فایل دیگر در دسترس نباشد (منقضی‌شده)، حداقل متن تیکت نمایش داده شود
            await cb.message.answer(caption + "\n\n⚠️ نمایش عکس ممکن نشد (فایل منقضی شده).",
                                    reply_markup=kb)
        await cb.answer()
        return
    try:
        await cb.message.edit_text(header + thread, reply_markup=kb)
    except Exception:
        await cb.message.answer(header + thread, reply_markup=kb)
    await cb.answer()


@ticket_router.callback_query(F.data.startswith("tkt:reply:"))
async def tkt_reply_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    _, _, tid, tg_id = cb.data.split(":")
    await state.set_state(TicketFSM.reply)
    await state.update_data(reply_tid=int(tid), reply_tg_id=int(tg_id), reply_bot_id=row.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.message.answer("💬 پاسخ خود را بنویسید:", reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


# === PATCH_TICKET_REPLY_ACCEPT_MEDIA_V1 ===
# باگِ گزارش‌شدهٔ ادمین‌ها: «برای جواب به تیکت می‌نویسه عملیات لغو شد و
# پیام ارسال نمی‌شه». علت: فیلترِ F.text فقط پیامِ متنیِ خالص را می‌گرفت،
# پس وقتی ادمین یک اسکرین‌شات (عکس + کپشن) می‌فرستاد — که در پشتیبانیِ
# VPN کارِ رایجی است — این هندلر اصلاً اجرا نمی‌شد، پیام به فالبکِ عمومی
# می‌رسید، فالبک state را پاک می‌کرد و «عملیات قبلی لغو شد» می‌داد.
# حالا هر نوع پیامی در این حالت گرفته می‌شود: متن، عکس با کپشن، عکسِ
# بی‌کپشن، و انواعِ پشتیبانی‌نشده که با پیامِ راهنما و *بدونِ پاک‌کردنِ
# state* رد می‌شوند تا ادمین بتواند دوباره تلاش کند.
@ticket_router.message(TicketFSM.reply)
async def tkt_reply_send(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, main_kb_or_remove, inline_back_kb, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    tid = data.get("reply_tid")
    tg_id = data.get("reply_tg_id")

    text = (msg.text or msg.caption or "").strip()
    photo_id = msg.photo[-1].file_id if msg.photo else None

    if text in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
        await msg.answer("لغو شد.", reply_markup=kb or ReplyKeyboardRemove())
        return

    if not tid:
        await state.clear()
        await msg.answer("❌ تیکت مشخص نیست. دوباره از لیستِ تیکت‌ها «💬 پاسخ» را بزن.")
        return

    # نه متن، نه عکس (مثلاً ویدیو/فایل/استیکر) → state حفظ می‌شود
    if not text and not photo_id:
        await msg.answer(
            "لطفاً پاسخ را به‌صورت <b>متن</b> یا <b>عکس (با توضیح)</b> بفرست.\n"
            "برای انصراف «❌ لغو» را بزن.",
            reply_markup=await smart_cancel_kb(bot))
        return

    await state.clear()

    async with Session() as s:
        t = await s.get(TenantTicket, tid)
        if not t or t.bot_id != row.id:
            await msg.answer("❌ تیکت یافت نشد.")
            return
        t.status = "answered"
        s.add(TenantTicketReply(ticket_id=tid, sender="admin",
                                text=text or "🖼 (تصویر)"))
        await s.commit()

    kb = await main_kb_or_remove(row.id, row.owner_id, msg.from_user.id)
    await msg.answer(
        f"✅ پاسخ تیکت #{tid} ارسال شد.",
        reply_markup=kb or ReplyKeyboardRemove()
    )
    await msg.answer(
        "برای مشاهده تیکت‌ها:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎫 تیکت‌ها", callback_data="tadm:tickets", style=ButtonStyle.PRIMARY)
        ]])
    )

    # ارسال پاسخ به کاربر
    try:
        _back = await inline_back_kb(row.id)
        if photo_id:
            await bot.send_photo(
                tg_id, photo_id,
                caption=f"💬 پاسخ تیکت #{tid}:" + (f"\n\n{text}" if text else ""),
                reply_markup=_back)
        else:
            await bot.send_message(tg_id, f"💬 پاسخ تیکت #{tid}:\n\n{text}",
                                   reply_markup=_back)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@ticket_router.callback_query(F.data.startswith("tkt:close:"))
async def tkt_close(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    tid = int(cb.data.split(":")[2])
    closed_now = False
    cust_tg_id = None  # PATCH_TICKET_RATING_V1
    async with Session() as s:
        t = await s.get(TenantTicket, tid)
        if t and t.bot_id == row.id:
            t.status = "closed"
            await s.commit()
            closed_now = True
            # === PATCH_TICKET_RATING_V1 ===
            # آیدیِ کاربر رو همین‌جا (وقتی سشن هنوز بازه) می‌خونیم تا بعداً
            # بتونیم پیامِ امتیازدهی رو براش بفرستیم.
            bu = await s.get(BotUser, t.user_id)
            cust_tg_id = bu.telegram_id if bu else None
    if not closed_now:
        await cb.answer("⛔️ این تیکت پیدا نشد یا متعلق به این ربات نیست.", show_alert=True)
        return
    await cb.answer("✅ تیکت بسته شد", show_alert=True)
    # پیام فعلی می‌تواند متنی یا عکس‌دار (caption) باشد؛ هر دو حالت را جدا هندل می‌کنیم
    # تا در صورت شکستِ یکی، حذفِ کیبورد و تاییدِ نهایی برای ادمین از دست نرود.
    if has_media(cb.message):  # === PATCH_INACCESSIBLE_MESSAGE_GUARD_V1 ===
        try:
            base_cap = cb.message.caption or ""
            suffix = "\n\n✅ این تیکت بسته شد."
            if len(base_cap) + len(suffix) > 1024:
                base_cap = base_cap[:1024 - len(suffix)]
            await cb.message.edit_caption(caption=base_cap + suffix, reply_markup=None)
        except Exception:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    else:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    try:
        await cb.message.answer(
            f"✅ تیکت #{tid} بسته شد.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت به تیکت‌ها", callback_data="tadm:tickets", style=ButtonStyle.DANGER)
            ]]))
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    # === PATCH_TICKET_RATING_V1 ===
    # قبلاً وقتی ادمین تیکت رو می‌بست، کاربر اصلاً هیچ پیامی نمی‌گرفت — باید
    # خودش می‌رفت «تیکت‌های من» رو چک می‌کرد تا بفهمه بسته شده. حالا هم
    # مطلع می‌شه، هم می‌تونه به پشتیبانی امتیاز بده.
    if cust_tg_id:
        try:
            await bot.send_message(
                cust_tg_id,
                f"✅ تیکت #{tid} توسطِ پشتیبانی بسته شد.\n\n"
                "لطفاً به کیفیتِ پاسخ‌گویی امتیاز بده:",
                reply_markup=_rating_kb(tid))
        except Exception:
            logger.exception("اطلاعِ کاربر از بسته‌شدنِ تیکت ناموفق بود")


@ticket_router.callback_query(F.data.startswith("tkt:reopen:"))
async def tkt_reopen(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    tid = int(cb.data.split(":")[2])
    async with Session() as s:
        t = await s.get(TenantTicket, tid)
        if t and t.bot_id == row.id:
            t.status = "open"
            await s.commit()
        # === PATCH_CB_DATA_FROZEN_FIX_V1 ===
        # نکته: قبلاً اینجا از رویِ عمد "0" به‌جایِ tg_id واقعی گذاشته
        # می‌شد (فقط برایِ حالتِ نادری که کاربر از دیتابیس پیدا نشود
        # استفاده می‌شود)؛ حالا که داریم درست می‌سازیمش، آیدیِ واقعی را
        # هم می‌خوانیم تا آن حالتِ نادر هم درست نمایش داده شود.
        bu = await s.get(BotUser, t.user_id) if t else None
        tg_id = bu.telegram_id if bu else 0
    await cb.answer("🔓 تیکت دوباره باز شد", show_alert=True)
    await _tkt_view_render(cb, tid, tg_id)
