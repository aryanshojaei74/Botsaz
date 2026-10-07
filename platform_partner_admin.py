"""
platform_partner_admin.py — پنلِ نظارتیِ «ربات‌های همکار» در ربات مادر.

=== PATCH_PARTNER_ADMIN_V1 ===
جدا از platform_partner.py نگه داشته شد چون آن فایل با جریانِ درخواست و
ساختِ ربات همکار به ۸۰۰+ خط رسیده بود؛ همان قاعده‌ای که برایِ
platform_bot.py گذاشته‌ایم این‌جا هم رعایت می‌شود.

چرا این پنل لازم است: همکار می‌تواند ربات بفروشد و پول ندهد، یا با ربات
همکارش ربات‌هایی بسازد که در فهرستِ عادیِ «ربات‌های ساخته‌شده» گم شوند.
این‌جا زنجیرهٔ کاملِ مالکیت دیده می‌شود:
    ربات مادر → ربات همکار → ربات‌هایِ فروخته‌شده
و مدیر می‌تواند هر سه سطح را متوقف/فعال کند.

دسترسی: PLATFORM_ADMIN_ID، یا ادمینِ فرعیِ فعال با کلیدِ دسترسیِ
"adm:partner" (=== PATCH_PARTNER_SUBADMIN_V1 ===؛ مدیریتِ خودِ
ادمین‌هایِ فرعی — افزودن/حذف/تنظیمِ دسترسی — همچنان فقط برایِ
PLATFORM_ADMIN_ID است، از طریقِ دکمهٔ «ادمین‌های فرعی» که به
adm:sub_admins در platform_bot.py وصل می‌شود).
"""
from __future__ import annotations

import datetime
import html as _html  # === PATCH_PARTNER_MSGOWNER_ESCAPE_V1 ===
import json  # === PATCH_PARTNER_SUBADMIN_V1 ===

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import (  # === PATCH_ESC_REPLY_IMPORT_FIX_V1 ===
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    BufferedInputFile,  # === PATCH_PARTNER_BROADCAST_PHOTO_V1 ===
)
import base64 as _b64  # === PATCH_PARTNER_BROADCAST_PHOTO_V1 ===
from io import BytesIO as _BytesIO  # === PATCH_PARTNER_BROADCAST_PHOTO_V1 ===
from sqlalchemy import select, func, or_

from config import Session, PLATFORM_ADMIN_ID
from models import (
    Bot as BotRow, BotUser, PlatformUser, PlatformTxn,
    PartnerBot, PartnerSale, PartnerStockOrder,
    SubAdmin,  # === PATCH_PARTNER_SUBADMIN_V1 ===
)
from partner_rules import (
    LOW_STOCK_GRACE_DAYS, MIN_ACTIVE_SALES, min_active_sales, set_min_active_sales,
    partner_term_enabled, set_partner_term_enabled,  # PATCH_PARTNER_NO_TERM_EXPIRY_V1
    log_stock,
    jalali,  # === PATCH_PARTNER_V4 ===
)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

partner_admin_router = Router()

PAGE_SIZE = 8


class PartnerAdminFSM(StatesGroup):
    search = State()
    days_manual = State()   # === PATCH_PARTNER_V34 ===
    stock_manual = State()  # === PATCH_PARTNER_V34 ===
    message_owner = State()
    set_discount = State()
    set_discount_all = State()  # === PATCH_PARTNER_DISCOUNT_ALL_V1 ===
    broadcast_msg = State()  # === PATCH_PARTNER_BROADCAST_V1 ===
    wallet_adj = State()     # === PATCH_PARTNER_WALLET_CHARGE_V1 ===


# === PATCH_PARTNER_SUBADMIN_V1 ===
# پیاده‌سازیِ محلی (بدونِ ایمپورت از platform_bot.py، چون آن فایل خودش
# partner_admin_router را از این‌جا ایمپورت می‌کند و ایمپورتِ برعکس
# باعثِ circular import می‌شود). PLATFORM_ADMIN_ID همیشه دسترسیِ کامل
# دارد؛ ادمینِ فرعی فقط اگر فعال باشد و کلیدِ "adm:partner" را در
# دسترسی‌هایش داشته باشد.
async def _can_access(uid: int) -> bool:
    if uid == PLATFORM_ADMIN_ID:
        return True
    async with Session() as s:
        sa = await s.scalar(
            select(SubAdmin).where(SubAdmin.telegram_id == uid,
                                    SubAdmin.is_active == True)
        )
        if not sa:
            return False
        try:
            perms = json.loads(sa.permissions) if sa.permissions else []
        except Exception:
            return False
        return "adm:partner" in perms


async def _admin_only(cb: CallbackQuery) -> bool:
    return await _can_access(cb.from_user.id)


def _self_dealing(uid: int, pb) -> bool:
    """ادمینِ فرعی نباید کیف‌پول/موجودی/روزِ رباتِ همکارِ *خودش* را عوض کند."""
    return uid != PLATFORM_ADMIN_ID and pb is not None and pb.owner_id == uid


# === PATCH_PARTNER_BACKNAV_V1 ===
# هر مرحله‌ای که منتظرِ متنِ ادمین می‌ماند باید راهِ بیرون‌آمدن داشته باشد،
# و آن راه باید دقیقاً «یک صفحه عقب» باشد نه پرش به خانه.
def _cancel_kb(back_to: str, label: str = "🔙 انصراف") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=back_to,
                              style=ButtonStyle.DANGER)]])


def _fmt_dt(dt: datetime.datetime | None) -> str:
    # === PATCH_PARTNER_V4 === شمسی، به وقتِ تهران
    return jalali(dt)


# ────────── فهرستِ همکاران ──────────

@partner_admin_router.callback_query(F.data == "pa:home")
async def pa_home(cb: CallbackQuery):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    async with Session() as s:
        total = await s.scalar(select(func.count(PartnerBot.id))) or 0
        active = await s.scalar(select(func.count(PartnerBot.id))
                                .where(PartnerBot.status == "active")) or 0
        low = await s.scalar(select(func.count(PartnerBot.id))
                             .where(PartnerBot.low_stock_since.isnot(None))) or 0

    # === PATCH_PARTNER_V2 ===
    # وضعیتِ کلیدِ پذیرشِ درخواست، تا مدیر بدونِ رفتن به دیتابیس ببیندش.
    from platform_partner import partner_requests_enabled
    req_on = await partner_requests_enabled()

    b = InlineKeyboardBuilder()
    b.button(text="📋 مشاهدهٔ ربات‌های همکار", callback_data="pa:list:0",
             style=ButtonStyle.PRIMARY)
    b.button(text="🔎 جستجوی ربات همکار", callback_data="pa:search",
             style=ButtonStyle.PRIMARY)
    # === PATCH_PARTNER_MANAGE_V1 ===
    b.button(text="🗂 مدیریت همکاران", callback_data="pm:home",
             style=ButtonStyle.SUCCESS)
    b.button(text="👥 مدیریت کاربری همکاران", callback_data="pa:users:0",
             style=ButtonStyle.PRIMARY)
    b.button(text="🆘 تیکت‌های ارجاعی همکاران", callback_data="pae:list",
             style=ButtonStyle.PRIMARY)  # === PATCH_PARTNER_V12 ===
    # === PATCH_PARTNER_FINALIZE_LIST_V1 ===
    pending_n = await s_pending_requests_count()
    b.button(
        text=f"📋 درخواست‌های ثبت‌شده برای همکاری ({pending_n})",
        callback_data="pa:reqs:0", style=ButtonStyle.PRIMARY)
    # === PATCH_PARTNER_BROADCAST_V1 ===
    b.button(text="📢 پیام همگانی به همکاران", callback_data="pa:bcast:start",
             style=ButtonStyle.PRIMARY)
    # === PATCH_PARTNER_DISCOUNT_ALL_V1 ===
    _min_now = await min_active_sales()   # PATCH_PARTNER_MIN_ACTIVE_SETTING_V1
    _term_on = await partner_term_enabled()   # PATCH_PARTNER_NO_TERM_EXPIRY_V1
    b.button(text="🏷 تخفیف همگانی روی همهٔ همکاران", callback_data="pa:discall:start",
             style=ButtonStyle.PRIMARY)
    # === PATCH_PARTNER_MIN_ACTIVE_SETTING_V1 ===
    b.button(text=f"🎯 تعیین حد نصاب (الان: {_min_now})",
             callback_data="pa:minactive:start", style=ButtonStyle.PRIMARY)
    # === PATCH_PARTNER_NO_TERM_EXPIRY_V1 ===
    b.button(
        text=("🔴 خاموش‌کردن انقضای دوره‌ای همکار" if _term_on
              else "🟢 روشن‌کردن انقضای دوره‌ای همکار"),
        callback_data="pa:termtoggle",
        style=(ButtonStyle.DANGER if _term_on else ButtonStyle.SUCCESS))
    b.button(
        text=("🔴 غیرفعال‌کردن درخواست همکاری" if req_on
              else "🟢 فعال‌کردن درخواست همکاری"),
        callback_data="pa:reqtoggle",
        style=(ButtonStyle.DANGER if req_on else ButtonStyle.SUCCESS))
    # === PATCH_PARTNER_SUBADMIN_V1 ===
    # فقط مدیرِ اصلی این دکمه را می‌بیند؛ مدیریتِ خودِ ادمین‌های فرعی
    # (افزودن/حذف/تنظیمِ دسترسی) صرفاً کارِ مدیرِ اصلی است.
    if cb.from_user.id == PLATFORM_ADMIN_ID:
        b.button(text="👑 ادمین‌های فرعیِ این بخش", callback_data="adm:sub_admins",
                 style=ButtonStyle.PRIMARY)
    b.button(text="🔙 بازگشت به پنل مدیریت", callback_data="adm:back",
             style=ButtonStyle.DANGER)
    b.adjust(1)
    await cb.message.edit_text(
        "🤝 <b>ربات‌های همکار</b>\n\n"
        f"مجموع: <b>{total}</b>\n"
        f"فعال: <b>{active}</b>\n"
        f"زیر حد نصاب: <b>{low}</b>\n"
        f"🎯 حد نصاب فعلی: <b>{_min_now}</b> ربات فعال"
        + ("  (خاموش)" if _min_now == 0 else "") + "\n\n"
        f"📥 پذیرش درخواست همکاری: "
        f"<b>{'باز' if req_on else 'بسته'}</b>\n"
        f"⏳ انقضای دوره‌ای ربات همکار: "
        f"<b>{'روشن' if _term_on else 'خاموش (بی‌انقضا)'}</b>",
        reply_markup=b.as_markup())
    await cb.answer()


# === PATCH_PARTNER_MIN_ACTIVE_SETTING_V1 ===
class PaMinActiveFSM(StatesGroup):
    value = State()


@partner_admin_router.callback_query(F.data == "pa:minactive:start")
async def pa_min_active_start(cb: CallbackQuery, state: FSMContext):
    """پرسیدنِ حدِ نصابِ جدیدِ ربات‌های فعالِ همکار."""
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    cur = await min_active_sales()
    await state.set_state(PaMinActiveFSM.value)
    await cb.message.answer(
        "🎯 <b>تعیین حد نصاب</b>\n\n"
        f"حد نصاب فعلی: <b>{cur}</b> ربات فعال\n"
        f"مهلت ارفاقی: <b>{LOW_STOCK_GRACE_DAYS} روز</b>\n\n"
        "عددِ جدید را بفرست. هر همکاری که ربات‌های فعالش کمتر از این عدد "
        "باشد، از همان لحظه مهلت می‌گیرد و اگر تا پایانِ مهلت جبران نکند، "
        "ربات همکارش خودکار غیرفعال می‌شود.\n\n"
        "عددِ <b>۰</b> یعنی این قاعده کلاً خاموش شود.",
        reply_markup=_cancel_kb("pa:home"))
    await cb.answer()


@partner_admin_router.message(PaMinActiveFSM.value, F.text)
async def pa_min_active_save(msg: Message, state: FSMContext):
    if not await _can_access(msg.from_user.id):
        await state.clear()
        return
    raw = (msg.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not raw.isdigit():
        await msg.answer("عدد نامعتبر است. یک عددِ صحیح بفرست (مثلاً 10):")
        return
    val = int(raw)
    if val > 1000:
        await msg.answer("این عدد خیلی بزرگ است؛ حداکثر ۱۰۰۰.")
        return
    await state.clear()
    await set_min_active_sales(val)
    await msg.answer(
        f"✅ حد نصاب روی <b>{val}</b> ربات فعال تنظیم شد."
        + ("\n\n⚠️ با ۰، قاعدهٔ حد نصاب عملاً خاموش است و هیچ همکاری "
           "بابتِ کمبودِ فروش غیرفعال نمی‌شود." if val == 0 else
           f"\n\nهمکارانی که زیر این عدد باشند، {LOW_STOCK_GRACE_DAYS} روز "
           "مهلت می‌گیرند. این تغییر در اجرای بعدیِ بررسیِ روزانه اعمال می‌شود."))


# === PATCH_PARTNER_NO_TERM_EXPIRY_V1 ===
@partner_admin_router.callback_query(F.data == "pa:termtoggle")
async def pa_term_toggle(cb: CallbackQuery):
    """روشن/خاموش‌کردنِ «انقضایِ دوره‌ایِ رباتِ همکار».

    خاموش (پیش‌فرض) یعنی رباتِ همکار تاریخِ انقضا ندارد: نه پیامِ «پلن را
    تمدید کن» می‌گیرد و نه سرِ ۳۰ روز بایگانی می‌شود. تنها قاعده‌ای که
    می‌تواند خوابانَدش، حدِ نصابِ ربات‌های فعال است.
    """
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    new_val = not await partner_term_enabled()
    await set_partner_term_enabled(new_val)
    # cb.answer عمداً اینجا صدا زده نمی‌شود: خودِ pa_home در انتها
    # کالبک را پاسخ می‌دهد و پاسخِ دوباره خطا می‌دهد. وضعیتِ جدید در
    # همان منو دیده می‌شود.
    await pa_home(cb)


@partner_admin_router.callback_query(F.data == "pa:reqtoggle")
async def pa_req_toggle(cb: CallbackQuery):
    """=== PATCH_PARTNER_V2 === باز/بستنِ پذیرشِ درخواستِ همکاری.

    وقتی بسته باشد، دکمهٔ «درخواست همکاری» اصلاً در منویِ کاربران دیده
    نمی‌شود (نگاه کن به _append_partner_buttons در platform_bot.py).
    """
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    from platform_partner import partner_requests_enabled, set_pset
    now_on = await partner_requests_enabled()
    await set_pset("partner_req_on", "off" if now_on else "on")
    await cb.answer("پذیرش درخواست " + ("بسته شد" if now_on else "باز شد"))
    await pa_home(cb)


# === PATCH_PARTNER_BROADCAST_V1 ===
BCAST_HEADER = "📩 <b>پیام پشتیبانی بات‌ساز</b>"


async def _partner_owner_targets() -> list[tuple[int, str | None, str | None]]:
    """(owner_id, token, username) برایِ هرِ رباتِ همکار."""
    async with Session() as s:
        rows = (await s.execute(
            select(PartnerBot, BotRow)
            .join(BotRow, BotRow.id == PartnerBot.bot_id))).all()
    return [(pb.owner_id, br.token, br.username) for pb, br in rows]


@partner_admin_router.callback_query(F.data == "pa:bcast:start")
async def pa_bcast_start(cb: CallbackQuery, state: FSMContext):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await state.set_state(PartnerAdminFSM.broadcast_msg)
    b = InlineKeyboardBuilder()
    b.button(text="🔙 انصراف", callback_data="pa:home", style=ButtonStyle.DANGER)
    b.adjust(1)
    await cb.message.edit_text(
        "📢 <b>پیام همگانی به همکاران</b>\n\n"
        "متن یا یک عکس (با کپشنِ دلخواه) را بفرست. این پیام با عنوانِ «📩 پیام پشتیبانی بات‌ساز» "
        "از رباتِ خودِ هر همکار برایش ارسال می‌شود.",  # === PATCH_PARTNER_BROADCAST_PHOTO_V1 ===
        reply_markup=b.as_markup())
    await cb.answer()


@partner_admin_router.message(PartnerAdminFSM.broadcast_msg)
async def pa_bcast_got_text(msg: Message, state: FSMContext, bot: Bot):
    if not await _can_access(msg.from_user.id):
        return
    # === PATCH_PARTNER_BROADCAST_PHOTO_V1 ===
    # اگه عکس فرستاده، بایت‌هاش رو همین‌جا دانلود می‌کنیم (چون بعداً موقعِ
    # ارسال، file_id رباتِ پلتفرم رویِ رباتِ هر همکار معتبر نیست) و
    # base64 در state نگه می‌داریم تا در pa_bcast_send دوباره آپلود شود.
    if msg.photo:
        caption = (msg.caption or "").strip()
        buf = _BytesIO()
        await bot.download(msg.photo[-1].file_id, destination=buf)
        photo_b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
        await state.update_data(pa_bcast_text=caption, pa_bcast_photo_b64=photo_b64)
        n = len(await _partner_owner_targets())
        b = InlineKeyboardBuilder()
        b.button(text=f"✅ ارسال به {n} همکار", callback_data="pa:bcast:send",
                 style=ButtonStyle.SUCCESS)
        b.button(text="🔙 انصراف", callback_data="pa:home", style=ButtonStyle.DANGER)
        b.adjust(1)
        await msg.answer_photo(
            photo=msg.photo[-1].file_id,
            caption=f"{BCAST_HEADER}\n\n{caption}\n\n"
                    f"— پیش‌نمایش؛ برایِ <b>{n}</b> همکار ارسال می‌شود —",
            reply_markup=b.as_markup())
        return
    text = (msg.text or "").strip()
    if not text:
        await msg.answer("لطفاً یک متن یا عکس (با کپشنِ دلخواه) بفرست.")
        return
    await state.update_data(pa_bcast_text=text, pa_bcast_photo_b64=None)
    n = len(await _partner_owner_targets())
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ ارسال به {n} همکار", callback_data="pa:bcast:send",
             style=ButtonStyle.SUCCESS)
    b.button(text="🔙 انصراف", callback_data="pa:home", style=ButtonStyle.DANGER)
    b.adjust(1)
    await msg.answer(
        f"{BCAST_HEADER}\n\n{text}\n\n"
        f"— پیش‌نمایش؛ برایِ <b>{n}</b> همکار ارسال می‌شود —",
        reply_markup=b.as_markup())


@partner_admin_router.callback_query(F.data == "pa:bcast:send")
async def pa_bcast_send(cb: CallbackQuery, state: FSMContext):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("pa_bcast_text")
    photo_b64 = data.get("pa_bcast_photo_b64")  # === PATCH_PARTNER_BROADCAST_PHOTO_V1 ===
    await state.clear()
    if not text and not photo_b64:
        await cb.answer("پیام پیدا نشد", show_alert=True)
        return
    # === PATCH_PARTNER_BCAST_EDIT_FIX_V1 ===
    # پیامِ پیش‌نمایش وقتی عکس دارد، با send_photo/answer_photo ساخته
    # شده — یعنی متن ندارد و caption دارد. edit_text رویِ چنین پیامی
    # از تلگرام خطای «there is no text in the message to edit» می‌گیرد
    # که بی‌صدا به هندلرِ خطایِ عمومی می‌رفت و کدِ ERR-XXXXXX می‌ساخت.
    try:
        if photo_b64:
            await cb.message.edit_caption(caption="⏳ در حالِ ارسال ...")
        else:
            await cb.message.edit_text("⏳ در حالِ ارسال ...")
    except Exception:
        logger.exception("ویرایشِ پیامِ پیش‌نمایش ناموفق بود؛ پیامِ نو می‌فرستم")
        await cb.message.answer("⏳ در حالِ ارسال ...")
    await cb.answer()

    import asyncio
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from bot_utils import _temp_bot

    targets = await _partner_owner_targets()
    full_text = f"{BCAST_HEADER}\n\n{text}" if text else BCAST_HEADER
    # === PATCH_PARTNER_BROADCAST_PHOTO_V1 === بایت‌های عکس (اگه بود) یک‌بار دیکد می‌شود؛ هر ارسال یک
    # BufferedInputFile تازه از رویِ همین بایت‌ها می‌سازد (قابلِ استفادهٔ مجدد).
    photo_bytes = _b64.b64decode(photo_b64) if photo_b64 else None
    ok = 0
    fail = 0

    async def _one(owner_id: int, token: str | None, uname: str | None) -> None:
        nonlocal ok, fail
        sent = False
        if token:
            try:
                async with _temp_bot(
                    token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
                ) as tb:
                    if photo_bytes:
                        await tb.send_photo(
                            owner_id,
                            BufferedInputFile(photo_bytes, filename="broadcast.jpg"),
                            caption=full_text)
                    else:
                        await tb.send_message(owner_id, full_text)
                sent = True
            except Exception:
                logger.exception(
                    "ارسالِ پیامِ همگانی از رباتِ همکار @%s ناموفق بود", uname)
        if not sent:
            try:
                if photo_bytes:
                    await cb.bot.send_photo(
                        owner_id,
                        BufferedInputFile(photo_bytes, filename="broadcast.jpg"),
                        caption=full_text)
                else:
                    await cb.bot.send_message(owner_id, full_text)
                sent = True
            except Exception:
                logger.exception(
                    "ارسالِ جایگزینِ پیامِ همگانی از رباتِ مادر هم ناموفق بود")
        if sent:
            ok += 1
        else:
            fail += 1

    # === PATCH_PARTNER_BROADCAST_V1 === دسته‌ای از ۵ تا ۵، با فاصلهٔ کوتاه بینِ دسته‌ها؛ همان
    # الگویِ chunking که در main.py._do_reconnect برایِ ارسالِ گروهی استفاده شده.
    for i in range(0, len(targets), 5):
        chunk = targets[i:i + 5]
        await asyncio.gather(*[_one(o, t, u) for o, t, u in chunk])
        if i + 5 < len(targets):
            await asyncio.sleep(0.5)

    b = InlineKeyboardBuilder()
    b.button(text="🔙 بازگشت", callback_data="pa:home", style=ButtonStyle.DANGER)
    b.adjust(1)
    await cb.message.answer(
        f"📢 ارسال تمام شد.\n✅ موفق: {ok}\n❌ ناموفق: {fail}",
        reply_markup=b.as_markup())


# === PATCH_PARTNER_FINALIZE_LIST_V1 ===
async def s_pending_requests_count() -> int:
    from models import PartnerRequest
    async with Session() as s:
        return int(await s.scalar(
            select(func.count(PartnerRequest.id))
            .where(PartnerRequest.status == "pending")) or 0)


async def _pending_request_rows(offset: int = 0) -> tuple[list, int]:
    from models import PartnerRequest
    async with Session() as s:
        total = await s.scalar(
            select(func.count(PartnerRequest.id))
            .where(PartnerRequest.status == "pending")) or 0
        rows = (await s.scalars(
            select(PartnerRequest)
            .where(PartnerRequest.status == "pending")
            .order_by(PartnerRequest.id.asc())
            .offset(offset).limit(PAGE_SIZE))).all()
        out = [(r.id, r.full_name, r.username) for r in rows]
    return out, int(total)


@partner_admin_router.callback_query(F.data.startswith("pa:reqs:"))
async def pa_reqs_list(cb: CallbackQuery):
    """=== PATCH_PARTNER_FINALIZE_LIST_V1 === فهرستِ درخواست‌هایِ در حالِ بررسی."""
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    offset = int(cb.data.split(":")[2])
    rows, total = await _pending_request_rows(offset)

    b = InlineKeyboardBuilder()
    for req_id, name, uname in rows:
        label = name or (("@" + uname) if uname else f"#{req_id}")
        b.button(text=f"👤 {label}", callback_data=f"pa:reqview:{req_id}",
                 style=ButtonStyle.PRIMARY)
    b.adjust(1)

    nav = []
    if offset > 0:
        nav.append(("⬅️ قبلی", f"pa:reqs:{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        nav.append(("بعدی ➡️", f"pa:reqs:{offset + PAGE_SIZE}"))
    for text, cbd in nav:
        b.button(text=text, callback_data=cbd, style=ButtonStyle.PRIMARY)
    if nav:
        b.adjust(*([1] * len(rows)), len(nav))

    b.button(text="🔙 بازگشت", callback_data="pa:home", style=ButtonStyle.DANGER)
    b.adjust(1)

    txt = (f"📋 <b>درخواست‌های ثبت‌شده برای همکاری</b>\n\n"
           f"در حالِ بررسی: <b>{total}</b>")
    if not rows:
        txt += "\n\nدرخواستِ در حالِ بررسی‌ای وجود ندارد."
    await cb.message.edit_text(txt, reply_markup=b.as_markup())
    await cb.answer()


@partner_admin_router.callback_query(F.data.startswith("pa:reqview:"))
async def pa_reqview(cb: CallbackQuery):
    """=== PATCH_PARTNER_FINALIZE_LIST_V1 === نمایشِ جزئیاتِ یک درخواست با دکمه‌هایِ تایید/رد
    — از هندلرهایِ pnr:approve / pnr:reject در platform_partner.py
    استفاده می‌شود؛ پیامِ همین نما نیز پس از تصمیم بسته خواهد شد."""
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    req_id = int(cb.data.split(":")[2])
    from platform_partner import _request_detail_text, _admin_request_kb
    txt = await _request_detail_text(req_id)
    if not txt:
        await cb.answer("درخواست پیدا نشد", show_alert=True)
        return
    await cb.message.edit_text(
        txt, reply_markup=await _admin_request_kb(req_id, "pending"))
    await cb.answer()


async def _partner_rows(offset: int = 0, term: str = "") -> tuple[list, int]:
    async with Session() as s:
        q = (select(PartnerBot, BotRow).join(BotRow, BotRow.id == PartnerBot.bot_id)
             .where(PartnerBot.status != "deleted"))  # === PATCH_PARTNER_DELETE_V1 ===
        if term:
            like = f"%{term}%"
            q = q.where(or_(BotRow.username.ilike(like),
                            BotRow.token.ilike(like)))
        total = await s.scalar(
            select(func.count()).select_from(q.subquery())) or 0
        rows = (await s.execute(
            q.order_by(PartnerBot.id.desc()).offset(offset).limit(PAGE_SIZE))).all()
        out = [(pb.id, pb.status, pb.owner_id, br.username,
                pb.low_stock_since is not None) for pb, br in rows]
    return out, total


def _list_kb(rows: list, offset: int, total: int, term: str = "") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for pb_id, status, owner, uname, low in rows:
        mark = "🟢" if status == "active" else ("⏸" if status == "paused" else "🔴")
        warn = "⚠️" if low else ""
        b.button(text=f"{mark}{warn} @{uname or '—'} · {owner}",
                 callback_data=f"pa:view:{pb_id}",
                 style=(ButtonStyle.SUCCESS if status == "active"
                        else ButtonStyle.DANGER))
    b.adjust(1)

    nav = []
    if offset > 0:
        nav.append(("⬅️ قبلی", f"pa:list:{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        nav.append(("بعدی ➡️", f"pa:list:{offset + PAGE_SIZE}"))
    for text, cbd in nav:
        b.button(text=text, callback_data=cbd, style=ButtonStyle.PRIMARY)
    if nav:
        b.adjust(*([1] * len(rows)), len(nav))

    b.button(text="🔙 بازگشت", callback_data="pa:home",
             style=ButtonStyle.DANGER)
    b.adjust(1)
    return b.as_markup()


@partner_admin_router.callback_query(F.data.startswith("pa:list:"))
async def pa_list(cb: CallbackQuery):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    offset = int(cb.data.split(":")[2])
    rows, total = await _partner_rows(offset)
    if not rows:
        await cb.answer("هیچ ربات همکاری ثبت نشده است.", show_alert=True)
        return
    await cb.message.edit_text(
        f"📋 <b>ربات‌های همکار</b> ({total} مورد)\n\n"
        "🟢 فعال · ⏸ متوقف · ⚠️ زیر حد نصاب",
        reply_markup=_list_kb(rows, offset, total))
    await cb.answer()


# ────────── جستجو ──────────

@partner_admin_router.callback_query(F.data == "pa:search")
async def pa_search_start(cb: CallbackQuery, state: FSMContext):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await state.set_state(PartnerAdminFSM.search)
    await cb.message.answer(
        "🔎 نام کاربری ربات همکار را بفرست (با یا بدون @):",
        reply_markup=_cancel_kb("pa:home"))  # PATCH_PARTNER_BACKNAV_V1
    await cb.answer()


@partner_admin_router.message(PartnerAdminFSM.search)
async def pa_search_do(msg: Message, state: FSMContext):
    if not await _can_access(msg.from_user.id):
        return
    await state.clear()
    term = (msg.text or "").strip().lstrip("@")
    if not term:
        await msg.answer("عبارت جستجو خالی بود.")
        return
    rows, total = await _partner_rows(0, term)
    if not rows:
        await msg.answer("چیزی پیدا نشد.")
        return
    await msg.answer(f"🔎 <b>نتایج برای «{term}»</b> ({total} مورد)",
                     reply_markup=_list_kb(rows, 0, total, term))


# ────────── جزئیاتِ یک همکار ──────────

async def _partner_detail(pb_id: int) -> dict | None:
    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id)
        if not pb:
            return None
        br = await s.get(BotRow, pb.bot_id)
        pu = await s.scalar(select(PlatformUser)
                            .where(PlatformUser.telegram_id == pb.owner_id))

        sold = await s.scalar(select(func.count(PartnerSale.id)).where(
            PartnerSale.partner_bot_id == pb_id,
            PartnerSale.kind == "sold")) or 0
        trials = await s.scalar(select(func.count(PartnerSale.id)).where(
            PartnerSale.partner_bot_id == pb_id,
            PartnerSale.kind == "trial")) or 0
        active = await s.scalar(
            select(func.count(PartnerSale.id))
            .join(BotRow, BotRow.id == PartnerSale.sold_bot_id)
            .where(PartnerSale.partner_bot_id == pb_id,
                   PartnerSale.kind == "sold",
                   BotRow.status == "active",
                   BotRow.archived_at.is_(None))) or 0
        revenue = await s.scalar(
            select(func.coalesce(func.sum(PartnerSale.price), 0))
            .where(PartnerSale.partner_bot_id == pb_id)) or 0
        paid = await s.scalar(
            select(func.coalesce(func.sum(PartnerStockOrder.amount), 0))
            .where(PartnerStockOrder.partner_bot_id == pb_id,
                   PartnerStockOrder.status == "approved")) or 0
        users = await s.scalar(select(func.count(BotUser.id))
                               .where(BotUser.bot_id == pb.bot_id)) or 0

        return {
            "pb_id": pb.id, "bot_id": pb.bot_id, "owner": pb.owner_id,
            "username": br.username if br else None,
            "bot_status": br.status if br else "—",
            "status": pb.status, "discount": pb.discount_percent,
            "expires_at": pb.expires_at, "low_since": pb.low_stock_since,
            "stock_g": pb.stock_growth, "stock_p": pb.stock_pro,
            "bought_g": pb.bought_growth, "bought_p": pb.bought_pro,
            "sold": sold, "trials": trials, "active": active,
            "revenue": int(revenue), "paid": int(paid), "users": users,
            "owner_name": pu.full_name if pu else None,
            "owner_balance": int(pu.balance) if pu else 0,
        }


def _detail_kb(d: dict) -> InlineKeyboardMarkup:
    pid = d["pb_id"]
    b = InlineKeyboardBuilder()
    b.button(text="➖ ۳۰ روز", callback_data=f"pa:days:{pid}:-30",
             style=ButtonStyle.DANGER)
    b.button(text="➖ ۷", callback_data=f"pa:days:{pid}:-7",
             style=ButtonStyle.DANGER)
    b.button(text="➕ ۷", callback_data=f"pa:days:{pid}:7",
             style=ButtonStyle.SUCCESS)
    b.button(text="➕ ۳۰ روز", callback_data=f"pa:days:{pid}:30",
             style=ButtonStyle.SUCCESS)
    # === PATCH_PARTNER_V34 ===
    # حالتِ دستی: تعدادِ روز و تعدادِ رباتِ انبار را با تایپ تعیین کن،
    # به‌جایِ محدودبودن به ±۷ و ±۳۰.
    b.button(text="⌨️ تعیین دستی روز", callback_data=f"pa:daysman:{pid}",
             style=ButtonStyle.PRIMARY)
    b.button(text="📦 افزودن / کسر ربات", callback_data=f"pa:stock:{pid}",
             style=ButtonStyle.PRIMARY)
    b.button(text="🏷 تغییر درصد تخفیف", callback_data=f"pa:disc:{pid}",
             style=ButtonStyle.PRIMARY)
    b.button(text="✉️ پیام به صاحب ربات", callback_data=f"pa:msg:{pid}",
             style=ButtonStyle.PRIMARY)
    # === PATCH_PARTNER_WALLET_CHARGE_V1 ===
    b.button(text="💰 شارژ / کسر کیف پول همکار", callback_data=f"pa:wal:{pid}",
             style=ButtonStyle.SUCCESS)
    b.button(text=f"🤖 ربات‌های ساخته‌شده ({d['sold'] + d['trials']})",
             callback_data=f"pa:bots:{pid}:0", style=ButtonStyle.PRIMARY)
    if d["status"] == "active":
        b.button(text="⏸ توقف ربات همکار", callback_data=f"pa:pause:{pid}",
                 style=ButtonStyle.DANGER)
    else:
        b.button(text="▶️ فعال‌سازی ربات همکار", callback_data=f"pa:resume:{pid}",
                 style=ButtonStyle.SUCCESS)
    b.button(text="⏸ توقف همهٔ ربات‌های فروخته‌شده",
             callback_data=f"pa:allpause:{pid}", style=ButtonStyle.DANGER)
    b.button(text="▶️ فعال‌سازی همهٔ ربات‌های فروخته‌شده",
             callback_data=f"pa:allresume:{pid}", style=ButtonStyle.SUCCESS)
    # === PATCH_PARTNER_TOKEN_OWNER_V1 ===
    # تعویضِ توکن و انتقالِ مالکیتِ خودِ ربات همکار — منطقشان در
    # platform_partner_token.py است (پیشوندِ pat:).
    b.button(text="🔁 تعویض توکن", callback_data=f"pat:tok:{pid}",
             style=ButtonStyle.PRIMARY)
    b.button(text="👤 انتقال مالکیت", callback_data=f"pat:own:{pid}",
             style=ButtonStyle.PRIMARY)
    # === PATCH_PARTNER_DELETE_V1 ===
    b.button(text="🗑 حذف همکار", callback_data=f"pa:del:{pid}",
             style=ButtonStyle.DANGER)
    # === PATCH_PARTNER_V2 ===
    b.button(text="🔙 فهرست همکاران", callback_data="pa:list:0",
             style=ButtonStyle.PRIMARY)
    # === PATCH_PARTNER_BACKNAV_V1 === یک پله عقب، نه پرشِ دو پله‌ای به پنل
    b.button(text="🏠 ربات‌های همکار", callback_data="pa:home",
             style=ButtonStyle.PRIMARY)
    b.adjust(4, 2, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2)
    return b.as_markup()


def _detail_text(d: dict, min_active: int = MIN_ACTIVE_SALES) -> str:
    # === PATCH_PARTNER_MIN_ACTIVE_SETTING_V1 ===
    # این تابع همزمان (sync) است، پس نمی‌تواند خودش حد نصاب را از دیتابیس
    # بخواند؛ صداکننده که async است آن را پاس می‌دهد.
    warn = ""
    if d["low_since"]:
        deadline = d["low_since"] + datetime.timedelta(days=LOW_STOCK_GRACE_DAYS)
        left = max(0, (deadline - datetime.datetime.now()).days)
        warn = (f"\n\n⚠️ زیر حد نصاب ({d['active']}/{min_active}) — "
                f"{left} روز تا غیرفعال‌شدن خودکار")

    settle = d["revenue"] - d["paid"]
    return (
        f"🤝 <b>ربات همکار @{d['username'] or '—'}</b>\n\n"
        f"👤 صاحب: {d['owner_name'] or '—'} (<code>{d['owner']}</code>)\n"
        f"👛 کیف پول صاحب: {d['owner_balance']:,} تومان\n"
        f"📊 وضعیت: {d['status']} | ربات: {d['bot_status']}\n"
        f"🏷 تخفیف: {d['discount']}٪\n"
        f"⏳ اعتبار تا: {_fmt_dt(d['expires_at'])}\n"
        f"👥 کاربران ربات: {d['users']}\n\n"
        f"🛒 خریده: رشد {d['bought_g']} | حرفه‌ای {d['bought_p']}\n"
        f"📦 موجودی: رشد {d['stock_g']} | حرفه‌ای {d['stock_p']}\n"
        f"✅ فروخته: {d['sold']} (فعال: {d['active']})\n"
        f"🎁 تست داده: {d['trials']}\n\n"
        f"💵 پرداختی به ما: {d['paid']:,} تومان\n"
        f"💰 فروش او: {d['revenue']:,} تومان\n"
        f"⚖️ اختلاف: {settle:,} تومان"
        + warn
    )


# === PATCH_PARTNER_CLEANCHAT_V1 ===
# همون قاعده‌ای که در tenant/vpn_admin.py استفاده می‌شود: بعد از دریافتِ
# پاسخِ ادمین، پیامِ سوال و پیامِ جواب هر دو پاک می‌شوند و کارتِ همکار
# به‌جایِ فرستادنِ پیامِ تازه، سرِ جایِ خودش ادیت می‌شود — تا چتِ ادمین با
# ده‌ها پیامِ «سوال/جواب/تأیید» شلوغ نشود.
async def _cleanchat_finish(bot: Bot, data: dict, msg: Message, note: str = "") -> None:
    pb_id = data.get("pa_pb")
    for mid in (data.get("pa_prompt_id"), msg.message_id):
        if mid:
            try:
                await bot.delete_message(msg.chat.id, mid)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده در پاک‌کردنِ پیامِ سوال/جواب")
    d = await _partner_detail(pb_id) if pb_id else None
    if not d:
        return
    text = (note + "\n\n" if note else "") + _detail_text(d, await min_active_sales())
    chat_id = data.get("pa_panel_chat") or msg.chat.id
    panel_id = data.get("pa_panel_id")
    try:
        if panel_id:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=panel_id,
                                        reply_markup=_detail_kb(d))
        else:
            raise ValueError("no panel_id")
    except Exception:
        await msg.answer(text, reply_markup=_detail_kb(d))


# === PATCH_PARTNER_TOKEN_OWNER_V1 ===
# نسخهٔ قابلِ‌فراخوانی از ماژول‌های دیگر (platform_partner_token.py برایِ
# «بازگشت» بعد از انصراف از جریانِ تعویض توکن / انتقال مالکیت).
async def pa_view_by_id(cb: CallbackQuery, pb_id: int) -> None:
    d = await _partner_detail(pb_id)
    if not d:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    try:
        await cb.message.edit_text(_detail_text(d, await min_active_sales()), reply_markup=_detail_kb(d))
    except Exception:
        await cb.message.answer(_detail_text(d, await min_active_sales()), reply_markup=_detail_kb(d))
    await cb.answer()


@partner_admin_router.callback_query(F.data.startswith("pa:view:"))
async def pa_view(cb: CallbackQuery):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await pa_view_by_id(cb, int(cb.data.split(":")[2]))


# ────────── کم/زیادکردنِ روز ──────────

@partner_admin_router.callback_query(F.data.startswith("pa:days:"))
async def pa_days(cb: CallbackQuery):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    _, _, raw_id, raw_delta = cb.data.split(":")
    pb_id, delta = int(raw_id), int(raw_delta)

    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id, with_for_update=True)
        if not pb:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        if _self_dealing(cb.from_user.id, pb):
            await cb.answer("تغییرِ رباتِ همکارِ خودت مجاز نیست", show_alert=True)
            return
        base = pb.expires_at or datetime.datetime.now()
        pb.expires_at = base + datetime.timedelta(days=delta)
        br = await s.get(BotRow, pb.bot_id)
        if br:
            br.plan_expires_at = pb.expires_at
            # === PATCH_PARTNER_DAYS_UNARCHIVE_FIX_V1 ===
            # اگر ربات قبلاً به‌خاطرِ عدم‌تمدید بایگانی شده بود، با افزودنِ
            # روز باید از بایگانی خارج شود؛ وگرنه archived_at قدیمی می‌ماند
            # و subscription_loop برای همیشه از چکِ سررسید معافش می‌کند
            # (حتی اگر status الان active باشد).
            if br.archived_at and br.plan_expires_at > datetime.datetime.now():
                br.archived_at = None
                br.archive_reason = None
                br.last_expiry_notice = None
                br.status = "active"
        new_exp = pb.expires_at
        await s.commit()

    d = await _partner_detail(pb_id)
    try:
        await cb.message.edit_text(_detail_text(d, await min_active_sales()), reply_markup=_detail_kb(d))
    except Exception:
        logger.exception("به‌روزرسانیِ صفحهٔ همکار ناموفق بود")
    await cb.answer(f"اعتبار تا {_fmt_dt(new_exp)}")


# ────────── تغییرِ درصدِ تخفیف ──────────

@partner_admin_router.callback_query(F.data.startswith("pa:disc:"))
async def pa_disc_ask(cb: CallbackQuery, state: FSMContext):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    await state.update_data(pa_pb=pb_id, pa_panel_chat=cb.message.chat.id,
                             pa_panel_id=cb.message.message_id)
    await state.set_state(PartnerAdminFSM.set_discount)
    prompt = await cb.message.answer(
        "🏷 درصد تخفیف جدید را بفرست (عدد بین ۰ تا ۹۰):",
        reply_markup=_cancel_kb(f"pa:view:{pb_id}"))  # PATCH_PARTNER_BACKNAV_V1
    await state.update_data(pa_prompt_id=prompt.message_id)  # === PATCH_PARTNER_CLEANCHAT_V1 ===
    await cb.answer()


@partner_admin_router.message(PartnerAdminFSM.set_discount)
async def pa_disc_save(msg: Message, state: FSMContext):
    if not await _can_access(msg.from_user.id):
        return
    raw = (msg.text or "").strip()
    if not raw.isdigit() or not (0 <= int(raw) <= 90):
        await msg.answer("عدد بین ۰ تا ۹۰ بفرست.")
        return
    data = await state.get_data()
    pb_id = data.get("pa_pb")
    await state.clear()
    if not pb_id:
        await msg.answer("این مرحله منقضی شده.")
        return

    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id)
        if not pb:
            await msg.answer("پیدا نشد.")
            return
        pb.discount_percent = int(raw)
        owner = pb.owner_id
        await s.commit()

    await _cleanchat_finish(msg.bot, data, msg, f"✅ تخفیف روی {raw}٪ تنظیم شد.")
    try:
        await msg.bot.send_message(
            owner, f"🏷 درصد تخفیف همکاری شما به <b>{raw}٪</b> تغییر کرد.")
    except Exception:
        logger.exception("اطلاعِ تغییرِ تخفیف به همکار ناموفق بود")


# ────────── تخفیف همگانی روی همهٔ همکاران ──────────
# === PATCH_PARTNER_DISCOUNT_ALL_V1 ===
# برخلافِ pa:disc که فقط رویِ یک همکار اعمال می‌شود، این‌جا مدیر یک درصد
# می‌دهد و همان درصد رویِ discount_percent همهٔ ردیف‌هایِ PartnerBot
# (فارغ از وضعیت) نشانده می‌شود. برایِ جلوگیری از اشتباه، قبل از اعمال یک
# پیش‌نمایش با تعدادِ همکارانِ تحتِ تأثیر و دکمهٔ تأییدِ جدا نشان داده می‌شود.

@partner_admin_router.callback_query(F.data == "pa:discall:start")
async def pa_discall_ask(cb: CallbackQuery, state: FSMContext):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await state.update_data(pa_panel_chat=cb.message.chat.id, pa_panel_id=cb.message.message_id)
    await state.set_state(PartnerAdminFSM.set_discount_all)
    prompt = await cb.message.answer(
        "🏷 <b>تخفیف همگانی</b>\n\n"
        "درصدِ تخفیفی که می‌خواهی رویِ <b>همهٔ</b> ربات‌هایِ همکار (بدونِ استثنا) "
        "جایگزینِ تخفیفِ فعلی‌شان شود را بفرست (عدد بین ۰ تا ۹۰):",
        reply_markup=_cancel_kb("pa:home"))
    await state.update_data(pa_prompt_id=prompt.message_id)
    await cb.answer()


@partner_admin_router.message(PartnerAdminFSM.set_discount_all)
async def pa_discall_preview(msg: Message, state: FSMContext):
    if not await _can_access(msg.from_user.id):
        return
    raw = (msg.text or "").strip()
    if not raw.isdigit() or not (0 <= int(raw) <= 90):
        await msg.answer("عدد بین ۰ تا ۹۰ بفرست.")
        return
    pct = int(raw)
    await state.update_data(pa_discall_pct=pct)
    async with Session() as s:
        n = await s.scalar(select(func.count(PartnerBot.id))) or 0
    if not n:
        await state.clear()
        await msg.answer("هیچ ربات همکاری ثبت نشده.", reply_markup=_cancel_kb("pa:home"))
        return
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ اعمال {pct}٪ روی همهٔ {n} همکار", callback_data=f"pa:discall:apply:{pct}",
             style=ButtonStyle.SUCCESS)
    b.button(text="🔙 انصراف", callback_data="pa:home", style=ButtonStyle.DANGER)
    b.adjust(1)
    await msg.answer(
        f"⚠️ با تأیید، تخفیفِ <b>{pct}٪</b> جایگزینِ تخفیفِ فعلیِ <b>هرِ {n}</b> ربات همکار می‌شود "
        "(تخفیف‌های تک‌به‌تکِ قبلی از بین می‌رود). ادامه می‌دهی؟",
        reply_markup=b.as_markup())


@partner_admin_router.callback_query(F.data.startswith("pa:discall:apply:"))
async def pa_discall_apply(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        pct = int(cb.data.split(":")[3])
    except (IndexError, ValueError):
        await cb.answer()
        return
    if not 0 <= pct <= 90:
        await cb.answer("درصد باید بینِ ۰ تا ۹۰ باشد", show_alert=True)
        return
    await state.clear()
    async with Session() as s:
        rows = (await s.scalars(select(PartnerBot))).all()
        owners = set()
        for pb in rows:
            pb.discount_percent = pct
            owners.add(pb.owner_id)
        await s.commit()
    try:
        await cb.message.edit_text(f"⏳ در حالِ اعمال روی {len(rows)} همکار ...")
    except Exception:
        await cb.message.answer(f"⏳ در حالِ اعمال روی {len(rows)} همکار ...")
    await cb.answer()

    import asyncio
    ok = 0
    for owner_id in owners:
        try:
            await bot.send_message(
                owner_id, f"🏷 درصد تخفیفِ همکاری شما به <b>{pct}٪</b> تغییر کرد.")
            ok += 1
        except Exception:
            logger.exception("اطلاعِ تخفیفِ همگانی به یکی از همکاران ناموفق بود")
        await asyncio.sleep(0.05)

    b = InlineKeyboardBuilder()
    b.button(text="🏠 ربات‌های همکار", callback_data="pa:home", style=ButtonStyle.DANGER)
    b.adjust(1)
    try:
        await cb.message.edit_text(
            f"✅ تخفیفِ {pct}٪ رویِ {len(rows)} ربات همکار اعمال شد.\n"
            f"📨 به {ok} از {len(owners)} همکار اطلاع داده شد.",
            reply_markup=b.as_markup())
    except Exception:
        await cb.message.answer(
            f"✅ تخفیفِ {pct}٪ رویِ {len(rows)} ربات همکار اعمال شد.\n"
            f"📨 به {ok} از {len(owners)} همکار اطلاع داده شد.",
            reply_markup=b.as_markup())


# ────────── پیام به صاحبِ ربات ──────────

@partner_admin_router.callback_query(F.data.startswith("pa:msg:"))
async def pa_msg_ask(cb: CallbackQuery, state: FSMContext):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    await state.update_data(pa_pb=pb_id, pa_panel_chat=cb.message.chat.id,
                             pa_panel_id=cb.message.message_id)
    await state.set_state(PartnerAdminFSM.message_owner)
    prompt = await cb.message.answer(
        "✉️ پیامت را برای صاحب ربات همکار بنویس:",
        reply_markup=_cancel_kb(f"pa:view:{pb_id}"))  # PATCH_PARTNER_BACKNAV_V1
    await state.update_data(pa_prompt_id=prompt.message_id)  # === PATCH_PARTNER_CLEANCHAT_V1 ===
    await cb.answer()


@partner_admin_router.message(PartnerAdminFSM.message_owner)
async def pa_msg_send(msg: Message, state: FSMContext, bot: Bot):
    if not await _can_access(msg.from_user.id):
        return
    data = await state.get_data()
    pb_id = data.get("pa_pb")
    await state.clear()
    text = (msg.text or "").strip()
    if not pb_id or not text:
        await msg.answer("پیام نامعتبر بود.")
        return

    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id)
        owner = pb.owner_id if pb else None
    if not owner:
        await msg.answer("پیدا نشد.")
        return

    note = "✅ ارسال شد."
    try:
        # === PATCH_PARTNER_MSGOWNER_ESCAPE_V1 ===
        # متنِ آزادِ ادمین ممکن است هر کاراکتری (از جمله < >) داشته باشد؛
        # چون پیام با HTML فرستاده می‌شود، باید escape شود وگرنه تلگرام
        # با «can't parse entities» رد می‌کند و کلِ ارسال fail می‌شود.
        await bot.send_message(
            owner, f"📩 <b>پیام از مدیریت بات‌ساز</b>\n\n{_html.escape(text)}")
    except Exception:
        logger.exception("ارسالِ پیام به صاحبِ رباتِ همکار ناموفق بود")
        note = "⚠️ ارسال ناموفق بود (شاید ربات را بلاک کرده)."
    await _cleanchat_finish(bot, data, msg, note)


# ────────── توقف / فعال‌سازی ──────────

async def _set_partner_status(pb_id: int, status: str) -> int | None:
    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id)
        if not pb:
            return None
        pb.status = status
        br = await s.get(BotRow, pb.bot_id)
        if br:
            br.status = "active" if status == "active" else "paused"
        owner = pb.owner_id
        await s.commit()
    return owner


@partner_admin_router.callback_query(F.data.startswith("pa:pause:"))
async def pa_pause(cb: CallbackQuery, bot: Bot):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    owner = await _set_partner_status(pb_id, "paused")
    if owner:
        try:
            await bot.send_message(
                owner, "⏸ ربات همکار شما توسط مدیریت <b>متوقف</b> شد. "
                       "برای پیگیری از بخش پشتیبانی پیام بده.")
        except Exception:
            logger.exception("اطلاعِ توقف به همکار ناموفق بود")
    d = await _partner_detail(pb_id)
    if not d:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    await cb.message.edit_text(_detail_text(d, await min_active_sales()), reply_markup=_detail_kb(d))
    await cb.answer("متوقف شد")


@partner_admin_router.callback_query(F.data.startswith("pa:resume:"))
async def pa_resume(cb: CallbackQuery, bot: Bot):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    owner = await _set_partner_status(pb_id, "active")
    if owner:
        try:
            await bot.send_message(owner, "▶️ ربات همکار شما دوباره فعال شد.")
        except Exception:
            logger.exception("اطلاعِ فعال‌سازی به همکار ناموفق بود")
    d = await _partner_detail(pb_id)
    if not d:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    await cb.message.edit_text(_detail_text(d, await min_active_sales()), reply_markup=_detail_kb(d))
    await cb.answer("فعال شد")


# === PATCH_PARTNER_DELETE_V1 ===
# حذفِ همکار همیشه با یک مرحلهٔ تأییدِ جدا انجام می‌شود چون برگشت‌ناپذیر
# است (soft-delete: ردیف پاک نمی‌شود، فقط status="deleted" و از فهرست و
# جست‌وجو کنار گذاشته می‌شود؛ تاریخچهٔ فروش/تسویه دست‌نخورده می‌ماند).

@partner_admin_router.callback_query(F.data.startswith("pa:del:"))
async def pa_delete_ask(cb: CallbackQuery):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    d = await _partner_detail(pb_id)
    if not d:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text="✅ بله، حذف کن", callback_data=f"pa:delyes:{pb_id}",
             style=ButtonStyle.DANGER)
    b.button(text="❌ انصراف", callback_data=f"pa:view:{pb_id}",
             style=ButtonStyle.PRIMARY)
    b.adjust(1)
    await cb.message.edit_text(
        f"🗑 <b>حذف ربات همکار @{d['username'] or '—'}</b>\n\n"
        "ربات همکار متوقف می‌شود و از فهرست همکاران کنار گذاشته می‌شود. "
        "ربات‌های قبلاً فروخته‌شده و تاریخچهٔ تسویه دست‌نخورده می‌مانند.\n\n"
        "⚠️ این عمل قابلِ بازگشت نیست. مطمئنی؟",
        reply_markup=b.as_markup())
    await cb.answer()


@partner_admin_router.callback_query(F.data.startswith("pa:delyes:"))
async def pa_delete_confirm(cb: CallbackQuery, bot: Bot):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    owner = await _set_partner_status(pb_id, "deleted")
    if owner:
        try:
            await bot.send_message(
                owner, "🗑 همکاری‌ات با پلتفرم توسط مدیریت پایان یافت. "
                       "برای پیگیری از بخش پشتیبانی پیام بده.")
        except Exception:
            logger.exception("اطلاعِ حذف به همکار ناموفق بود")
    await cb.answer("حذف شد", show_alert=True)
    rows, total = await _partner_rows(0)
    if rows:
        await cb.message.edit_text(
            f"📋 <b>ربات‌های همکار</b> ({total} مورد)\n\n"
            "🟢 فعال · ⏸ متوقف · ⚠️ زیر حد نصاب",
            reply_markup=_list_kb(rows, 0, total))
    else:
        await cb.message.edit_text("🗑 همکار حذف شد.")


# ────────── ربات‌هایِ ساخته‌شده با این همکار ──────────

@partner_admin_router.callback_query(F.data.startswith("pa:bots:"))
async def pa_bots(cb: CallbackQuery):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    _, _, raw_id, raw_off = cb.data.split(":")
    pb_id, offset = int(raw_id), int(raw_off)

    async with Session() as s:
        q = (select(PartnerSale, BotRow)
             .join(BotRow, BotRow.id == PartnerSale.sold_bot_id)
             .where(PartnerSale.partner_bot_id == pb_id))
        total = await s.scalar(select(func.count()).select_from(q.subquery())) or 0
        rows = (await s.execute(
            q.order_by(PartnerSale.id.desc())
            .offset(offset).limit(PAGE_SIZE))).all()
        items = [(ps.kind, ps.plan, int(ps.price or 0), ps.buyer_telegram_id,
                  br.username, br.status, br.id) for ps, br in rows]

    if not items:
        await cb.answer("هنوز رباتی با این ربات همکار ساخته نشده.", show_alert=True)
        return

    lines = [f"🤖 <b>ربات‌های ساخته‌شده</b> ({total} مورد)\n"]
    for kind, plan, price, buyer, uname, status, _bid in items:
        mark = "🟢" if status == "active" else "⏸"
        tag = "🎁 تست" if kind == "trial" else f"{price:,}"
        lines.append(f"{mark} @{uname or '—'} · {plan} · {tag} · "
                     f"<code>{buyer}</code>")

    b = InlineKeyboardBuilder()
    if offset > 0:
        b.button(text="⬅️ قبلی", callback_data=f"pa:bots:{pb_id}:{max(0, offset - PAGE_SIZE)}",
                 style=ButtonStyle.DANGER)
    if offset + PAGE_SIZE < total:
        b.button(text="بعدی ➡️", callback_data=f"pa:bots:{pb_id}:{offset + PAGE_SIZE}",
                 style=ButtonStyle.DANGER)
    b.button(text="🔙 بازگشت", callback_data=f"pa:view:{pb_id}",
             style=ButtonStyle.DANGER)
    b.button(text="🏠 ربات‌های همکار", callback_data="pa:home",  # PATCH_PARTNER_BACKNAV_V1
             style=ButtonStyle.DANGER)  # === PATCH_PARTNER_V2 ===
    b.adjust(2, 1, 1)

    await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


@partner_admin_router.callback_query(F.data.startswith("pa:allpause:"))
async def pa_all_pause(cb: CallbackQuery):
    await _bulk_sold_status(cb, "paused")


@partner_admin_router.callback_query(F.data.startswith("pa:allresume:"))
async def pa_all_resume(cb: CallbackQuery):
    await _bulk_sold_status(cb, "active")


async def _bulk_sold_status(cb: CallbackQuery, status: str) -> None:
    """توقف/فعال‌سازیِ دسته‌جمعیِ همهٔ ربات‌هایی که این همکار فروخته است.

    دلیلِ وجودش: اگر همکار پول ندهد یا سوءاستفاده کند، باید بتوان کلِ
    شاخهٔ زیرِ او را یک‌جا خواباند — نه تک‌تک و دستی.
    """
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])

    async with Session() as s:
        ids = (await s.scalars(
            select(PartnerSale.sold_bot_id)
            .where(PartnerSale.partner_bot_id == pb_id))).all()
        n = 0
        for bid in ids:
            br = await s.get(BotRow, bid)
            if br and br.archived_at is None and br.status != status:
                br.status = status
                n += 1
        await s.commit()

    d = await _partner_detail(pb_id)
    try:
        await cb.message.edit_text(_detail_text(d, await min_active_sales()), reply_markup=_detail_kb(d))
    except Exception:
        logger.exception("به‌روزرسانیِ صفحه بعد از تغییرِ دسته‌جمعی ناموفق بود")
    await cb.answer(f"{n} ربات {'متوقف' if status == 'paused' else 'فعال'} شد")


# ────────── مدیریتِ کاربریِ همکاران ──────────

@partner_admin_router.callback_query(F.data.startswith("pa:users:"))
async def pa_users(cb: CallbackQuery):
    """فهرستِ *اشخاصِ* همکار (نه ربات‌ها).

    === PATCH_PARTNER_USERS_INLINE_V1 ===
    قبلاً یک بلوکِ متنیِ طولانی بود که نه می‌شد رویش زد و نه با زیاد شدنِ
    همکارها خوانا می‌ماند. حالا هر نفر یک دکمهٔ اینلاینِ شیشه‌ای است و با
    زدنش کارتِ کاملِ آن شخص باز می‌شود (pmu:view در
    platform_partner_manage.py).
    """
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    offset = int(cb.data.split(":")[2])

    async with Session() as s:
        total = await s.scalar(
            select(func.count(func.distinct(PartnerBot.owner_id)))) or 0
        owners = (await s.scalars(
            select(PartnerBot.owner_id).distinct()
            .offset(offset).limit(PAGE_SIZE))).all()

        people = []
        for owner in owners:
            pu = await s.scalar(select(PlatformUser)
                                .where(PlatformUser.telegram_id == owner))
            pb_ids = (await s.scalars(
                select(PartnerBot.id)
                .where(PartnerBot.owner_id == owner))).all()
            bought = await s.scalar(
                select(func.coalesce(
                    func.sum(PartnerStockOrder.qty_growth + PartnerStockOrder.qty_pro), 0))
                .where(PartnerStockOrder.partner_bot_id.in_(pb_ids),
                       PartnerStockOrder.status == "approved")) or 0
            sold = await s.scalar(
                select(func.count(PartnerSale.id))
                .where(PartnerSale.partner_bot_id.in_(pb_ids),
                       PartnerSale.kind == "sold")) or 0
            people.append({
                "owner": owner,
                "name": pu.full_name if pu else None,
                "banned": bool(pu.is_banned) if pu else False,
                "bots": len(pb_ids), "bought": int(bought), "sold": int(sold),
            })

    if not people:
        await cb.answer("هنوز هیچ همکاری ثبت نشده است.", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    for p in people:
        ban = "🚫 " if p["banned"] else "👤 "
        who = p["name"] or str(p["owner"])
        b.button(
            text=f"{ban}{who} · 🤖{p['bots']} · 🛒{p['bought']} · ✅{p['sold']}",
            callback_data=f"pmu:view:{p['owner']}",
            style=(ButtonStyle.DANGER if p["banned"] else ButtonStyle.SUCCESS))
    b.adjust(1)

    nav = []
    if offset > 0:
        nav.append(("⬅️ قبلی", f"pa:users:{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        nav.append(("بعدی ➡️", f"pa:users:{offset + PAGE_SIZE}"))
    for text, cbd in nav:
        b.button(text=text, callback_data=cbd, style=ButtonStyle.PRIMARY)
    if nav:
        b.adjust(*([1] * len(people)), len(nav))

    b.button(text="🔙 بازگشت", callback_data="pa:home",
             style=ButtonStyle.DANGER)
    b.adjust(1)

    await cb.message.edit_text(
        f"👥 <b>مدیریت کاربری همکاران</b> ({total} نفر)\n"
        f"نمایشِ {offset + 1} تا {offset + len(people)}\n\n"
        "🤖 تعداد ربات همکار · 🛒 ربات خریده · ✅ ربات فروخته\n"
        "🚫 یعنی این شخص در بات‌ساز بن است.\n\n"
        "رویِ هر نفر بزن تا اطلاعاتِ کاملش بیاید.",
        reply_markup=b.as_markup())
    await cb.answer()


# ======================= تیکت‌هایِ ارجاعیِ همکاران =======================
# === PATCH_PARTNER_V12 ===
# جدا از تیکت‌هایِ عادیِ کاربران (platform_tickets.py) نگه داشته شد تا
# در فهرستِ شلوغِ آن‌ها گم نشوند و بشود سریع به همکار رسیدگی کرد.

class EscFSM(StatesGroup):
    reply = State()


@partner_admin_router.callback_query(F.data.startswith("pae:list"))
async def pae_list(cb: CallbackQuery):
    """فهرستِ تیکت‌هایِ ارجاعیِ همکاران."""
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    from models import PartnerEscalation

    parts = cb.data.split(":")
    offset = int(parts[2]) if len(parts) > 2 else 0

    async with Session() as s:
        total = await s.scalar(select(func.count(PartnerEscalation.id))) or 0
        rows = (await s.execute(
            select(PartnerEscalation, PartnerBot, BotRow)
            .join(PartnerBot, PartnerBot.id == PartnerEscalation.partner_bot_id)
            .join(BotRow, BotRow.id == PartnerBot.bot_id)
            .order_by(PartnerEscalation.updated_at.desc())
            .offset(offset).limit(PAGE_SIZE))).all()
        items = [(e.id, e.subject, e.status, br.username, pb.id)
                 for e, pb, br in rows]

    if not items:
        await cb.answer("هیچ تیکت ارجاعی ثبت نشده است.", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    for eid, subj, st, uname, _pbid in items:
        mark = {"open": "🟡", "answered": "🟢", "closed": "⚪️"}.get(st, "🟡")
        b.button(text=f"{mark} #{eid} @{uname or '—'} — {(subj or '')[:22]}",
                 callback_data=f"pae:view:{eid}",
                 style=(ButtonStyle.DANGER if st == "open"
                        else ButtonStyle.PRIMARY))
    b.adjust(1)
    if offset > 0:
        b.button(text="⬅️ قبلی", callback_data=f"pae:list:{max(0, offset - PAGE_SIZE)}",
                 style=ButtonStyle.DANGER)
    if offset + PAGE_SIZE < total:
        b.button(text="بعدی ➡️", callback_data=f"pae:list:{offset + PAGE_SIZE}",
                 style=ButtonStyle.DANGER)
    b.button(text="🔙 بازگشت", callback_data="pa:home",
             style=ButtonStyle.DANGER)
    b.adjust(*([1] * len(items)), 2, 1)

    await cb.message.edit_text(
        f"🆘 <b>تیکت‌های ارجاعی همکاران</b> ({total} مورد)\n\n"
        "🟡 در انتظار پاسخ · 🟢 پاسخ داده شده · ⚪️ بسته",
        reply_markup=b.as_markup())
    await cb.answer()


@partner_admin_router.callback_query(F.data.startswith("pae:view:"))
async def pae_view(cb: CallbackQuery):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    from models import PartnerEscalation, PartnerEscalationMsg

    eid = int(cb.data.split(":")[2])
    async with Session() as s:
        e = await s.get(PartnerEscalation, eid)
        if not e:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        pb = await s.get(PartnerBot, e.partner_bot_id)
        br = await s.get(BotRow, pb.bot_id) if pb else None
        msgs = (await s.scalars(
            select(PartnerEscalationMsg)
            .where(PartnerEscalationMsg.escalation_id == eid)
            .order_by(PartnerEscalationMsg.id))).all()
        rows = [(x.sender, x.text, x.created_at) for x in msgs]
        st, owner, pb_id = e.status, e.owner_id, e.partner_bot_id
        uname = br.username if br else None

    lines = [
        f"🎫 <b>تیکت ارجاعی #{eid}</b>",
        f"🤖 ربات: @{uname or '—'}",
        f"👤 صاحب: <code>{owner}</code>\n",
    ]
    for sender, text, created in rows[-15:]:
        who = "🛡 شما" if sender == "admin" else "👤 همکار"
        lines.append(f"{who} — {_fmt_dt(created)}\n{text}\n")

    b = InlineKeyboardBuilder()
    if st != "closed":
        b.button(text="✍️ پاسخ", callback_data=f"pae:reply:{eid}",
                 style=ButtonStyle.SUCCESS)
        b.button(text="✅ بستن تیکت", callback_data=f"pae:close:{eid}",
                 style=ButtonStyle.DANGER)
    b.button(text="🤝 ربات همکار این شخص", callback_data=f"pa:view:{pb_id}",
             style=ButtonStyle.PRIMARY)
    b.button(text="🔙 بازگشت", callback_data="pae:list",
             style=ButtonStyle.DANGER)
    b.adjust(2, 1, 1)

    await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


@partner_admin_router.callback_query(F.data.startswith("pae:reply:"))
async def pae_reply_ask(cb: CallbackQuery, state: FSMContext):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    eid = int(cb.data.split(":")[2])
    await state.update_data(pae_id=eid)
    await state.set_state(EscFSM.reply)
    await cb.message.answer(
        f"✍️ پاسخت به تیکت #{eid} را بنویس:",
        reply_markup=_cancel_kb(f"pae:view:{eid}"))  # PATCH_PARTNER_BACKNAV_V1
    await cb.answer()


@partner_admin_router.message(EscFSM.reply)
async def pae_reply_send(msg: Message, state: FSMContext, bot: Bot):
    if not await _can_access(msg.from_user.id):
        return
    from models import PartnerEscalation, PartnerEscalationMsg

    data = await state.get_data()
    eid = data.get("pae_id")
    text = (msg.text or "").strip()
    await state.clear()
    if not eid or not text:
        await msg.answer("پیام نامعتبر بود.")
        return

    async with Session() as s:
        e = await s.get(PartnerEscalation, eid)
        if not e:
            await msg.answer("تیکت پیدا نشد.")
            return
        e.status = "answered"
        e.updated_at = datetime.datetime.now()
        s.add(PartnerEscalationMsg(
            escalation_id=eid, sender="admin", text=text[:4000]))
        pb = await s.get(PartnerBot, e.partner_bot_id)
        br = await s.get(BotRow, pb.bot_id) if pb else None
        token = br.token if br else None
        owner = e.owner_id
        await s.commit()

    # پاسخ باید از *رباتِ همکار* برسد، نه ربات مادر — چون همکار تیکت را
    # آن‌جا ثبت کرده و آن‌جا دنبالِ جواب می‌گردد.
    ok = False
    if token:
        try:
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode
            from bot_utils import _temp_bot

            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✍️ پاسخ",
                                     callback_data=f"pp:esc:reply:{eid}",
                                     style=ButtonStyle.SUCCESS)]])
            tb = Bot(token=token,
                     default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                await tb.send_message(
                    owner,
                    f"🛡 <b>پاسخ پشتیبانی بات‌ساز</b> — تیکت #{eid}\n\n{text}",
                    reply_markup=kb)
                ok = True
            finally:
                await tb.session.close()
        except Exception:
            logger.exception("ارسالِ پاسخِ تیکتِ ارجاعی به همکار ناموفق بود")

    if not ok:
        try:
            await bot.send_message(
                owner,
                f"🛡 <b>پاسخ پشتیبانی بات‌ساز</b> — تیکت #{eid}\n\n{text}")
            ok = True
        except Exception:
            logger.exception("ارسالِ جایگزین از ربات مادر هم ناموفق بود")

    await msg.answer("✅ پاسخ ارسال شد." if ok else
                     "⚠️ ثبت شد ولی ارسال به همکار ناموفق بود.")


@partner_admin_router.callback_query(F.data.startswith("pae:close:"))
async def pae_close(cb: CallbackQuery):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    from models import PartnerEscalation

    eid = int(cb.data.split(":")[2])
    async with Session() as s:
        e = await s.get(PartnerEscalation, eid)
        if e:
            e.status = "closed"
            e.updated_at = datetime.datetime.now()
            await s.commit()
    await cb.answer("تیکت بسته شد")
    # === PATCH_PARTNER_V12 ===
    # cb.data هنوز "pae:close:{id}" است؛ pae_view روی ایندکسِ ۲ پارس
    # می‌کند و همان id را می‌گیرد، پس صدازدنش امن است.
    await pae_view(cb)


# ======================= حالتِ دستی: روز و انبار =======================
# === PATCH_PARTNER_V34 ===
# دکمه‌هایِ ±۷ و ±۳۰ برایِ تغییرهایِ کوچک خوب‌اند، ولی برایِ «۹۰ روز
# اضافه کن» یا «۲۵ ربات بده» باید ده بار کلیک می‌شد.

# === PATCH_PARTNER_WALLET_CHARGE_V1 ===
# درخواستِ ادمین: از صفحهٔ همکار بشود کیفِ پولِ صاحبِ ربات را شارژ (یا کم) کرد.
# کیف پول همان موجودیِ PlatformUser است که همکار با آن انبارِ ربات می‌خرد.
@partner_admin_router.callback_query(F.data.startswith("pa:wal:"))
async def pa_wallet_ask(cb: CallbackQuery, state: FSMContext):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    d = await _partner_detail(pb_id)
    if not d:
        await cb.answer("پیدا نشد", show_alert=True)
        return
    await state.update_data(pa_pb=pb_id, pa_panel_chat=cb.message.chat.id,
                             pa_panel_id=cb.message.message_id)
    await state.set_state(PartnerAdminFSM.wallet_adj)
    prompt = await cb.message.answer(
        "💰 <b>شارژ کیف پول همکار</b>\n\n"
        f"👤 {d['owner_name'] or '—'} (<code>{d['owner']}</code>)\n"
        f"👛 موجودی فعلی: {d['owner_balance']:,} تومان\n\n"
        "مبلغ را به تومان بفرست:\n"
        "• <code>500000</code> → ۵۰۰٬۰۰۰ تومان <b>اضافه</b> می‌شود\n"
        "• <code>-100000</code> → ۱۰۰٬۰۰۰ تومان <b>کم</b> می‌شود\n"
        "• <code>=2000000</code> → موجودی را روی این عدد <b>می‌گذارد</b>\n\n"
        "اختیاری: بعد از مبلغ یک توضیح بنویس (مثلاً <code>500000 بابت واریز کارت</code>).",
        reply_markup=_cancel_kb(f"pa:view:{pb_id}"))
    await state.update_data(pa_prompt_id=prompt.message_id)
    await cb.answer()


@partner_admin_router.message(PartnerAdminFSM.wallet_adj)
async def pa_wallet_set(msg: Message, state: FSMContext, bot: Bot):
    if not await _can_access(msg.from_user.id):
        return
    data = await state.get_data()
    pb_id = data.get("pa_pb")
    raw = (msg.text or "").strip()
    if not pb_id:
        await state.clear()
        await msg.answer("این مرحله منقضی شده.")
        return
    first, _, reason = raw.partition(" ")
    first = first.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٬،", "0123456789,,")).replace(",", "")
    absolute = first.startswith("=")
    body = first[1:] if absolute else first
    try:
        amount = int(body)
    except ValueError:
        await msg.answer("عدد معتبر نیست. مثال: <code>500000</code> یا <code>-100000</code> "
                         "یا <code>=2000000</code>")
        return
    if abs(amount) > 10_000_000_000:
        await msg.answer("مبلغ بیش از حد بزرگ است.")
        return
    await state.clear()
    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id, with_for_update=True)
        if not pb:
            await msg.answer("همکار پیدا نشد.")
            return
        if _self_dealing(msg.from_user.id, pb):
            await msg.answer("تغییرِ کیف‌پولِ همکاریِ خودت مجاز نیست.")
            return
        owner = pb.owner_id
        u = await s.scalar(select(PlatformUser).where(
            PlatformUser.telegram_id == owner).with_for_update())
        if not u:
            await msg.answer("کیف پولِ صاحبِ این ربات پیدا نشد (هنوز در ربات اصلی استارت نزده).")
            return
        old = int(float(u.balance or 0))
        delta = (amount - old) if absolute else amount
        if old + delta < 0:
            await msg.answer(f"موجودی منفی می‌شود (موجودی فعلی {old:,}). عدد کمتری بفرست.")
            return
        u.balance = float(old + delta)
        note = "شارژ/تنظیم دستی ادمین" + (f" — {reason.strip()}" if reason.strip() else "")
        s.add(PlatformTxn(user_telegram_id=owner, amount=delta, kind="admin", note=note[:200]))
        await s.commit()
        newbal = int(float(u.balance))
    sign = "اضافه" if delta >= 0 else "کم"
    await _cleanchat_finish(
        bot, data, msg,
        f"✅ {abs(delta):,} تومان {sign} شد.\n👛 موجودی جدید: {newbal:,} تومان")
    try:
        if delta > 0:
            txt = (f"💰 <b>کیف پول شما شارژ شد</b>\n\n➕ {delta:,} تومان\n"
                   f"👛 موجودی جدید: {newbal:,} تومان")
        else:
            txt = (f"💰 <b>کیف پول شما تغییر کرد</b>\n\n➖ {abs(delta):,} تومان\n"
                   f"👛 موجودی جدید: {newbal:,} تومان")
        if reason.strip():
            txt += f"\n📝 {reason.strip()}"
        await bot.send_message(owner, txt)
    except Exception:
        logger.exception("اطلاعِ شارژ کیف پول به همکار ناموفق بود")


@partner_admin_router.callback_query(F.data.startswith("pa:daysman:"))
async def pa_days_manual_ask(cb: CallbackQuery, state: FSMContext):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id)
        if not pb:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        exp = pb.expires_at

    await state.update_data(pa_pb=pb_id, pa_panel_chat=cb.message.chat.id,
                             pa_panel_id=cb.message.message_id)
    await state.set_state(PartnerAdminFSM.days_manual)
    prompt = await cb.message.answer(
        "⌨️ <b>تعیین دستی روز</b>\n\n"
        f"اعتبار فعلی تا: {_fmt_dt(exp)}\n\n"
        "تعداد روز را بفرست:\n"
        "• <code>90</code> → ۹۰ روز <b>اضافه</b> می‌کند\n"
        "• <code>-15</code> → ۱۵ روز <b>کم</b> می‌کند\n"
        "• <code>=60</code> → اعتبار را روی <b>۶۰ روز از امروز</b> می‌گذارد",
        reply_markup=_cancel_kb(f"pa:view:{pb_id}"))  # PATCH_PARTNER_BACKNAV_V1
    await state.update_data(pa_prompt_id=prompt.message_id)  # === PATCH_PARTNER_CLEANCHAT_V1 ===
    await cb.answer()


@partner_admin_router.message(PartnerAdminFSM.days_manual)
async def pa_days_manual_set(msg: Message, state: FSMContext, bot: Bot):
    if not await _can_access(msg.from_user.id):
        return
    data = await state.get_data()
    pb_id = data.get("pa_pb")
    raw = (msg.text or "").strip().replace("٪", "")
    await state.clear()
    if not pb_id:
        await msg.answer("این مرحله منقضی شده.")
        return

    absolute = raw.startswith("=")
    body = raw[1:].strip() if absolute else raw
    try:
        days = int(body)
    except ValueError:
        await msg.answer(
            "عدد معتبر نیست. مثال: <code>90</code> یا <code>-15</code> "
            "یا <code>=60</code>")
        return
    if abs(days) > 3650:
        await msg.answer("حداکثر ۳۶۵۰ روز.")
        return
    if absolute and days <= 0:
        await msg.answer("برای تعیین مطلق، عدد باید مثبت باشد.")
        return

    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id, with_for_update=True)
        if not pb:
            await msg.answer("همکار پیدا نشد.")
            return
        if _self_dealing(msg.from_user.id, pb):
            await msg.answer("تغییرِ رباتِ همکارِ خودت مجاز نیست.")
            return
        now = datetime.datetime.now()
        if absolute:
            pb.expires_at = now + datetime.timedelta(days=days)
        else:
            base = pb.expires_at or now
            pb.expires_at = base + datetime.timedelta(days=days)
        br = await s.get(BotRow, pb.bot_id)
        if br:
            br.plan_expires_at = pb.expires_at
        new_exp = pb.expires_at
        owner = pb.owner_id
        # اگر اعتبار به آینده رفت و ربات قبلاً منقضی شده بود، برگردانش
        revived = False
        if pb.status == "expired" and new_exp > now:
            pb.status = "active"
            if br:
                br.status = "active"
                br.lock_reason = None
            revived = True
        # === PATCH_PARTNER_DAYS_UNARCHIVE_FIX_V1 ===
        # همین‌جا هم اگر ربات قبلاً بایگانی شده بود، archived_at را پاک کن
        # وگرنه با وجودِ status=active و plan_expires_at تازه، برای همیشه
        # از چکِ سررسیدِ subscription_loop معاف می‌ماند (باگِ کشف‌شده).
        if br and br.archived_at and new_exp > now:
            br.archived_at = None
            br.archive_reason = None
            br.last_expiry_notice = None
            br.status = "active"
        await s.commit()

    if absolute:
        what = f"اعتبار روی {days} روز از امروز تنظیم شد"
    elif days >= 0:
        what = f"{days} روز اضافه شد"
    else:
        what = f"{abs(days)} روز کم شد"
    await _cleanchat_finish(
        bot, data, msg,
        f"✅ {what}.\n⏳ اعتبار تا: {_fmt_dt(new_exp)}"
        + ("\n▶️ ربات همکار دوباره فعال شد." if revived else ""))

    try:
        await bot.send_message(
            owner,
            f"⏳ <b>اعتبار ربات همکار شما به‌روز شد</b>\n\n"
            f"تا تاریخ: {_fmt_dt(new_exp)}"
            + ("\n\n▶️ ربات شما دوباره فعال شد." if revived else ""))
    except Exception:
        logger.exception("اطلاعِ تغییرِ اعتبار به همکار ناموفق بود")


@partner_admin_router.callback_query(F.data.startswith("pa:stock:"))
async def pa_stock_ask(cb: CallbackQuery, state: FSMContext):
    if not await _admin_only(cb):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    pb_id = int(cb.data.split(":")[2])
    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id)
        if not pb:
            await cb.answer("پیدا نشد", show_alert=True)
            return
        g, p = pb.stock_growth, pb.stock_pro

    await state.update_data(pa_pb=pb_id, pa_panel_chat=cb.message.chat.id,
                             pa_panel_id=cb.message.message_id)
    await state.set_state(PartnerAdminFSM.stock_manual)
    prompt = await cb.message.answer(
        "📦 <b>افزودن / کسر ربات</b>\n\n"
        f"موجودی فعلی:\n🚀 رشد: <b>{g}</b>\n💎 حرفه‌ای: <b>{p}</b>\n\n"
        "به این شکل بفرست: <code>پلن تعداد</code>\n\n"
        "• <code>رشد 10</code> → ۱۰ ربات رشد اضافه\n"
        "• <code>حرفه‌ای -3</code> → ۳ ربات حرفه‌ای کم\n"
        "• <code>رشد =25</code> → موجودی رشد را روی ۲۵ بگذار",
        reply_markup=_cancel_kb(f"pa:view:{pb_id}"))  # PATCH_PARTNER_BACKNAV_V1
    await state.update_data(pa_prompt_id=prompt.message_id)  # === PATCH_PARTNER_CLEANCHAT_V1 ===
    await cb.answer()


_PLAN_WORDS = {
    "رشد": "growth", "growth": "growth", "g": "growth",
    "حرفه‌ای": "pro", "حرفه ای": "pro", "حرفهای": "pro",
    "pro": "pro", "p": "pro",
}


@partner_admin_router.message(PartnerAdminFSM.stock_manual)
async def pa_stock_set(msg: Message, state: FSMContext, bot: Bot):
    if not await _can_access(msg.from_user.id):
        return
    data = await state.get_data()
    pb_id = data.get("pa_pb")
    raw = (msg.text or "").strip()
    await state.clear()
    if not pb_id:
        await msg.answer("این مرحله منقضی شده.")
        return

    parts = raw.split()
    if len(parts) < 2:
        await msg.answer(
            "قالب درست: <code>رشد 10</code> یا <code>حرفه‌ای -3</code>")
        return

    plan_key = _PLAN_WORDS.get(" ".join(parts[:-1]).strip().lower())
    if not plan_key:
        await msg.answer(
            "پلن را نشناختم. از <code>رشد</code> یا <code>حرفه‌ای</code> "
            "استفاده کن.")
        return

    body = parts[-1]
    absolute = body.startswith("=")
    if absolute:
        body = body[1:]
    try:
        n = int(body)
    except ValueError:
        await msg.answer("تعداد معتبر نیست.")
        return
    if abs(n) > 10_000:
        await msg.answer("حداکثر ۱۰۰۰۰ عدد.")
        return
    if absolute and n < 0:
        await msg.answer("برای تعیین مطلق، عدد نمی‌تواند منفی باشد.")
        return

    async with Session() as s:
        pb = await s.get(PartnerBot, pb_id, with_for_update=True)
        if not pb:
            await msg.answer("همکار پیدا نشد.")
            return
        if _self_dealing(msg.from_user.id, pb):
            await msg.answer("تغییرِ موجودیِ رباتِ همکارِ خودت مجاز نیست.")
            return
        cur = pb.stock_growth if plan_key == "growth" else pb.stock_pro
        new = n if absolute else cur + n
        if new < 0:
            await msg.answer(
                f"موجودی نمی‌تواند منفی شود (فعلی: {cur}، درخواست: {n}).")
            return
        if plan_key == "growth":
            pb.stock_growth = new
            # آمارِ «خریداری‌شده» فقط با افزایش بالا می‌رود، نه با کسر
            if not absolute and n > 0:
                pb.bought_growth += n
        else:
            pb.stock_pro = new
            if not absolute and n > 0:
                pb.bought_pro += n
        # === PATCH_PARTNER_STOCK_LOG_V1 ===
        await log_stock(
            s, pb.id, plan_key, new - cur, "admin",
            ("تعیین مطلق توسط مدیر" if absolute else "تنظیم دستی توسط مدیر")
            + f" ({msg.from_user.id})", new)
        owner = pb.owner_id
        g, p = pb.stock_growth, pb.stock_pro
        await s.commit()

    # === PATCH_PARTNER_V34 === کشِ سمتِ مستأجر باید باطل شود
    try:
        from tenant.partner_bot.partner_sales import invalidate_pb_cache
        async with Session() as s:
            pbx = await s.get(PartnerBot, pb_id)
            if pbx:
                invalidate_pb_cache(pbx.bot_id)
    except Exception:
        logger.exception("باطل‌سازیِ کشِ PartnerBot ناموفق بود")

    fa = "رشد" if plan_key == "growth" else "حرفه‌ای"
    if absolute:
        what = f"موجودی {fa} روی {new} تنظیم شد"
    elif n >= 0:
        what = f"{n} ربات {fa} اضافه شد"
    else:
        what = f"{abs(n)} ربات {fa} کم شد"

    await _cleanchat_finish(
        bot, data, msg,
        f"✅ {what}.\n📦 موجودی جدید: رشد {g} | حرفه‌ای {p}")

    if not absolute and n > 0:
        try:
            await bot.send_message(
                owner,
                f"🎁 <b>{n} ربات {fa}</b> توسط مدیریت به موجودی شما "
                f"اضافه شد.\n\n📦 موجودی جدید:\n🚀 رشد: <b>{g}</b>\n"
                f"💎 حرفه‌ای: <b>{p}</b>")
        except Exception:
            logger.exception("اطلاعِ افزایشِ موجودی به همکار ناموفق بود")
