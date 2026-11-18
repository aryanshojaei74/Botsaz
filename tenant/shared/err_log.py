"""PATCH_ERR_LOG_CHANNEL_V1 — «کانالِ لاگِ خطاها و مشکلات» (nitroteeam).

کانالی جدا از «لاگِ تراکنش‌ها» که ربات در آن خطاهایی را که مشتری دیده (ساختِ کانفیگ ناموفق، خریدِ ناتمام، …) با تاریخ و ساعت
پست می‌کند تا مالک بداند چه چیزی کِی خراب شده. کلیدِ تنظیمات: err_log_channel (آیدیِ عددیِ کانال/گروه). خطایِ ارسال هرگز جلوی کارِ
اصلی را نمی‌گیرد.
"""
from __future__ import annotations

import datetime as _dt
from html import escape as _esc

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

SETTING_KEY = "err_log_channel"
err_log_router = Router(name="err_log")


class ErrChFSM(StatesGroup):
    wait = State()


def stamp() -> str:
    """«۱۴۰۵/۰۸/۲۸ — ۰۰:۰۱» به وقتِ تهران (از UTC)."""
    try:
        from tenant import _jalali_date_time
        d, t = _jalali_date_time(_dt.datetime.utcnow())
        return f"{d} — {t}"
    except Exception:
        return (_dt.datetime.utcnow() + _dt.timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d — %H:%M")


async def channel_of(bot_id: int):
    from tenant import get_setting
    ch = ((await get_setting(bot_id, SETTING_KEY, "")) or "").strip()
    if not ch:
        return None
    try:
        return int(ch)
    except ValueError:
        return ch


async def post(bot: Bot, bot_id: int, text: str, kb: InlineKeyboardMarkup | None = None) -> bool:
    """متن را (با تاریخ/ساعت) در کانالِ لاگِ خطاها می‌گذارد؛ True اگر ارسال شد. هر خطا بی‌صدا رد می‌شود."""
    try:
        chat = await channel_of(bot_id)
        if chat is None:
            return False
        await bot.send_message(chat, f"{text}\n\n🕒 {stamp()}", reply_markup=kb, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning("err_log: ارسال به کانالِ لاگِ خطاها ناموفق (bot_id=%s): %s", bot_id, e)
        return False


# ───────────── پنلِ مالک ─────────────
async def _content(bot_id: int):
    from tenant import get_setting
    ch = ((await get_setting(bot_id, SETTING_KEY, "")) or "").strip()
    txt = ("🚨 <b>کانالِ لاگِ خطاها و مشکلات</b>\n\n"
           "هر خطایی که مشتری دیده (مثلاً ساختِ کانفیگ ناموفق بود)، و هر خریدی که ناتمام مانده باشد "
           "(پول کم شده ولی سرویس نرسیده)، با تاریخ و ساعت در این کانال پست می‌شود. جدا از «لاگِ تراکنش‌ها» است.\n\n")
    txt += (f"وضعیت: ✅ متصل به <code>{_esc(ch)}</code>" if ch else
            "وضعیت: ❌ تنظیم نشده\n\nربات را در کانال/گروهِ خودت <b>ادمین</b> کن، بعد یک پیام از آن کانال را همین‌جا فوروارد کن "
            "(یا آیدیِ عددی را بفرست).")
    rows = []
    if ch:
        rows.append([InlineKeyboardButton(text="🧪 ارسالِ پیامِ تست", callback_data="errch:test", style=ButtonStyle.SUCCESS)])
        rows.append([InlineKeyboardButton(text="🗑 حذفِ اتصال", callback_data="errch:clear", style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="🔗 اتصال / تغییرِ کانال", callback_data="errch:set", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="set:txlog", style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(cb_or_msg, bot_id: int):
    txt, kb = await _content(bot_id)
    m = getattr(cb_or_msg, "message", None) or cb_or_msg
    try:
        await m.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(txt, reply_markup=kb, parse_mode="HTML")


@err_log_router.callback_query(F.data == "errch:menu")
async def errch_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row.id)
    await cb.answer()


@err_log_router.callback_query(F.data == "errch:set")
async def errch_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(ErrChFSM.wait)
    await cb.message.answer(
        "یک پیام از کانالِ موردنظر را فوروارد کن، یا آیدیِ عددیِ کانال/گروه (مثلاً <code>-1001234567890</code>) را بفرست.\n\n"
        "⚠️ ربات باید در آن‌جا ادمین (یا دسترسیِ ارسالِ پیام) داشته باشد.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@err_log_router.message(ErrChFSM.wait)
async def errch_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(msg, bot)
    if not row:
        return
    chat_id = None
    fwd = getattr(msg, "forward_from_chat", None)
    if fwd is not None and getattr(fwd, "type", "") in ("channel", "supergroup", "group"):
        chat_id = fwd.id
    else:
        t = (msg.text or "").strip()
        if t.lstrip("-").isdigit():
            chat_id = int(t)
    if chat_id is None:
        return await msg.answer("یک پیام از کانال فوروارد کن یا آیدیِ عددی را بفرست.")
    try:
        await bot.send_message(chat_id, "✅ کانالِ لاگِ خطاها و مشکلات وصل شد.")
    except Exception as e:
        return await msg.answer("❌ ارسال به این کانال ممکن نشد. مطمئن شو ربات آن‌جا <b>ادمین</b> است.\n\n"
                                f"<code>{_esc(str(e))}</code>", parse_mode="HTML")
    await set_setting(row.id, SETTING_KEY, str(chat_id))
    await state.clear()
    await msg.answer("✅ ذخیره شد.")
    await _show(msg, row.id)


@err_log_router.callback_query(F.data == "errch:test")
async def errch_test(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    ok = await post(bot, row.id, "🧪 <b>پیامِ تست</b>\nاگر این را می‌بینی، لاگِ خطاها درست وصل است.")
    await cb.answer("پیامِ تست ارسال شد." if ok else "ارسال ناموفق بود؛ دسترسیِ ربات در کانال را بررسی کن.", show_alert=True)


@err_log_router.callback_query(F.data == "errch:clear")
async def errch_clear(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await set_setting(row.id, SETTING_KEY, "")
    await _show(cb, row.id)
    await cb.answer("اتصال حذف شد.")
