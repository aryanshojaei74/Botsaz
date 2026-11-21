"""
frenzyex_admin.py — PATCH_FRENZYEX_GATEWAY_V1

پنلِ تنظیماتِ درگاهِ فرنزی‌اکس برایِ مالکِ ربات (فقط پلنِ حرفه‌ای) + جریانِ شارژِ کاربر (فاکتور ← پرداخت ← بررسی).
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session
from models import BotUser, FrenzyExCharge
from tenant.gateways import frenzyex as FX

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

frenzyex_router = Router()


class FxFSM(StatesGroup):
    key = State()
    amount = State()
    min_amount = State()
    max_amount = State()


async def _pro_gate(cb, row) -> bool:
    """مالکِ غیرِ حرفه‌ای قفل است (وگرنه با callbackِ دستی هم نمی‌تواند تنظیم کند)."""
    from plans import Feature, has_feature, upgrade_hint
    if has_feature(row.plan, Feature.GATEWAY_FRENZYEX):
        return True
    try:
        await cb.answer(upgrade_hint(Feature.GATEWAY_FRENZYEX), show_alert=True)
    except Exception:
        await cb.answer("🔒 این درگاه فقط در پلنِ حرفه‌ای فعال است.")
    return False


# ═════════════════ پنلِ مالکِ ربات ═════════════════
async def _menu_content(bot_id: int):
    key = await FX._get(bot_id, "frenzyex_api_key", "")
    on = await FX._get(bot_id, "frenzyex_enabled", "0") == "1"
    show_wallet = await FX._get(bot_id, "frenzyex_show_wallet", "1") == "1"
    show_purchase = await FX._get(bot_id, "frenzyex_show_purchase", "1") == "1"
    mn, mx = await FX.get_min_amount(bot_id), await FX.get_max_amount(bot_id)
    method = await FX.get_method(bot_id)
    masked = (key[:9] + "…" + key[-4:]) if key and len(key) > 16 else ("ثبت شده" if key else "—")
    txt = ("💳 <b>درگاهِ فرنزی‌اکس</b> (کارت‌به‌کارت + کریپتو) — ویژهٔ پلنِ حرفه‌ای\n\n"
           "کاربر روی لینکِ پرداخت می‌رود (ربات فرنزی‌اکس)، «شروع پرداخت» را می‌زند، مبلغِ دقیقِ نمایش‌داده‌شده را کارت‌به‌کارت "
           "می‌کند و کیفِ پولش خودکار شارژ می‌شود.\n\n"
           f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}\n"
           f"🔑 کلیدِ API: <code>{masked}</code>\n"
           f"🧭 روشِ پرداخت: <b>{FX.METHOD_FA[method]}</b>\n"
           f"🔽 حداقلِ مبلغ: <b>{mn:,}</b> تومان\n"
           f"🔼 حداکثرِ مبلغ: <b>{(f'{mx:,}' if mx else 'بدونِ سقف')}</b>\n"
           f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}\n"
           f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}\n\n"
           "ℹ️ کلیدِ API (شبیهِ <code>frx_live_…</code>) را مدیرِ فرنزی‌اکس به تو می‌دهد. Callback Secret لازم نیست؛ پرداخت‌ها "
           "هر ۲ دقیقه خودکار از API بررسی می‌شوند. کارمزدِ فرنزی‌اکس روی مبلغِ پرداختیِ مشتری اضافه می‌شود و کیفِ پولِ او کامل "
           "(به‌اندازهٔ مبلغِ فاکتور) شارژ می‌شود؛ مبلغِ نهایی را مشتری داخلِ صفحهٔ پرداخت می‌بیند.")
    rows = [
        [InlineKeyboardButton(text=("⛔️ غیرفعال‌کردن" if on else "✅ فعال‌کردن"), callback_data="fx:toggle",
                              style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="🔑 ثبت / تغییرِ کلیدِ API", callback_data="fx:setkey", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🧭 روشِ پرداخت: {'فقط کارت‌به‌کارت' if method == 'standard' else 'خودکار'} (تغییر)",
                              callback_data="fx:method", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🔽 حداقلِ مبلغ: {mn:,}", callback_data="fx:setmin", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text=f"🔼 حداکثر: {mx:,}" if mx else "🔼 حداکثر: بدونِ سقف", callback_data="fx:setmax",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"👛 نمایش در کیف پول: {'✅' if show_wallet else '❌'}", callback_data="fx:show_wallet",
                              style=(ButtonStyle.SUCCESS if show_wallet else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text=f"🛒 نمایش موقعِ پرداختِ محصول: {'✅' if show_purchase else '❌'}",
                              callback_data="fx:show_purchase",
                              style=(ButtonStyle.SUCCESS if show_purchase else ButtonStyle.DANGER))],
        [InlineKeyboardButton(text="🧪 تستِ اتصال", callback_data="fx:test", style=ButtonStyle.SUCCESS)],
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


async def _owner_pro(cb, bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row or not await _pro_gate(cb, row):
        return None
    return row


@frenzyex_router.callback_query(F.data.in_({"set:frenzyex", "fx:menu"}))
async def fx_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_pro(cb, bot)
    if not row:
        return
    await state.clear()
    await _show(cb, row.id)
    await cb.answer()


@frenzyex_router.callback_query(F.data == "fx:toggle")
async def fx_toggle(cb: CallbackQuery, bot: Bot):
    row = await _owner_pro(cb, bot)
    if not row:
        return
    cur = await FX._get(row.id, "frenzyex_enabled", "0") == "1"
    if not cur and not await FX._get(row.id, "frenzyex_api_key", ""):
        return await cb.answer("اول کلیدِ API را ثبت کن.", show_alert=True)
    await FX._set(row.id, "frenzyex_enabled", "0" if cur else "1")
    await cb.answer("خاموش شد." if cur else "فعال شد.")
    await _show(cb, row.id)


@frenzyex_router.callback_query(F.data == "fx:method")
async def fx_method(cb: CallbackQuery, bot: Bot):
    row = await _owner_pro(cb, bot)
    if not row:
        return
    cur = await FX.get_method(row.id)
    nxt = FX.METHODS[(FX.METHODS.index(cur) + 1) % len(FX.METHODS)]
    await FX._set(row.id, "frenzyex_method", nxt)
    await cb.answer("روشِ پرداخت: " + ("فقط کارت‌به‌کارت" if nxt == "standard" else "خودکار"))
    await _show(cb, row.id)


async def _flip(cb: CallbackQuery, bot: Bot, key: str, label: str):
    row = await _owner_pro(cb, bot)
    if not row:
        return
    cur = await FX._get(row.id, key, "1") == "1"
    await FX._set(row.id, key, "0" if cur else "1")
    await cb.answer(f"{label}: " + ("پنهان شد" if cur else "نمایش داده می‌شود"))
    await _show(cb, row.id)


@frenzyex_router.callback_query(F.data == "fx:show_wallet")
async def fx_show_wallet(cb: CallbackQuery, bot: Bot):
    await _flip(cb, bot, "frenzyex_show_wallet", "در کیف پول")


@frenzyex_router.callback_query(F.data == "fx:show_purchase")
async def fx_show_purchase(cb: CallbackQuery, bot: Bot):
    await _flip(cb, bot, "frenzyex_show_purchase", "موقعِ پرداختِ محصول")


async def _ask(cb: CallbackQuery, bot: Bot, state: FSMContext, st, prompt: str):
    from tenant import smart_cancel_kb
    row = await _owner_pro(cb, bot)
    if not row:
        return
    await state.set_state(st)
    await cb.message.answer(prompt, reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@frenzyex_router.callback_query(F.data == "fx:setmin")
async def fx_setmin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    cur = await FX.get_min_amount(row.id) if row else FX.MIN_AMOUNT
    await _ask(cb, bot, state, FxFSM.min_amount,
               f"🔽 حداقلِ مبلغِ شارژ را به تومان بفرست (فعلاً: <b>{cur:,}</b>؛ حداقلِ مجاز {FX.FLOOR_AMOUNT:,}).")


@frenzyex_router.message(FxFSM.min_amount, F.text)
async def fx_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if not n or n < FX.FLOOR_AMOUNT:
        return await msg.answer(f"عددِ نامعتبر؛ حداقل {FX.FLOOR_AMOUNT:,} تومان. دوباره امتحان کن.")
    await FX._set(row.id, "frenzyex_min_amount", str(int(n)))
    await msg.answer(f"✅ حداقلِ مبلغ روی {int(n):,} تومان تنظیم شد.")
    await _show(msg, row.id)


@frenzyex_router.callback_query(F.data == "fx:setmax")
async def fx_setmax(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    cur = await FX.get_max_amount(row.id) if row else 0
    await _ask(cb, bot, state, FxFSM.max_amount,
               f"🔼 حداکثرِ مبلغِ شارژ را به تومان بفرست (فعلاً: <b>{(f'{cur:,}' if cur else 'بدونِ سقف')}</b>).\n"
               "برایِ برداشتنِ سقف عددِ ۰ بفرست.")


@frenzyex_router.message(FxFSM.max_amount, F.text)
async def fx_setmax_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, fa_to_int
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    n = fa_to_int(msg.text)
    if n is None or n < 0:
        return await msg.answer("عددِ نامعتبر. دوباره امتحان کن.")
    await FX._set(row.id, "frenzyex_max_amount", str(int(n)))
    await msg.answer("✅ حداکثرِ مبلغ برداشته شد (بدونِ سقف)." if int(n) == 0 else f"✅ حداکثرِ مبلغ روی {int(n):,} تومان تنظیم شد.")
    await _show(msg, row.id)


@frenzyex_router.callback_query(F.data == "fx:setkey")
async def fx_setkey(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _ask(cb, bot, state, FxFSM.key,
               "🔑 <b>کلیدِ API</b> فرنزی‌اکس (MERCHANT_API_KEY) را بفرست (شبیهِ <code>frx_live_…</code>).\n\n"
               "این کلید فقط روی سرورِ ما نگه‌داری می‌شود. بعد از ارسال، پیامِ حاویِ کلید را از چت پاک کن.")


@frenzyex_router.message(FxFSM.key, F.text)
async def fx_setkey_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(msg, bot)
    await state.clear()
    if not row:
        return
    from plans import Feature, has_feature
    if not has_feature(row.plan, Feature.GATEWAY_FRENZYEX):
        return await msg.answer("🔒 این درگاه فقط در پلنِ حرفه‌ای فعال است.")
    key = (msg.text or "").strip()
    try:                                  # کلید در چت نماند
        await msg.delete()
    except Exception:
        logger.debug("frenzyex: پاک‌کردنِ پیامِ کلید ممکن نشد")
    if len(key) < 12 or " " in key:
        return await msg.answer("کلید نامعتبر به‌نظر می‌رسد. دوباره امتحان کن.")
    await msg.answer("⏳ در حالِ تستِ کلید…")
    ok, why = await FX.validate_key(key)
    if not ok:
        return await msg.answer(f"❌ {why}")
    await FX._set(row.id, "frenzyex_api_key", key)
    await msg.answer("✅ کلید ذخیره و تأیید شد.")
    await _show(msg, row.id)


@frenzyex_router.callback_query(F.data == "fx:test")
async def fx_test(cb: CallbackQuery, bot: Bot):
    row = await _owner_pro(cb, bot)
    if not row:
        return
    key = await FX._get(row.id, "frenzyex_api_key", "")
    if not key:
        return await cb.answer("اول کلید را ثبت کن.", show_alert=True)
    await cb.answer("در حالِ تست…")
    ok, why = await FX.validate_key(key)
    await cb.message.answer(("✅ " if ok else "❌ ") + why)


# ═════════════════ جریانِ کاربر ═════════════════
@frenzyex_router.callback_query(F.data.in_({"fxu:start", "w:frenzyex"}))
async def fxu_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    if not await FX.is_available(row.id):
        return await cb.answer("این درگاه فعال نیست.", show_alert=True)
    mn, mx = await FX.get_min_amount(row.id), await FX.get_max_amount(row.id)
    rng = f"حداقل: <b>{mn:,}</b> تومان" + (f" | حداکثر: <b>{mx:,}</b> تومان" if mx else "")
    await state.set_state(FxFSM.amount)
    await cb.message.answer(f"💳 چه مبلغی می‌خواهی شارژ کنی؟ (تومان)\n\n{rng}",
                            reply_markup=await smart_cancel_kb(bot), parse_mode="HTML")
    await cb.answer()


@frenzyex_router.message(FxFSM.amount, F.text)
async def fxu_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import fa_to_int, get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    amount = fa_to_int(msg.text)
    mn, mx = await FX.get_min_amount(row.id), await FX.get_max_amount(row.id)
    if not amount or amount < mn:
        return await msg.answer(f"حداقلِ مبلغ {mn:,} تومان است. دوباره بفرست.")
    if mx and amount > mx:
        return await msg.answer(f"حداکثرِ مبلغ {mx:,} تومان است. دوباره بفرست.")
    await state.clear()
    if not await FX.is_available(row.id):
        return await msg.answer("این درگاه فعال نیست.")
    try:                                                    # سقفِ احرازِ هویت
        from tenant.shared.kyc import check_charge
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == msg.from_user.id))
        ok, why = await check_charge(row.id, u, amount)
        if not ok:
            return await msg.answer(why)
    except Exception:
        logger.exception("frenzyex: بررسیِ سقف ناموفق بود")
    key = await FX._get(row.id, "frenzyex_api_key", "")
    if not key:
        return await msg.answer("درگاه تنظیم نشده است.")
    oid = FX.new_order_id()
    method = await FX.get_method(row.id)
    await msg.answer("⏳ در حالِ ساختِ فاکتور…")
    # ساختِ فاکتور idempotent نیست ⇒ هیچ تلاشِ مجددی نداریم؛ خطا = کاربر دوباره امتحان می‌کند.
    res = await FX.create_request(key, amount, oid, method, "شارژ کیف پول")
    link = str(res.get("checkout_url") or res.get("bot_pay_url") or res.get("pay_url") or "")
    if not res.get("success") or not res.get("request_id") or not link.startswith("http"):
        return await msg.answer("❌ ساختِ فاکتور ناموفق بود.\n" + FX._err(res))
    async with Session() as s:
        s.add(FrenzyExCharge(bot_id=row.id, user_telegram_id=msg.from_user.id, order_id=oid,
                             request_id=str(res["request_id"])[:64], amount_toman=int(amount), method=method,
                             status="waiting"))
        await s.commit()
    rows = [[InlineKeyboardButton(text="💳 پرداخت", url=link)]]
    alt = str(res.get("bot_pay_url") or "")
    if alt.startswith("http") and alt != link:
        rows.append([InlineKeyboardButton(text="🤖 باز کردن در ربات", url=alt)])
    rows += [
        [InlineKeyboardButton(text="🔍 بررسیِ پرداخت", callback_data=f"fxu:check:{oid}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data=f"fxu:cancel:{oid}", style=ButtonStyle.DANGER)],
    ]
    await msg.answer(
        "🧾 <b>فاکتورِ شارژِ کیف پول</b>\n➖➖➖➖➖➖➖➖➖➖\n\n"
        f"💵 مبلغِ شارژ: <b>{amount:,}</b> تومان\n"
        f"🔑 کدِ پیگیری: <code>{oid}</code>\n\n"
        "⚠️ مبلغِ نهایی (با کارمزد/رقمِ یکتا) داخلِ صفحهٔ پرداخت نمایش داده می‌شود؛ "
        "<b>دقیقاً همان مبلغ</b> را کارت‌به‌کارت کن (رند یا کم‌وزیاد، دیرتر تأیید می‌شود).\n\n"
        "📋 <b>مراحل:</b>\n"
        "۱. روی «💳 پرداخت» بزن و در ربات فرنزی‌اکس «شروع پرداخت» را بزن.\n"
        "۲. مبلغ را کارت‌به‌کارت کن و شمارهٔ کارتِ پرداخت‌کننده و عکسِ رسید را همان‌جا بفرست.\n"
        "۳. برگرد اینجا و «🔍 بررسیِ پرداخت» را بزن (خودکار هم بررسی می‌شود). کیفِ پولت شارژ می‌شود. ✅\n\n"
        "⏳ اگر منصرف شدی، «❌ لغو و بازگشت» را بزن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@frenzyex_router.callback_query(F.data.startswith("fxu:check:"))
async def fxu_check(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    oid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(FrenzyExCharge).where(FrenzyExCharge.bot_id == row.id, FrenzyExCharge.order_id == oid))
    if not ch or ch.user_telegram_id != cb.from_user.id:
        return await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
    from tenant.gateways import inplace as IP
    ok, msg_, credit = await FX.settle_charge(row.id, oid, notify=False)
    if ok:
        await cb.answer("✅ پرداخت تأیید و کیف پولت شارژ شد.")
        await IP.show_paid(cb, row.id, credit, cb.from_user.id, Session)
    else:
        await IP.toast(cb, "ℹ️ " + msg_)


@frenzyex_router.callback_query(F.data.startswith("fxu:cancel:"))
async def fxu_cancel(cb: CallbackQuery, bot: Bot):
    """لغوِ یک فاکتورِ پرداخت‌نشده. رکورد پاک نمی‌شود؛ اگر واریز بعداً برسد (تا ۲ ساعت) همچنان شارژ می‌شود."""
    from tenant import get_bot_row, inline_back_kb
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    oid = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(FrenzyExCharge).where(FrenzyExCharge.bot_id == row.id, FrenzyExCharge.order_id == oid))
        if not ch or ch.user_telegram_id != cb.from_user.id:
            return await cb.answer("این فاکتور پیدا نشد.", show_alert=True)
        if ch.status == "paid":
            return await cb.answer("این فاکتور قبلاً پرداخت شده و قابلِ لغو نیست.", show_alert=True)
        rid = ch.request_id
    # اگر همین لحظه پرداخت شده بود، به‌جایِ لغو شارژ کن (پولِ واریزشده نسوزد)
    ok_paid, _m, credit = await FX.settle_charge(row.id, oid)
    if ok_paid:
        return await cb.answer(f"✅ پرداختت رسیده بود و {credit:,} تومان به کیف پولت اضافه شد.", show_alert=True)
    key = await FX._get(row.id, "frenzyex_api_key", "")
    if key and rid:
        res = await FX.cancel_request(key, rid)
        if res.get("http") == 409:      # پرداخت دریافت شده/در حالِ بررسی ⇒ فاکتور باز می‌ماند و بعداً شارژ می‌شود
            return await cb.answer("ℹ️ پرداختت در حالِ بررسی/دریافت است و قابلِ لغو نیست؛ به‌زودی کیف پولت شارژ می‌شود.",
                                   show_alert=True)
    async with Session() as s:
        ch = await s.scalar(select(FrenzyExCharge).where(FrenzyExCharge.bot_id == row.id,
                                                         FrenzyExCharge.order_id == oid).with_for_update())
        if not ch or ch.status == "paid":
            return await cb.answer("این فاکتور قبلاً پرداخت شده و قابلِ لغو نیست.", show_alert=True)
        ch.status = "cancelled"
        await s.commit()
    await cb.answer("فاکتور لغو شد.")
    from tenant.gateways import inplace as IP
    await IP.show(cb, IP.CANCELLED_TEXT, await inline_back_kb(row.id))
