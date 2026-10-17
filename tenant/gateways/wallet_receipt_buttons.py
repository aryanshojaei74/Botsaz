from __future__ import annotations

import json

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from config import Session
from models import MainButton

wallet_receipt_buttons_router = Router()
SETTING_KEY = "wallet_receipt_buttons"
LABEL = "🔘 انتخاب دکمه زیر پیام تأیید واریز"

async def _selected_ids(bot_id: int) -> set[int]:
    from tenant import get_setting
    raw = await get_setting(bot_id, SETTING_KEY, "")
    try:
        return {int(x) for x in json.loads(raw or "[]")}
    except Exception:
        return set()

async def _available(bot_id: int):
    async with Session() as s:
        return list((await s.scalars(select(MainButton).where(
            MainButton.bot_id == bot_id, MainButton.parent_id.is_(None)
        ).order_by(MainButton.row, MainButton.col, MainButton.id))).all())

async def _effective_ids(bot_id: int, buttons) -> set[int]:
    selected = await _selected_ids(bot_id)
    valid = {int(b.id) for b in buttons}
    selected &= valid
    if selected:
        return selected
    buy = [b for b in buttons if b.action == "buy_product"]
    return {int(b.id) for b in buy[:1]}

async def wallet_receipt_keyboard(bot_id: int) -> InlineKeyboardMarkup | None:
    buttons = await _available(bot_id)
    selected = await _effective_ids(bot_id, buttons)
    rows = []
    for b in buttons:
        if int(b.id) in selected:
            rows.append([InlineKeyboardButton(
                text=(b.label or "دکمهٔ اصلی").strip()[:64],
                callback_data=f"wrb:main:{b.id}", style=ButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

async def _render(cb: CallbackQuery, bot: Bot, bot_id: int):
    buttons = await _available(bot_id)
    selected = await _effective_ids(bot_id, buttons)
    lines = [
        "🔘 <b>انتخاب دکمه زیر پیام تأیید واریز</b>", "",
        "دکمه‌های صفحهٔ اصلی همین ربات را انتخاب کن تا زیر پیام تأیید واریز نمایش داده شوند.",
        "اگر انتخابی ذخیره نشده باشد، اولین دکمهٔ خرید محصول به‌صورت پیش‌فرض نمایش داده می‌شود.", "",
    ]
    rows = []
    for b in buttons:
        on = int(b.id) in selected
        label = (b.label or "بدون عنوان").strip()[:48]
        lines.append(f"{'✅' if on else '⬜️'} {label}")
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if on else '⬜️'} {label}", callback_data=f"wrb:toggle:{b.id}",
            style=ButtonStyle.SUCCESS if on else ButtonStyle.DANGER)])
    if not buttons:
        lines.append("⚠️ هنوز هیچ دکمه‌ای در صفحهٔ اصلی این ربات ثبت نشده است.")
    rows.append([
        InlineKeyboardButton(text="✅ ذخیره", callback_data="wrb:save", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="🔄 پیش‌فرض", callback_data="wrb:reset", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")

async def wallet_receipt_buttons_menu(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if row:
        await _render(cb, bot, row.id)
        await cb.answer()

@wallet_receipt_buttons_router.callback_query(F.data == "set:wallet_receipt_buttons")
async def open_wallet_receipt_buttons(cb: CallbackQuery, bot: Bot):
    await wallet_receipt_buttons_menu(cb, bot)

@wallet_receipt_buttons_router.callback_query(F.data.startswith("wrb:toggle:"))
async def toggle_wallet_receipt_button(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    bid = int(cb.data.rsplit(":", 1)[1])
    buttons = await _available(row.id)
    if bid not in {int(b.id) for b in buttons}:
        await cb.answer("این دکمه دیگر در صفحهٔ اصلی وجود ندارد.", show_alert=True)
        return
    selected = await _selected_ids(row.id)
    if not selected:
        selected = await _effective_ids(row.id, buttons)
    if bid in selected:
        selected.remove(bid)
    else:
        selected.add(bid)
    await set_setting(row.id, SETTING_KEY, json.dumps(sorted(selected)))
    await _render(cb, bot, row.id)
    await cb.answer("انتخاب به‌روزرسانی شد.")

@wallet_receipt_buttons_router.callback_query(F.data == "wrb:reset")
async def reset_wallet_receipt_buttons(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if row:
        await set_setting(row.id, SETTING_KEY, "")
        await _render(cb, bot, row.id)
        await cb.answer("حالت پیش‌فرض فعال شد.")

@wallet_receipt_buttons_router.callback_query(F.data == "wrb:save")
async def save_wallet_receipt_buttons(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    if await _owner_ok(cb, bot):
        await cb.answer("✅ تنظیمات ذخیره است.")

@wallet_receipt_buttons_router.callback_query(F.data.startswith("wrb:main:"))
async def run_wallet_receipt_button(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _exec_main_button, get_bot_row, get_or_create_user
    row = await get_bot_row(bot)
    if not row:
        await cb.answer("ربات پیدا نشد.", show_alert=True)
        return
    bid = int(cb.data.rsplit(":", 1)[1])
    async with Session() as s:
        button = await s.scalar(select(MainButton).where(
            MainButton.id == bid, MainButton.bot_id == row.id, MainButton.parent_id.is_(None)))
    if not button:
        await cb.answer("این دکمه دیگر فعال نیست.", show_alert=True)
        return
    user, _ = await get_or_create_user(row.id, cb.from_user.id,
        cb.from_user.full_name, cb.from_user.username or "", return_created=True)
    await _exec_main_button(button, cb.message, bot, state, row, user, row.id, cb=cb)
