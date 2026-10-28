# -*- coding: utf-8 -*-
"""
premium_emoji_admin.py — بخشِ «⭐ افزودن ایموجی پرمیوم» در تنظیماتِ هر
ربات مستأجر (PATCH_TENANT_PREMIUM_EMOJI_V1).

فقط پلنِ حرفه‌ای و ربات‌هایِ همکار این قابلیت را می‌بینند (نگاه کن
Feature.PREMIUM_EMOJI در plans.py) — رایگان و رشد ندارند.

مالکِ ربات می‌تواند از داخلِ تنظیماتِ رباتِ خودش، یک ایموجیِ معمولی
(مثلاً ❤️) را به یک ایموجیِ پرمیومِ متحرک (که خودش با اکانتِ پرمیومِ
تلگرام ارسال می‌کند) نگاشت کند. ثبت به‌صورتِ یک ویزاردِ دو مرحله‌ای است:

  مرحلهٔ ۱: ادمین همان ایموجیِ معمولی را می‌فرستد (مثلاً ❤️).
  مرحلهٔ ۲: ادمین ایموجیِ پرمیومِ متناظر را می‌فرستد؛ چون ایموجیِ پرمیوم
            در بدنهٔ پیام به‌صورتِ یک entity از نوعِ custom_emoji می‌آید،
            آیدیِ عددی‌اش خودکار از رویِ همان entity استخراج می‌شود —
            نیازی نیست ادمین آیدی را دستی تایپ کند.

ثبتِ دوبارهٔ همان ایموجیِ معمولی، رکوردِ قبلی را آپدیت می‌کند (تازه‌ترین
آیدیِ پرمیوم فعال است) — برایِ همین دکمهٔ «ویرایش» هم دقیقاً همین
مرحلهٔ ۲ را دوباره باز می‌کند.

نکته: فرستادنِ یک ایموجیِ پرمیومِ واقعی (custom_emoji entity) فقط از
اکانت‌هایِ دارایِ اشتراکِ Telegram Premium ممکن است.

جایگزینیِ خودکار (نکتهٔ مهم): پس از ثبتِ نگاشت، هرجایِ همین ربات که
متنِ خروجی (پیام یا کپشن) حاویِ همان ایموجیِ معمولی باشد — چه در متنِ
دستیِ ادمین (مثلاً پیامِ خوش‌آمد)، چه در پیام‌هایِ خودکارِ خودِ ربات —
همان بخش خودکار با ایموجیِ پرمیوم جایگزین می‌شود؛ منطقِ عمومیِ این
جایگزینی (پارسِ HTML + میان‌افزارِ سراسریِ aiogram Session) در فایلِ
مشترکِ premium_emoji_dynamic.py است (نه اینجا) تا هم رباتِ اصلیِ بات‌ساز
(platform_premium_emoji.py) بتواند از همان زیرساخت استفاده کند و هم
importِ این فایل هیچ‌وقت کاری با پکیجِ platform نداشته باشد.

callbackها با پیشوند: pemj:
  pemj:menu:{offset}     — صفحهٔ اصلی/لیستِ صفحه‌بندی‌شده
  pemj:add               — شروعِ افزودنِ ایموجیِ جدید (مرحلهٔ ۱)
  pemj:edit:{id}         — شروعِ ویرایش (مستقیم می‌رود مرحلهٔ ۲ برایِ همان id)
  pemj:del:{id}          — درخواستِ حذف
  pemj:del_ok:{id}       — تأییدِ حذف
  pemj:toggle:{offset}   — روشن/خاموش‌کردنِ کلِ قابلیت برایِ این ربات
  pemj:refresh:{offset}  — رفرشِ همان صفحه
  pemj:close             — بستنِ صفحه
  pemj:cancel            — لغوِ ویزاردِ در جریان و بازگشت به لیست
"""
from __future__ import annotations

import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import Session
from models import TenantPremiumEmoji
from premium_emoji_dynamic import (
    extract_custom_emoji, plain_apply_map, register_emoji_map_provider,
)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

premium_emoji_admin_router = Router()

PAGE_SIZE = 6
SETTING_KEY = "premium_emoji_enabled"

# کشِ سبکِ نگاشت (برایِ اینکه providerِ زیر روی هر پیامِ خروجی بارِ
# دیتابیسِ اضافه نگذارد). با هر افزودن/ویرایش/حذف/تغییرِ وضعیت باطل
# می‌شود.
_PMAP_CACHE: dict[int, tuple[dict, float]] = {}
_PMAP_CACHE_TTL = 30.0
_TOKEN_INFO_CACHE: dict[str, tuple[int | None, str | None, float]] = {}
_TOKEN_CACHE_TTL = 300.0


class PremiumEmojiFSM(StatesGroup):
    wait_normal = State()
    wait_premium = State()


# ─────────────── کمک‌توابعِ عمومی (مالک/تنظیمات) ───────────────

async def _owner(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    return await _owner_ok(cb, bot)


async def _owner_msg(msg: Message, bot: Bot):
    from tenant import _owner_ok_msg
    return await _owner_ok_msg(msg, bot)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ لغو", callback_data="pemj:cancel", style=ButtonStyle.DANGER)]
    ])


async def _is_enabled(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, SETTING_KEY, "1")) == "1"


# ─────────────── ذخیره/خواندن نگاشت‌ها ───────────────

async def _count(bot_id: int) -> int:
    async with Session() as s:
        return int(await s.scalar(
            select(func.count()).select_from(TenantPremiumEmoji)
            .where(TenantPremiumEmoji.bot_id == bot_id)) or 0)


async def _page(bot_id: int, offset: int) -> list[TenantPremiumEmoji]:
    async with Session() as s:
        rows = (await s.scalars(
            select(TenantPremiumEmoji).where(TenantPremiumEmoji.bot_id == bot_id)
            .order_by(TenantPremiumEmoji.id).offset(offset).limit(PAGE_SIZE))).all()
    return list(rows)


async def _upsert(bot_id: int, normal_emoji: str, premium_id: str, premium_display: str) -> None:
    async with Session() as s:
        row = await s.scalar(select(TenantPremiumEmoji).where(
            TenantPremiumEmoji.bot_id == bot_id,
            TenantPremiumEmoji.normal_emoji == normal_emoji))
        if row:
            row.premium_emoji_id = premium_id
            row.premium_display = premium_display
        else:
            s.add(TenantPremiumEmoji(
                bot_id=bot_id, normal_emoji=normal_emoji,
                premium_emoji_id=premium_id, premium_display=premium_display))
        await s.commit()
    _PMAP_CACHE.pop(bot_id, None)


async def _update_by_id(row_id: int, bot_id: int, premium_id: str, premium_display: str) -> TenantPremiumEmoji | None:
    async with Session() as s:
        row = await s.get(TenantPremiumEmoji, row_id)
        if not row or row.bot_id != bot_id:
            return None
        row.premium_emoji_id = premium_id
        row.premium_display = premium_display
        await s.commit()
        _PMAP_CACHE.pop(bot_id, None)
        return row


# ─────────────── نمایشِ منو/لیست ───────────────

async def _render_menu(target, bot_id: int, offset: int, plan: str = "", *, edit: bool = True):
    total = await _count(bot_id)
    rows = await _page(bot_id, offset)
    enabled = await _is_enabled(bot_id)

    kb_rows: list[list[InlineKeyboardButton]] = []
    for r in rows:
        kb_rows.append([
            InlineKeyboardButton(text=r.normal_emoji or "❔", callback_data="noop"),
            InlineKeyboardButton(text=r.premium_display or "⭐",
                                  icon_custom_emoji_id=r.premium_emoji_id, callback_data="noop"),
            InlineKeyboardButton(text="✏️ ویرایش", callback_data=f"pemj:edit:{r.id}", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="🗑 حذف", callback_data=f"pemj:del:{r.id}", style=ButtonStyle.DANGER),
        ])

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    cur_page = offset // PAGE_SIZE + 1
    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(
            text="« قبل", callback_data=f"pemj:menu:{max(0, offset - PAGE_SIZE)}"))
    nav_row.append(InlineKeyboardButton(text=f"📄 {cur_page}/{total_pages}", callback_data="noop"))
    if offset + PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton(
            text="بعد »", callback_data=f"pemj:menu:{offset + PAGE_SIZE}"))
    if len(nav_row) > 1 or total > PAGE_SIZE:
        kb_rows.append(nav_row)

    kb_rows.append([InlineKeyboardButton(
        text="➕ افزودن ایموجی دیگر", callback_data="pemj:add", style=ButtonStyle.SUCCESS)])
    kb_rows.append([InlineKeyboardButton(
        text=("🔴 خاموش کردن قابلیت" if enabled else "🟢 روشن کردن قابلیت"),
        callback_data=f"pemj:toggle:{offset}",
        style=(ButtonStyle.DANGER if enabled else ButtonStyle.SUCCESS))])
    auto_on = await _auto_enabled(bot_id)
    kb_rows.append([InlineKeyboardButton(
        text=("🤖 تبدیلِ خودکارِ ایموجی‌ها: روشن" if auto_on else "🤖 تبدیلِ خودکارِ ایموجی‌ها: خاموش"),
        callback_data=f"pemj:auto:{offset}",
        style=(ButtonStyle.SUCCESS if auto_on else ButtonStyle.DANGER))])
    # === PATCH_TENANT_PREMIUM_EMOJI_BACK_FIX_V1 ===
    # مسیرِ صحیحِ بازگشت بسته به پلن فرق دارد: ربات‌های عادیِ حرفه‌ای از
    # «tadm:sec:settings» (زیرمنویِ دکمه‌ها و تنظیمات) به این‌جا می‌آیند،
    # ولی ربات‌هایِ همکار پنلِ کاملاً جداگانه‌ای دارند («pp:settings» در
    # tenant/partner_menu.py). «tadm:settings» مسیرِ درستی نیست — هیچ‌کدام
    # از منوهایِ فعلی به آن لینک نمی‌دهند.
    _back_cb = "pp:settings" if plan == "partner" else "tadm:sec:settings"
    kb_rows.append([
        InlineKeyboardButton(text="⬅️ بازگشت", callback_data=_back_cb, style=ButtonStyle.DANGER),
        InlineKeyboardButton(text="🔄 رفرش", callback_data=f"pemj:refresh:{offset}"),
        InlineKeyboardButton(text="❌ بستن", callback_data="pemj:close", style=ButtonStyle.DANGER),
    ])

    text = (
        "⭐ <b>افزودن ایموجی پرمیوم</b>\n\n"
        f"وضعیت: {'🟢 فعال' if enabled else '🔴 غیرفعال'}\n"
        f"تعداد نگاشت‌های ثبت‌شده: {total}\n\n"
        "هر ایموجیِ معمولی که اینجا ثبتش کنی، هرجایِ این ربات که قرار باشد "
        "همان ایموجی نمایش داده شود، به‌جایش نسخهٔ پرمیومِ ثبت‌شده نمایش "
        "داده می‌شود.\n\n"
        "🤖 با «تبدیلِ خودکار»، ایموجی‌هایِ رایج (✅ ❌ 💰 🔗 ...) بدونِ ثبتِ دستی "
        "پرمیوم می‌شوند؛ ثبتِ دستیِ شما همیشه اولویت دارد. (نیازمندِ پرمیوم "
        "بودنِ اکانتِ مالکِ ربات)\n\n"
        "➕ برایِ افزودنِ ایموجیِ جدید روی دکمهٔ زیر بزن؛ اول ایموجیِ معمولی "
        "(مثلاً ❤️) را می‌فرستی، بعد ایموجیِ پرمیومِ متناظرش را (باید از "
        "اکانتِ پرمیوم ارسال شود)."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    # === PATCH_PEMJ_FAKECB_COMPAT_V1 ===
    # قبلاً اینجا isinstance(target, CallbackQuery) چک می‌شد که فقط با
    # آبجکتِ واقعیِ CallbackQuery درست کار می‌کند؛ ولی وقتی این تابع از
    # حالتِ منویِ کیبوردی (tenant/__init__.py) صدا زده می‌شود، یک _FakeCb
    # پاس داده می‌شود که زیرکلاسِ CallbackQuery نیست (فقط شبیه‌سازی شده) —
    # پس isinstance همیشه False برمی‌گشت، msg به‌جایِ .message برابرِ خودِ
    # target می‌شد و در نهایت msg.answer در واقع متدِ cb.answer (که هیچ
    # پیامی نمی‌فرستد) صدا زده می‌شد؛ نتیجه: در حالتِ کیبوردی هیچ پیامی
    # ارسال نمی‌شد، بدونِ خطا. با duck-typing (وجودِ ویژگیِ data) هم
    # CallbackQuery واقعی و هم _FakeCb درست تشخیص داده می‌شوند.
    _is_cb_like = hasattr(target, "data")
    msg = target.message if _is_cb_like else target
    try:
        if edit and _is_cb_like:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@premium_emoji_admin_router.callback_query(F.data.startswith("pemj:menu:"))
async def pemj_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.clear()
    offset = int(cb.data.split(":")[2])
    await _render_menu(cb, row.id, offset, row.plan, edit=True)
    await cb.answer()


@premium_emoji_admin_router.callback_query(F.data.startswith("pemj:refresh:"))
async def pemj_refresh(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    offset = int(cb.data.split(":")[2])
    await _render_menu(cb, row.id, offset, row.plan, edit=True)
    await cb.answer("رفرش شد")


@premium_emoji_admin_router.callback_query(F.data.startswith("pemj:toggle:"))
async def pemj_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import get_setting, set_setting
    offset = int(cb.data.split(":")[2])
    cur = await get_setting(row.id, SETTING_KEY, "1")
    new = "0" if cur == "1" else "1"
    await set_setting(row.id, SETTING_KEY, new)
    _PMAP_CACHE.pop(row.id, None)
    await _render_menu(cb, row.id, offset, row.plan, edit=True)
    await cb.answer("✅ قابلیت " + ("فعال شد" if new == "1" else "غیرفعال شد"))


@premium_emoji_admin_router.callback_query(F.data.startswith("pemj:auto:"))
async def pemj_auto(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    from tenant import set_setting
    offset = int(cb.data.split(":")[2])
    new = "0" if await _auto_enabled(row.id) else "1"
    await set_setting(row.id, AUTO_KEY, new)
    _PMAP_CACHE.pop(row.id, None)
    await _render_menu(cb, row.id, offset, row.plan, edit=True)
    await cb.answer("✅ تبدیلِ خودکار " + ("روشن شد" if new == "1" else "خاموش شد"))


@premium_emoji_admin_router.callback_query(F.data == "pemj:close")
async def pemj_close(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (بستنِ پیام)")
    await cb.answer()


@premium_emoji_admin_router.callback_query(F.data == "pemj:cancel")
async def pemj_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.clear()
    await _render_menu(cb, row.id, 0, row.plan, edit=False)
    await cb.answer("لغو شد")


# ─────────────── مرحلهٔ ۱: افزودنِ ایموجیِ جدید ───────────────

@premium_emoji_admin_router.callback_query(F.data == "pemj:add")
async def pemj_add_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    await state.clear()
    await state.set_state(PremiumEmojiFSM.wait_normal)
    await cb.message.answer(
        "1️⃣ مرحله ۱ از ۲:\n\n"
        "لطفاً ابتدا ایموجی عادی (پایه) را ارسال کنید.\n\n"
        "مثال: ❤️ یا ✅ یا 🔥",
        reply_markup=cancel_kb())
    await cb.answer()


@premium_emoji_admin_router.message(PremiumEmojiFSM.wait_normal, F.text)
async def pemj_add_normal(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    normal = (msg.text or "").strip()
    if not normal or len(normal) > 16:
        await msg.answer("یک ایموجیِ تکی بفرست (نه متن دیگری):", reply_markup=cancel_kb())
        return
    await _pemj_save_normal_and_advance(msg, state, normal)


# === PATCH_PEMJ_DICE_EMOJI_FIX_V1 ===
# باگِ گزارش‌شده: از ۶ ایموجیِ خاص (🎲 🎯 🏀 ⚽ 🎰 🎳)، اگر کاربر فقط
# همان ایموجیِ تنها را بفرستد، خودِ تلگرام آن را به یک پیامِ تعاملیِ
# «Dice» تبدیل می‌کند که اصلاً message.text ندارد — پس فیلترِ F.text
# بالا هیچ‌وقت match نمی‌شد و ثبت بی‌صدا کار نمی‌کرد. حالا این حالت را
# هم می‌گیریم و از message.dice.emoji استفاده می‌کنیم.
@premium_emoji_admin_router.message(PremiumEmojiFSM.wait_normal, F.dice)
async def pemj_add_normal_dice(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    normal = (msg.dice.emoji or "").strip() if msg.dice else ""
    if not normal:
        await msg.answer("یک ایموجیِ تکی بفرست (نه متن دیگری):", reply_markup=cancel_kb())
        return
    await _pemj_save_normal_and_advance(msg, state, normal)


async def _pemj_save_normal_and_advance(msg: Message, state: FSMContext, normal: str) -> None:
    await state.update_data(pemj_normal=normal)
    await state.set_state(PremiumEmojiFSM.wait_premium)
    await msg.answer(
        f"✅ ایموجی پایه ذخیره شد: {normal}\n\n"
        "2️⃣ حالا ایموجی پرمیومی که باید جای این نمایش داده شود را ارسال کنید.\n\n"
        "⚠️ توجه: باید یک ایموجیِ پرمیومِ واقعی (از اکانتِ دارایِ Telegram "
        "Premium) بفرستید — نه شکلِ معمولیِ همان ایموجی.",
        reply_markup=cancel_kb())


# ─────────────── ویرایش: مستقیم می‌رود مرحلهٔ ۲ ───────────────

@premium_emoji_admin_router.callback_query(F.data.startswith("pemj:edit:"))
async def pemj_edit_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner(cb, bot)
    if not row:
        return
    row_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(TenantPremiumEmoji, row_id)
    if not r or r.bot_id != row.id:
        await cb.answer("این نگاشت پیدا نشد.", show_alert=True)
        return
    await state.clear()
    await state.update_data(pemj_edit_id=row_id)
    await state.set_state(PremiumEmojiFSM.wait_premium)
    await cb.message.answer(
        f"✏️ ویرایشِ ایموجیِ پرمیومِ «{r.normal_emoji}»\n\n"
        "ایموجی پرمیومِ جدید را ارسال کنید (باید از اکانتِ دارایِ Telegram "
        "Premium باشد):",
        reply_markup=cancel_kb())
    await cb.answer()


# ─────────────── مرحلهٔ ۲: دریافتِ ایموجیِ پرمیوم + استخراجِ خودکارِ آیدی ───────────────

@premium_emoji_admin_router.message(PremiumEmojiFSM.wait_premium, F.text)
async def pemj_add_premium(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_msg(msg, bot)
    if not row:
        return
    found = extract_custom_emoji(msg)
    if not found:
        await msg.answer(
            "❗️ در این پیام هیچ ایموجیِ پرمیومی پیدا نشد.\n\n"
            "این پیام باید حاویِ یک ایموجیِ پرمیومِ واقعی باشد (فقط با "
            "اکانتِ دارایِ اشتراکِ Telegram Premium قابلِ ارسال است). دوباره "
            "امتحان کن:", reply_markup=cancel_kb())
        return
    premium_id, premium_display = found
    data = await state.get_data()
    await state.clear()

    if data.get("pemj_edit_id"):
        r = await _update_by_id(int(data["pemj_edit_id"]), row.id, premium_id, premium_display)
        if not r:
            await msg.answer("این نگاشت دیگر وجود ندارد.")
            await _render_menu(msg, row.id, 0, row.plan, edit=False)
            return
        await msg.answer(
            f"✅ ایموجی پرمیوم برایِ «{r.normal_emoji}» بروزرسانی شد.\n\n"
            f"آیدی پرمیوم: <code>{premium_id}</code>", parse_mode="HTML")
        await _render_menu(msg, row.id, 0, row.plan, edit=False)
        return

    normal = data.get("pemj_normal")
    if not normal:
        await msg.answer("چیزی برایِ ثبت پیدا نشد؛ از اول شروع کن.")
        await _render_menu(msg, row.id, 0, row.plan, edit=False)
        return

    await _upsert(row.id, normal, premium_id, premium_display)
    await msg.answer(
        "✅ <b>ایموجی پرمیوم جدید با موفقیت اضافه شد!</b>\n\n"
        f"• ایموجی پایه: {normal}\n"
        f"• آیدی پرمیوم: <code>{premium_id}</code>\n\n"
        f"✨ از این لحظه، در پیام‌هایِ این ربات، «{normal}» به نسخهٔ "
        "پرمیوم تبدیل می‌شود.", parse_mode="HTML")
    await _render_menu(msg, row.id, 0, row.plan, edit=False)


# ─────────────── حذف ───────────────

@premium_emoji_admin_router.callback_query(F.data.startswith("pemj:del:"))
async def pemj_del_confirm(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    row_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(TenantPremiumEmoji, row_id)
    if not r or r.bot_id != row.id:
        await cb.answer("این نگاشت پیدا نشد.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف شود", callback_data=f"pemj:del_ok:{row_id}", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="❌ لغو", callback_data="pemj:menu:0")],
    ])
    try:
        await cb.message.edit_text(
            f"مطمئنی می‌خوای نگاشتِ «{r.normal_emoji}» حذف بشه؟", reply_markup=kb)
    except Exception:
        await cb.message.answer(
            f"مطمئنی می‌خوای نگاشتِ «{r.normal_emoji}» حذف بشه؟", reply_markup=kb)
    await cb.answer()


@premium_emoji_admin_router.callback_query(F.data.startswith("pemj:del_ok:"))
async def pemj_del_ok(cb: CallbackQuery, bot: Bot):
    row = await _owner(cb, bot)
    if not row:
        return
    row_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(TenantPremiumEmoji, row_id)
        if r and r.bot_id == row.id:
            await s.delete(r)
            await s.commit()
    _PMAP_CACHE.pop(row.id, None)
    await cb.answer("حذف شد.")
    await _render_menu(cb, row.id, 0, row.plan, edit=True)


# ─────────────── ابزارِ نگاشت (کش‌شده) + جایگزینیِ متن ───────────────

AUTO_KEY = "premium_emoji_auto"
_PLAT_CACHE: tuple[dict, float] | None = None


async def _auto_enabled(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, AUTO_KEY, "1")) == "1"


async def _platform_defaults() -> dict[str, tuple[str, str]]:
    """نگاشتِ پیش‌فرضِ پلتفرم: ثابتِ premium_emoji.PREMIUM_EMOJI_IDS +
    جدولِ PlatformPremiumEmoji (مدیریت‌شده توسطِ مدیرِ اصلی)."""
    global _PLAT_CACHE
    now = time.monotonic()
    if _PLAT_CACHE and _PLAT_CACHE[1] > now:
        return _PLAT_CACHE[0]
    out: dict[str, tuple[str, str]] = {}
    try:
        from premium_emoji import PREMIUM_EMOJI_IDS
        for k, v in PREMIUM_EMOJI_IDS.items():
            if v and k.strip("\ufe0f"):
                out[k] = (v, k)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (نگاشتِ ثابتِ پلتفرم)")
    try:
        from models import PlatformPremiumEmoji
        async with Session() as s:
            rows = (await s.scalars(select(PlatformPremiumEmoji))).all()
        for r in rows:
            out[r.normal_emoji] = (r.premium_emoji_id, r.premium_display)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (جدولِ پرمیومِ پلتفرم)")
    _PLAT_CACHE = (out, now + _PMAP_CACHE_TTL)
    return out


async def get_premium_map(bot_id: int) -> dict[str, tuple[str, str]]:
    """نگاشتِ {normal_emoji: (premium_emoji_id, premium_display)} این ربات
    را برمی‌گرداند؛ اگر قابلیت خاموش باشد، دیکشنریِ خالی. نتیجه تا
    _PMAP_CACHE_TTL ثانیه کش می‌شود."""
    now = time.monotonic()
    cached = _PMAP_CACHE.get(bot_id)
    if cached and cached[1] > now:
        return cached[0]
    if not await _is_enabled(bot_id):
        mapping: dict[str, tuple[str, str]] = {}
    else:
        async with Session() as s:
            rows = (await s.scalars(select(TenantPremiumEmoji).where(
                TenantPremiumEmoji.bot_id == bot_id))).all()
        mapping = {r.normal_emoji: (r.premium_emoji_id, r.premium_display) for r in rows}
        if await _auto_enabled(bot_id):
            # نگاشتِ پیش‌فرضِ پلتفرم زیرِ نگاشتِ دستیِ ربات؛ ثبتِ دستی برنده است
            for k, v in (await _platform_defaults()).items():
                mapping.setdefault(k, v)
    _PMAP_CACHE[bot_id] = (mapping, now + _PMAP_CACHE_TTL)
    return mapping


async def apply_premium_emoji(bot_id: int, text: str):
    """میان‌بُرِ عمومی: نگاشتِ این ربات را می‌خواند و روی یک متنِ ساده
    اعمال می‌کند. خروجی (plain_text, entities). در عمل لازم نیست هیچ
    ماژولی این را دستی صدا بزند — providerِ زیر این کار را خودکار رویِ
    همهٔ پیام‌ها/کپشن‌هایِ خروجیِ ربات انجام می‌دهد؛ این تابع فقط برایِ
    مواردِ خاص نگه داشته شده."""
    mapping = await get_premium_map(bot_id)
    return plain_apply_map(text, mapping)


# ─────────────── اتصال به میان‌افزارِ سراسری (premium_emoji_dynamic) ───────────────
#
# فقط رباتی که در جدولِ bots ثبت است و پلنش Feature.PREMIUM_EMOJI را
# دارد (پلنِ حرفه‌ای یا ربات همکار) و خودش این قابلیت را روشن نگه
# داشته، تحتِ تأثیر قرار می‌گیرد.

async def _bot_info_for_token(token: str) -> tuple[int, str] | None:
    now = time.monotonic()
    cached = _TOKEN_INFO_CACHE.get(token)
    if cached and cached[2] > now:
        bot_id, plan, _ = cached
        return (bot_id, plan) if bot_id is not None else None
    from models import Bot as BotRow
    async with Session() as s:
        row = await s.execute(select(BotRow.id, BotRow.plan).where(BotRow.token == token))
        rec = row.first()
    if rec:
        _TOKEN_INFO_CACHE[token] = (rec[0], rec[1], now + _TOKEN_CACHE_TTL)
        return rec[0], rec[1]
    _TOKEN_INFO_CACHE[token] = (None, None, now + _TOKEN_CACHE_TTL)
    return None


async def _tenant_map_provider(token: str):
    info = await _bot_info_for_token(token)
    if not info:
        return None
    bot_id, plan = info
    from plans import has_feature, Feature
    if not has_feature(plan, Feature.PREMIUM_EMOJI):
        return None
    return await get_premium_map(bot_id)


register_emoji_map_provider(_tenant_map_provider)
