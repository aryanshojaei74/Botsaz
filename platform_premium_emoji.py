# -*- coding: utf-8 -*-
"""
platform_premium_emoji.py — بخشِ «⭐ افزودن ایموجی پرمیوم (ربات اصلی)»
در پنلِ مدیریتِ خودِ رباتِ اصلیِ بات‌ساز (پلتفرم/مادر)
(PATCH_PLATFORM_PREMIUM_EMOJI_V1).

دقیقاً همان قابلیتِ «⭐ افزودن ایموجی پرمیوم» که برایِ ربات‌های مستأجرِ
پلنِ حرفه‌ای/همکار در tenant/premium_emoji_admin.py ساخته شد، ولی اینجا
برایِ خودِ رباتِ مادر — چون فقط یک رباتِ پلتفرم وجود دارد، نگاشت‌ها
سراسری هستند (بدونِ bot_id) و در جدولِ PlatformPremiumEmoji ذخیره
می‌شوند؛ فقط مدیرِ اصلیِ پلتفرم (PLATFORM_ADMIN_ID) دسترسی دارد.

این فایل عمداً از tenant/premium_emoji_admin.py یا از پکیجِ tenant
چیزی import نمی‌کند — importِ tenant.<هرچیزی> یعنی اجرایِ کاملِ
tenant/__init__.py (۲۰هزار خط) داخلِ پروسسِ main_platform.py، دقیقاً
همان جفت‌شدنِ سرویس‌ها که architecture.md ممنوع کرده. به‌جایش، منطقِ
عمومی (پارسِ HTML، میان‌افزارِ سراسری) از premium_emoji_dynamic.py
می‌آید که در ریشهٔ پروژه است و به هیچ‌کدام از دو سرویس وابسته نیست.

callbackها با پیشوند: pemj: (همان پیشوندِ نسخهٔ تننت — چون این دو در دو
پروسسِ کاملاً جدا اجرا می‌شوند، تصادمی پیش نمی‌آید؛ نگاه کن
callback-map.md، بخشِ «پیشوندهایی که در دو ماژول زندگی می‌کنند»).
"""
from __future__ import annotations

import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import PLATFORM_ADMIN_ID, PLATFORM_BOT_TOKEN, Session
from models import PlatformPremiumEmoji, PlatformSetting
from premium_emoji_dynamic import (
    extract_custom_emoji, plain_apply_map, register_emoji_map_provider,
)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_premium_emoji_router = Router()

PAGE_SIZE = 6
SETTING_KEY = "premium_emoji_enabled"

_PMAP_CACHE: dict | None = None
_PMAP_CACHE_EXP = 0.0
_PMAP_CACHE_TTL = 30.0


class PlatformPremiumEmojiFSM(StatesGroup):
    wait_normal = State()
    wait_premium = State()


# ─────────────── دسترسی (فقط مدیرِ اصلی) ───────────────

async def _admin_ok(cb: CallbackQuery) -> bool:
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("فقط مدیر اصلی به این بخش دسترسی دارد.", show_alert=True)
        return False
    return True


async def _admin_ok_msg(msg: Message) -> bool:
    return msg.from_user.id == PLATFORM_ADMIN_ID


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ لغو", callback_data="pemj:cancel", style=ButtonStyle.DANGER)]
    ])


# ─────────────── تنظیمِ کلید/مقدارِ پلتفرم (روشن/خاموش) ───────────────

async def _get_platform_setting(key: str, default: str = "") -> str:
    async with Session() as s:
        row = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
    return row.value if row else default


async def _set_platform_setting(key: str, value: str) -> None:
    async with Session() as s:
        row = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == key))
        if row:
            row.value = value
        else:
            s.add(PlatformSetting(key=key, value=value))
        await s.commit()


async def _is_enabled() -> bool:
    return (await _get_platform_setting(SETTING_KEY, "1")) == "1"


# ─────────────── ذخیره/خواندنِ نگاشت‌ها ───────────────

async def _count() -> int:
    async with Session() as s:
        return int(await s.scalar(select(func.count()).select_from(PlatformPremiumEmoji)) or 0)


async def _page(offset: int) -> list[PlatformPremiumEmoji]:
    async with Session() as s:
        rows = (await s.scalars(
            select(PlatformPremiumEmoji).order_by(PlatformPremiumEmoji.id)
            .offset(offset).limit(PAGE_SIZE))).all()
    return list(rows)


def _invalidate_cache() -> None:
    global _PMAP_CACHE, _PMAP_CACHE_EXP
    _PMAP_CACHE, _PMAP_CACHE_EXP = None, 0.0


async def _upsert(normal_emoji: str, premium_id: str, premium_display: str) -> None:
    async with Session() as s:
        row = await s.scalar(select(PlatformPremiumEmoji).where(
            PlatformPremiumEmoji.normal_emoji == normal_emoji))
        if row:
            row.premium_emoji_id = premium_id
            row.premium_display = premium_display
        else:
            s.add(PlatformPremiumEmoji(
                normal_emoji=normal_emoji, premium_emoji_id=premium_id,
                premium_display=premium_display))
        await s.commit()
    _invalidate_cache()


async def _update_by_id(row_id: int, premium_id: str, premium_display: str) -> PlatformPremiumEmoji | None:
    async with Session() as s:
        row = await s.get(PlatformPremiumEmoji, row_id)
        if not row:
            return None
        row.premium_emoji_id = premium_id
        row.premium_display = premium_display
        await s.commit()
        _invalidate_cache()
        return row


async def get_platform_premium_map() -> dict[str, tuple[str, str]]:
    """نگاشتِ سراسریِ رباتِ اصلی — با کشِ سبک، مشابهِ نسخهٔ تننت."""
    global _PMAP_CACHE, _PMAP_CACHE_EXP
    now = time.monotonic()
    if _PMAP_CACHE is not None and _PMAP_CACHE_EXP > now:
        return _PMAP_CACHE
    if not await _is_enabled():
        mapping: dict[str, tuple[str, str]] = {}
    else:
        async with Session() as s:
            rows = (await s.scalars(select(PlatformPremiumEmoji))).all()
        mapping = {r.normal_emoji: (r.premium_emoji_id, r.premium_display) for r in rows}
    _PMAP_CACHE, _PMAP_CACHE_EXP = mapping, now + _PMAP_CACHE_TTL
    return mapping


# ─────────────── نمایشِ منو/لیست ───────────────

async def _render_menu(target, offset: int, *, edit: bool = True):
    total = await _count()
    rows = await _page(offset)
    enabled = await _is_enabled()

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
    kb_rows.append([
        InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:cat:settings", style=ButtonStyle.DANGER),
        InlineKeyboardButton(text="🔄 رفرش", callback_data=f"pemj:refresh:{offset}"),
        InlineKeyboardButton(text="❌ بستن", callback_data="pemj:close", style=ButtonStyle.DANGER),
    ])

    text = (
        "⭐ <b>افزودن ایموجی پرمیوم (ربات اصلی بات‌ساز)</b>\n\n"
        f"وضعیت: {'🟢 فعال' if enabled else '🔴 غیرفعال'}\n"
        f"تعداد نگاشت‌های ثبت‌شده: {total}\n\n"
        "این نگاشت فقط رویِ خودِ رباتِ اصلیِ بات‌ساز اثر می‌گذارد (نه "
        "ربات‌هایِ مستأجر). هر ایموجیِ معمولی که اینجا ثبت کنی، هرجایِ "
        "همین ربات که همان ایموجی نمایش داده شود، به‌جایش نسخهٔ پرمیوم "
        "نمایش داده می‌شود.\n\n"
        "➕ برایِ افزودنِ ایموجیِ جدید روی دکمهٔ زیر بزن؛ اول ایموجیِ معمولی "
        "(مثلاً ❤️) را می‌فرستی، بعد ایموجیِ پرمیومِ متناظرش را (باید از "
        "اکانتِ پرمیوم ارسال شود)."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    msg = target.message if isinstance(target, CallbackQuery) else target
    try:
        if edit and isinstance(target, CallbackQuery):
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@platform_premium_emoji_router.callback_query(F.data.startswith("pemj:menu:"))
async def pemj_menu(cb: CallbackQuery, state: FSMContext):
    if not await _admin_ok(cb):
        return
    await state.clear()
    offset = int(cb.data.split(":")[2])
    await _render_menu(cb, offset, edit=True)
    await cb.answer()


@platform_premium_emoji_router.callback_query(F.data.startswith("pemj:refresh:"))
async def pemj_refresh(cb: CallbackQuery):
    if not await _admin_ok(cb):
        return
    offset = int(cb.data.split(":")[2])
    await _render_menu(cb, offset, edit=True)
    await cb.answer("رفرش شد")


@platform_premium_emoji_router.callback_query(F.data.startswith("pemj:toggle:"))
async def pemj_toggle(cb: CallbackQuery):
    if not await _admin_ok(cb):
        return
    offset = int(cb.data.split(":")[2])
    cur = await _get_platform_setting(SETTING_KEY, "1")
    new = "0" if cur == "1" else "1"
    await _set_platform_setting(SETTING_KEY, new)
    _invalidate_cache()
    await _render_menu(cb, offset, edit=True)
    await cb.answer("✅ قابلیت " + ("فعال شد" if new == "1" else "غیرفعال شد"))


@platform_premium_emoji_router.callback_query(F.data == "pemj:close")
async def pemj_close(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (بستنِ پیام)")
    await cb.answer()


@platform_premium_emoji_router.callback_query(F.data == "pemj:cancel")
async def pemj_cancel(cb: CallbackQuery, state: FSMContext):
    if not await _admin_ok(cb):
        return
    await state.clear()
    await _render_menu(cb, 0, edit=False)
    await cb.answer("لغو شد")


# ─────────────── مرحلهٔ ۱: افزودنِ ایموجیِ جدید ───────────────

@platform_premium_emoji_router.callback_query(F.data == "pemj:add")
async def pemj_add_start(cb: CallbackQuery, state: FSMContext):
    if not await _admin_ok(cb):
        return
    await state.clear()
    await state.set_state(PlatformPremiumEmojiFSM.wait_normal)
    await cb.message.answer(
        "1️⃣ مرحله ۱ از ۲:\n\n"
        "لطفاً ابتدا ایموجی عادی (پایه) را ارسال کنید.\n\n"
        "مثال: ❤️ یا ✅ یا 🔥",
        reply_markup=cancel_kb())
    await cb.answer()


@platform_premium_emoji_router.message(PlatformPremiumEmojiFSM.wait_normal, F.text)
async def pemj_add_normal(msg: Message, state: FSMContext):
    if not await _admin_ok_msg(msg):
        return
    normal = (msg.text or "").strip()
    if not normal or len(normal) > 16:
        await msg.answer("یک ایموجیِ تکی بفرست (نه متن دیگری):", reply_markup=cancel_kb())
        return
    await _pemj_save_normal_and_advance(msg, state, normal)


# === PATCH_PEMJ_DICE_EMOJI_FIX_V1 ===
# باگِ گزارش‌شده: از ۶ ایموجیِ خاص (🎲 🎯 🏀 ⚽ 🎰 🎳)، اگر کاربر فقط
# همان ایموجیِ تنها را بفرستد (نه از طریقِ منویِ ضمیمه/دایس)، خودِ
# تلگرام آن را به یک پیامِ تعاملیِ «Dice» تبدیل می‌کند — این نوع پیام
# اصلاً message.text ندارد (محتوایش توی message.dice است)، پس فیلترِ
# F.text بالا هیچ‌وقت باهاش match نمی‌شد و ثبتِ ایموجی بی‌صدا کار
# نمی‌کرد. حالا این حالت را هم می‌گیریم و از message.dice.emoji (که
# دقیقاً همان کاراکترِ ایموجی است) استفاده می‌کنیم.
@platform_premium_emoji_router.message(PlatformPremiumEmojiFSM.wait_normal, F.dice)
async def pemj_add_normal_dice(msg: Message, state: FSMContext):
    if not await _admin_ok_msg(msg):
        return
    normal = (msg.dice.emoji or "").strip() if msg.dice else ""
    if not normal:
        await msg.answer("یک ایموجیِ تکی بفرست (نه متن دیگری):", reply_markup=cancel_kb())
        return
    await _pemj_save_normal_and_advance(msg, state, normal)


async def _pemj_save_normal_and_advance(msg: Message, state: FSMContext, normal: str) -> None:
    await state.update_data(pemj_normal=normal)
    await state.set_state(PlatformPremiumEmojiFSM.wait_premium)
    await msg.answer(
        f"✅ ایموجی پایه ذخیره شد: {normal}\n\n"
        "2️⃣ حالا ایموجی پرمیومی که باید جای این نمایش داده شود را ارسال کنید.\n\n"
        "⚠️ توجه: باید یک ایموجیِ پرمیومِ واقعی (از اکانتِ دارایِ Telegram "
        "Premium) بفرستید — نه شکلِ معمولیِ همان ایموجی.",
        reply_markup=cancel_kb())


# ─────────────── ویرایش: مستقیم می‌رود مرحلهٔ ۲ ───────────────

@platform_premium_emoji_router.callback_query(F.data.startswith("pemj:edit:"))
async def pemj_edit_start(cb: CallbackQuery, state: FSMContext):
    if not await _admin_ok(cb):
        return
    row_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(PlatformPremiumEmoji, row_id)
    if not r:
        await cb.answer("این نگاشت پیدا نشد.", show_alert=True)
        return
    await state.clear()
    await state.update_data(pemj_edit_id=row_id)
    await state.set_state(PlatformPremiumEmojiFSM.wait_premium)
    await cb.message.answer(
        f"✏️ ویرایشِ ایموجیِ پرمیومِ «{r.normal_emoji}»\n\n"
        "ایموجی پرمیومِ جدید را ارسال کنید (باید از اکانتِ دارایِ Telegram "
        "Premium باشد):",
        reply_markup=cancel_kb())
    await cb.answer()


# ─────────────── مرحلهٔ ۲: دریافتِ ایموجیِ پرمیوم + استخراجِ خودکارِ آیدی ───────────────

@platform_premium_emoji_router.message(PlatformPremiumEmojiFSM.wait_premium, F.text)
async def pemj_add_premium(msg: Message, state: FSMContext):
    if not await _admin_ok_msg(msg):
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
        r = await _update_by_id(int(data["pemj_edit_id"]), premium_id, premium_display)
        if not r:
            await msg.answer("این نگاشت دیگر وجود ندارد.")
            await _render_menu(msg, 0, edit=False)
            return
        await msg.answer(
            f"✅ ایموجی پرمیوم برایِ «{r.normal_emoji}» بروزرسانی شد.\n\n"
            f"آیدی پرمیوم: <code>{premium_id}</code>", parse_mode="HTML")
        await _render_menu(msg, 0, edit=False)
        return

    normal = data.get("pemj_normal")
    if not normal:
        await msg.answer("چیزی برایِ ثبت پیدا نشد؛ از اول شروع کن.")
        await _render_menu(msg, 0, edit=False)
        return

    await _upsert(normal, premium_id, premium_display)
    await msg.answer(
        "✅ <b>ایموجی پرمیوم جدید با موفقیت اضافه شد!</b>\n\n"
        f"• ایموجی پایه: {normal}\n"
        f"• آیدی پرمیوم: <code>{premium_id}</code>\n\n"
        f"✨ از این لحظه، در پیام‌هایِ رباتِ اصلی، «{normal}» به نسخهٔ "
        "پرمیوم تبدیل می‌شود.", parse_mode="HTML")
    await _render_menu(msg, 0, edit=False)


# ─────────────── حذف ───────────────

@platform_premium_emoji_router.callback_query(F.data.startswith("pemj:del:"))
async def pemj_del_confirm(cb: CallbackQuery):
    if not await _admin_ok(cb):
        return
    row_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(PlatformPremiumEmoji, row_id)
    if not r:
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


@platform_premium_emoji_router.callback_query(F.data.startswith("pemj:del_ok:"))
async def pemj_del_ok(cb: CallbackQuery):
    if not await _admin_ok(cb):
        return
    row_id = int(cb.data.split(":")[2])
    async with Session() as s:
        r = await s.get(PlatformPremiumEmoji, row_id)
        if r:
            await s.delete(r)
            await s.commit()
    _invalidate_cache()
    await cb.answer("حذف شد.")
    await _render_menu(cb, 0, edit=True)


# ─────────────── اتصال به میان‌افزارِ سراسری (premium_emoji_dynamic) ───────────────

async def _platform_map_provider(token: str):
    if not PLATFORM_BOT_TOKEN or token != PLATFORM_BOT_TOKEN:
        return None
    return await get_platform_premium_map()


register_emoji_map_provider(_platform_map_provider)
