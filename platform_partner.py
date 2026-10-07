"""
# === PATCH_PARTNER_FINALIZE_LIST_V1 ===
platform_partner.py — بخشِ «ربات همکار» جدا از platform_bot.py.

=== PATCH_PARTNER_BOT_V1 ===
فازِ ۱: جریانِ درخواستِ همکاری.
  • دکمهٔ «🤝 درخواست همکاری» برای کاربرِ بات‌ساز (قابلِ خاموش/روشن‌کردن
    توسطِ مدیر از طریقِ pset کلیدِ "partner_req_on")
  • پذیرشِ قوانینِ ضدکلاهبرداری قبل از ثبت
  • اطلاع‌رسانی به مدیرِ پلتفرم با اطلاعاتِ متقاضی
  • گفت‌وگویِ دوطرفهٔ مدیر↔متقاضی (شبیهِ تیکت)
  • تایید / رد، و کم‌وزیادکردنِ روزهایِ اعتبارِ ربات همکار

فازهایِ بعدی (پنلِ مدیریتِ همکار، خریدِ عمده، پنلِ نظارتیِ مادر) در
همین فایل یا فایل‌هایِ هم‌خانواده اضافه می‌شوند.
"""
from __future__ import annotations

import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select, func

from config import Session, PLATFORM_ADMIN_ID
from models import (
    PlatformUser, PartnerRequest, PartnerMessage, PartnerBot, PartnerSale,
    PlatformTxn,
)
from plans import Plan

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_router = Router()

# === PATCH_PARTNER_PANEL_V1 ===
# قواعدِ عددی به partner_rules.py منتقل شد تا سرویسِ مستأجر هم دقیقاً
# همان اعداد را ببیند (قبلاً این‌جا محلی تعریف شده بود).
from partner_rules import (  # noqa: E402
    LOW_STOCK_GRACE_DAYS, MIN_ACTIVE_SALES, min_active_sales, log_stock,
    DEFAULT_PARTNER_DISCOUNT,
    jalali,  # === PATCH_PARTNER_V4 ===
)


# ────────── lazy-import wrappers از platform_bot ──────────

async def smart_home_kb(uid: int, show_admin: bool = False):
    from platform_bot import smart_home_kb as _fn
    return await _fn(uid, show_admin=show_admin)


async def smart_cancel_kb():
    from platform_bot import smart_cancel_kb as _fn
    return await _fn()


async def get_pset(key: str, default: str = "") -> str:
    from platform_bot import get_pset as _fn
    return await _fn(key, default)


async def set_pset(key: str, value: str) -> None:
    from platform_bot import set_pset as _fn
    await _fn(key, value)


# ────────── FSM ──────────

class PartnerFSM(StatesGroup):
    note = State()          # متقاضی توضیحش را می‌نویسد
    admin_reply = State()   # مدیر به متقاضی پیام می‌دهد
    user_reply = State()    # متقاضی به مدیر پیام می‌دهد
    reject_reason = State() # مدیر دلیلِ رد را می‌نویسد
    set_days = State()      # مدیر تعدادِ روزِ اعتبار را وارد می‌کند


# ────────── متنِ قوانین ──────────

PARTNER_TERMS = (
    "📜 <b>قوانین همکاری</b>\n\n"
    "۱. ربات همکار به شما اجازه می‌دهد ربات‌های «رشد» و «حرفه‌ای» را با "
    "<b>تخفیف همکاری</b> به‌صورت عمده تهیه و به کاربران خودتان بفروشید.\n\n"
    "۲. <b>مسئولیت فروش کاملاً با شماست.</b> هرگونه اختلاف مالی، وعدهٔ "
    "خدماتی که ارائه نکرده‌اید، یا شکایت کاربرانِ شما، به بات‌ساز مربوط "
    "نیست و بات‌ساز هیچ تعهدی در قبال آن ندارد.\n\n"
    "۳. <b>کلاهبرداری ممنوع است.</b> اگر مشخص شود از ربات همکار برای "
    "کلاهبرداری، فروش خدمات غیرقانونی، یا فریب کاربران استفاده کرده‌اید، "
    "حساب شما <b>مسدود</b> و ربات همکار و همهٔ ربات‌های ساخته‌شده با آن "
    "<b>بدون بازگشت وجه غیرفعال</b> می‌شود.\n\n"
    # === PATCH_PARTNER_MIN_ACTIVE_SETTING_V1 === {min} در لحظهٔ نمایش پر می‌شود
    "۴. اگر تعداد ربات‌های فعالِ فروخته‌شدهٔ شما کمتر از <b>{min}</b> "
    f"ربات باشد، از همان لحظه یک مهلت <b>{LOW_STOCK_GRACE_DAYS} روزه</b> شروع "
    "می‌شود؛ اگر تا پایان مهلت به حد نصاب نرسید، ربات همکار به‌صورت خودکار "
    "غیرفعال می‌شود. به‌محض رسیدن دوباره به حد نصاب، مهلت لغو می‌شود.\n\n"
    "۵. مدیریت بات‌ساز در هر زمان به اطلاعات ربات همکار و ربات‌های "
    "ساخته‌شده با آن دسترسی نظارتی دارد.\n\n"
    "با زدن دکمهٔ زیر، این قوانین را می‌پذیرید."
)


# ────────── کمکی‌ها ──────────

async def partner_requests_enabled() -> bool:
    """آیا دکمهٔ «درخواست همکاری» برای کاربران فعال است؟ (پیش‌فرض: خاموش)"""
    return (await get_pset("partner_req_on", "off")) == "on"


async def active_sales_count(partner_bot_id: int) -> int:
    """تعدادِ ربات‌هایِ فعالِ فروخته‌شدهٔ این همکار (تست‌ها شمرده نمی‌شوند)."""
    from models import Bot as BotRow
    async with Session() as s:
        n = await s.scalar(
            select(func.count(PartnerSale.id))
            .join(BotRow, BotRow.id == PartnerSale.sold_bot_id)
            .where(
                PartnerSale.partner_bot_id == partner_bot_id,
                PartnerSale.kind == "sold",
                BotRow.status == "active",
                BotRow.archived_at.is_(None),
            )
        )
    return int(n or 0)


async def refresh_low_stock_timer(partner_bot_id: int) -> None:
    """تایمرِ کشویی (LOW_STOCK_GRACE_DAYS روزه) را بر اساسِ تعدادِ فعلیِ ربات‌های فعال به‌روز می‌کند.

    هر جا که یک فروش ثبت/باطل می‌شود یا رباتی فعال/غیرفعال می‌گردد باید
    صدا زده شود. زیرِ حدِ نصاب → تایمر (اگر نبود) شروع می‌شود؛ بالای حدِ
    نصاب → تایمر پاک می‌شود.
    """
    n = await active_sales_count(partner_bot_id)
    async with Session() as s:
        pb = await s.get(PartnerBot, partner_bot_id)
        if not pb:
            return
        # === PATCH_PARTNER_MIN_ACTIVE_SETTING_V1 ===
        if n >= await min_active_sales():
            if pb.low_stock_since is not None:
                pb.low_stock_since = None
                pb.low_stock_notice = None
                await s.commit()
        else:
            if pb.low_stock_since is None:
                pb.low_stock_since = datetime.datetime.now()
                await s.commit()


def _fmt_dt(dt: datetime.datetime | None) -> str:
    # === PATCH_PARTNER_V4 === شمسی، به وقتِ تهران
    return jalali(dt, with_time=True)


async def _admin_request_kb(req_id: int, status: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if status == "pending":
        b.button(text="✅ تایید و فعال‌سازی", callback_data=f"pnr:approve:{req_id}",
                 style=ButtonStyle.SUCCESS)
        b.button(text="❌ رد درخواست", callback_data=f"pnr:reject:{req_id}",
                 style=ButtonStyle.DANGER)
    b.button(text="✉️ ارسال پیام به متقاضی", callback_data=f"pnr:amsg:{req_id}",
             style=ButtonStyle.PRIMARY)
    b.button(text="🗂 مشاهدهٔ گفت‌وگو", callback_data=f"pnr:thread:{req_id}",
             style=ButtonStyle.PRIMARY)
    # === PATCH_PARTNER_BACKNAV_V1 ===
    # این کارت هیچ راهِ برگشتی نداشت و ادمین توش گیر می‌کرد.
    b.button(text="🔙 فهرستِ درخواست‌ها", callback_data="pa:reqs:0",
             style=ButtonStyle.DANGER)
    b.adjust(2, 1, 1, 1)
    return b.as_markup()


async def _request_detail_text(req_id: int) -> str | None:
    """=== PATCH_PARTNER_FINALIZE_LIST_V1 === متنِ جزئیاتِ یک درخواست؛ هم برای اطلاعِ اولیه و
    هم برای فهرستِ «درخواست‌های ثبت‌شده» در پنلِ ربات‌های همکار استفاده می‌شود."""
    async with Session() as s:
        r = await s.get(PartnerRequest, req_id)
        if not r:
            return None
        pu = await s.scalar(
            select(PlatformUser).where(PlatformUser.telegram_id == r.user_telegram_id))
        bal = int(pu.balance) if pu else 0
        joined = _fmt_dt(pu.created_at) if pu else "—"
        uname, name, note, tg = r.username, r.full_name, r.note, r.user_telegram_id

    from models import Bot as BotRow
    async with Session() as s:
        bots_n = await s.scalar(
            select(func.count(BotRow.id)).where(BotRow.owner_id == tg))

    return (
        "🤝 <b>درخواست همکاری</b>\n\n"
        f"👤 نام: {name or '—'}\n"
        f"🆔 آیدی عددی: <code>{tg}</code>\n"
        f"📎 یوزرنیم: {('@' + uname) if uname else '—'}\n"
        f"💰 موجودی کیف پول: {bal:,} تومان\n"
        f"🤖 تعداد ربات‌های فعلی: {bots_n or 0}\n"
        f"📅 عضویت در بات‌ساز: {joined}\n\n"
        f"📝 توضیح متقاضی:\n{note or '—'}"
    )


async def _notify_admin_new_request(bot: Bot, req_id: int) -> None:
    txt = await _request_detail_text(req_id)
    if not txt:
        return
    try:
        sent = await bot.send_message(PLATFORM_ADMIN_ID, txt,
                               reply_markup=await _admin_request_kb(req_id, "pending"))
        # === PATCH_PARTNER_FINALIZE_LIST_V1 === ذخیرهٔ شناسهٔ پیام برای ویرایشِ آن هنگامِ تایید/رد
        async with Session() as s:
            r = await s.get(PartnerRequest, req_id)
            if r:
                r.admin_chat_id = sent.chat.id
                r.admin_msg_id = sent.message_id
                await s.commit()
    except Exception:
        logger.exception("ارسالِ اطلاعِ درخواستِ همکاری به مدیر ناموفق بود")


# ────────── سمتِ کاربر: ثبتِ درخواست ──────────

async def partner_request_start(msg: Message, state: FSMContext, bot: Bot):
    """ورودیِ دکمهٔ «🤝 درخواست همکاری» در منویِ اصلیِ بات‌ساز."""
    await state.clear()
    uid = msg.from_user.id

    if not await partner_requests_enabled():
        await msg.answer(
            "🚧 در حال حاضر پذیرش درخواست همکاری <b>غیرفعال</b> است. "
            "لطفاً بعداً دوباره سر بزن.",
            reply_markup=await smart_home_kb(uid))
        return

    async with Session() as s:
        pending = await s.scalar(select(PartnerRequest).where(
            PartnerRequest.user_telegram_id == uid,
            PartnerRequest.status == "pending"))
        approved = await s.scalar(select(PartnerRequest).where(
            PartnerRequest.user_telegram_id == uid,
            PartnerRequest.status == "approved"))
        # === PATCH_PARTNER_REQUEST_AFTER_DELETE_V1 ===
        # صرفِ «approved» بودنِ یک درخواستِ قدیمی نباید برایِ همیشه جلویِ
        # درخواستِ جدید را بگیرد؛ اگر مدیر رباتِ همکارِ کاربر را حذف کرده
        # (soft-delete: status="deleted") یا اصلاً هیچ‌وقت نساخته، دیگر
        # رباتِ همکارِ زنده‌ای وجود ندارد و کاربر باید بتواند دوباره
        # درخواست بدهد.
        live_partner_bot = None
        if approved:
            live_partner_bot = await s.scalar(select(PartnerBot).where(
                PartnerBot.owner_id == uid,
                PartnerBot.status != "deleted"))

    if approved and live_partner_bot:
        await msg.answer(
            "✅ درخواست همکاری شما قبلاً <b>تایید</b> شده است. "
            "ربات همکار شما از بخش «🤖 ربات‌های من» در دسترس است.",
            reply_markup=await smart_home_kb(uid))
        return
    if pending:
        b = InlineKeyboardBuilder()
        b.button(text="✉️ ارسال پیام به پشتیبانی", callback_data=f"pnr:umsg:{pending.id}",
                 style=ButtonStyle.PRIMARY)
        b.adjust(1)
        await msg.answer(
            "⏳ شما یک درخواست همکاری <b>در حال بررسی</b> دارید. "
            "نتیجه به‌زودی اطلاع داده می‌شود.",
            reply_markup=b.as_markup())
        return

    b = InlineKeyboardBuilder()
    b.button(text="✅ قوانین را می‌پذیرم", callback_data="pnr:accept",
             style=ButtonStyle.SUCCESS)  # === PATCH_PARTNER_V2 ===
    b.button(text="🔙 انصراف", callback_data="pnr:cancel",
             style=ButtonStyle.DANGER)  # === PATCH_PARTNER_V2 ===
    b.adjust(1)
    await msg.answer(PARTNER_TERMS.replace("{min}", str(await min_active_sales())),
                     reply_markup=b.as_markup())


@partner_router.callback_query(F.data == "pnr:cancel")
async def partner_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("درخواست همکاری لغو شد.",
                                   reply_markup=await smart_home_kb(cb.from_user.id))
    except Exception:
        await cb.message.answer("درخواست همکاری لغو شد.",
                                reply_markup=await smart_home_kb(cb.from_user.id))
    await cb.answer()


@partner_router.callback_query(F.data == "pnr:home")
async def partner_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.delete()
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("حذف پیام درخواست همکاری ناموفق بود")
    await cb.message.answer("🏠 به منوی اصلی برگشتی.",
                            reply_markup=await smart_home_kb(cb.from_user.id))
    await cb.answer()


def _partner_note_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🏠 بازگشت به خانه", callback_data="pnr:home", style=ButtonStyle.DANGER)
    b.adjust(1)
    return b.as_markup()


@partner_router.callback_query(F.data == "pnr:accept")
async def partner_accept_terms(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PartnerFSM.note)
    await cb.message.edit_text(
        "📝 لطفاً در یک پیام کوتاه بنویس:\n\n"
        "• چه تجربه‌ای در فروش ربات یا خدمات آنلاین داری؟\n"
        "• کانال/گروه یا مشتری‌های فعلی‌ات چقدرند؟\n"
        "• ماهانه تقریباً چند ربات فکر می‌کنی بفروشی؟\n\n"
        "این توضیح مستقیماً برای مدیریت ارسال می‌شود.",
        reply_markup=_partner_note_kb())
    await cb.answer()


@partner_router.message(PartnerFSM.note)
async def partner_note_received(msg: Message, state: FSMContext, bot: Bot):
    note = (msg.text or "").strip()
    if len(note) < 20:
        await msg.answer("لطفاً توضیح کامل‌تری بنویس (حداقل ۲۰ کاراکتر).")
        return
    await state.clear()
    uid = msg.from_user.id

    async with Session() as s:
        dup = await s.scalar(select(PartnerRequest).where(
            PartnerRequest.user_telegram_id == uid,
            PartnerRequest.status == "pending"))
        if dup:
            req_id = dup.id
        else:
            r = PartnerRequest(
                user_telegram_id=uid,
                full_name=msg.from_user.full_name,
                username=msg.from_user.username,
                note=note[:4000],
                status="pending",
                terms_accepted=True,
            )
            s.add(r)
            await s.commit()
            req_id = r.id

    await msg.answer(
        "✅ درخواست همکاری شما ثبت شد.\n\n"
        "پس از بررسی، نتیجه از همین ربات به شما اطلاع داده می‌شود. "
        "اگر لازم باشد، مدیریت از همین‌جا با شما گفت‌وگو می‌کند.",
        reply_markup=await smart_home_kb(uid))
    await _notify_admin_new_request(bot, req_id)


# ────────── گفت‌وگویِ دوطرفه ──────────

@partner_router.callback_query(F.data.startswith("pnr:amsg:"))
async def partner_admin_msg_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    req_id = int(cb.data.split(":")[2])
    await state.update_data(pnr_req=req_id)
    await state.set_state(PartnerFSM.admin_reply)
    await cb.message.answer("✉️ پیامت را برای متقاضی بنویس و بفرست:",
                            reply_markup=await smart_cancel_kb())
    await cb.answer()


@partner_router.message(PartnerFSM.admin_reply)
async def partner_admin_msg_send(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    req_id = data.get("pnr_req")
    await state.clear()
    text = (msg.text or "").strip()
    if not req_id or not text:
        await msg.answer("پیام نامعتبر بود.")
        return

    async with Session() as s:
        r = await s.get(PartnerRequest, req_id)
        if not r:
            await msg.answer("درخواست پیدا نشد.")
            return
        target = r.user_telegram_id
        s.add(PartnerMessage(request_id=req_id, sender="admin", text=text[:4000]))
        await s.commit()

    b = InlineKeyboardBuilder()
    b.button(text="✍️ پاسخ", callback_data=f"pnr:umsg:{req_id}",
             style=ButtonStyle.PRIMARY)
    b.adjust(1)
    try:
        await bot.send_message(
            target,
            f"📩 <b>پیام از پشتیبانی بات‌ساز</b> (دربارهٔ درخواست همکاری)\n\n{text}",
            reply_markup=b.as_markup())
        await msg.answer("✅ پیام ارسال شد.")
    except Exception:
        logger.exception("ارسالِ پیامِ همکاری به کاربر ناموفق بود")
        await msg.answer("⚠️ ارسال پیام ناموفق بود (شاید کاربر ربات را بلاک کرده).")


@partner_router.callback_query(F.data.startswith("pnr:umsg:"))
async def partner_user_msg_start(cb: CallbackQuery, state: FSMContext):
    req_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(PartnerRequest, req_id)
        owner = r.user_telegram_id if r else None
    if owner != cb.from_user.id:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await state.update_data(pnr_req=req_id)
    await state.set_state(PartnerFSM.user_reply)
    await cb.message.answer("✍️ پاسخت را بنویس و بفرست:",
                            reply_markup=await smart_cancel_kb())
    await cb.answer()


@partner_router.message(PartnerFSM.user_reply)
async def partner_user_msg_send(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    req_id = data.get("pnr_req")
    await state.clear()
    text = (msg.text or "").strip()
    if not req_id or not text:
        await msg.answer("پیام نامعتبر بود.")
        return

    async with Session() as s:
        s.add(PartnerMessage(request_id=req_id, sender="user", text=text[:4000]))
        await s.commit()

    b = InlineKeyboardBuilder()
    b.button(text="✉️ پاسخ", callback_data=f"pnr:amsg:{req_id}",
             style=ButtonStyle.PRIMARY)
    b.button(text="🗂 گفت‌وگو", callback_data=f"pnr:thread:{req_id}",
             style=ButtonStyle.PRIMARY)
    b.adjust(2)
    try:
        await bot.send_message(
            PLATFORM_ADMIN_ID,
            f"📨 <b>پاسخ متقاضی همکاری</b> (#{req_id})\n"
            f"👤 <code>{msg.from_user.id}</code>\n\n{text}",
            reply_markup=b.as_markup())
    except Exception:
        logger.exception("ارسالِ پاسخِ متقاضی به مدیر ناموفق بود")
    await msg.answer("✅ پیامت ارسال شد.",
                     reply_markup=await smart_home_kb(msg.from_user.id))


@partner_router.callback_query(F.data.startswith("pnr:thread:"))
async def partner_thread_view(cb: CallbackQuery):
    req_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(PartnerRequest, req_id)
        if not r:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        owner, status = r.user_telegram_id, r.status
        msgs = (await s.scalars(
            select(PartnerMessage)
            .where(PartnerMessage.request_id == req_id)
            .order_by(PartnerMessage.id))).all()
        rows = [(m.sender, m.text, m.created_at) for m in msgs]

    if cb.from_user.id not in (PLATFORM_ADMIN_ID, owner):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return

    if not rows:
        await cb.answer("هنوز پیامی رد و بدل نشده", show_alert=True)
        return

    lines = [f"🗂 <b>گفت‌وگوی درخواست #{req_id}</b>\n"]
    for sender, text, created in rows[-20:]:
        who = "🛡 پشتیبانی" if sender == "admin" else "👤 متقاضی"
        lines.append(f"{who} — {_fmt_dt(created)}\n{text}\n")
    await cb.message.answer("\n".join(lines),
                            reply_markup=await _admin_request_kb(req_id, status)
                            if cb.from_user.id == PLATFORM_ADMIN_ID else None)
    await cb.answer()


# === PATCH_PARTNER_FINALIZE_LIST_V1 ===
async def _close_request_messages(
    bot: Bot, req_id: int, final_text: str,
    extra_chat_id: int | None = None, extra_msg_id: int | None = None,
) -> None:
    """پیامِ اطلاعِ اولیهٔ درخواست (و پیامِ محلِ کلیکِ فعلی، اگر متفاوت
    باشد) را ویرایش می‌کند تا نتیجهٔ نهایی را نشان دهد و دکمه‌هایِ
    تایید/رد از بین بروند — از پردازشِ دوباره جلوگیری می‌کند."""
    targets: set[tuple[int, int]] = set()
    async with Session() as s:
        r = await s.get(PartnerRequest, req_id)
        if r and r.admin_chat_id and r.admin_msg_id:
            targets.add((r.admin_chat_id, r.admin_msg_id))
    if extra_chat_id and extra_msg_id:
        targets.add((extra_chat_id, extra_msg_id))
    for chat_id, msg_id in targets:
        try:
            await bot.edit_message_text(
                final_text, chat_id=chat_id, message_id=msg_id)
        except Exception:
            logger.exception("بستنِ پیامِ درخواستِ همکاری ناموفق بود")


# ────────── رد درخواست ──────────

@partner_router.callback_query(F.data.startswith("pnr:reject:"))
async def partner_reject_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    req_id = int(cb.data.split(":")[2])
    # === PATCH_PARTNER_FINALIZE_LIST_V1 === محلِ کلیک ذخیره می‌شود تا پس از تصمیم، همان پیام
    # هم (در کنارِ پیامِ اطلاعِ اولیه) ویرایش و دکمه‌هایش حذف شود.
    await state.update_data(
        pnr_req=req_id,
        pnr_src_chat=cb.message.chat.id, pnr_src_msg=cb.message.message_id)
    await state.set_state(PartnerFSM.reject_reason)
    await cb.message.answer("❌ دلیل رد را بنویس (عیناً به متقاضی نشان داده می‌شود):",
                            reply_markup=await smart_cancel_kb())
    await cb.answer()


@partner_router.message(PartnerFSM.reject_reason)
async def partner_reject_finish(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    req_id = data.get("pnr_req")
    src_chat = data.get("pnr_src_chat")
    src_msg = data.get("pnr_src_msg")
    await state.clear()
    reason = (msg.text or "").strip()
    if not req_id or not reason:
        await msg.answer("عملیات نامعتبر بود.")
        return

    async with Session() as s:
        r = await s.get(PartnerRequest, req_id)
        if not r or r.status != "pending":
            await msg.answer("این درخواست قبلاً تعیین تکلیف شده است.")
            return
        r.status = "rejected"
        r.reject_reason = reason[:2000]
        r.decided_at = datetime.datetime.now()
        target = r.user_telegram_id
        await s.commit()

    try:
        await bot.send_message(
            target,
            "❌ <b>درخواست همکاری شما پذیرفته نشد.</b>\n\n"
            f"دلیل:\n{reason}\n\n"
            "در صورت رفع مورد بالا می‌توانی دوباره درخواست بدهی.")
    except Exception:
        logger.exception("اطلاعِ ردِ درخواستِ همکاری به کاربر ناموفق بود")
    # === PATCH_PARTNER_FINALIZE_LIST_V1 === بستنِ پیامِ (های) نمایشِ درخواست
    await _close_request_messages(
        bot, req_id,
        f"❌ درخواست #{req_id} رد شد.\n\nدلیل:\n{reason}",
        src_chat, src_msg)
    await msg.answer("✅ درخواست رد شد و به متقاضی اطلاع داده شد.")


# ────────── تایید و فعال‌سازی ──────────

@partner_router.callback_query(F.data.startswith("pnr:approve:"))
async def partner_approve_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    req_id = int(cb.data.split(":")[2])
    # === PATCH_PARTNER_FINALIZE_LIST_V1 === محلِ کلیک ذخیره می‌شود تا پس از تایید، همان پیام
    # هم (در کنارِ پیامِ اطلاعِ اولیه) ویرایش و دکمه‌هایش حذف شود.
    await state.update_data(
        pnr_req=req_id, pnr_days=30,
        pnr_src_chat=cb.message.chat.id, pnr_src_msg=cb.message.message_id)
    menu = await cb.message.answer(_approve_text(30, "none", 30), reply_markup=_days_kb(30))
    # شناسهٔ همین منو ذخیره می‌شود تا «تایید نهایی» از منویِ قدیمیِ درخواستِ
    # دیگری، این درخواست را تأیید نکند
    await state.update_data(pnr_menu_msg=menu.message_id)
    await cb.answer()


# === PATCH_PARTNER_WELCOME_GIFT_V1 ===
# هنگامِ تاییدِ همکاری، مدیر می‌تواند علاوه بر رباتِ همکار، یک رباتِ
# رشد/حرفه‌ای هم به‌عنوانِ «هدیهٔ شروعِ همکاری» بدهد. هدیه از همان سازوکارِ
# «اهدای مستقیمِ تستِ پلن» استفاده می‌کند (plan_trials.grant_direct)، پس
# کاربر می‌تواند یک رباتِ تازه با آن پلن بسازد و سررسیدش خودکار اعمال شود.
GIFT_CYCLE = ["none", "growth", "pro"]
GIFT_LABELS = {
    "none":   "🎁 هدیه: بدون هدیه",
    "growth": "🎁 هدیه: رباتِ رشد",
    "pro":    "🎁 هدیه: رباتِ حرفه‌ای",
}


def _gift_plan_value(gift: str) -> str | None:
    if gift == "growth":
        return Plan.GROWTH.value
    if gift == "pro":
        return Plan.PRO.value
    return None


def _approve_text(days: int, gift: str, gdays: int) -> str:
    lines = [
        "🗓 <b>تاییدِ درخواستِ همکاری</b>",
        "",
        f"⏳ اعتبارِ رباتِ همکار: <b>{days} روز</b>",
    ]
    if gift == "none":
        lines.append("🎁 هدیهٔ شروعِ همکاری: <b>ندارد</b>")
    else:
        name = "رشد" if gift == "growth" else "حرفه‌ای"
        lines.append(f"🎁 هدیهٔ شروعِ همکاری: یک رباتِ <b>{name}</b> "
                     f"به مدتِ <b>{gdays} روز</b>")
    lines += ["", "با دکمه‌ها تنظیم کن، بعد «تایید نهایی» را بزن."]
    return "\n".join(lines)


def _days_kb(days: int, gift: str = "none", gdays: int = 30) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    sfx = f"{gift}:{gdays}"
    b.button(text="➖ ۳۰", callback_data=f"pnr:days:{max(1, days - 30)}:{sfx}",
             style=ButtonStyle.DANGER)
    b.button(text="➖ ۷", callback_data=f"pnr:days:{max(1, days - 7)}:{sfx}",
             style=ButtonStyle.DANGER)
    b.button(text=f"{days} روز", callback_data="pnr:noop",
             style=ButtonStyle.PRIMARY)
    b.button(text="➕ ۷", callback_data=f"pnr:days:{days + 7}:{sfx}",
             style=ButtonStyle.SUCCESS)
    b.button(text="➕ ۳۰", callback_data=f"pnr:days:{days + 30}:{sfx}",
             style=ButtonStyle.SUCCESS)
    # === PATCH_PARTNER_WELCOME_GIFT_V1 === انتخابِ هدیه (چرخشی)
    nxt = GIFT_CYCLE[(GIFT_CYCLE.index(gift) + 1) % len(GIFT_CYCLE)]
    b.button(text=GIFT_LABELS[gift], callback_data=f"pnr:gift:{days}:{nxt}:{gdays}",
             style=ButtonStyle.SUCCESS if gift != "none" else ButtonStyle.PRIMARY)
    if gift != "none":
        b.button(text="➖ ۷ روزِ هدیه",
                 callback_data=f"pnr:gift:{days}:{gift}:{max(1, gdays - 7)}",
                 style=ButtonStyle.DANGER)
        b.button(text=f"هدیه: {gdays} روز", callback_data="pnr:noop",
                 style=ButtonStyle.PRIMARY)
        b.button(text="➕ ۷ روزِ هدیه",
                 callback_data=f"pnr:gift:{days}:{gift}:{gdays + 7}",
                 style=ButtonStyle.SUCCESS)
    b.button(text="✅ تایید نهایی و فعال‌سازی",
             callback_data=f"pnr:confirm:{days}:{gift}:{gdays}",
             style=ButtonStyle.SUCCESS)
    b.button(text="🔙 انصراف", callback_data="pnr:cancel",
             style=ButtonStyle.DANGER)
    b.adjust(*((5, 1, 3, 1, 1) if gift != "none" else (5, 1, 1, 1)))
    return b.as_markup()


@partner_router.callback_query(F.data == "pnr:noop")
async def partner_noop(cb: CallbackQuery):
    await cb.answer()


@partner_router.callback_query(F.data.startswith("pnr:days:"))
async def partner_days_adjust(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    parts = cb.data.split(":")          # pnr:days:{days}[:{gift}:{gdays}]
    days = int(parts[2])
    gift = parts[3] if len(parts) > 3 else "none"
    gdays = int(parts[4]) if len(parts) > 4 else 30
    await state.update_data(pnr_days=days, pnr_gift=gift, pnr_gdays=gdays)
    await _refresh_approve_msg(cb, days, gift, gdays)
    await cb.answer(f"{days} روز")


@partner_router.callback_query(F.data.startswith("pnr:gift:"))
async def partner_gift_adjust(cb: CallbackQuery, state: FSMContext):
    """تغییرِ نوع/مدتِ هدیهٔ شروعِ همکاری. === PATCH_PARTNER_WELCOME_GIFT_V1 ==="""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    _, _, days, gift, gdays = cb.data.split(":")
    days, gdays = int(days), int(gdays)
    if gift not in GIFT_CYCLE:
        gift = "none"
    await state.update_data(pnr_days=days, pnr_gift=gift, pnr_gdays=gdays)
    await _refresh_approve_msg(cb, days, gift, gdays)
    await cb.answer(GIFT_LABELS[gift])


async def _refresh_approve_msg(cb: CallbackQuery, days: int, gift: str, gdays: int) -> None:
    try:
        await cb.message.edit_text(_approve_text(days, gift, gdays),
                                   reply_markup=_days_kb(days, gift, gdays))
    except Exception:
        logger.exception("ویرایشِ پیامِ تاییدِ همکاری ناموفق بود")


@partner_router.callback_query(F.data.startswith("pnr:confirm:"))
async def partner_approve_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    parts = cb.data.split(":")          # pnr:confirm:{days}[:{gift}:{gdays}]
    days = int(parts[2])
    gift = parts[3] if len(parts) > 3 else "none"
    gdays = int(parts[4]) if len(parts) > 4 else 0
    data = await state.get_data()
    req_id = data.get("pnr_req")
    src_chat = data.get("pnr_src_chat")
    src_msg = data.get("pnr_src_msg")
    menu_msg = data.get("pnr_menu_msg")
    if menu_msg and cb.message and cb.message.message_id != menu_msg:
        await cb.answer("این منو مالِ درخواستِ دیگری است؛ دوباره «تایید» را "
                        "رویِ همان درخواست بزن.", show_alert=True)
        return
    await state.clear()
    if not req_id:
        await cb.answer("درخواست نامعتبر", show_alert=True)
        return

    async with Session() as s:
        r = await s.get(PartnerRequest, req_id, with_for_update=True)
        if not r or r.status != "pending":
            await cb.answer("این درخواست قبلاً تعیین تکلیف شده", show_alert=True)
            return
        r.status = "approved"
        r.granted_days = days  # === PATCH_PARTNER_CREATE_V1 ===
        r.decided_at = datetime.datetime.now()
        target = r.user_telegram_id
        await s.commit()

    # === PATCH_PARTNER_WELCOME_GIFT_V1 === اهدایِ رباتِ هدیه (اگر انتخاب شده)
    gift_note = ""
    gift_admin = ""
    plan_value = _gift_plan_value(gift)
    if plan_value and gdays > 0:
        try:
            import plan_trials
            status, info = await plan_trials.grant_direct(
                target, "", plan_value, gdays)
            gname = "رشد" if gift == "growth" else "حرفه‌ای"
            if status == "ok":
                gift_note = (f"\n🎁 <b>هدیهٔ شروع همکاری:</b> یک رباتِ <b>{gname}</b> "
                             f"به مدتِ <b>{gdays} روز</b> برایت فعال شد — "
                             "از «🤖 ساخت ربات» می‌توانی بسازیش.\n")
                gift_admin = f"\n🎁 هدیه: رباتِ {gname} — {gdays} روز."
            elif status == "exists_active":
                gift_admin = ("\n⚠️ هدیه داده نشد: این کاربر همین حالا یک "
                              "پلنِ اهدایی/تستِ فعال دارد.")
            else:
                gift_admin = f"\n⚠️ هدیه داده نشد (وضعیت: {status})."
        except Exception:
            logger.exception("اهدایِ رباتِ هدیهٔ شروعِ همکاری ناموفق بود")
            gift_admin = "\n⚠️ هدیه داده نشد (خطای داخلی — لاگ را ببین)."

    try:
        await bot.send_message(
            target,
            "🎉 <b>درخواست همکاری شما تایید شد!</b>\n\n"
            f"⏳ مدت اعتبار: <b>{days} روز</b>\n"
            f"🏷 تخفیف همکاری: <b>{DEFAULT_PARTNER_DISCOUNT}٪</b> روی قیمت عادی پلن‌ها\n"
            f"{gift_note}\n"
            "برای ساخت ربات همکار، از منوی اصلی وارد بخش «🤖 ساخت ربات» شو و "
            "گزینهٔ «🤝 ربات همکار» را انتخاب کن. سپس مثل بقیهٔ ربات‌ها "
            "صفحهٔ اصلی و دکمه‌هایش را می‌چینی.")
    except Exception:
        logger.exception("اطلاعِ تاییدِ همکاری به کاربر ناموفق بود")

    final_txt = (
        f"✅ درخواست #{req_id} تایید شد. اعتبار: {days} روز.{gift_admin}\n"
        "کاربر می‌تواند از «ساخت ربات» ربات همکارش را بسازد.")
    await cb.message.edit_text(final_txt)
    # === PATCH_PARTNER_FINALIZE_LIST_V1 === بستنِ پیامِ اطلاعِ اولیه (اگر پیامِ فعلی همان نیست)
    await _close_request_messages(bot, req_id, final_txt, src_chat, src_msg)
    await cb.answer()


# ────────── کلیدِ فعال/غیرفعالِ پذیرشِ درخواست (مدیر) ──────────

@partner_router.callback_query(F.data == "pnr:toggle")
async def partner_toggle_requests(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    now_on = await partner_requests_enabled()
    await set_pset("partner_req_on", "off" if now_on else "on")
    state_txt = "غیرفعال" if now_on else "فعال"
    b = InlineKeyboardBuilder()
    b.button(text=("🔴 غیرفعال‌سازی" if not now_on else "🟢 فعال‌سازی"),
             callback_data="pnr:toggle",
             style=(ButtonStyle.SUCCESS if now_on else ButtonStyle.DANGER))
    b.adjust(1)
    await cb.message.edit_text(
        f"🤝 پذیرش درخواست همکاری اکنون <b>{state_txt}</b> است.",
        reply_markup=b.as_markup())
    await cb.answer()


# ======================= ساخت ربات همکار =======================
# === PATCH_PARTNER_CREATE_V1 ===
# ربات همکار عمداً از مسیرِ choose_field/Create عبور نمی‌کند:
#   • سقفِ رایگانِ ساختِ ربات شاملش نمی‌شود (کاربر بابتِ همکاری هزینه داده)
#   • انتخابِ «زمینه» ندارد — زمینه‌اش همیشه virtual_service است
#   • پلنش با پول خریده نمی‌شود؛ با تاییدِ مدیر باز می‌شود
# ولی *بعد* از ساخت، عیناً به همان ویزاردِ چیدمانِ صفحهٔ اصلی (Build.grid)
# تحویل داده می‌شود تا تجربهٔ کاربر یکسان بماند.

# === PATCH_PARTNER_V4 ===
# قبلاً "virtual_service" بود که باعث می‌شد در مرحلهٔ تعیینِ عملکردِ
# دکمه‌ها، گزینه‌هایِ «افزایش ممبر/ری‌اکشن/ویو» بیاید — هیچ‌کدام به
# رباتِ همکار ربطی ندارند. حالا شاخهٔ اختصاصیِ خودش را دارد
# (نگاه کن به action_kb در platform_bot.py).
PARTNER_FIELD = "partner"


class PartnerCreate(StatesGroup):
    token = State()


async def _pb_set_tenant_webhook(token: str) -> bool:
    from platform_bot import set_tenant_webhook as _fn
    return await _fn(token)


async def _pb_apply_brand_bio(token: str, plan: str) -> None:
    from platform_bot import apply_brand_bio as _fn
    await _fn(token, plan)


async def partner_eligibility(uid: int) -> tuple[int, int] | None:
    """اگر کاربر تاییدشده است و هنوز ربات همکارش را نساخته،
    (request_id, granted_days) برمی‌گرداند؛ وگرنه None."""
    async with Session() as s:
        r = await s.scalar(
            select(PartnerRequest)
            .where(PartnerRequest.user_telegram_id == uid,
                   PartnerRequest.status == "approved")
            .order_by(PartnerRequest.id.desc()))
        if not r:
            return None
        req_id, days = r.id, (r.granted_days or 30)
        # === PATCH_PARTNER_REQUEST_AFTER_DELETE_V1 ===
        # ردیف‌هایِ حذف‌شده (soft-delete, status="deleted") نباید مانعِ
        # ساختِ رباتِ همکارِ جدید برایِ همین کاربر بشوند.
        existing = await s.scalar(
            select(func.count(PartnerBot.id)).where(
                PartnerBot.owner_id == uid,
                PartnerBot.status != "deleted"))
        # همان درخواستِ تأییدشده فقط یک بار ربات می‌سازد؛ اگر رباتِ آن حذف شد
        # (مثلاً به‌خاطرِ تخلف)، ساختِ دوباره درخواستِ تأییدِ جدید می‌خواهد
        used = await s.scalar(
            select(func.count(PartnerBot.id)).where(PartnerBot.request_id == req_id))
    if existing or used:
        return None
    return req_id, days


async def partner_build_start(msg: Message, state: FSMContext, bot: Bot):
    """ورودیِ دکمهٔ «🤝 ساخت ربات همکار»."""
    uid = msg.from_user.id
    elig = await partner_eligibility(uid)
    if not elig:
        await msg.answer(
            "🚫 برای ساخت ربات همکار، اول باید درخواست همکاری‌ات "
            "<b>تایید</b> شده باشد و قبلاً ربات همکاری نساخته باشی.",
            reply_markup=await smart_home_kb(uid))
        return

    req_id, days = elig
    await state.clear()
    await state.update_data(pnr_req=req_id, pnr_days=days)
    await state.set_state(PartnerCreate.token)
    await msg.answer(
        "🤝 <b>ساخت ربات همکار</b>\n\n"
        f"⏳ اعتبار: <b>{days} روز</b>\n\n"
        "۱. به <a href=\"https://t.me/BotFather\">@BotFather</a> برو\n"
        "۲. با <code>/newbot</code> یک ربات جدید بساز\n"
        "۳. توکنی که می‌دهد را همین‌جا بفرست\n\n"
        "⚠️ توکن ربات را در اختیار کسی قرار نده.",
        reply_markup=await smart_cancel_kb(),
        disable_web_page_preview=True)


@partner_router.message(PartnerCreate.token)
async def partner_save_token(msg: Message, state: FSMContext, bot: Bot):
    from models import Bot as BotRow
    from models import Setting

    token = (msg.text or "").strip()
    if ":" not in token or len(token) < 30:
        await msg.answer("توکن معتبر نیست. دوباره بفرست.")
        return

    from bot_utils import _temp_bot
    try:
        async with _temp_bot(token) as tmp:
            me = await tmp.get_me()
    except Exception:
        await msg.answer("نتونستم با این توکن وصل بشم. درست کپی شده؟")
        return

    data = await state.get_data()
    req_id = data.get("pnr_req")
    days = int(data.get("pnr_days") or 30)
    uid = msg.from_user.id

    # بازبینیِ دوباره: بینِ شروعِ فلو و ارسالِ توکن ممکن است شرایط عوض شده باشد
    elig = await partner_eligibility(uid)
    if not req_id or not elig or int(elig[0]) != int(req_id):
        await state.clear()
        await msg.answer(
            "⚠️ این مرحله دیگر معتبر نیست. لطفاً دوباره از منوی اصلی شروع کن.",
            reply_markup=await smart_home_kb(uid))
        return

    async with Session() as s:
        from bot_utils import reclaim_token_if_archived
        _freed, dup = await reclaim_token_if_archived(s, token, uid)  # PATCH_TOKEN_RECLAIM_V1
        if dup:
            await msg.answer(
                "این توکن همین حالا رویِ یک رباتِ فعال در بات‌ساز ثبت است. "
                "اگر مالِ خودته اول از «🗂 ربات‌های من» حذفش کن، بعد دوباره بفرست.")
            return

        # قفلِ درخواست: دو پیامِ توکنِ هم‌زمان از یک تأیید دو ربات نسازند
        await s.get(PartnerRequest, req_id, with_for_update=True)
        if await s.scalar(select(func.count(PartnerBot.id)).where(
                PartnerBot.request_id == req_id)):
            await state.clear()
            await msg.answer("رباتِ همکارت قبلاً ساخته شده.",
                             reply_markup=await smart_home_kb(uid))
            return

        expiry = datetime.datetime.now() + datetime.timedelta(days=days)
        row = BotRow(owner_id=uid, token=token, username=me.username,
                     field=PARTNER_FIELD, plan=Plan.PARTNER.value,
                     status="active", plan_expires_at=expiry)
        s.add(row)
        await s.flush()

        s.add(Setting(bot_id=row.id, key="card_number", value=""))

        pb = PartnerBot(
            bot_id=row.id,
            owner_id=uid,
            request_id=req_id,
            discount_percent=DEFAULT_PARTNER_DISCOUNT,
            status="active",
            expires_at=expiry,
            trial_enabled=True,
        )
        s.add(pb)
        await s.commit()
        bot_id, pb_id = row.id, pb.id

    # تایمرِ LOW_STOCK_GRACE_DAYS‌روزه از همان ابتدا شروع می‌شود (چون هنوز صفر فروش دارد)
    await refresh_low_stock_timer(pb_id)

    import asyncio
    results = await asyncio.gather(
        _pb_set_tenant_webhook(token),
        _pb_apply_brand_bio(token, Plan.PARTNER.value),
        return_exceptions=True,
    )
    webhook_ok = results[0] if not isinstance(results[0], Exception) else False

    hook_note = "" if webhook_ok else (
        "\n\n⚠️ <b>اتصال ربات برقرار نشد!</b>\n"
        "برو «🗂 ربات‌های من» → روی ربات بزن → «🔄 فعال‌سازی/اتصال مجدد».")

    try:
        await bot.send_message(
            PLATFORM_ADMIN_ID,
            "🤝 <b>ربات همکار ساخته شد</b>\n\n"
            f"👤 مالک: <code>{uid}</code>\n"
            f"🤖 ربات: @{me.username}\n"
            f"⏳ اعتبار: {days} روز\n"
            f"🏷 تخفیف: {DEFAULT_PARTNER_DISCOUNT}٪")
    except Exception:
        logger.exception("اطلاعِ ساختِ رباتِ همکار به مدیر ناموفق بود")

    # تحویل به ویزاردِ مشترکِ چیدمانِ صفحهٔ اصلی
    from platform_bot import Build, grid_kb, GRID_INTRO
    await state.clear()
    await state.update_data(build_bot_id=bot_id, grid_selected=[],
                            grid_plan=Plan.PARTNER.value, grid_field=PARTNER_FIELD)
    await state.set_state(Build.grid)
    await msg.answer(
        f"✅ ربات همکار @{me.username} ساخته شد!\n"
        f"⏳ اعتبار تا: {jalali(expiry)}\n"
        f"🏷 تخفیف همکاری: {DEFAULT_PARTNER_DISCOUNT}٪ روی قیمت عادی پلن‌ها"
        f"{hook_note}\n\n" + GRID_INTRO,
        reply_markup=grid_kb(set(), Plan.PARTNER.value),
    )


# ======================= تاییدِ رسیدِ خریدِ عمده (مدیرِ پلتفرم) =======================
# === PATCH_PARTNER_PANEL_V1 ===
# رسید در سرویسِ مستأجر ثبت می‌شود ولی تاییدش این‌جاست، چون مدیر روی
# دکمه‌اش در *ربات مادر* کلیک می‌کند.

# === PATCH_PARTNER_ORDER_SMS_AUTO_V1 ===
async def pp_order_auto_approve(order_id: int) -> bool:
    """تاییدِ خودکارِ سفارشِ خریدِ عمدهٔ همکار، وقتی پیامکِ بانکی با مبلغِ
    اختصاصیِ فاکتور تطبیق پیدا کنه (بدونِ نیاز به کلیکِ دستیِ ادمین) — همون
    اثری که دکمهٔ «✅ تایید» تویِ partner_stock_order_decide داره، فقط
    منبعِ صدازدن فرق می‌کنه (پیامکِ بانکی به‌جایِ کلیکِ ادمین)."""
    from models import PartnerStockOrder
    async with Session() as s:
        # قفلِ ردیف: تأییدِ پیامکی و کلیکِ دستیِ ادمین هم‌زمان دو بار موجودی اضافه نکنند
        o = await s.get(PartnerStockOrder, order_id, with_for_update=True)
        if not o or o.status != "pending":
            return False
        pb = await s.get(PartnerBot, o.partner_bot_id, with_for_update=True)
        if not pb:
            return False
        owner = pb.owner_id
        g, p = o.qty_growth, o.qty_pro
        o.status = "approved"
        pb.stock_growth += g
        pb.stock_pro += p
        if g:
            await log_stock(s, pb.id, "growth", g, "buy",
                            "خرید عمده (تاییدِ خودکار — پیامکِ بانکی)", pb.stock_growth)
        if p:
            await log_stock(s, pb.id, "pro", p, "buy",
                            "خرید عمده (تاییدِ خودکار — پیامکِ بانکی)", pb.stock_pro)
        pb.bought_growth += g
        pb.bought_pro += p
        new_g, new_p = pb.stock_growth, pb.stock_pro
        o.decided_at = datetime.datetime.now()
        # === PATCH_PARTNER_PURCHASE_IN_FIN_REPORT_V1 ===
        # این تراکنش هم باید تویِ گزارشِ ماه‌به‌ماهِ «شارژهایِ موفق» دیده
        # بشه، پس یک PlatformTxn با کایندِ partner_purchase ثبت می‌کنیم.
        s.add(PlatformTxn(user_telegram_id=owner, amount=float(o.amount or 0),
                          kind="partner_purchase",
                          note=f"AUTO:خریدِ عمدهٔ همکار — سفارش #{order_id}"))
        await s.commit()

    try:
        from main_platform import platform_bot as _pbot
        await _pbot.send_message(
            owner,
            "✅ <b>پرداختت خودکار تایید شد!</b>\n\n"
            f"🚀 رشد: +{g}\n💎 حرفه‌ای: +{p}\n\n"
            f"📦 موجودیِ جدید:\n🚀 رشد: <b>{new_g}</b>\n💎 حرفه‌ای: <b>{new_p}</b>\n\n"
            "🧾 روش: 📲 کارت‌به‌کارتِ خودکار (پیامکِ بانکی)")
    except Exception:
        logger.exception("اطلاعِ تاییدِ خودکارِ سفارشِ خریدِ عمده به همکار ناموفق بود")

    try:
        if PLATFORM_ADMIN_ID:
            from main_platform import platform_bot as _pbot_notify
            await _pbot_notify.send_message(
                PLATFORM_ADMIN_ID,
                "📲 <b>خریدِ عمدهٔ همکار خودکار تایید شد</b>\n\n"
                f"👤 همکار: <code>{owner}</code>\n"
                f"🚀 رشد: {g} | 💎 حرفه‌ای: {p}\n"
                f"💰 مبلغ: {int(o.amount or 0):,} تومان\n"
                f"🧾 سفارش #{order_id}\n"
                "🧾 روش: 📲 کارت‌به‌کارتِ خودکار (پیامکِ بانکی)")
    except Exception:
        logger.exception("اطلاعِ تاییدِ خودکارِ سفارشِ خریدِ عمده به ادمین ناموفق بود")
    return True


# === PATCH_PARTNER_ORDER_SMS_AUTO_V1 ===
@partner_router.callback_query(F.data.startswith("ppsmsexp:yes:"))
async def pp_order_expired_admin_confirm(cb: CallbackQuery):
    """ادمین تایید کرد که همکار واقعاً واریز کرده — همون تاییدِ خودکار رو
    دستی صدا می‌زنه (چون پیامکِ بانکی نرسیده بود)."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    order_id = int(cb.data.split(":")[2])
    from models import PartnerStockOrder
    async with Session() as s:
        o = await s.get(PartnerStockOrder, order_id)
        if not o or o.status != "pending":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
    ok = await pp_order_auto_approve(order_id)
    try:
        done = (cb.message.text or "") + "\n\n✅ <b>تایید شد.</b> موجودیِ همکار اضافه گردید."
        await cb.message.edit_text(done, reply_markup=None)
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("تایید و اعمال شد ✅" if ok else "خطا در اعمالِ سفارش", show_alert=not ok)


@partner_router.callback_query(F.data.startswith("ppsmsexp:no:"))
async def pp_order_expired_admin_reject(cb: CallbackQuery, bot: Bot):
    """ادمین گفت این همکار واریز نکرده — سفارش رد می‌شود."""
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    order_id = int(cb.data.split(":")[2])
    from models import PartnerStockOrder
    async with Session() as s:
        o = await s.get(PartnerStockOrder, order_id)
        if not o or o.status != "pending":
            await cb.answer("این سفارش قبلاً رسیدگی شده.", show_alert=True)
            return
        o.status = "rejected"
        o.decided_at = datetime.datetime.now()
        pb = await s.get(PartnerBot, o.partner_bot_id)
        owner = pb.owner_id if pb else None
        await s.commit()
    if owner:
        try:
            from main_platform import platform_bot as _pbot
            await _pbot.send_message(
                owner,
                "❌ <b>سفارشِ خریدِ عمده‌ات رد شد.</b>\n\n"
                "اگر فکر می‌کنی اشتباهی رخ داده، از بخش پشتیبانی پیام بده.")
        except Exception:
            logger.exception("اطلاعِ ردِ سفارشِ خریدِ عمده به همکار ناموفق بود")
    try:
        done = (cb.message.text or "") + "\n\n❌ <b>رد شد.</b> واریزی برایِ این سفارش ثبت نشد."
        await cb.message.edit_text(done, reply_markup=None)
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    await cb.answer("رد شد ❌")


@partner_router.callback_query(F.data.startswith("ppord:"))
async def partner_stock_order_decide(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("دسترسی ندارید", show_alert=True)
        return

    from models import PartnerStockOrder
    _, raw_id, verdict = cb.data.split(":")
    order_id = int(raw_id)

    async with Session() as s:
        # قفلِ ردیف: دو کلیکِ هم‌زمان (یا کلیک + تأییدِ پیامکی) دو بار موجودی اضافه نکند
        o = await s.get(PartnerStockOrder, order_id, with_for_update=True)
        if not o:
            await cb.answer("سفارش پیدا نشد", show_alert=True)
            return
        if o.status != "pending":
            await cb.answer("این سفارش قبلاً تعیین تکلیف شده", show_alert=True)
            return

        pb = await s.get(PartnerBot, o.partner_bot_id, with_for_update=True)
        if not pb:
            await cb.answer("ربات همکار پیدا نشد", show_alert=True)
            return

        owner = pb.owner_id
        g, p, amount = o.qty_growth, o.qty_pro, int(o.amount or 0)

        if verdict == "y":
            o.status = "approved"
            pb.stock_growth += g
            pb.stock_pro += p
            # === PATCH_PARTNER_STOCK_LOG_V1 ===
            if g:
                await log_stock(s, pb.id, "growth", g, "buy",
                                "خرید عمده", pb.stock_growth)
            if p:
                await log_stock(s, pb.id, "pro", p, "buy",
                                "خرید عمده", pb.stock_pro)
            pb.bought_growth += g
            pb.bought_pro += p
            new_g, new_p = pb.stock_growth, pb.stock_pro
            # === PATCH_PARTNER_PURCHASE_IN_FIN_REPORT_V1 ===
            s.add(PlatformTxn(user_telegram_id=owner, amount=float(amount),
                              kind="partner_purchase",
                              note=f"خریدِ عمدهٔ همکار (تاییدِ دستیِ ادمین) — سفارش #{order_id}"))
        else:
            o.status = "rejected"
            new_g = new_p = 0
        o.decided_at = datetime.datetime.now()
        await s.commit()

    if verdict == "y":
        user_txt = (
            "✅ <b>پرداخت شما تایید شد</b>\n\n"
            f"🚀 رشد: +{g}\n💎 حرفه‌ای: +{p}\n\n"
            f"📦 موجودی جدید:\n🚀 رشد: <b>{new_g}</b>\n💎 حرفه‌ای: <b>{new_p}</b>")
        admin_txt = f"✅ سفارش #{order_id} تایید شد ({g} رشد + {p} حرفه‌ای)."
    else:
        user_txt = (
            "❌ <b>رسید شما تایید نشد.</b>\n\n"
            "اگر فکر می‌کنی اشتباهی رخ داده، از بخش پشتیبانی پیام بده.")
        admin_txt = f"❌ سفارش #{order_id} رد شد."

    try:
        await bot.send_message(owner, user_txt)
    except Exception:
        logger.exception("اطلاعِ نتیجهٔ سفارشِ عمده به همکار ناموفق بود")

    try:
        await cb.message.edit_caption(caption=admin_txt)
    except Exception:
        try:
            await cb.message.edit_text(admin_txt)
        except Exception:
            logger.exception("ویرایشِ پیامِ سفارش ناموفق بود")
    await cb.answer()


# ======================= ورودیِ ساخت از فلویِ «ساخت ربات» =======================
# === PATCH_PARTNER_V3 ===
# دکمهٔ «🤝 ساخت ربات همکار» از صفحهٔ اصلی به داخلِ choose_field منتقل شد،
# پس ورودی‌اش حالا یک callback است نه یک دکمهٔ متنی.

@partner_router.callback_query(F.data == "pnr:buildstart")
async def partner_build_from_create(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await cb.answer()
    fake = cb.message.model_copy(update={"from_user": cb.from_user}).as_(bot)
    await partner_build_start(fake, state, bot)
