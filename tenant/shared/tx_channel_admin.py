"""
tx_channel_admin.py — PATCH_TX_LOG_CHANNEL_V1

تنظیمِ «کانالِ لاگِ تراکنش‌ها» برایِ هر رباتِ مستأجر.
کلیدِ تنظیمات: tx_log_channel
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

tx_channel_admin_router = Router()


class TxChFSM(StatesGroup):
    wait = State()


async def _content(bot_id: int):
    from tenant import get_setting
    ch = await get_setting(bot_id, "tx_log_channel", "")
    txt = ("📊 <b>کانالِ لاگِ تراکنش‌ها</b>\n\n"
           "هر شارژِ کیفِ پول — از هر درگاهی — با مشخصاتِ کامل در این کانال "
           "پست می‌شود: نامِ کاربر، آیدی، مبلغ، موجودیِ قبل و بعد، و کدِ پیگیری.\n\n")
    if ch:
        txt += f"وضعیت: ✅ متصل به <code>{ch}</code>"
    else:
        txt += ("وضعیت: ❌ تنظیم نشده\n\n"
                "ربات را در کانالِ خودت <b>ادمین</b> کن، بعد یک پیام از آن کانال "
                "را این‌جا فوروارد کن (یا آیدیِ عددیِ کانال را بفرست).")
    rows = []
    if ch:
        rows.append([InlineKeyboardButton(text="🧪 ارسالِ پیامِ تست",
                                          callback_data="txch:test",
                                          style=ButtonStyle.SUCCESS)])
        rows.append([InlineKeyboardButton(text="🗑 حذفِ اتصال",
                                          callback_data="txch:clear",
                                          style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="🔗 اتصال / تغییرِ کانال",
                                      callback_data="txch:set",
                                      style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت",
                                      callback_data="tadm:sec:finance",
                                      style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


@tx_channel_admin_router.callback_query(F.data == "set:txlog")
async def txch_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    txt, kb = await _content(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@tx_channel_admin_router.callback_query(F.data == "txch:set")
async def txch_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(TxChFSM.wait)
    await cb.message.answer(
        "یک پیام از کانالِ موردنظر را فوروارد کن، یا آیدیِ عددیِ کانال "
        "(مثلاً <code>-1001234567890</code>) را بفرست.\n\n"
        "⚠️ ربات باید در آن کانال ادمین باشد.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@tx_channel_admin_router.message(TxChFSM.wait)
async def txch_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(msg, bot)
    if not row:
        return

    chat_id = None
    fwd = getattr(msg, "forward_from_chat", None)
    if fwd is not None and getattr(fwd, "type", "") == "channel":
        chat_id = fwd.id
    else:
        t = (msg.text or "").strip()
        if t.lstrip("-").isdigit():
            chat_id = int(t)

    if chat_id is None:
        await msg.answer("یک پیام از کانال فوروارد کن یا آیدیِ عددیِ کانال را بفرست.")
        return

    try:
        m = await bot.send_message(chat_id, "✅ کانالِ لاگِ تراکنش‌ها وصل شد.")
        try:
            await bot.unpin_chat_message(chat_id, message_id=m.message_id)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    except Exception as e:
        await msg.answer(
            "❌ ارسال به این کانال ممکن نشد. مطمئن شو ربات در کانال "
            f"<b>ادمین</b> است.\n\n<code>{e}</code>", parse_mode="HTML")
        return

    await set_setting(row.id, "tx_log_channel", str(chat_id))
    await state.clear()
    txt, kb = await _content(row.id)
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(txt, reply_markup=kb, parse_mode="HTML")


@tx_channel_admin_router.callback_query(F.data == "txch:test")
async def txch_test(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    ch = await get_setting(row.id, "tx_log_channel", "")
    if not ch:
        return await cb.answer("کانالی تنظیم نشده.", show_alert=True)
    from tenant.shared.tx_channel import tx_log

    class _Fake:
        full_name = "کاربرِ نمونه"
        username = "example"
        telegram_id = cb.from_user.id

    await tx_log(bot, row.id, buyer=_Fake(), amount=150000,
                 gateway="zarinpal", old_balance=50000, new_balance=200000,
                 ref="TEST-123456")
    await cb.answer("پیامِ تست ارسال شد.", show_alert=True)


@tx_channel_admin_router.callback_query(F.data == "txch:clear")
async def txch_clear(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await set_setting(row.id, "tx_log_channel", "")
    txt, kb = await _content(row.id)
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")
    await cb.answer("اتصال حذف شد.")
