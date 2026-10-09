"""
trial_report_admin.py — PATCH_TRIAL_REPORT_CHANNEL_V1
اتصالِ «کانالِ گزارشِ اکانتِ تست» (کلید: trial_report_channel) برایِ مالکِ ربات.
"""
from __future__ import annotations

from html import escape as _esc

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

trial_report_admin_router = Router()


class TrRepFSM(StatesGroup):
    wait = State()


async def _content(bot_id: int):
    from tenant import get_setting
    ch = await get_setting(bot_id, "trial_report_channel", "")
    show = (await get_setting(bot_id, "trial_report_link", "1")) != "0"
    txt = ("🔑 <b>گزارشِ اکانتِ تست</b>\n\n"
           "هر بار کسی تستِ رایگان بگیرد، این مشخصات در کانال/گروهِ شما پست می‌شود: "
           "آیدی و یوزرنیمِ کاربر، حجم، مدت، نامِ پنل و (اختیاری) لینکِ سرویس.\n\n")
    if ch:
        txt += f"وضعیت: ✅ متصل به <code>{_esc(str(ch))}</code>"
    else:
        txt += ("وضعیت: ❌ تنظیم نشده\n\n"
                "ربات را در کانال/گروهِ خودت <b>ادمین</b> کن، بعد یک پیام از آن کانال را "
                "این‌جا فوروارد کن (یا آیدیِ عددیِ کانال/گروه را بفرست).")
    rows = []
    if ch:
        rows.append([InlineKeyboardButton(text="🧪 ارسالِ گزارشِ نمونه", callback_data="trrep:test",
                                          style=ButtonStyle.SUCCESS)])
        rows.append([InlineKeyboardButton(
            text=f"🔗 نمایشِ لینکِ سرویس در گزارش: {'✅' if show else '❌'}",
            callback_data="trrep:link", style=ButtonStyle.PRIMARY)])
        rows.append([InlineKeyboardButton(text="🗑 حذفِ اتصال", callback_data="trrep:clear",
                                          style=ButtonStyle.DANGER)])
    rows.append([InlineKeyboardButton(text="🔗 اتصال / تغییرِ کانال", callback_data="trrep:set",
                                      style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance",
                                      style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _gate(cb_or_msg, bot: Bot):
    from tenant import _owner_ok, _owner_ok_msg
    from plans import has_feature, Feature
    row = await (_owner_ok(cb_or_msg, bot) if isinstance(cb_or_msg, CallbackQuery)
                 else _owner_ok_msg(cb_or_msg, bot))
    if not row:
        return None
    if getattr(row, "field", None) != "vpn_sales":
        if isinstance(cb_or_msg, CallbackQuery):
            await cb_or_msg.answer("این قابلیت فقط برایِ رباتِ فروشِ VPN است.", show_alert=True)
        return None
    if not has_feature(row.plan, Feature.TRIAL_REPORT_CHANNEL):
        if isinstance(cb_or_msg, CallbackQuery):
            await cb_or_msg.answer("این قابلیت از پلن «رشد» به بالا فعال است.", show_alert=True)
        return None
    return row


async def _show(target, txt, kb):
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(txt, reply_markup=kb, parse_mode="HTML")


@trial_report_admin_router.callback_query(F.data == "set:trrep")
async def trrep_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _gate(cb, bot)
    if not row:
        return
    await state.clear()
    txt, kb = await _content(row.id)
    await _show(cb, txt, kb)
    await cb.answer()


@trial_report_admin_router.callback_query(F.data == "trrep:set")
async def trrep_set(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import smart_cancel_kb
    row = await _gate(cb, bot)
    if not row:
        return
    await state.set_state(TrRepFSM.wait)
    await cb.message.answer(
        "یک پیام از کانال/گروهِ موردنظر را فوروارد کن، یا آیدیِ عددیِ آن "
        "(مثلاً <code>-1001234567890</code>) را بفرست.\n\n"
        "⚠️ ربات باید در آن‌جا ادمین باشد.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@trial_report_admin_router.message(TrRepFSM.wait)
async def trrep_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _gate(msg, bot)
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
        return await msg.answer("یک پیام از کانال فوروارد کن یا آیدیِ عددیِ کانال/گروه را بفرست.")
    try:
        m = await bot.send_message(chat_id, "✅ کانالِ گزارشِ اکانتِ تست وصل شد.")
        try:
            await bot.unpin_chat_message(chat_id, message_id=m.message_id)
        except Exception:
            logger.debug("trial_report: unpin لازم نبود")
    except Exception as e:
        return await msg.answer(
            "❌ ارسال به این کانال ممکن نشد. مطمئن شو ربات آن‌جا <b>ادمین</b> است.\n\n"
            f"<code>{_esc(str(e))}</code>", parse_mode="HTML")
    await set_setting(row.id, "trial_report_channel", str(chat_id))
    await state.clear()
    txt, kb = await _content(row.id)
    await msg.answer("✅ ذخیره شد.")
    await msg.answer(txt, reply_markup=kb, parse_mode="HTML")


@trial_report_admin_router.callback_query(F.data == "trrep:link")
async def trrep_link(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting, set_setting
    row = await _gate(cb, bot)
    if not row:
        return
    cur = (await get_setting(row.id, "trial_report_link", "1")) != "0"
    await set_setting(row.id, "trial_report_link", "0" if cur else "1")
    txt, kb = await _content(row.id)
    await _show(cb, txt, kb)
    await cb.answer("بدون لینک" if cur else "با لینک")


@trial_report_admin_router.callback_query(F.data == "trrep:test")
async def trrep_test(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting
    from tenant.shared.trial_report import report
    row = await _gate(cb, bot)
    if not row:
        return
    if not await get_setting(row.id, "trial_report_channel", ""):
        return await cb.answer("کانالی تنظیم نشده.", show_alert=True)

    class _U:
        telegram_id = cb.from_user.id
        full_name = cb.from_user.full_name
        username = cb.from_user.username
    await report(bot, row.id, _U(), link="https://example.com/sub/SAMPLE", volume="0.1 گیگ",
                 duration="1 ساعت", panel_name="پنل نمونه")
    await cb.answer("گزارشِ نمونه ارسال شد.", show_alert=True)


@trial_report_admin_router.callback_query(F.data == "trrep:clear")
async def trrep_clear(cb: CallbackQuery, bot: Bot):
    from tenant import set_setting
    row = await _gate(cb, bot)
    if not row:
        return
    await set_setting(row.id, "trial_report_channel", "")
    txt, kb = await _content(row.id)
    await _show(cb, txt, kb)
    await cb.answer("اتصال حذف شد.")
