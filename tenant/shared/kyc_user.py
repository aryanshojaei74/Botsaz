"""
kyc_user.py — PATCH_KYC_V1

جریانِ احرازِ هویت از سمتِ کاربر، و صفِ بررسی برایِ ادمین.

  سطح ۱ — کاربر شمارهٔ کارت و عکسِ کارت را می‌فرستد؛ ادمین تأیید می‌کند.
  سطح ۲ — کاربر فایلی که مالکِ ربات تعیین کرده می‌فرستد؛ ادمین تأیید می‌کند.
"""
from __future__ import annotations

import html
import re

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import select

from config import Session
from models import BotUser, KycRequest
from tenant.shared import kyc as K

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

kyc_user_router = Router()

_DEF_L2_TEXT = (
    "برایِ احرازِ هویتِ سطحِ ۲:\n\n"
    "۱) کارتِ بانکی‌ای که با آن پرداخت کرده‌ای و کارتِ ملی را کنارِ هم بگیر.\n"
    "۲) روی یک برگه بنویس: «این احرازِ هویت برایِ این مجموعه است و "
    "قوانینِ مجموعه را پذیرفتم.» به‌همراهِ تاریخ و امضا.\n"
    "۳) هر سه را جلویِ صورتت بگیر و همان متن را بخوان.\n\n"
    "هر سه مورد باید واضح و خوانا باشند."
)


class KycUserFSM(StatesGroup):
    card_no = State()
    card_photo = State()
    l2_file = State()


def _norm_digits(t: str) -> str:
    tr = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return re.sub(r"\D", "", (t or "").translate(tr))


def _mask(card: str) -> str:
    c = _norm_digits(card)
    return f"{c[:6]}******{c[-4:]}" if len(c) == 16 else c


async def _get_user(bot_id: int, tg_id: int):
    async with Session() as s:
        return await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))


async def _pending_of(bot_id: int, user_id: int):
    async with Session() as s:
        return await s.scalar(select(KycRequest).where(
            KycRequest.bot_id == bot_id, KycRequest.user_id == user_id,
            KycRequest.status == "pending").order_by(KycRequest.id.desc()))


# ───────────────────────── صفحهٔ کاربر ─────────────────────────
async def user_content(bot_id: int, tg_id: int):
    u = await _get_user(bot_id, tg_id)
    if not u:
        return "کاربر پیدا نشد.", InlineKeyboardMarkup(inline_keyboard=[])

    lvl = int(getattr(u, "kyc_level", 0) or 0)
    blocked = bool(getattr(u, "kyc_blocked", False))
    cap = await K.cap_for(bot_id, lvl)
    used = await K.charged_today(bot_id, u.id)
    pend = await _pending_of(bot_id, u.id)

    names = {0: "سطح ۰ — بدونِ احراز",
             1: "سطح ۱ — کارتِ بانکیِ تأییدشده",
             2: "سطح ۲ — احرازِ کامل"}
    txt = ["🛡 <b>احرازِ هویت</b>\n", f"وضعیتِ فعلی: <b>{names.get(lvl, '—')}</b>"]

    if blocked:
        txt.append("\n🔒 حسابِ شما محدود شده و تا انجامِ احرازِ سطحِ بعدی "
                   "امکانِ شارژ ندارید.")
    elif cap > 0:
        txt.append(f"\nسقفِ شارژِ روزانه: <b>{cap:,}</b> تومان")
        txt.append(f"مصرفِ ۲۴ ساعتِ اخیر: <b>{used:,}</b> تومان")
        txt.append(f"باقی‌مانده: <b>{max(0, cap - used):,}</b> تومان")
    else:
        txt.append("\nشارژِ شما محدودیتی ندارد. ✅")

    can_change = False
    if getattr(u, "kyc_card", None):
        txt.append(f"\n💳 کارتِ ثبت‌شده: <code>{_mask(u.kyc_card)}</code>")
        txt.append("پرداخت فقط با همین کارت ممکن است.")
        # === PATCH_KYC_CARD_CHANGE_V1 ===
        if pend is None and not blocked:
            can_change, _why = await K.card_change_status(bot_id, u.id)
            txt.append(f"🔄 {_why}")

    # === PATCH_KYC_REJECT_REASON_V1 === آخرین درخواست اگر رد شده، دلیلِ ادمین (اگر نوشته) نشان داده شود
    last_rej = None
    if pend is None:
        async with Session() as s:
            last = await s.scalar(select(KycRequest).where(
                KycRequest.bot_id == bot_id, KycRequest.user_id == u.id).order_by(KycRequest.id.desc()))
        if last is not None and last.status == "rejected":
            last_rej = last
    if last_rej is not None:
        txt.append(f"\n❌ آخرین درخواستِ شما (سطح {last_rej.level}) تأیید نشد.")
        if last_rej.admin_note:
            txt.append(f"📝 دلیل: {html.escape(last_rej.admin_note)}")

    rows = []
    if pend is not None:
        txt.append(f"\n⏳ درخواستِ سطح {pend.level} شما در انتظارِ بررسیِ ادمین است.")
    elif lvl == 0:
        rows.append([InlineKeyboardButton(
            text="💳 احرازِ سطح ۱ (کارتِ بانکی)", callback_data="kycu:l1",
            style=ButtonStyle.SUCCESS)])
    elif lvl == 1:
        rows.append([InlineKeyboardButton(
            text="📄 احرازِ سطح ۲ (ارسالِ مدارک)", callback_data="kycu:l2",
            style=ButtonStyle.SUCCESS)])

    if can_change:
        rows.append([InlineKeyboardButton(text="🔄 تغییر کارت", callback_data="kycu:cc",
                                          style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="🏠 بازگشت", callback_data="mbtn:home",
                                      style=ButtonStyle.DANGER)])
    return "\n".join(txt), InlineKeyboardMarkup(inline_keyboard=rows)


async def show_kyc_user(msg, bot_id: int, tg_id: int):
    txt, kb = await user_content(bot_id, tg_id)
    await msg.answer(txt, reply_markup=kb, parse_mode="HTML")


@kyc_user_router.callback_query(F.data == "kycu:menu")
async def kycu_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    await state.clear()
    txt, kb = await user_content(row.id, cb.from_user.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# ───────────────────────── سطح ۱ ─────────────────────────
@kyc_user_router.callback_query(F.data == "kycu:l1")
async def kycu_l1(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    await state.set_state(KycUserFSM.card_no)
    await cb.message.answer(
        "💳 <b>احرازِ سطح ۱</b>\n\n"
        "شمارهٔ ۱۶ رقمیِ کارتِ بانکی‌ات را بفرست.\n\n"
        "⚠️ بعد از تأیید، پرداخت فقط با همین کارت ممکن خواهد بود.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


# === PATCH_KYC_CARD_CHANGE_V1 ===
@kyc_user_router.callback_query(F.data == "kycu:cc")
async def kycu_card_change(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    u = await _get_user(row.id, cb.from_user.id)
    if not u or not getattr(u, "kyc_card", None) or bool(getattr(u, "kyc_blocked", False)):
        return await cb.answer("تغییرِ کارت برایِ حسابِ شما در دسترس نیست.", show_alert=True)
    if await _pending_of(row.id, u.id) is not None:
        return await cb.answer("درخواستِ قبلی‌ات هنوز در انتظارِ بررسی است.", show_alert=True)
    ok, why = await K.card_change_status(row.id, u.id)
    if not ok:
        return await cb.answer(why, show_alert=True)
    await state.set_state(KycUserFSM.card_no)
    await state.update_data(change=True)
    await cb.message.answer(
        "🔄 <b>تغییرِ کارت</b>\n\n"
        f"{why}\n\nشمارهٔ ۱۶ رقمیِ <b>کارتِ جدید</b> را بفرست.\n"
        "⚠️ بعد از تأییدِ ادمین، پرداخت فقط با کارتِ جدید ممکن است.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@kyc_user_router.message(KycUserFSM.card_no, F.text)
async def kycu_card_no(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        return
    c = _norm_digits(msg.text)
    if len(c) != 16:
        return await msg.answer("شمارهٔ کارت باید دقیقاً ۱۶ رقم باشد. دوباره بفرست.")
    await state.update_data(card=c)
    await state.set_state(KycUserFSM.card_photo)
    await msg.answer(
        f"شمارهٔ کارت: <code>{_mask(c)}</code>\n\n"
        "حالا یک <b>عکسِ واضح از همان کارت</b> بفرست "
        "(نامِ صاحبِ کارت باید خوانا باشد).",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")


@kyc_user_router.message(KycUserFSM.card_photo, F.photo)
async def kycu_card_photo(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    data = await state.get_data()
    card = data.get("card") or ""
    is_change = bool(data.get("change"))
    await state.clear()

    u = await _get_user(row.id, msg.from_user.id)
    if not u:
        return await msg.answer("کاربر پیدا نشد.")
    if is_change:   # PATCH_KYC_CARD_CHANGE_V1 — دوباره چک (بین شروع و ارسالِ عکس ممکن است تغییر کرده باشد)
        ok, why = await K.card_change_status(row.id, u.id)
        if not ok or await _pending_of(row.id, u.id) is not None:
            return await msg.answer(f"⚠️ {why}")

    async with Session() as s:
        s.add(KycRequest(bot_id=row.id, user_id=u.id, level=1, card=card,
                         file_id=msg.photo[-1].file_id, file_type="photo",
                         kind=("card_change" if is_change else None)))
        await s.commit()

    await msg.answer("✅ درخواستِ تغییرِ کارت ثبت شد.\n" if is_change
                     else "✅ درخواستِ احرازِ سطح ۱ ثبت شد.\n")
    await msg.answer("بعد از بررسیِ ادمین نتیجه را اطلاع می‌دهیم.")
    await _notify_admins_new(bot, row, u, 1)


@kyc_user_router.message(KycUserFSM.card_photo)
async def kycu_card_photo_wrong(msg: Message):
    await msg.answer("لطفاً یک <b>عکس</b> از کارت بفرست.", parse_mode="HTML")


# ───────────────────────── سطح ۲ ─────────────────────────
@kyc_user_router.callback_query(F.data == "kycu:l2")
async def kycu_l2(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    guide = await get_setting(row.id, "kyc_l2_text", "") or _DEF_L2_TEXT
    media = await get_setting(row.id, "kyc_l2_media", "video_note")
    await state.set_state(KycUserFSM.l2_file)
    await cb.message.answer(
        f"📄 <b>احرازِ سطح ۲</b>\n\n{guide}\n\n"
        f"نوعِ فایلِ موردِ نیاز: <b>{K.media_label(media)}</b>",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@kyc_user_router.message(KycUserFSM.l2_file)
async def kycu_l2_file(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, get_setting
    row = await get_bot_row(bot)
    if not row:
        return
    want = await get_setting(row.id, "kyc_l2_media", "video_note")

    fid = ftype = None
    if msg.video_note:
        fid, ftype = msg.video_note.file_id, "video_note"
    elif msg.video:
        fid, ftype = msg.video.file_id, "video"
    elif msg.photo:
        fid, ftype = msg.photo[-1].file_id, "photo"

    if not fid:
        return await msg.answer(
            f"لطفاً {K.media_label(want)} بفرست.")
    if want != "any" and ftype != want:
        return await msg.answer(
            f"فقط {K.media_label(want)} پذیرفته می‌شود. دوباره بفرست.")

    await state.clear()
    u = await _get_user(row.id, msg.from_user.id)
    if not u:
        return await msg.answer("کاربر پیدا نشد.")

    async with Session() as s:
        s.add(KycRequest(bot_id=row.id, user_id=u.id, level=2,
                         file_id=fid, file_type=ftype))
        await s.commit()

    await msg.answer("✅ مدارکِ سطح ۲ دریافت شد.\n"
                     "بعد از بررسیِ ادمین نتیجه را اطلاع می‌دهیم.")
    await _notify_admins_new(bot, row, u, 2)


# ───────────────────────── اطلاع به ادمین ─────────────────────────
async def _notify_admins_new(bot: Bot, row, u, level: int) -> None:
    try:
        from tenant import _notify_admins
        uname = f"@{u.username}" if u.username else "—"
        await _notify_admins(bot, row, (
            f"🛡 <b>درخواستِ احرازِ هویت — سطح {level}</b>\n\n"
            f"👤 {u.full_name or '—'}\n"
            f"🆔 <code>{u.telegram_id}</code>\n"
            f"🔗 {uname}\n\n"
            "از پنل → مالی → احرازِ هویت → «درخواست‌هایِ در انتظار» بررسی کن."))
    except Exception:
        logger.exception("kyc: اطلاع به ادمین ناموفق بود")
