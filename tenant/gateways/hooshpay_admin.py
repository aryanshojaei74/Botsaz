"""
hooshpay_admin.py — PATCH_HOOSHPAY_GATEWAY_V1

پنلِ تنظیماتِ درگاهِ هوش‌پی برایِ مالکِ ربات + جریانِ شارژِ کاربر (فاکتور ← پرداخت ← بررسی).
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session
from models import BotUser, HooshPayCharge
from tenant.gateways import hooshpay as HP

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

hooshpay_router = Router()


class HpFSM(StatesGroup):
    key = State()
    amount = State()
    min_amount = State()
    max_amount = State()


# ═════════════════ پنلِ مالکِ ربات ═════════════════
async def _menu_content(bot_id: int):
    key = await HP._get(bot_id, "hooshpay_api_key", "")
    on = await HP._get(bot_id, "hooshpay_enabled", "0") == "1"
    show_wallet = await HP._get(bot_id, "hooshpay_show_wallet", "1") == "1"
    show_purchase = await HP._get(bot_id, "hooshpay_show_purchase", "1") == "1"
    mn, mx = await HP.get_min_amount(bot_id), await HP.get_max_amount(bot_id)
    fee_mode = await HP.get_fee_mode(bot_id)
    masked = (key[:8] + "…" + key[-4:]) if key and len(key) > 14 else (key or "—")
    fee_help = {
        "buyer": "خریدار کارمزد را روی مبلغِ پرداختی‌اش می‌دهد و کیفِ پولش کامل (به‌اندازهٔ مبلغِ شارژ) پر می‌شود؛ تو چیزی از دست نمی‌دهی.",
        "seller": "مشتری دقیقاً مبلغِ شارژ را می‌دهد و کارمزد از موجودیِ هوش‌پیِ <b>تو</b> کم می‌شود.",
        "split": "کارمزد نصف‌نصف بینِ مشتری و تو تقسیم می‌شود.",
    }[fee_mode]
    txt = ("💳 <b>درگاهِ هوش‌پی</b> (کارت‌به‌کارتِ آنی با تأییدِ پیامکی)\n\n"
           "کاربر روی لینکِ پرداخت می‌رود، دقیقاً مبلغِ نمایش‌داده‌شده را کارت‌به‌کارت می‌کند و کیفِ پولش خودکار شارژ می‌شود.\n\n"
           f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n"
           f"🔑 کلیدِ API: <code>{masked}</code>\n"
           f"💸 پرداختِ کارمزد: <b>{HP.FEE_MODE_FA[fee_mode]}</b>\n<i>{fee_help}</i>\n"
           f"🔽 حداقلِ مبلغ: <b>{mn:,}</b> تومان\n"
           f"🔼 حداکثرِ مبلغ: <b>{(f'{mx:,}' if mx else 'بدونِ سقف')}</b>\n"
           f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}\n"
           f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}\n\n"
           "ℹ️ کارمزدِ پیش‌فرضِ هوش‌پی ۲۰٪ است (از پنلِ هوش‌پی قابلِ تنظیم/مشاهده است). کلیدِ API را از "
           "hooshpay.xyz ← بخشِ «توسعه» بردار؛ نیازی به Secret نیست.")
    rows = [
        [InlineKeyboardButton(text=("⛔️ غیرفعال‌کردن" if on else "✅ فعال‌کردن"), callback_data="ho:toggle",
                              style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="🔑 ثبت / تغییرِ کلیدِ API", callback_data="ho:setkey", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"💸 کارمزد با: {HP.FEE_MODE_FA[fee_mode]} (تغییر)", callback_data="ho:fee",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🔽 حداقلِ مبلغ: {mn:,}", callback_data="ho:setmin", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text=f"🔼 حداکثر: {mx:,}" if mx else "🔼 حداکثر: بدونِ سقف", callback_data="ho:setmax",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}", callback_data="ho:show_wallet",
                              style=(ButtonStyle.SUCCESS if show_wallet else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text=f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}",
                              callback_data="ho:show_purchase",
                              style=(ButtonStyle.SUCCESS if show_purchase else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text="🧪 تستِ اتصال", callback_data="ho:test", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ]
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target, bot_id):
    txt, kb = await _menu_content(bot_id)
    m = getattr(target, "message", None) or target
    try:
        await m.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await m.answer(txt, reply_markup=kb, parse_mode="HTML")


@hooshpay_router.callback_query(F.data.in_({"set:hooshpay", "ho:menu"}))
async def ho_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row.id)
    await cb.answer()


@hooshpay_router.callback_query(F.data == "ho:toggle")
async def ho_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await HP._get(row.id, "hooshpay_enabled", "0") == "1"
    if not cur and not await HP._get(row.id, "hooshpay_api_key", ""):
        return await cb.answer("اول کلیدِ API را ثبت کن.", show_alert=True)
    await HP._set(row.id, "hooshpay_enabled", "0" if cur else "1")
    await cb.answer("خاموش شد." if cur else "فعال شد.")
    await _show(cb, row.id)


@hooshpay_router.callback_query(F.data == "ho:fee")
async def ho_fee(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await HP.get_fee_mode(row.id)
    nxt = HP.FEE_MODES[(HP.FEE_MODES.index(cur) + 1) % len(HP.FEE_MODES)]
    await HP._set(row.id, "hooshpay_fee_mode", nxt)
    await cb.answer(f"کارمزد با: {HP.FEE_MODE_FA[nxt]}")
    await _show(cb, row.id)


async def _flip(cb: CallbackQuery, bot: Bot, key: str, label: str):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await HP._get(row.id, key, "1") == "1"
    await HP._set(row.id, key, "0" if cur else "1")
    await cb.answer(f"{label}: " + ("پنهان شد" if cur else "نمایش داده می‌شود"))
    await _show(cb, row.id)


@hooshpay_router.callback_query(F.data == "ho:show_wallet")
async def ho_show_wallet(cb: CallbackQuery, bot: Bot):
    await _flip(cb, bot, "hooshpay_show_wallet", "در کیف پول")


@hooshpay_router.callback_query(F.data == "ho:show_purchase")
async def ho_show_purchase(cb: CallbackQuery, bot: Bot):
    await _flip(cb, bot, "hooshpay_show_purchase", "موقعِ پرداختِ محصول")


async def _ask_number(cb: CallbackQuery, bot: Bot, state: FSMContext, st, prompt: str):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(st)
    await cb.message.answer(prompt, reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@hooshpay_router.callback_query(F.data == "ho:setmin")
async def ho_setmin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    cur = await HP.get_min_amount(row.id) if row else HP.MIN_AMOUNT
    await _ask_number(cb, bot, state, HpFSM.min_amount,
                      f"🔽 حداقلِ مبلغِ شارژ را به تومان بفرست (فعلاً: <b>{cur:,}</b>؛ کفِ فنیِ هوش‌پی ۱٬۰۰۰).")


@hooshpay_router.message(HpFSM.min_amount, F.text)
async def ho_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if not n or n < HP.FLOOR_AMOUNT:
        return await msg.answer(f"عددِ نامعتبر؛ حداقل {HP.FLOOR_AMOUNT:,} تومان. دوباره امتحان کن.")
    await HP._set(row.id, "hooshpay_min_amount", str(int(n)))
    await msg.answer(f"✅ حداقلِ مبلغ روی {int(n):,} تومان تنظیم شد.")
    await _show(msg, row.id)


@hooshpay_router.callback_query(F.data == "ho:setmax")
async def ho_setmax(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    cur = await HP.get_max_amount(row.id) if row else 0
    await _ask_number(cb, bot, state, HpFSM.max_amount,
                      f"🔼 حداکثرِ مبلغِ شارژ را به تومان بفرست (فعلاً: <b>{(f'{cur:,}' if cur else 'بدونِ سقف')}</b>).\n"
                      "برایِ برداشتنِ سقف عددِ ۰ بفرست.")


@hooshpay_router.message(HpFSM.max_amount, F.text)
async def ho_setmax_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if n is None or n < 0:
        return await msg.answer("عددِ نامعتبر. دوباره امتحان کن.")
    await HP._set(row.id, "hooshpay_max_amount", str(int(n)))
    await msg.answer("✅ حداکثرِ مبلغ برداشته شد (بدونِ سقف)." if int(n) == 0 else f"✅ حداکثرِ مبلغ روی {int(n):,} تومان تنظیم شد.")
    await _show(msg, row.id)


@hooshpay_router.callback_query(F.data == "ho:setkey")
async def ho_setkey(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _ask_number(cb, bot, state, HpFSM.key,
                      "🔑 <b>کلیدِ API</b> هوش‌پی را بفرست (شبیهِ <code>hp_live_…</code>؛ از hooshpay.xyz ← «توسعه»).\n\n"
                      "این کلید فقط روی سرورِ ما نگه‌داری می‌شود. Secret لازم نیست.")


@hooshpay_router.message(HpFSM.key, F.text)
async def ho_setkey_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    key = (msg.text or "").strip()
    if len(key) < 12 or " " in key:
        return await msg.answer("کلید نامعتبر به‌نظر می‌رسد. دوباره امتحان کن.")
    await msg.answer("⏳ در حالِ تستِ کلید…")
    ok, why = await HP.validate_key(key)
    if not ok:
        return await msg.answer(f"❌ {why}")
    await HP._set(row.id, "hooshpay_api_key", key)
    await msg.answer("✅ کلید ذخیره و تأیید شد.")
    await _show(msg, row.id)


@hooshpay_router.callback_query(F.data == "ho:test")
async def ho_test(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = await HP._get(row.id, "hooshpay_api_key", "")
    if not key:
        return await cb.answer("اول کلید را ثبت کن.", show_alert=True)
    await cb.answer("در حالِ تست…")
    ok, why = await HP.validate_key(key)
    await cb.message.answer(("✅ " if ok else "❌ ") + why)


# ═════════════════ جریانِ کاربر ═════════════════
@hooshpay_router.callback_query(F.data.in_({"hou:start", "w:hooshpay"}))
async def hou_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if not await HP.is_available(row.id):
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    mn, mx = await HP.get_min_amount(row.id), await HP.get_max_amount(row.id)
    rng = f"حداقل: <b>{mn:,}</b> تومان" + (f" | حداکثر: <b>{mx:,}</b> تومان" if mx else "")
    await state.set_state(HpFSM.amount)
    await cb.message.answer(f"💳 چه مبلغی می‌خواهی شارژ کنی؟ (تومان)\n\n{rng}",
                            reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@hooshpay_router.message(HpFSM.amount, F.text)
async def hou_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import fa_to_int, get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    amount = fa_to_int(msg.text)
    mn, mx = await HP.get_min_amount(row.id), await HP.get_max_amount(row.id)
    if not amount or amount < mn:
        return await msg.answer(f"حداقلِ مبلغ {mn:,} تومان است. دوباره بفرست.")
    if mx and amount > mx:
        return await msg.answer(f"حداکثرِ مبلغ {mx:,} تومان است. دوباره بفرست.")
    await state.clear()
    try:                                                    # سقفِ احرازِ هویت
        from tenant.shared.kyc import check_charge
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == msg.from_user.id))
        ok, why = await check_charge(row.id, u, amount)
        if not ok:
            return await msg.answer(why)
    except Exception:
        logger.exception("hooshpay: بررسیِ سقف ناموفق بود")
    key = await HP._get(row.id, "hooshpay_api_key", "")
    if not key:
        return await msg.answer("درگاه تنظیم نشده است.")
    oid = HP.new_order_id()
    fee_mode = await HP.get_fee_mode(row.id)
    await msg.answer("⏳ در حالِ ساختِ فاکتور…")
    res = await HP.create_invoice(key, amount, oid, fee_mode, "شارژ کیف پول")
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    if not res.get("success") or not data.get("uid") or not data.get("payment_url"):
        return await msg.answer("❌ ساختِ فاکتور ناموفق بود.\n" + HP._err(res))
    payable = int(data.get("payable_amount") or amount)
    fee = int(data.get("fee_amount") or 0)
    async with Session() as s:
        s.add(HooshPayCharge(bot_id=row.id, user_telegram_id=msg.from_user.id, order_id=oid, invoice_uid=str(data["uid"])[:64],
                             amount_toman=int(amount), payable_amount=payable, fee_mode=fee_mode, status="waiting"))
        await s.commit()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 پرداخت", url=str(data["payment_url"]))],
        [InlineKeyboardButton(text="🔍 بررسیِ پرداخت", callback_data=f"hou:check:{oid}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data=f"hou:cancel:{oid}", style=ButtonStyle.DANGER)],
    ])
    fee_line = ""
    if payable != amount:
        fee_line = f"💸 شاملِ کارمزد/رقمِ یکتا: <b>{payable - amount:,}</b> تومان\n"
    await msg.answer(
        "🧾 <b>فاکتورِ شارژِ کیف پول</b>\n➖➖➖➖➖➖➖➖➖➖\n\n"
        f"💵 مبلغِ شارژ: <b>{amount:,}</b> تومان\n{fee_line}"
        f"💳 <b>مبلغِ قابلِ پرداخت: {payable:,} تومان</b>\n"
        f"🔑 کدِ پیگیری: <code>{oid}</code>\n\n"
        "⚠️ <b>دقیقاً همین مبلغِ قابلِ پرداخت</b> را کارت‌به‌کارت کن (کم‌وزیاد یا رند، خودکار تأیید نمی‌شود).\n\n"
        "📋 <b>مراحل:</b>\n"
        "۱. روی «💳 پرداخت» بزن و کارت‌به‌کارت کن.\n"
        "۲. برگرد اینجا و «🔍 بررسیِ پرداخت» را بزن (خودکار هم بررسی می‌شود).\n"
        "۳. کیفِ پولت شارژ می‌شود. ✅\n\n"
        "⏳ اگر منصرف شدی، «❌ لغو و بازگشت» را بزن.",
        reply_markup=kb, parse_mode="HTML")


@hooshpay_router.callback_query(F.data.startswith("hou:check:"))
async def hou_check(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    oid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(HooshPayCharge).where(HooshPayCharge.bot_id == row.id, HooshPayCharge.order_id == oid))
    if not ch or ch.user_telegram_id != cb.from_user.id:
        return await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
    await cb.answer("در حالِ استعلام…")
    ok, msg_, credit = await HP.settle_charge(row.id, oid)
    if ok:
        from tenant.gateways.wallet_receipt import build_wallet_charge_message_for_bot
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == cb.from_user.id))
        bal = float(u.balance) if u else credit
        try:
            t, k = await build_wallet_charge_message_for_bot(row.id, credit, bal, "card")
            await cb.message.answer(t, reply_markup=k)
        except Exception:
            await cb.message.answer(f"✅ {credit:,} تومان به کیفِ پولت اضافه شد.")
    else:
        await cb.message.answer("ℹ️ " + msg_)


@hooshpay_router.callback_query(F.data.startswith("hou:cancel:"))
async def hou_cancel(cb: CallbackQuery, bot: Bot):
    """لغوِ یک فاکتورِ پرداخت‌نشده؛ رکورد پاک نمی‌شود (اگر کاربر واقعاً واریز کرده باشد پشتیبانی می‌تواند بررسی کند)."""
    from tenant import get_bot_row, inline_back_kb
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    oid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(HooshPayCharge).where(HooshPayCharge.bot_id == row.id,
                                                         HooshPayCharge.order_id == oid).with_for_update())
        if not ch or ch.user_telegram_id != cb.from_user.id:
            return await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        if ch.status == "paid":
            return await cb.answer("این فاکتور قبلاً پرداخت شده و قابلِ لغو نیست.", show_alert=True)
        uid = ch.invoice_uid
    # اگر همین لحظه پرداخت شده بود، به‌جایِ لغو شارژ کن (پولِ واریزشده نسوزد)
    ok_paid, _m, credit = await HP.settle_charge(row.id, oid)
    if ok_paid:
        return await cb.answer(f"✅ پرداختت رسیده بود و {credit:,} تومان به کیف پولت اضافه شد.", show_alert=True)
    async with Session() as s:
        ch = await s.scalar(select(HooshPayCharge).where(HooshPayCharge.bot_id == row.id,
                                                         HooshPayCharge.order_id == oid).with_for_update())
        if not ch or ch.status == "paid":
            return await cb.answer("این فاکتور قبلاً پرداخت شده و قابلِ لغو نیست.", show_alert=True)
        ch.status = "cancelled"
        await s.commit()
    key = await HP._get(row.id, "hooshpay_api_key", "")
    if key and uid:                                         # سمتِ درگاه هم بسته شود (فقط pending قابلِ لغو است)
        try:
            await HP.cancel_invoice(key, uid)
        except Exception:
            logger.exception("hooshpay: لغوِ فاکتور سمتِ درگاه ناموفق بود (بی‌اهمیت)")
    await cb.answer("فاکتور لغو شد.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")
    await cb.message.answer(
        "❌ فاکتور لغو شد.\n\nاگر واریز کردی ولی اینجا لغو زدی، نگران نباش؛ به پشتیبانیِ ربات بگو با همین کدِ پیگیری بررسیش کنه.",
        reply_markup=await inline_back_kb(row.id))
