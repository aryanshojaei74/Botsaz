"""
dice_texts_admin.py — PATCH_DICE_CUSTOM_TEXTS_V1
پنلِ ویرایشِ متن‌هایِ بازیِ «بازی کن و جایزه بگیر».
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from tenant.shared.dice_texts import TEXTS

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

dice_texts_router = Router()

_VARS = (
    "<code>{name}</code> نامِ بازیکن · <code>{game}</code> نامِ بازی\n"
    "<code>{emoji}</code> اموجی · <code>{value}</code> نتیجهٔ تاس\n"
    "<code>{prize}</code> مبلغِ جایزه · <code>{left}</code> سهمیهٔ باقی‌مانده\n"
    "<code>{limit}</code> کلِ سهمیه · <code>{period}</code> بازهٔ سهمیه\n"
    # === PATCH_DICE_QUOTA_COUNTDOWN_V1 ===
    "\n<b>فقط در متنِ «پایانِ سهمیه»:</b>\n"
    "<code>{reset_in}</code> فاصله تا تجدیدِ سهمیه · "
    "<code>{reset_at}</code> ساعتِ تجدید\n"
    "<code>{reset_line}</code> جملهٔ آمادهٔ هر دو"
)


class DTxtFSM(StatesGroup):
    edit = State()


async def _menu(bot_id: int):
    from tenant import get_setting
    rows = []
    for key, (label, _d) in TEXTS.items():
        custom = (await get_setting(bot_id, f"dice_txt_{key}", "") or "").strip()
        mark = "✏️" if custom else "▫️"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {label}", callback_data=f"dgtxt:e:{key}")])
    rows.append([InlineKeyboardButton(
        text="♻️ بازگرداندنِ همه به پیش‌فرض",
        callback_data="dgtxt:resetall", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(
        text="⬅️ بازگشت", callback_data="dga:menu", style=ButtonStyle.DANGER)])
    txt = ("✍️ <b>متن‌هایِ بازی</b>\n\n"
           "این‌ها پیام‌هایی هستند که بعد از هر نوبتِ بازی — چه در چتِ "
           "خصوصی و چه در گروه — به بازیکن نشان داده می‌شوند.\n\n"
           "<b>متغیرهایِ قابلِ استفاده:</b>\n" + _VARS + "\n\n"
           "✏️ = متنِ دلخواهِ تو · ▫️ = متنِ پیش‌فرض")
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target, bot_id):
    txt, kb = await _menu(bot_id)
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(txt, reply_markup=kb, parse_mode="HTML")


@dice_texts_router.callback_query(F.data == "dga:texts")
async def dgtxt_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row.id)
    await cb.answer()


@dice_texts_router.callback_query(F.data.startswith("dgtxt:e:"))
async def dgtxt_edit(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    from tenant.shared.dice_texts import get_text
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    if key not in TEXTS:
        return await cb.answer()
    label, _d = TEXTS[key]
    from tenant import get_setting
    cur = (await get_setting(row.id, f"dice_txt_{key}", "") or "").strip() or _d
    await state.update_data(dgtxt_key=key)
    await state.set_state(DTxtFSM.edit)
    await cb.message.answer(
        f"✍️ <b>{label}</b>\n\n"
        f"<b>متنِ فعلی:</b>\n<code>{cur}</code>\n\n"
        "<b>متغیرها:</b>\n" + _VARS + "\n\n"
        "متنِ تازه را بفرست.\n"
        "برایِ برگشت به پیش‌فرض، کلمهٔ <code>پیشفرض</code> را بفرست.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@dice_texts_router.message(DTxtFSM.edit, F.text)
async def dgtxt_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(msg, bot)
    data = await state.get_data()
    key = data.get("dgtxt_key")
    await state.clear()
    if not row or key not in TEXTS:
        return
    t = (msg.text or "").strip()
    if t in ("پیشفرض", "پیش‌فرض", "default"):
        await set_setting(row.id, f"dice_txt_{key}", "")
        await msg.answer("♻️ به متنِ پیش‌فرض برگشت.")
    else:
        await set_setting(row.id, f"dice_txt_{key}", t[:1000])
        await msg.answer("✅ ذخیره شد.")
    await _show(msg, row.id)


@dice_texts_router.callback_query(F.data == "dgtxt:resetall")
async def dgtxt_reset(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    for key in TEXTS:
        await set_setting(row.id, f"dice_txt_{key}", "")
    await cb.answer("همه به پیش‌فرض برگشتند.", show_alert=True)
    await _show(cb, row.id)
