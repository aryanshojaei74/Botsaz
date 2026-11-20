"""قیمت‌یاب در گروه‌های ربات اصلی بات‌ساز (PATCH_PRICE_LOOKUP_PLATFORM_GROUP_V1).

مدیر اصلی: پنل ← 🩺 سیستم و نگهداری ← 🛡 محافظ گروه ← «🔎 قیمت‌یاب گروه»: روشن/خاموش، دکمه‌های زیر پیام (همان ویرایشگر
پیشرفتهٔ ربات‌های مشتری: لینک، رنگ، جابه‌جایی، کنار هم)، پیش‌نمایش راهنما.
همهٔ امکانات نسخهٔ ربات‌ها: همهٔ ارزها با نام/نماد/آدرس، طلا و سکه و ارز، آمار کامل، نمودار، «به ریال»، «راهنما قیمت».
پیام ادمین با حالت ناشناس یا به اسم کانال (sender_chat) هم جواب می‌گیرد، چون پاسخ به خود پیام ریپلای می‌شود.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from config import PLATFORM_ADMIN_ID
from logging_setup import get_logger as _get_logger
from premium_emoji import hx_button
from tenant.shared import price_buttons as pb

logger = _get_logger(__name__)
platform_price_group_router = Router(name="platform_price_group")

ON_KEY = "platform_pq_on"
BTN_KEY = "platform_pq_buttons"


async def _gp(k: str, d: str = "") -> str:
    from platform_bot import get_pset
    return await get_pset(k, d)


async def _sp(k: str, v: str) -> None:
    from platform_bot import set_pset
    await set_pset(k, v)


async def is_on() -> bool:
    return (await _gp(ON_KEY, "off")) == "on"


class PlatformStore(pb.Store):
    async def get(self) -> str:
        return await _gp(BTN_KEY, "")

    async def set(self, raw: str) -> None:
        await _sp(BTN_KEY, raw)


async def maybe_answer(msg: Message, bot: Bot) -> bool:
    """از هندلر گروه (platform_group_guard) صدا زده می‌شود. True ⇒ جواب داده شد."""
    if not (msg.text or "").strip():
        return False
    from tenant.shared import price_lookup as pl
    if not pl.looks_like(msg.text) or not await is_on():
        return False
    body, rial = pl.split_unit(msg.text)
    q = None if pl.is_help(body) else pl.parse_query(body)
    try:
        return await pl.answer_core(msg, bot, 0, q, rial, pb.keyboard(pb.parse(await PlatformStore().get())))
    except Exception:  # noqa: BLE001
        logger.warning("platform price lookup failed", exc_info=True)
        return False


def _admin(ev) -> bool:
    return ev.from_user.id == PLATFORM_ADMIN_ID


async def menu_content():
    on = await is_on()
    n = sum(len(r) for r in pb.parse(await PlatformStore().get()))
    text = ("🔎 <b>قیمت‌یاب گروه بات‌ساز</b>\n\n"
            "وقتی روشن باشد، در هر گروهی که ربات اصلی عضو است (Group Privacy خاموش یا ربات ادمین) اعضا می‌نویسند «تون»، "
            "«5 تون»، «btc»، «قیمت pepe»، آدرس قرارداد، «2 گرم طلا»، «5 دلار»، «سکه امامی» و ربات قیمت دلاری و تومانی، "
            "آمار 24 ساعته و تصویر نمودار را ریپلای می‌کند. «به ریال» در آخر پیام ⇒ مبلغ ریالی. «راهنما قیمت» ⇒ راهنما.\n"
            "پیام شما با حالت ناشناس یا به اسم کانال هم جواب می‌گیرد.\n\n"
            f"وضعیت: <b>{'✅ روشن' if on else '❌ خاموش'}</b>\n"
            f"دکمه‌های زیر پیام: <b>{n}</b>")
    rows = [[hx_button("🔴 خاموش کردن" if on else "🟢 روشن کردن", callback_data="ppg:toggle",
                       style=ButtonStyle.DANGER if on else ButtonStyle.SUCCESS)],
            [hx_button("🔘 دکمه‌های زیر پیام (افزودن، رنگ، جابه‌جایی)", callback_data="ppb:open:0",
                       style=ButtonStyle.PRIMARY)],
            [hx_button("📖 پیش‌نمایش راهنمای کاربران", callback_data="ppg:help", style=ButtonStyle.PRIMARY)],
            [hx_button("⬅️ بازگشت به محافظ گروه", callback_data="adm:group_guard", style=ButtonStyle.DANGER)]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@platform_price_group_router.callback_query(F.data.in_({"ppg:menu", "ppg:toggle", "ppg:help"}))
async def ppg_cb(cb: CallbackQuery):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    if cb.data == "ppg:help":
        from tenant.shared.price_lookup import HELP_TEXT
        await cb.message.answer(HELP_TEXT, parse_mode="HTML",
                                reply_markup=pb.keyboard(pb.parse(await PlatformStore().get())))
        await cb.answer()
        return
    if cb.data == "ppg:toggle":
        await _sp(ON_KEY, "off" if await is_on() else "on")
    text, kb = await menu_content()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_price_group_router.callback_query(F.data.startswith("ppb:"))
async def ppb_cb(cb: CallbackQuery, state: FSMContext):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    await pb.on_cb(cb, state, PlatformStore(), "ppb", "ppg:menu", False)


@platform_price_group_router.message(StateFilter(pb.PbFSM.text, pb.PbFSM.label, pb.PbFSM.url), F.chat.type == "private")
async def ppb_msg(msg: Message, state: FSMContext):
    if not _admin(msg):
        await state.clear()
        return
    await pb.on_msg(msg, state, PlatformStore(), "ppb", "ppg:menu", False)
