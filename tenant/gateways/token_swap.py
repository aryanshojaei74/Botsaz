"""
token_swap.py — درخواستِ تعویضِ توکنِ ربات (وقتی صاحبِ رباتِ مستأجر می‌خواهد
توکنِ رباتش را عوض کند، مثلاً بعدِ لو رفتنِ توکنِ قدیمی از طریقِ بات‌فادر).

جریان:
  ۱) صاحبِ ربات از پنلِ ادمینِ ربات، درخواستِ تعویضِ توکن می‌دهد (این درخواست
     خودش جای دیگری در tenant/__init__.py ساخته می‌شود، نه اینجا).
  ۲) اینجا (tkswap:start:) توکنِ جدید از بات‌فادر را می‌گیرد و صحتش را با
     get_me() تست می‌کند.
  ۳) درخواست برایِ تاییدِ نهایی به مدیرِ اصلیِ بات‌ساز (platform_bot.py،
     callback «tkswapdecide:») فرستاده می‌شود — تاییدِ نهایی و اعمالِ واقعیِ
     جابه‌جاییِ توکن در platform_bot.py انجام می‌شود، نه اینجا.

این فایل فقط نیمهٔ «سمتِ ربات مستأجر» این قابلیت است.

نکته: قبلاً این جریان از استیتِ AdminFSM.new_token (کلاسِ FSM بزرگِ مشترکِ
tenant/__init__.py) استفاده می‌کرد. بررسی شد که این استیت *فقط* همین‌جا
استفاده می‌شد (هیچ بخشِ دیگری از پروژه بهش وابسته نبود)، برای همین یک
FSM اختصاصیِ کوچک (TokenSwapFSM) اینجا تعریف شده — هم رفتار عیناً یکی است،
هم دیگر نیازی به importِ AdminFSM (که importِ حلقوی می‌ساخت، چون در
سطحِ‌ماژول قابلِ استفاده نیست) نیست.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    ReplyKeyboardRemove,
)

from config import Session, PLATFORM_BOT_TOKEN, PLATFORM_ADMIN_ID
from models import TokenSwapRequest

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

token_swap_router = Router()


class TokenSwapFSM(StatesGroup):
    new_token = State()


@token_swap_router.callback_query(F.data.startswith("tkswap:start:"))
async def tkswap_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    req_id = int(cb.data.split(":")[2])
    async with Session() as s:
        swap = await s.get(TokenSwapRequest, req_id)
    if not swap or swap.old_bot_id != row.id or swap.status != "pending_token":
        await cb.answer("این درخواست دیگر معتبر نیست.", show_alert=True)
        return
    await state.set_state(TokenSwapFSM.new_token)
    await state.update_data(swap_req_id=req_id)
    await cb.message.answer(
        "🔁 توکن جدید ربات (از بات‌فادر) را همین‌جا بفرست.\n"
        "⚠️ تا تایید نهایی مدیریت بات‌ساز، ربات فعلی همچنان فعال می‌ماند.",
        reply_markup=await smart_cancel_kb(bot),
    )
    await cb.answer()


@token_swap_router.message(TokenSwapFSM.new_token, F.text)
async def tkswap_receive_token(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok_msg
    row = await _owner_ok_msg(msg, bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    req_id = data.get("swap_req_id")
    new_token = (msg.text or "").strip()
    if not new_token or ":" not in new_token:
        await msg.answer("توکن نامعتبر است. دوباره توکن صحیح را بفرست یا «❌ لغو» را بزن.")
        return
    try:
        test_bot = Bot(token=new_token)
        try:
            me = await test_bot.get_me()
        finally:
            await test_bot.session.close()
    except Exception:
        await msg.answer("❌ این توکن معتبر نیست یا ربات در دسترس نیست. دوباره بفرست.")
        return
    async with Session() as s:
        swap = await s.get(TokenSwapRequest, req_id) if req_id else None
        if not swap or swap.old_bot_id != row.id or swap.status != "pending_token":
            await state.clear()
            await msg.answer("⚠️ این درخواست دیگر معتبر نیست.", reply_markup=ReplyKeyboardRemove())
            return
        swap.new_token = new_token
        swap.new_username = me.username
        swap.new_tg_id = me.id
        swap.status = "pending_admin"
        await s.commit()
        swap_id = swap.id
    await state.clear()
    await msg.answer(
        "✅ توکن جدید دریافت شد و برای تایید نهایی به مدیریت بات‌ساز ارسال شد.\n"
        "به محض تایید، اطلاعات ربات شما به ربات جدید منتقل و فعال می‌شود.",
        reply_markup=ReplyKeyboardRemove(),
    )
    if PLATFORM_BOT_TOKEN and PLATFORM_ADMIN_ID:
        try:
            # === PATCH_TKSWAP_PARSEMODE_V1 ===
            # بدونِ default=HTML، تگ‌هایِ <b>/<code> عیناً به‌صورتِ متنِ خام
            # برایِ مدیر نمایش داده می‌شدند (بقیهٔ فایل‌ها همین الگو را دارند).
            pb = Bot(token=PLATFORM_BOT_TOKEN,
                     default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ تایید انتقال", callback_data=f"tkswapdecide:{swap_id}:y"),
                    InlineKeyboardButton(text="❌ رد", callback_data=f"tkswapdecide:{swap_id}:n"),
                ]])
                await pb.send_message(
                    PLATFORM_ADMIN_ID,
                    "🔁 <b>درخواست تعویض توکن ربات</b>\n\n"
                    f"ربات قدیم: <code>{row.id}</code> — @{row.username}\n"
                    f"ربات جدید: @{me.username} (<code>{me.id}</code>)\n"
                    f"مالک: <code>{row.owner_id}</code>\n\n"
                    "آیا انتقال کامل اطلاعات به ربات جدید تایید می‌شود؟",
                    reply_markup=kb,
                )
            finally:
                await pb.session.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
