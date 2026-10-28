"""
blupal_admin.py — PATCH_BLUPAL_TENANT_V1

پنلِ تنظیماتِ درگاهِ بلوپال برایِ مالکِ ربات + جریانِ شارژِ کاربر (فاکتور ← واریزِ دقیق ← بررسی).
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session
from models import BotUser, BluPalCharge
from tenant.gateways import blupal as BP

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

blupal_router = Router()


class BpFSM(StatesGroup):
    key = State()
    amount = State()
    min_amount = State()
    max_amount = State()


# ═════════════════ پنلِ مالکِ ربات ═════════════════
async def _menu_content(bot_id: int):
    key = await BP._get(bot_id, "blupal_api_key", "")
    on = await BP._get(bot_id, "blupal_enabled", "0") == "1"
    show_wallet = await BP._get(bot_id, "blupal_show_wallet", "1") == "1"
    show_purchase = await BP._get(bot_id, "blupal_show_purchase", "1") == "1"
    repl = await BP._get(bot_id, "blupal_replace_card", "0") == "1"
    mn, mx = await BP.get_min_amount(bot_id), await BP.get_max_amount(bot_id)
    masked = (key[:9] + "…" + key[-4:]) if key and len(key) > 16 else (key or "—")
    mode = {"sandbox": " (حالتِ تست)", "live": " (اصلی)"}.get(BP.key_mode(key), "") if key else ""
    txt = ("💳 <b>درگاهِ بلوپال</b> (کارت‌به‌کارتِ خودکار)\n\n"
           "کاربر مبلغِ دقیقِ فاکتور را به کارتِ نمایش‌داده‌شده واریز می‌کند و کیفِ پولش خودکار شارژ می‌شود.\n\n"
           f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n"
           f"🔑 کلیدِ API: <code>{masked}</code>{mode}\n"
           f"🔽 حداقلِ مبلغ: <b>{mn:,}</b> تومان\n"
           f"🔼 حداکثرِ مبلغ: <b>{(f'{mx:,}' if mx else f'{BP.CEIL_AMOUNT:,} (سقفِ درگاه)')}</b> تومان\n"
           f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}\n"
           f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}\n"
           f"🔁 جایگزینِ کارت‌به‌کارتِ دستی: {'✅' if repl else '❌'}\n\n"
           "ℹ️ بازهٔ درگاه ۱۰٬۰۰۰ تا ۵۰٬۰۰۰٬۰۰۰ تومان است. مبلغِ فاکتور تا ۹۹۹ ریال (کمتر از ۱۰۰ تومان) با عددِ تصادفی "
           "تفاوت دارد تا واریزها از هم تشخیص داده شوند؛ کیفِ پولِ مشتری <b>کامل</b> به‌اندازهٔ مبلغِ شارژ پر می‌شود. "
           "کلیدِ API را از داشبوردِ بلوپال ← «مدیریت API Key» بردار (کلیدِ <code>blu_live_…</code> برایِ اصلی، "
           "<code>blu_test_…</code> فقط برایِ تست). حتماً یک کارتِ فعال در بلوپال ثبت کرده باشی.")
    rows = [
        [InlineKeyboardButton(text=("⛔️ غیرفعال‌کردن" if on else "✅ فعال‌کردن"), callback_data="bpl:toggle",
                              style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="🔑 ثبت / تغییرِ کلیدِ API", callback_data="bpl:setkey", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🔽 حداقلِ مبلغ: {mn:,}", callback_data="bpl:setmin", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text=f"🔼 حداکثر: {mx:,}" if mx else "🔼 حداکثر: سقفِ درگاه", callback_data="bpl:setmax",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}", callback_data="bpl:show_wallet",
                              style=(ButtonStyle.SUCCESS if show_wallet else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text=f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}",
                              callback_data="bpl:show_purchase",
                              style=(ButtonStyle.SUCCESS if show_purchase else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text=f"🔁 جایگزینِ کارت‌به‌کارتِ دستی: {'✅' if repl else '❌'}", callback_data="bpl:replace",
                              style=(ButtonStyle.SUCCESS if repl else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text="🧪 تستِ اتصال", callback_data="bpl:test", style=ButtonStyle.SUCCESS)],
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


@blupal_router.callback_query(F.data.in_({"set:blupal", "bpl:menu"}))
async def bp_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row.id)
    await cb.answer()


@blupal_router.callback_query(F.data == "bpl:toggle")
async def bp_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await BP._get(row.id, "blupal_enabled", "0") == "1"
    if not cur and not await BP._get(row.id, "blupal_api_key", ""):
        return await cb.answer("اول کلیدِ API را ثبت کن.", show_alert=True)
    await BP._set(row.id, "blupal_enabled", "0" if cur else "1")
    await cb.answer("خاموش شد." if cur else "فعال شد.")
    await _show(cb, row.id)


async def _flip(cb: CallbackQuery, bot: Bot, key: str, label: str):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await BP._get(row.id, key, "1") == "1"
    await BP._set(row.id, key, "0" if cur else "1")
    await cb.answer(f"{label}: " + ("پنهان شد" if cur else "نمایش داده می‌شود"))
    await _show(cb, row.id)


@blupal_router.callback_query(F.data == "bpl:show_wallet")
async def bp_show_wallet(cb: CallbackQuery, bot: Bot):
    await _flip(cb, bot, "blupal_show_wallet", "در کیف پول")


@blupal_router.callback_query(F.data == "bpl:replace")
async def bp_replace(cb: CallbackQuery, bot: Bot):
    """PATCH_BLUPAL_UX_V1: بلوپال به‌جایِ «کارت‌به‌کارتِ دستی» (شمارهٔ کارتِ ثابت + رسید) نمایش داده شود."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await BP._get(row.id, "blupal_replace_card", "0") == "1"
    if not cur and not await BP.is_available(row.id):
        return await cb.answer("اول درگاه را فعال کن (کلیدِ API + روشن).", show_alert=True)
    await BP._set(row.id, "blupal_replace_card", "0" if cur else "1")
    await cb.answer("کارت‌به‌کارتِ دستی دوباره نمایش داده می‌شود." if cur else "بلوپال جایگزینِ کارت‌به‌کارتِ دستی شد.", show_alert=not cur)
    await _show(cb, row.id)


@blupal_router.callback_query(F.data == "bpl:show_purchase")
async def bp_show_purchase(cb: CallbackQuery, bot: Bot):
    await _flip(cb, bot, "blupal_show_purchase", "موقعِ پرداختِ محصول")


async def _ask_number(cb: CallbackQuery, bot: Bot, state: FSMContext, st, prompt: str):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(st)
    await cb.message.answer(prompt, reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@blupal_router.callback_query(F.data == "bpl:setmin")
async def bp_setmin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    cur = await BP.get_min_amount(row.id) if row else BP.MIN_AMOUNT
    await _ask_number(cb, bot, state, BpFSM.min_amount,
                      f"🔽 حداقلِ مبلغِ شارژ را به تومان بفرست (فعلاً: <b>{cur:,}</b>؛ کفِ درگاه {BP.FLOOR_AMOUNT:,}).")


@blupal_router.message(BpFSM.min_amount, F.text)
async def bp_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if not n or n < BP.FLOOR_AMOUNT:
        return await msg.answer(f"عددِ نامعتبر؛ حداقل {BP.FLOOR_AMOUNT:,} تومان. دوباره امتحان کن.")
    await BP._set(row.id, "blupal_min_amount", str(int(n)))
    await msg.answer(f"✅ حداقلِ مبلغ روی {int(n):,} تومان تنظیم شد.")
    await _show(msg, row.id)


@blupal_router.callback_query(F.data == "bpl:setmax")
async def bp_setmax(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    cur = await BP.get_max_amount(row.id) if row else 0
    await _ask_number(cb, bot, state, BpFSM.max_amount,
                      f"🔼 حداکثرِ مبلغِ شارژ را به تومان بفرست (فعلاً: <b>{(f'{cur:,}' if cur else 'سقفِ درگاه')}</b>؛ "
                      f"بیشینهٔ درگاه {BP.CEIL_AMOUNT:,}).\nبرایِ برگشت به سقفِ درگاه عددِ ۰ بفرست.")


@blupal_router.message(BpFSM.max_amount, F.text)
async def bp_setmax_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if n is None or n < 0:
        return await msg.answer("عددِ نامعتبر. دوباره امتحان کن.")
    if n and n > BP.CEIL_AMOUNT:
        return await msg.answer(f"سقفِ درگاه {BP.CEIL_AMOUNT:,} تومان است؛ عددِ کمتری بفرست.")
    await BP._set(row.id, "blupal_max_amount", str(int(n)))
    await msg.answer("✅ حداکثر روی سقفِ درگاه برگشت." if int(n) == 0 else f"✅ حداکثرِ مبلغ روی {int(n):,} تومان تنظیم شد.")
    await _show(msg, row.id)


@blupal_router.callback_query(F.data == "bpl:setkey")
async def bp_setkey(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _ask_number(cb, bot, state, BpFSM.key,
                      "🔑 <b>کلیدِ API</b> بلوپال را بفرست (شبیهِ <code>blu_live_…</code>؛ از داشبوردِ بلوپال ← «مدیریت API Key»).\n\n"
                      "این کلید فقط روی سرورِ ما نگه‌داری می‌شود. آدرسِ وب‌هوک لازم نیست.")


@blupal_router.message(BpFSM.key, F.text)
async def bp_setkey_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    key = (msg.text or "").strip()
    if len(key) < 12 or " " in key:
        return await msg.answer("کلید نامعتبر به‌نظر می‌رسد. دوباره امتحان کن.")
    await msg.answer("⏳ در حالِ تستِ کلید…")
    ok, why = await BP.validate_key(key)
    if not ok:
        return await msg.answer(f"❌ {why}")
    await BP._set(row.id, "blupal_api_key", key)
    extra = "\n⚠️ این کلیدِ <b>تست</b> است؛ برایِ پرداختِ واقعی کلیدِ <code>blu_live_…</code> بگذار." if BP.key_mode(key) == "sandbox" else ""
    await msg.answer("✅ کلید ذخیره و تأیید شد." + extra, parse_mode="HTML")
    await _show(msg, row.id)


@blupal_router.callback_query(F.data == "bpl:test")
async def bp_test(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = await BP._get(row.id, "blupal_api_key", "")
    if not key:
        return await cb.answer("اول کلید را ثبت کن.", show_alert=True)
    await cb.answer("در حالِ تست…")
    ok, why = await BP.validate_key(key)
    await cb.message.answer(("✅ " if ok else "❌ ") + why)


# ═════════════════ جریانِ کاربر ═════════════════
def _amount_kb(presets) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{x:,} تومان", callback_data=f"bpu:amt:{x}", style=ButtonStyle.SUCCESS)
             for x in presets[i:i + 2]] for i in range(0, len(presets), 2)]
    rows.append([InlineKeyboardButton(text="✏️ مبلغ دلخواه", callback_data="bpu:custom", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="w:back", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@blupal_router.callback_query(F.data.in_({"bpu:start", "w:blupal"}))
async def bpu_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """PATCH_BLUPAL_UX_V1: مثلِ زرین‌پال/کارت — دکمه‌هایِ مبلغِ آماده + «مبلغ دلخواه»."""
    from tenant import get_bot_row
    from tenant.gateways import wallet_limits as _wl
    from tenant.gateways.card_wallet import WALLET_PRESETS
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if not await BP.is_available(row.id):
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    await state.clear()
    mn, mx = await BP.get_min_amount(row.id), await BP.effective_max(row.id)
    presets = [p for p in (await _wl.allowed_presets(row.id, "blupal", WALLET_PRESETS)) if mn <= p <= mx]
    await cb.message.answer(
        f"💳 مبلغِ شارژِ کیف پول را انتخاب کن یا «✏️ مبلغ دلخواه» را بزن.\n(حداقل {mn:,} و حداکثر {mx:,} تومان)",
        reply_markup=_amount_kb(presets))
    await cb.answer()


@blupal_router.callback_query(F.data == "bpu:custom")
async def bpu_custom(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row or not await BP.is_available(row.id):
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    mn, mx = await BP.get_min_amount(row.id), await BP.effective_max(row.id)
    await state.set_state(BpFSM.amount)
    await cb.message.answer(
        f"💳 چه مبلغی می‌خواهی شارژ کنی؟ (تومان)\n\nحداقل: <b>{mn:,}</b> تومان | حداکثر: <b>{mx:,}</b> تومان",
        reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@blupal_router.callback_query(F.data.startswith("bpu:amt:"))
async def bpu_fixed_amount(cb: CallbackQuery, bot: Bot, state: FSMContext):
    try:
        amount = int(cb.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return await cb.answer("مبلغ نامعتبر است.", show_alert=True)
    await state.clear()
    await cb.answer()
    # cb.message پیامِ خودِ *ربات* است؛ پرداخت‌کننده کسی است که دکمه را زده
    await _issue(cb.message, bot, amount, cb.from_user)


@blupal_router.message(BpFSM.amount, F.text)
async def bpu_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import fa_to_int
    amount = fa_to_int(msg.text)
    if not amount:
        return await msg.answer("❌ مبلغ نامعتبر است؛ یک عدد به تومان بفرست.")
    if await _issue(msg, bot, amount, msg.from_user, from_text=True):
        await state.clear()


async def _issue(message: Message, bot: Bot, amount: int, payer, from_text: bool = False) -> bool:
    """فاکتور را می‌سازد و می‌فرستد. خروجی True یعنی کارِ این مبلغ تمام شد (برایِ پاک‌کردنِ حالتِ مبلغ)."""
    from models import alloc_tracking_in
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return True
    mn, mx = await BP.get_min_amount(row.id), await BP.effective_max(row.id)
    if amount < mn:
        await message.answer(f"حداقلِ مبلغ {mn:,} تومان است." + (" دوباره بفرست." if from_text else ""))
        return not from_text
    if amount > mx:
        await message.answer(f"حداکثرِ مبلغ {mx:,} تومان است." + (" دوباره بفرست." if from_text else ""))
        return not from_text
    if not await BP.is_available(row.id):
        await message.answer("این درگاه فعال نیست.")
        return True
    try:                                                    # سقفِ احرازِ هویت
        from tenant.shared.kyc import check_charge
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == payer.id))
        ok, why = await check_charge(row.id, u, amount)
        if not ok:
            await message.answer(why)
            return True
    except Exception:
        logger.exception("blupal: بررسیِ سقف ناموفق بود")
    key = await BP._get(row.id, "blupal_api_key", "")
    if not key:
        await message.answer("درگاه تنظیم نشده است.")
        return True
    oid = BP.new_order_id()
    amount_rial = BP.to_rial(amount)
    wait = await message.answer("⏳ در حالِ ساختِ فاکتور…")
    res = await BP.create_invoice(key, amount_rial)
    if not res.get("success") or not res.get("invoice_id") or not res.get("payment_link"):
        text = "❌ ساختِ فاکتور ناموفق بود.\n" + BP._err(res)
        try:
            await wait.edit_text(text)
        except Exception:  # noqa: BLE001
            await message.answer(text)
        return True
    final_rial = int(res.get("final_amount") or amount_rial)
    card = str(res.get("card_number") or "")[:32]
    async with Session() as s:
        seq = await alloc_tracking_in(s, row.id)            # همان شمارندهٔ ترتیبیِ سفارش‌ها
        ch = BluPalCharge(bot_id=row.id, user_telegram_id=payer.id, order_id=oid, invoice_id=int(res["invoice_id"]),
                          amount_toman=int(amount), amount_rial=amount_rial, final_amount_rial=final_rial,
                          card_number=card or None, status="waiting", seq_code=seq)
        s.add(ch)
        await s.commit()
        cid = ch.id
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 صفحهٔ پرداخت", url=str(res["payment_link"]))],
        [InlineKeyboardButton(text="🔍 بررسیِ پرداخت", callback_data=f"bpu:check:{oid}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data=f"bpu:cancel:{oid}", style=ButtonStyle.DANGER)],
    ])
    card_line = f"🏦 شمارهٔ کارتِ مقصد: <code>{card}</code>\n" if card else ""
    text = (
        "🧾 <b>فاکتورِ شارژِ کیف پول</b>\n➖➖➖➖➖➖➖➖➖➖\n\n"
        # PATCH_BLUPAL_UX_V2 (فدایی): «مبلغِ شارژ» نمایش داده نمی‌شود؛ فقط مبلغِ دقیقِ واریز تا کاربر عدد را اشتباه نزند
        f"💳 <b>مبلغِ دقیقِ واریز: {final_rial:,} ریال</b>\n"
        f"{card_line}"
        f"🔑 کدِ پیگیری: <code>{seq}</code>\n\n"
        "⚠️ <b>دقیقاً همین مبلغ (به ریال، با همین رقم‌های آخر)</b> را کارت‌به‌کارت کن؛ کم‌وزیاد یا رند، خودکار تأیید نمی‌شود. "
        "مبلغ را در اپِ بانک به‌صورت ریال وارد کن و اگر شک داشتی، از «صفحهٔ پرداخت» مبلغِ دقیق را کپی کن.\n\n"
        "📋 <b>مراحل:</b>\n"
        "۱. روی «💳 صفحهٔ پرداخت» بزن و مبلغ/کارت را ببین.\n"
        "۲. واریز کن، بعد «🔍 بررسیِ پرداخت» را بزن (خودکار هم بررسی می‌شود).\n"
        "۳. کیفِ پولت شارژ می‌شود. ✅\n\n"
        "⏳ اگر منصرف شدی، «❌ لغو و بازگشت» را بزن.")
    try:                                                    # «در حال ساخت» همان پیامِ فاکتور می‌شود (نه پیامِ اضافه)
        await wait.edit_text(text, reply_markup=kb, parse_mode="HTML")
        sent = wait
    except Exception:  # noqa: BLE001
        sent = await message.answer(text, reply_markup=kb, parse_mode="HTML")
    async with Session() as s:
        ch = await s.get(BluPalCharge, cid)
        if ch:
            ch.chat_id, ch.msg_id = sent.chat.id, sent.message_id
            await s.commit()
    return True


@blupal_router.callback_query(F.data.startswith("bpu:check:"))
async def bpu_check(cb: CallbackQuery, bot: Bot):
    """PATCH_BLUPAL_UX_V1: نتیجه بدونِ پیامِ جدید — «هنوز پرداخت نشده» اعلانِ بالایِ صفحه است؛ پرداخت‌شد ⇒ همان فاکتور ویرایش می‌شود."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    oid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(BluPalCharge).where(BluPalCharge.bot_id == row.id, BluPalCharge.order_id == oid).with_for_update())
        if not ch or ch.user_telegram_id != cb.from_user.id:
            return await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        if not ch.msg_id and cb.message:                    # فاکتورِ قدیمی: همین پیام را برایِ ویرایش ثبت کن
            ch.chat_id, ch.msg_id = cb.message.chat.id, cb.message.message_id
            await s.commit()
    ok, msg_, _credit = await BP.settle_charge(row.id, oid)
    if ok:
        await cb.answer("✅ پرداخت تأیید و کیف پولت شارژ شد.")
    else:
        await cb.answer("ℹ️ " + msg_, show_alert=False)


@blupal_router.callback_query(F.data.startswith("bpu:cancel:"))
async def bpu_cancel(cb: CallbackQuery, bot: Bot):
    """لغوِ یک فاکتورِ پرداخت‌نشده؛ رکورد پاک نمی‌شود (اگر کاربر واقعاً واریز کرده باشد پشتیبانی می‌تواند بررسی کند)."""
    from tenant import get_bot_row, inline_back_kb
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    oid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(BluPalCharge).where(BluPalCharge.bot_id == row.id, BluPalCharge.order_id == oid))
        if not ch or ch.user_telegram_id != cb.from_user.id:
            return await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        if ch.status == "paid":
            return await cb.answer("این فاکتور قبلاً پرداخت شده و قابلِ لغو نیست.", show_alert=True)
    # اگر همین لحظه پرداخت شده بود، به‌جایِ لغو شارژ کن (پولِ واریزشده نسوزد)؛ settle خودش پیام را به «پرداخت شد» ویرایش می‌کند
    ok_paid, _m, credit = await BP.settle_charge(row.id, oid)
    if ok_paid:
        return await cb.answer(f"✅ پرداختت رسیده بود و {credit:,} تومان به کیف پولت اضافه شد.", show_alert=True)
    async with Session() as s:
        ch = await s.scalar(select(BluPalCharge).where(BluPalCharge.bot_id == row.id, BluPalCharge.order_id == oid).with_for_update())
        if not ch or ch.status == "paid":
            return await cb.answer("این فاکتور قبلاً پرداخت شده و قابلِ لغو نیست.", show_alert=True)
        ch.status = "cancelled"
        amt, code = int(ch.amount_toman or 0), BP.code_of(ch)
        await s.commit()
    await cb.answer("فاکتور لغو شد.")
    text = BP.closed_text(amt, code, "cancelled")
    try:                                                    # همان پیامِ فاکتور به «لغو شد» تبدیل می‌شود
        await cb.message.edit_text(text, reply_markup=await inline_back_kb(row.id), parse_mode="HTML")
    except Exception:  # noqa: BLE001
        await cb.message.answer(text, reply_markup=await inline_back_kb(row.id), parse_mode="HTML")


def _detail_text(ch) -> str:
    st = {"paid": "✅ پرداخت‌شده", "waiting": "⏳ در انتظار پرداخت", "expired": "⌛️ منقضی", "cancelled": "❌ لغوشده"}.get(ch.status, ch.status)
    jd = ""
    try:
        from tenant import _jalali_date_time
        jd = "\n📅 " + " ".join(_jalali_date_time(ch.paid_at or ch.created_at))
    except Exception:  # noqa: BLE001
        logger.debug("blupal: تاریخِ جلالی ساخته نشد")
    return ("💳 <b>شارژ با بلوپال</b>\n┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
            f"🆔 کدِ پیگیری: <code>{BP.code_of(ch)}</code>\n"
            f"👤 کاربر: <code>{ch.user_telegram_id}</code>\n"
            f"💰 مبلغِ شارژ: {int(ch.amount_toman or 0):,} تومان\n"
            f"💳 مبلغِ دقیقِ واریز: {int(ch.final_amount_rial or 0):,} ریال\n"
            f"📊 وضعیت: {st}"
            + (f"\n🏦 شمارهٔ تراکنشِ بانکی: <code>{ch.tracking_code}</code>" if ch.tracking_code else "")
            + (f"\n🙍 واریزکننده: {ch.payer_name}" if ch.payer_name else "")
            + f"\n🧷 شناسهٔ داخلی: <code>{ch.order_id}</code>" + jd)


@blupal_router.callback_query(F.data.startswith("tadm:bpl:"))
async def bpl_detail(cb: CallbackQuery, bot: Bot):
    """جزئیاتِ یک شارژِ بلوپال برایِ مالک (از «سفارش‌ها و رسیدها»)."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    try:
        cid = int(cb.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return await cb.answer()
    async with Session() as s:
        ch = await s.get(BluPalCharge, cid)
    if not ch or ch.bot_id != row.id:
        return await cb.answer("پیدا نشد.", show_alert=True)
    await cb.message.answer(_detail_text(ch), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:orders", style=ButtonStyle.DANGER)]]))
    await cb.answer()
