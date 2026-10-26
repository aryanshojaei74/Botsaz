"""platform_halloween.py — دکمهٔ مدیرِ بات‌ساز برایِ «🎃 تم هالوین» (روشن/خاموشِ همهٔ ربات‌ها)."""
from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from config import PLATFORM_ADMIN_ID
from premium_emoji import hx_button

import halloween as HW

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_halloween_router = Router(name="platform_halloween")


async def _panel() -> tuple[str, InlineKeyboardMarkup]:
    on = await HW.is_on()
    until = (await HW._pget(HW.KEY_UNTIL)).strip() if on else ""
    st = await HW.stats()
    text = ("🎃 <b>تم هالوین</b>\n\n"
            f"وضعیت: {'🟢 روشن (برایِ همهٔ ربات‌ها)' if on else '⚪️ خاموش'}\n"
            + (f"⏳ خاموشیِ خودکار: <code>{until[:16].replace('T', ' ')}</code> (UTC)\n" if until else "")
            + f"\n🎮 تاکنون: {st['players']} بازیکن در {st['bots']} ربات · {st['plays']} بار بازی\n\n"
            "با روشن‌کردن:\n"
            "• مینی‌اپِ رباتی که مالکش رنگ نگذاشته، بنفشِ هالوینی می‌شود.\n"
            "• بعد از /start یک پیامِ «🎃 Trick or Treat» می‌آید؛ هر کاربر روزی یک بار شیرینی 🍬 جمع می‌کند و جدولِ ۱۰ نفرِ برتر دارد.\n"
            "• جایزه‌ها شیرینیِ سرگرمی است، نه پول؛ پس هزینه‌ای برایِ مالکِ ربات ندارد.\n"
            f"• بعد از {HW.DEFAULT_DAYS} روز خودبه‌خود خاموش می‌شود؛ هر وقت هم دستی خاموشش کنی همه‌چیز فوراً برمی‌گردد.")
    rows = [[hx_button("⚪️ خاموش‌کردن" if on else "🟢 روشن‌کردن برایِ همهٔ ربات‌ها",
                       callback_data="adm:halloween:off" if on else "adm:halloween:on",
                       style=ButtonStyle.DANGER if on else ButtonStyle.SUCCESS)],
            [hx_button("⬅️ بازگشت", callback_data="adm:back", style=ButtonStyle.DANGER)]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@platform_halloween_router.callback_query(F.data.regexp(r"^adm:halloween(:(on|off))?$"))
async def adm_halloween(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        return await cb.answer("فقط مدیر اصلی", show_alert=True)
    parts = cb.data.split(":")
    if len(parts) == 3:
        await HW.set_on(parts[2] == "on", now=datetime.utcnow())
    text, kb = await _panel()
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer("🎃 روشن شد" if len(parts) == 3 and parts[2] == "on" else
                    ("⚪️ خاموش شد" if len(parts) == 3 else ""))
