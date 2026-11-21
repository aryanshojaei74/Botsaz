"""
uniquepay_admin.py — PATCH_UNIQUEPAY_V1

پنلِ تنظیماتِ درگاهِ یونیک‌پی برایِ مالکِ ربات + جریانِ شارژِ کاربر.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import select

from config import Session
from models import BotUser, UniquePayCharge
from tenant.gateways import uniquepay as UP

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

uniquepay_router = Router()


class UpFSM(StatesGroup):
    token = State()
    amount = State()
    min_amount = State()
    max_amount = State()


# ═════════════════ پنلِ مالکِ ربات ═════════════════
async def _menu_content(bot_id: int):
    tok = await UP._get(bot_id, "uniquepay_token", "")
    on = await UP._get(bot_id, "uniquepay_enabled", "0") == "1"
    show_wallet = await UP._get(bot_id, "uniquepay_show_wallet", "1") == "1"
    show_purchase = await UP._get(bot_id, "uniquepay_show_purchase", "1") == "1"
    min_amount = await UP.get_min_amount(bot_id)
    max_amount = await UP.get_max_amount(bot_id)
    masked = (tok[:4] + "…" + tok[-4:]) if tok and len(tok) > 10 else (tok or "—")

    txt = ("💳 <b>درگاهِ یونیک‌پی</b>\n\n"
           "کارت‌به‌کارتِ خودکار: کاربر روی لینکِ پرداخت می‌رود، کارت‌به‌کارت "
           "می‌کند، و کیفِ پولش خودکار شارژ می‌شود.\n\n"
           f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n"
           f"🔑 توکن: <code>{masked}</code>\n"
           f"🔽 حداقلِ مبلغ: <b>{min_amount:,}</b> تومان\n"
           f"🔼 حداکثرِ مبلغ: <b>{(f'{max_amount:,}' if max_amount else 'بدونِ سقف')}</b>\n"
           f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}\n"
           f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}\n\n"
           "همه‌جایِ ربات (نمایش، ورودی، شارژ) فقط تومان است؛ تبدیلِ واحد "
           "برایِ ارسال به درگاه کاملاً داخلی و خودکار انجام می‌شود.\n"
           "⚠️ اگر حسابِ بیزینسِ شما روی uniquepay.top خودش یک حداقلِ فاکتورِ "
           "بالاتر تنظیم کرده، حداقلِ اینجا را هم مطابقش بالا ببر.\n"
           "⚠️ <b>نکتهٔ مهم:</b> خودِ سایتِ uniquepay.top رویِ حسابِ بیزینس "
           "حداقلِ ۵۰,۰۰۰ تومان گذاشته، ولی این حداقل را به‌صورتِ «بزرگ‌تر "
           "از ۵۰,۰۰۰» چک می‌کند، نه «بزرگ‌تر یا مساوی». یعنی اگر حداقلِ "
           "اینجا را دقیقاً رویِ ۵۰,۰۰۰ بگذاری، فاکتورهایِ کاربر با پیامِ "
           "خطایِ «minimum amount is 50,000» رد می‌شوند. برایِ همین باید "
           "حداقل را حتماً رویِ عددی <b>بالاترِ ۵۰,۰۰۰</b> (پیش‌فرض و "
           "پیشنهادِ ما: ۵۱,۰۰۰) تنظیم کنی تا فاکتورها بدونِ مشکل ساخته "
           "شوند.")

    rows = [
        [InlineKeyboardButton(
            text=("⛔️ غیرفعال‌کردن" if on else "✅ فعال‌کردن"),
            callback_data="up:toggle",
            style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="🔑 ثبت / تغییرِ توکن",
                              callback_data="up:setkey", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🔽 حداقلِ مبلغ: {min_amount:,}",
                              callback_data="up:setmin", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text=f"🔼 حداکثر: {max_amount:,}" if max_amount else "🔼 حداکثر: بدونِ سقف",
                              callback_data="up:setmax", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(
            text=f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}",
            callback_data="up:show_wallet",
            style=(ButtonStyle.SUCCESS if show_wallet else ButtonStyle.DANGER))],
        [InlineKeyboardButton(
            text=f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}",
            callback_data="up:show_purchase",
            style=(ButtonStyle.SUCCESS if show_purchase else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text="🧪 تستِ اتصال",
                              callback_data="up:test", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⬅️ بازگشت",
                              callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ]
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target, bot_id):
    txt, kb = await _menu_content(bot_id)
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(txt, reply_markup=kb, parse_mode="HTML")


@uniquepay_router.callback_query(F.data.in_({"set:uniquepay", "up:menu"}))
async def up_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row.id)
    await cb.answer()


@uniquepay_router.callback_query(F.data == "up:toggle")
async def up_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await UP._get(row.id, "uniquepay_enabled", "0") == "1"
    if not cur and not await UP._get(row.id, "uniquepay_token", ""):
        return await cb.answer("اول توکنِ درگاه را ثبت کن.", show_alert=True)
    await UP._set(row.id, "uniquepay_enabled", "0" if cur else "1")
    await cb.answer("خاموش شد." if cur else "فعال شد.")
    await _show(cb, row.id)


@uniquepay_router.callback_query(F.data == "up:show_wallet")
async def up_show_wallet(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await UP._get(row.id, "uniquepay_show_wallet", "1") == "1"
    await UP._set(row.id, "uniquepay_show_wallet", "0" if cur else "1")
    await cb.answer("در کیف پول: " + ("پنهان شد" if cur else "نمایش داده می‌شود"))
    await _show(cb, row.id)


@uniquepay_router.callback_query(F.data == "up:show_purchase")
async def up_show_purchase(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await UP._get(row.id, "uniquepay_show_purchase", "1") == "1"
    await UP._set(row.id, "uniquepay_show_purchase", "0" if cur else "1")
    await cb.answer("موقعِ پرداختِ محصول: " + ("پنهان شد" if cur else "نمایش داده می‌شود"))
    await _show(cb, row.id)


@uniquepay_router.callback_query(F.data == "up:setmin")
async def up_setmin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await UP.get_min_amount(row.id)
    await state.set_state(UpFSM.min_amount)
    await cb.message.answer(
        f"🔽 حداقلِ مبلغِ شارژ را به تومان بفرست (فعلاً: <b>{cur:,}</b>).\n\n"
        "اگر حسابِ بیزینسِ شما روی خودِ uniquepay.top حداقلِ فاکتورِ بالاتری "
        "دارد، همین عدد را با آن هماهنگ کن، وگرنه تستِ اتصال و شارژها با "
        "خطایِ «کمتر از حداقل» رد می‌شوند.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@uniquepay_router.message(UpFSM.min_amount, F.text)
async def up_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if not n or n <= 0:
        return await msg.answer("عددِ نامعتبر. دوباره امتحان کن.")
    await UP._set(row.id, "uniquepay_min_amount", str(int(n)))
    await msg.answer(f"✅ حداقلِ مبلغ روی {int(n):,} تومان تنظیم شد.")
    await _show(msg, row.id)


@uniquepay_router.callback_query(F.data == "up:setmax")
async def up_setmax(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await UP.get_max_amount(row.id)
    await state.set_state(UpFSM.max_amount)
    await cb.message.answer(
        f"🔼 حداکثرِ مبلغِ شارژ را به تومان بفرست (فعلاً: "
        f"<b>{(f'{cur:,}' if cur else 'بدونِ سقف')}</b>).\n\n"
        "برای برداشتنِ سقف، عددِ ۰ بفرست.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@uniquepay_router.message(UpFSM.max_amount, F.text)
async def up_setmax_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if n is None or n < 0:
        return await msg.answer("عددِ نامعتبر. دوباره امتحان کن.")
    await UP._set(row.id, "uniquepay_max_amount", str(int(n)))
    await msg.answer(
        f"✅ حداکثرِ مبلغ {'برداشته شد (بدونِ سقف).' if int(n) == 0 else f'روی {int(n):,} تومان تنظیم شد.'}")
    await _show(msg, row.id)


@uniquepay_router.callback_query(F.data == "up:setkey")
async def up_setkey(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(UpFSM.token)
    await cb.message.answer(
        "🔑 <b>Business Token</b> را از پنلِ یونیک‌پی کپی کن و این‌جا بفرست.\n\n"
        "این توکن فقط روی سرورِ ما نگه‌داری می‌شود.",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@uniquepay_router.message(UpFSM.token, F.text)
async def up_setkey_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    tok = (msg.text or "").strip()
    if len(tok) < 8:
        return await msg.answer("توکن خیلی کوتاه است. دوباره امتحان کن.")
    await msg.answer("⏳ در حالِ تستِ توکن…")
    ok, why = await UP.validate_token(row.id, tok, row.username or "")
    if not ok:
        return await msg.answer(f"❌ {why}")
    await UP._set(row.id, "uniquepay_token", tok)
    await msg.answer("✅ توکن ذخیره و تأیید شد.")
    await _show(msg, row.id)


@uniquepay_router.callback_query(F.data == "up:test")
async def up_test(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    tok = await UP._get(row.id, "uniquepay_token", "")
    if not tok:
        return await cb.answer("اول توکن را ثبت کن.", show_alert=True)
    await cb.answer("در حالِ تست…")
    ok, why = await UP.validate_token(row.id, tok, row.username or "")
    await cb.message.answer(("✅ " if ok else "❌ ") + why)


# ═════════════════ جریانِ کاربر ═════════════════
@uniquepay_router.callback_query(F.data.in_({"upu:start", "w:uniquepay"}))
async def upu_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if await UP._get(row.id, "uniquepay_enabled", "0") != "1":
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    min_amount = await UP.get_min_amount(row.id)
    max_amount = await UP.get_max_amount(row.id)
    rng = f"حداقل: <b>{min_amount:,}</b> تومان"
    if max_amount:
        rng += f" | حداکثر: <b>{max_amount:,}</b> تومان"
    await state.set_state(UpFSM.amount)
    await cb.message.answer(
        f"💳 چه مبلغی می‌خواهی شارژ کنی؟ (تومان)\n\n{rng}",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@uniquepay_router.message(UpFSM.amount, F.text)
async def upu_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import fa_to_int, get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    amount = fa_to_int(msg.text)
    min_amount = await UP.get_min_amount(row.id)
    max_amount = await UP.get_max_amount(row.id)
    if not amount or amount < min_amount:
        return await msg.answer(
            f"حداقلِ مبلغ {min_amount:,} تومان است. دوباره بفرست.")
    if max_amount and amount > max_amount:
        return await msg.answer(
            f"حداکثرِ مبلغ {max_amount:,} تومان است. دوباره بفرست.")
    await state.clear()

    # سقفِ احرازِ هویت
    try:
        from tenant.shared.kyc import check_charge
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == row.id,
                BotUser.telegram_id == msg.from_user.id))
        ok, why = await check_charge(row.id, u, amount)
        if not ok:
            return await msg.answer(why)
    except Exception:
        logger.exception("uniquepay: بررسیِ سقف ناموفق بود")

    token = await UP._get(row.id, "uniquepay_token", "")
    if not token:
        return await msg.answer("درگاه تنظیم نشده است.")

    hid = UP.new_hash_id()
    await msg.answer("⏳ در حالِ ساختِ فاکتور…")
    out = await UP._amount_out(row.id, amount)
    res = await UP.create_invoice_safe(row.id, token, hid, out,
                                       row.username or "")
    if not res.get("status") or res.get("code") != 200:
        return await msg.answer(
            "❌ ساختِ فاکتور ناموفق بود.\n" + str(res.get("message") or ""))

    link = res.get("paymentLink") or ""
    async with Session() as s:
        s.add(UniquePayCharge(
            bot_id=row.id, user_telegram_id=msg.from_user.id,
            hash_id=hid, amount_toman=int(amount), status="waiting"))
        await s.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 پرداخت", url=link)],
        [InlineKeyboardButton(text="🔍 بررسیِ پرداخت",
                              callback_data=f"upu:check:{hid}",
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="❌ لغو و بازگشت",
                              callback_data=f"upu:cancel:{hid}",
                              style=ButtonStyle.DANGER)],
    ])
    await msg.answer(
        "🧾 <b>فاکتورِ شارژِ کیف پول</b>\n"
        "➖➖➖➖➖➖➖➖➖➖\n\n"
        f"💵 مبلغ: <b>{amount:,}</b> تومان\n"
        f"🔑 کدِ پیگیری: <code>{hid}</code>\n\n"
        "📋 <b>مراحل:</b>\n"
        "۱. روی «💳 پرداخت» بزن و کارت‌به‌کارت کن.\n"
        "۲. برگرد اینجا و «🔍 بررسیِ پرداخت» را بزن.\n"
        "۳. کیفِ پولت فوراً شارژ می‌شود. ✅\n\n"
        "🔒 پرداخت از طریقِ درگاهِ امنِ uniquepay.top انجام می‌شود.\n"
        "⏳ اگه منصرف شدی، «❌ لغو و بازگشت» رو بزن.",
        reply_markup=kb, parse_mode="HTML")


@uniquepay_router.callback_query(F.data.startswith("upu:check:"))
async def upu_check(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    hid = cb.data.split(":", 2)[2]
    # PATCH_GATEWAY_INPLACE_V1: نتیجه بدونِ پیامِ جدید (اعلانِ بالا یا ویرایشِ همان فاکتور)
    from tenant.gateways import inplace as IP
    ok, msg_, credit = await UP.settle_charge(row.id, hid, notify=False)
    if ok:
        await cb.answer("✅ پرداخت تأیید و کیف پولت شارژ شد.")
        await IP.show_paid(cb, row.id, credit, cb.from_user.id, Session)
    else:
        await IP.toast(cb, "ℹ️ " + msg_)


# === PATCH_UNIQUEPAY_CANCEL_BACK_V1 ===
@uniquepay_router.callback_query(F.data.startswith("upu:cancel:"))
async def upu_cancel(cb: CallbackQuery, bot: Bot):
    """لغوِ یک فاکتورِ پرداخت‌نشده و بازگشت — فقط سمتِ ما (لینکِ درگاه هنوز
    باز است اما دیگر پیگیری نمی‌شود). اگر کاربر واقعاً کارت‌به‌کارت کرده
    باشد، پولش نمی‌سوزد: هر وقت خواست می‌تواند از پشتیبانی بخواهد دستی
    بررسی شود، چون رکورد پاک نمی‌شود — فقط وضعیتش cancelled می‌گردد."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    hid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(UniquePayCharge).where(
            UniquePayCharge.bot_id == row.id, UniquePayCharge.hash_id == hid))
        if not ch or ch.user_telegram_id != cb.from_user.id:
            await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
            return
        if ch.status == "paid":
            await cb.answer("این فاکتور قبلاً پرداخت شده و قابلِ لغو نیست.",
                            show_alert=True)
            return
        ch.status = "cancelled"
        await s.commit()
    await cb.answer("فاکتور لغو شد.")
    from tenant import inline_back_kb
    from tenant.gateways import inplace as IP               # PATCH_GATEWAY_INPLACE_V1: همان پیام «لغو شد» می‌شود
    await IP.show(cb, IP.CANCELLED_TEXT, await inline_back_kb(row.id))
