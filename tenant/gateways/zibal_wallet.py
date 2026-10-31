"""tenant/zibal_wallet.py — درگاهِ زیبال برایِ شارژِ کیف‌پولِ ربات‌هایِ مستأجر.

=== PATCH_ZIBAL_GATEWAY_V1 ===
پورتِ کاملِ tenant/zarinpal_wallet.py برایِ زیبال — همون معماری (فاکتور،
تاییدِ خودکار+دستی، قفلِ کارتِ KYCِ سطح۱، حلقهٔ پس‌زمینه برایِ
فاکتورهایِ بازمونده)، فقط با APIِ زیبال (platform_zibal.py).

تفاوت‌هایِ عمدی با زرین‌پال:
  • زیبال محدودیتِ «تطابقِ دامنهٔ callback» نداره (callbackUrl هر بار
    داخلِ خودِ درخواست فرستاده می‌شه، نه از پیش تویِ پنلِ مرچنت ثبت
    بشه) — برایِ همین بخشِ «دامنهٔ اختصاصی» اینجا نیست.
  • زیبال گزینهٔ استعلامِ کارمزدِ جداگانه (مثلِ فیچرِ fee_on_payer
    زرین‌پال) تویِ مستنداتِ رسمی‌اش نداره — برایِ همین این‌جا اضافه
    نشده تا چیزی ادعا نشه که وجود نداره.
  • هویتِ تراکنش «trackId» است، نه «authority».
"""
from __future__ import annotations

from aiogram import F, Bot, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, update

from config import Session
from models import Bot as BotRow, BotUser, Order, Setting, WalletTxn
import platform_zibal

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

zibal_wallet_router = Router()
zibal_admin_router = Router()

# === PATCH_GATEWAY_OWN_DOMAIN_REQUIRED_V1 ===
# ثابتِ دامنهٔ پیش‌فرض حذف شد: دامنه‌ای که این‌جا بود مالِ این پلتفرم نبود و
# هر مستأجرِ بدونِ دامنه، callbackِ پرداختش به سرورِ شخصِ ثالث می‌رفت.
# حالا هر ربات باید دامنهٔ خودش را ثبت کند (اختصاصیِ تاییدشده یا ورودِ دستی)
# و اگر نکرد، درگاه فعال نمی‌شود. هیچ fallbackی عمداً وجود ندارد.

_PRESETS = (50_000, 100_000, 200_000, 500_000, 1_000_000)


def _rial(value: int | float) -> str:
    return f"{int(value):,} تومان"


class TenantZibalFSM(StatesGroup):
    amount = State()
    merchant = State()
    base = State()
    fee_val = State()      # PATCH_ZIBAL_FEE_V1


async def _setting(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _callback_base(bot_id: int) -> str:
    override = (await _setting(bot_id, "zibal_webhook_base", "")).strip().rstrip("/")
    if override:
        return override
    # === PATCH_GATEWAY_CALLBACK_BASE_V1 ===
    # قبلاً این‌جا مستقیم رویِ ثابتِ هاردکدِ expresspanel.ir می‌افتاد، در
    # حالی‌که get_bot_webhook_base دقیقاً برایِ همین ساخته شده: اول دامنهٔ
    # اختصاصیِ تاییدشدهٔ خودِ ربات، وگرنه دامنهٔ واقعیِ پلتفرم. نتیجهٔ باگ
    # این بود که مستأجرِ دارایِ دامنهٔ اختصاصی، کاربرش بعد از پرداخت به
    # دامنهٔ پلتفرم برمی‌گشت — و اگر پلتفرم روی دامنهٔ دیگری بالا آمده
    # بود، callback اصلاً به جایِ درستی نمی‌رسید.
    # فقط دامنهٔ اختصاصیِ *تاییدشدهٔ خودِ همین ربات*. عمداً از
    # get_bot_webhook_base استفاده نمی‌کنیم چون آن تابع در نبودِ دامنه به
    # دامنهٔ مشترکِ پلتفرم برمی‌گردد و همان چیزی است که نباید فاش شود.
    try:
        from models import BotCustomDomain
        async with Session() as s:
            d = await s.scalar(select(BotCustomDomain).where(
                BotCustomDomain.bot_id == bot_id,
                BotCustomDomain.status == "verified"))
        if d and d.domain:
            return f"https://{d.domain.strip().rstrip('/')}"
    except Exception:                                        # noqa: BLE001
        logger.exception("zibal_wallet: خواندنِ دامنهٔ اختصاصیِ ربات ناموفق بود")
    # === PATCH_GATEWAY_OWN_DOMAIN_REQUIRED_V1 ===
    # عمداً هیچ پیش‌فرضی برنمی‌گردانیم. تا دیروز این‌جا یک دامنهٔ ثابت بود
    # که اصلاً مالِ این پلتفرم نبود؛ یعنی callbackِ پرداختِ هر مستأجری که
    # دامنه نداشت به سرورِ شخصِ ثالث می‌رفت — هم نشتِ اطلاعات، هم وابستگی
    # به سروری که هر لحظه می‌توانست بخوابد و پرداختِ همه را بشکند.
    # رشتهٔ خالی یعنی «هنوز دامنه‌ای ثبت نشده» و صدازننده باید جلویش را
    # بگیرد، نه اینکه لینکِ خراب بسازد.
    return ""


async def _configured(bot_id: int) -> bool:
    # === PATCH_GATEWAY_OWN_DOMAIN_REQUIRED_V1 ===
    # بدونِ دامنه، فاکتور ساخته می‌شود ولی کاربر بعد از پرداخت به هیچ‌جا
    # برنمی‌گردد؛ پس درگاه «پیکربندی‌شده» حساب نمی‌شود.
    return ((await _setting(bot_id, "zibal_enabled", "0")) == "1"
            and bool((await _setting(bot_id, "zibal_merchant_id", "")).strip())
            and bool(await _callback_base(bot_id)))


# === PATCH_ZIBAL_FEE_V1 === «انتقالِ کارمزدِ درگاه به خریدار» (درخواستِ Nexivo)
# receipt_text: «zibal_track=X» یا «zibal_track=X|fee=N». خریدار amount+fee را به درگاه می‌پردازد ولی کیف‌پول فقط
# به‌اندازهٔ amount (مبلغِ اصلی) شارژ می‌شود.
def _zb_parse_receipt(txt: str) -> str:
    return _zb_parse_receipt_fee(txt)[0]


def _zb_parse_receipt_fee(txt: str) -> tuple[str, int]:
    raw = (txt or "").strip()
    fee = 0
    if "|fee=" in raw:
        raw, _, fee_s = raw.partition("|fee=")
        try:
            fee = max(0, int(fee_s.strip()))
        except ValueError:
            fee = 0
    return raw.replace("zibal_track=", "", 1).strip(), fee


def calc_fee(mode: str, val: float, amount: int) -> int:
    """mode: off / pct (درصدِ مبلغ، گردشده به بالا) / fixed (تومانِ ثابت)."""
    try:
        if mode == "pct" and val > 0:
            import math
            return int(math.ceil(int(amount) * float(val) / 100.0))
        if mode == "fixed" and val > 0:
            return int(val)
    except Exception:
        logger.exception("zibal: محاسبهٔ کارمزد ناموفق بود")
    return 0


async def fee_for(bot_id: int, amount: int) -> int:
    mode = (await _setting(bot_id, "zibal_fee_mode", "off")).strip()
    try:
        val = float((await _setting(bot_id, "zibal_fee_val", "0")).strip() or 0)
    except ValueError:
        val = 0.0
    return calc_fee(mode, val, amount)


async def _settle_zibal_order(order_id: int) -> tuple[str, str, int, int | None, int | None, float | None, str | None]:
    """idempotent؛ دقیقاً همون منطقِ _settle_zarinpal_order.

    خروجی: (status, message, amount, bot_id, telegram_id, new_balance, ref_id)
    """
    async with Session() as s:
        order = await s.scalar(select(Order).where(
            Order.id == order_id, Order.pay_method == "zibal_wallet",
        ).with_for_update())
        if not order:
            return "error", "سفارش پیدا نشد.", 0, None, None, None, None
        bot_id = order.bot_id
        if order.status == "paid":
            return "already", "این پرداخت قبلاً تأیید و به کیف پول اضافه شده است.", int(order.amount or 0), bot_id, None, None, None
        track_id, fee = _zb_parse_receipt_fee(order.receipt_text or "")
        if not track_id:
            return "error", "اطلاعاتِ این سفارش ناقص است.", 0, bot_id, None, None, None
        merchant_rec = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "zibal_merchant_id"))
        merchant = (merchant_rec.value if merchant_rec else "").strip()
        if not merchant:
            return "error", "مرچنت‌ایدی این ربات تنظیم نشده است.", 0, bot_id, None, None, None
        amount = int(order.amount or 0)
        ok, ref_or_msg, verified_amount = await platform_zibal.verify_payment(merchant, track_id)
        _card_pan = ""
        try:
            _card_pan = platform_zibal.pop_card_pan(track_id)
        except Exception:
            logger.exception("zibal: خواندنِ شمارهٔ کارت ناموفق بود")
        if not ok:
            return "not_paid", ref_or_msg, amount, bot_id, None, None, None
        if verified_amount:
            # با کارمزدِ خریدار، مبلغِ تأییدشدهٔ درگاه = مبلغِ اصلی + کارمزد؛ کیف‌پول فقط مبلغِ اصلی را می‌گیرد
            amount = verified_amount - fee if fee else verified_amount
            if amount <= 0:
                return "error", "مبلغِ تأییدشدهٔ درگاه با فاکتور نمی‌خواند.", 0, bot_id, None, None, None
        claim = await s.execute(update(Order).where(
            Order.id == order.id, Order.status != "paid").values(status="paid"))
        if claim.rowcount == 0:
            await s.rollback()
            return "already", "این پرداخت قبلاً تأیید شده است.", amount, bot_id, None, None, None
        user = await s.scalar(select(BotUser).where(BotUser.id == order.user_id).with_for_update())
        if not user:
            await s.rollback()
            return "error", "کاربرِ این سفارش پیدا نشد.", amount, bot_id, None, None, None
        _locked = "".join(ch for ch in str(user.kyc_card or "") if ch.isdigit())
        _paid4 = "".join(ch for ch in str(_card_pan or "") if ch.isdigit())[-4:]
        _mismatch = bool(int(user.kyc_level or 0) == 1 and _locked and _paid4
                         and _locked[-4:] != _paid4)
        if _mismatch:
            user.kyc_blocked = True
            user.kyc_note = f"پرداخت با کارتِ غیرِثبت‌شده (…{_paid4})"

        user.balance = float(user.balance or 0) + amount
        s.add(WalletTxn(bot_id=bot_id, user_id=user.id, amount=amount,
                        kind="zibal_charge",
                        note=f"زیبال؛ ref={ref_or_msg}"
                             + (f"؛ card={_card_pan}" if _card_pan else "")))
        telegram_id = user.telegram_id
        new_balance = float(user.balance)
        _kyc_uid = user.id
        await s.commit()
    from tenant.shared.kyc import after_charge as _ac
    await _ac(bot_id, _kyc_uid)
    if _mismatch:
        try:
            await _kyc_mismatch_notify(bot_id, telegram_id, _paid4)
        except Exception:
            logger.exception("kyc: اطلاعِ عدمِ تطابقِ کارت ناموفق بود")
    return ("credited", "شارژ با موفقیت انجام شد.", amount, bot_id,
            telegram_id, new_balance,
            str(ref_or_msg) + (f"|{_card_pan}" if _card_pan else ""))


async def _kyc_mismatch_notify(bot_id: int, telegram_id: int, last4: str) -> None:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
    if not brow:
        return
    tbot = Bot(token=brow.token,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await tbot.send_message(telegram_id, (
            "⚠️ <b>شارژ انجام شد، ولی حسابِ شما محدود شد.</b>\n\n"
            f"این پرداخت با کارتی انجام شد (…{last4}) که با کارتِ ثبت‌شدهٔ "
            "شما یکی نیست.\n\n"
            "مبلغ به کیفِ پولِ شما اضافه شده، اما تا انجامِ احرازِ هویتِ "
            "سطح ۲ امکانِ استفاده از آن نیست.\n"
            "از دکمهٔ «🛡 احرازِ هویت» اقدام کن."))
        try:
            from tenant import _notify_admins
            await _notify_admins(tbot, brow, (
                "🛡 <b>عدمِ تطابقِ کارت</b>\n\n"
                f"کاربر <code>{telegram_id}</code> با کارتِ …{last4} پرداخت "
                "کرد که با کارتِ ثبت‌شده‌اش یکی نیست.\n"
                "شارژ انجام شد و حساب تا احرازِ سطح ۲ مسدود شد."))
        except Exception:
            logger.exception("kyc: اطلاع به ادمین ناموفق بود")
    finally:
        try:
            await tbot.session.close()
        except Exception:
            logger.exception("kyc: بستنِ سشنِ ربات ناموفق بود")


async def _notify_credited(bot_id: int, telegram_id: int, amount: int, new_balance: float,
                           ref_id: str | None = None, order_id: int | None = None) -> None:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
    if not brow:
        return
    tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # === PATCH_INVOICE_MSG_EDIT_V1 === پیامِ فاکتور → «تأیید شد و حسابت شارژ شد» (بدونِ دکمه)
    try:
        from tenant.gateways import invoice_msg
        await invoice_msg.finalize(tbot, bot_id, "zb", order_id, amount, ref_id)
    except Exception:
        logger.exception("zibal: ادیتِ پیامِ فاکتور ناموفق بود")
    try:
        from tenant.gateways.wallet_receipt import build_wallet_charge_message
        text, kb = build_wallet_charge_message(amount, new_balance, "zibal", ref_id=ref_id)
        await tbot.send_message(telegram_id, text, reply_markup=kb)
    except Exception:
        logger.exception("zibal: ارسالِ پیامِ شارژ به کاربر ناموفق بود")
    try:
        from tenant.shared.tx_channel import tx_log
        async with Session() as s2:
            _u = await s2.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        _ref, _, _card = (ref_id or "").partition("|")
        await tx_log(tbot, bot_id, buyer=_u, amount=amount, gateway="zibal",
                     old_balance=(float(new_balance) - float(amount)),
                     new_balance=new_balance, ref=_ref, card=_card)
    except Exception:
        logger.exception("zibal: ثبتِ تراکنش در کانالِ گزارش ناموفق بود")
    try:
        await tbot.session.close()
    except Exception:
        logger.exception("zibal: بستنِ سشنِ ربات ناموفق بود")


_STALE_AFTER_HOURS = 6


async def zibal_poll_once() -> None:
    """همهٔ فاکتورهایِ بازِ شارژِ کیفِ پولِ زیبال (تازه‌تر از چند ساعتِ اخیر)
    را یک دور استعلام می‌کند و در صورتِ پرداختِ موفق، خودکار تسویه می‌کند."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(hours=_STALE_AFTER_HOURS)
    async with Session() as s:
        pend = (await s.scalars(select(Order).where(
            Order.pay_method == "zibal_wallet",
            Order.status == "pending",
            Order.created_at >= cutoff,
        ))).all()
        order_ids = [o.id for o in pend]
    for oid in order_ids:
        try:
            status, _msg, amount, bot_id, telegram_id, new_balance, ref_id = await _settle_zibal_order(oid)
        except Exception:
            continue
        if status == "credited" and bot_id and telegram_id:
            try:
                await _notify_credited(bot_id, telegram_id, amount, new_balance, ref_id, order_id=oid)
            except Exception:
                logger.exception("zibal: اطلاعِ شارژِ دسته‌ای به کاربر ناموفق بود")


def _amount_kb(presets=_PRESETS) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(presets), 2):
        rows.append([InlineKeyboardButton(text=_rial(x), callback_data=f"wzb:amt:{x}", style=ButtonStyle.SUCCESS) for x in presets[i:i+2]])
    rows.append([InlineKeyboardButton(text="✏️ مبلغ دلخواه", callback_data="wzb:custom", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="w:back", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@zibal_wallet_router.callback_query(F.data == "w:zibal")
async def zibal_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or not await _configured(row.id):
        await cb.answer("درگاه زیبال برای این ربات فعال نشده است.", show_alert=True)
        return
    await state.clear()
    # === PATCH_WALLET_LIMITS_V1 ===
    from tenant.gateways import wallet_limits as _wl
    presets = await _wl.allowed_presets(row.id, "zibal", _PRESETS)
    hint = f"\n({await _wl.range_hint(row.id, 'zibal')})" if await _wl.customized(row.id, "zibal") else ""
    await cb.message.answer("💳 مبلغ شارژ کیف پول را انتخاب کنید:" + hint, reply_markup=_amount_kb(presets))
    await cb.answer()


@zibal_wallet_router.callback_query(F.data == "wzb:custom")
async def zibal_custom(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import get_bot_row
    from tenant.gateways import wallet_limits as _wl   # === PATCH_WALLET_LIMITS_V1 ===
    await state.set_state(TenantZibalFSM.amount)
    row = await get_bot_row(bot)
    if row and await _wl.customized(row.id, "zibal"):
        await cb.message.answer(f"مبلغ شارژ را به تومان و به عدد بفرستید ({await _wl.range_hint(row.id, 'zibal')}):")
    else:
        await cb.message.answer("مبلغ شارژ را به تومان و به عدد بفرستید (حداقل ۱۰٬۰۰۰ تومان):")
    await cb.answer()


@zibal_wallet_router.message(TenantZibalFSM.amount, F.text)
async def zibal_custom_amount(msg: Message, state: FSMContext, bot: Bot):
    from tenant import fa_to_int, get_bot_row
    from tenant.gateways import wallet_limits as _wl   # === PATCH_WALLET_LIMITS_V1 ===
    amount = fa_to_int(msg.text)
    row = await get_bot_row(bot)
    if not amount or not row:
        await msg.answer("❌ مبلغ نامعتبر است؛ یک عدد به تومان بفرستید.")
        return
    if not await _wl.guard(msg, row.id, "zibal", amount):
        return
    await state.clear()
    await _create_payment(msg, bot, amount, from_user=msg.from_user)


@zibal_wallet_router.callback_query(F.data.startswith("wzb:amt:"))
async def zibal_fixed_amount(cb: CallbackQuery, bot: Bot, state: FSMContext):
    try:
        amount = int(cb.data.rsplit(":", 1)[1])
    except Exception:
        await cb.answer("مبلغ نامعتبر است.", show_alert=True)
        return
    await state.clear()
    # === PATCH_PAYER_IDENTITY_V1 === (cb.message = پیامِ خودِ ربات؛ پرداخت‌کننده = cb.from_user)
    await _create_payment(cb.message, bot, amount, from_user=cb.from_user)
    await cb.answer()


async def _create_payment(message: Message, bot: Bot, amount: int, from_user=None):
    from tenant import get_bot_row
    # === PATCH_PAYER_IDENTITY_V1 ===
    from tenant.gateways.payer_identity import resolve_payer, PayerIdentityError, PAYER_ERROR_TEXT
    try:
        payer = resolve_payer(message, from_user)
    except PayerIdentityError:
        await message.answer(PAYER_ERROR_TEXT)
        return
    row = await get_bot_row(bot)
    if not row or not await _configured(row.id):
        await message.answer("❌ درگاه زیبال برای این ربات آماده نیست.")
        return
    # === PATCH_WALLET_LIMITS_V1 ===
    from tenant.gateways import wallet_limits as _wl
    if not await _wl.guard(message, row.id, "zibal", amount):
        return
    merchant = (await _setting(row.id, "zibal_merchant_id", "")).strip()
    callback_url = f"{await _callback_base(row.id)}/zibal/tenant/callback"

    bot_label = f"@{row.username}" if row.username else f"#{row.id}"
    description = f"افزایش موجودیِ کیف‌پول کاربر {payer.id} در ربات {bot_label}"   # PATCH_PAYER_IDENTITY_V1

    fee = await fee_for(row.id, amount)          # PATCH_ZIBAL_FEE_V1
    gateway_amount = amount + fee
    ok, result = await platform_zibal.request_payment(
        merchant, gateway_amount, description, callback_url)
    if not ok:
        await message.answer(f"❌ ساخت پرداخت ناموفق بود:\n{result}")
        return
    async with Session() as s:
        user = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == payer.id))
    if not user:
        from tenant import get_or_create_user
        user = await get_or_create_user(
            row.id, payer.id,
            getattr(payer, "full_name", "") or "",
            getattr(payer, "username", "") or "")
        if not user:
            await message.answer("❌ ثبت حساب کاربری انجام نشد؛ لطفاً دوباره تلاش کن.")
            return
    try:
        from tenant.shared.kyc import check_charge
        _ok, _why = await check_charge(row.id, user, amount)
        if not _ok:
            await message.answer(_why)
            return
    except Exception:
        logger.exception("kyc: بررسیِ سقف ناموفق بود (zibal)")

    async with Session() as s:
        order = Order(bot_id=row.id, user_id=user.id, volume_gb=0, days=0, count=1,
                      amount=amount, pay_method="zibal_wallet", status="pending",
                      receipt_text=f"zibal_track={result}" + (f"|fee={fee}" if fee else ""))
        s.add(order)
        await s.commit()
    # === PATCH_GATEWAY_REDIRECT_LINK_V1 ===
    from tenant.gateways.gateway_redirect import startpay_for
    pay_url = await startpay_for(row.id, "zibal", result)
    _inv_msg = await message.answer(
        f"✅ فاکتور شارژ کیف پول ساخته شد.\n\nمبلغ شارژ: <b>{_rial(amount)}</b>\n"
        + (f"کارمزد درگاه: <b>{_rial(fee)}</b>\nمبلغ قابل پرداخت: <b>{_rial(gateway_amount)}</b>\n" if fee else "") + "\n"
        "روی دکمهٔ زیر بزنید و پرداخت را در درگاه زیبال انجام دهید. کیف پول معمولاً خودکار شارژ می‌شود؛ "
        "اگر بعد از پرداخت شارژ نشد، دکمهٔ «✅ بررسی و تأیید پرداخت» را بزنید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔐 پرداخت در درگاه زیبال", url=pay_url, style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="✅ بررسی و تأیید پرداخت", callback_data=f"wzb:verify:{order.id}", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="w:back", style=ButtonStyle.DANGER)],
        ]))
    # === PATCH_INVOICE_MSG_EDIT_V1 === شناسهٔ پیامِ فاکتور را نگه می‌داریم تا بعد از تأیید ادیت شود
    try:
        from tenant.gateways import invoice_msg
        await invoice_msg.remember(row.id, "zb", order.id, _inv_msg.chat.id, _inv_msg.message_id)
    except Exception:
        logger.exception("zibal: ذخیرهٔ پیامِ فاکتور ناموفق بود")


@zibal_wallet_router.callback_query(F.data.startswith("wzb:verify:"))
async def zibal_manual_verify(cb: CallbackQuery):
    try:
        order_id = int(cb.data.rsplit(":", 1)[1])
    except Exception:
        await cb.answer("شناسهٔ فاکتور نامعتبر است.", show_alert=True)
        return
    await cb.answer("در حال بررسیِ وضعیتِ پرداخت...", show_alert=False)
    status, msg, amount, bot_id, telegram_id, new_balance, ref_id = await _settle_zibal_order(order_id)
    if status == "credited":
        # === PATCH_INVOICE_MSG_EDIT_V1 === به‌جایِ پیامِ جدید، همان پیامِ فاکتور ادیت می‌شود
        from tenant.gateways import invoice_msg
        if not await invoice_msg.edit_message_confirmed(cb.message, amount, ref_id):
            await cb.message.answer(
                f"✅ پرداخت تأیید شد و <b>{_rial(amount)}</b> به کیف پولت اضافه شد.\n"
                + (f"🔢 کد پیگیریِ تراکنش: <code>{ref_id}</code>" if ref_id else ""))
        if bot_id and telegram_id:
            try:
                await _notify_credited(bot_id, telegram_id, amount, new_balance, ref_id, order_id=order_id)
            except Exception:
                logger.exception("zibal: اطلاعِ شارژ (بررسیِ دستی) به کاربر ناموفق بود")
    elif status == "already":
        # === PATCH_INVOICE_MSG_EDIT_V1 === فاکتورِ قدیمی/تکراری: دکمه‌ها برداشته می‌شود
        from tenant.gateways import invoice_msg
        if not await invoice_msg.edit_message_confirmed(cb.message, amount, already=True):
            await cb.message.answer("✅ این فاکتور قبلاً تأیید و به کیف پول اضافه شده است.")
    elif status == "not_paid":
        await cb.message.answer(
            "⏳ هنوز طبقِ استعلام از زیبال، پرداختی برای این فاکتور تأیید نشده.\n"
            f"جزئیات: <code>{msg}</code>\n\n"
            "اگر همین الان پرداخت کردی، چند لحظه صبر کن و دوباره همین دکمه را بزن.")
    else:
        await cb.message.answer(f"❌ بررسی ناموفق بود: {msg}")


async def _fee_btn_text(bot_id: int) -> str:
    mode = (await _setting(bot_id, "zibal_fee_mode", "off")).strip()
    val = (await _setting(bot_id, "zibal_fee_val", "0")).strip()
    if mode == "pct" and val not in ("", "0"):
        return f"💸 کارمزد با خریدار: {val}٪ 🟢"
    if mode == "fixed" and val not in ("", "0"):
        return f"💸 کارمزد با خریدار: {val} تومان 🟢"
    return "💸 کارمزد با خریدار: خاموش ⚪️"


async def zibal_admin_text(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await _setting(bot_id, "zibal_enabled", "0") == "1"
    merchant = await _setting(bot_id, "zibal_merchant_id", "")
    base_override = (await _setting(bot_id, "zibal_webhook_base", "")).strip().rstrip("/")
    _base = await _callback_base(bot_id)
    text = ("🔐 <b>درگاه زیبال ربات</b>\n\n"
            f"وضعیت درگاه: {'✅ فعال' if enabled else '❌ غیرفعال'}\n"
            f"مرچنت‌ایدی: {'✅ ثبت شده' if merchant else '⚠️ ثبت نشده'}\n"
            # === PATCH_GATEWAY_OWN_DOMAIN_REQUIRED_V1 ===
            f"دامنهٔ callback: {_base or '⛔ ثبت نشده'}")
    # === PATCH_GATEWAY_REDIRECT_LINK_V1 ===
    from tenant.gateways.gateway_redirect import status_line as _gwr_status, menu_button as _gwr_btn
    text += "\n" + await _gwr_status(bot_id, "zibal")
    if not _base:
        text += (
            "\n\n⛔ <b>تا دامنه ثبت نشود این درگاه کار نمی‌کند.</b>\n\n"
            "زیبال بعد از پرداخت، کاربر را به دامنهٔ تو برمی‌گرداند؛ بدونِ آن "
            "پرداخت تأیید نمی‌شود و پولِ مشتری معلق می‌ماند.\n\n"
            "<b>دو راه داری:</b>\n"
            "• دامنهٔ اختصاصیِ رباتت را ثبت و تأیید کن (توصیه‌شده) — بعد از آن "
            "این‌جا خودکار پر می‌شود.\n"
            "• یا با دکمهٔ «🌐 ثبت/ویرایشِ دامنهٔ callback» دامنهٔ خودت را "
            "دستی وارد کن.\n\n"
            "<i>⚠️ دامنه باید مالِ خودت باشد و به همین ربات وصل باشد.</i>")
    rows = [
        [InlineKeyboardButton(text="🔴 غیرفعال کن" if enabled else "🟢 فعال کن", callback_data="zbt:toggle", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="✏️ ثبت / ویرایش مرچنت‌ایدی", callback_data="zbt:merchant", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🌐 ثبت/ویرایشِ دامنهٔ callback", callback_data="zbt:base", style=ButtonStyle.PRIMARY)],
        _gwr_btn("zibal"),  # PATCH_GATEWAY_REDIRECT_LINK_V1
        [InlineKeyboardButton(text=await _fee_btn_text(bot_id), callback_data="zbt:fee", style=ButtonStyle.PRIMARY)],
    ]
    if enabled and merchant.strip() and _base:
        rows.append([InlineKeyboardButton(text="🧪 تست اتصال زیبال", callback_data="zbt:test", style=ButtonStyle.SUCCESS)])
    rows.extend([
        [InlineKeyboardButton(text="📖 راهنمای اتصال زیبال", callback_data="zbt:guide", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, kb


@zibal_admin_router.callback_query(F.data == "set:zibal")
async def zibal_admin_start(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    text, kb = await zibal_admin_text(row.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@zibal_admin_router.callback_query(F.data == "zbt:guide")
async def zibal_admin_guide(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    text = ("📖 <b>راهنمای اتصال زیبال</b>\n\n"
            "۱) Merchant ID همین ربات را از پنل زیبال دریافت کنید.\n"
            "۲) از گزینهٔ «✏️ ثبت / ویرایش مرچنت‌ایدی» همان کد را ذخیره کنید.\n"
            "۳) درگاه را فعال کنید و «🧪 تست اتصال زیبال» را بزنید.\n\n"
            "برخلافِ زرین‌پال، زیبال نیازی به ثبتِ از پیشِ دامنهٔ callback در پنلِ "
            "مرچنت ندارد؛ دامنهٔ پیش‌فرضِ بات‌ساز معمولاً بدونِ تنظیمِ اضافه کار می‌کند. "
            "فقط اگه از دامنهٔ اختصاصیِ خودتون استفاده می‌کنید، از «🌐 ثبت/ویرایشِ "
            "دامنهٔ callback» همون رو وارد کنید.\n\n"
            "رمز کارت یا اطلاعات بانکی را در ربات وارد نکنید.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت به تنظیمات زیبال", callback_data="set:zibal", style=ButtonStyle.DANGER)],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@zibal_admin_router.callback_query(F.data == "zbt:test")
async def zibal_admin_test(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    merchant = (await _setting(row.id, "zibal_merchant_id", "")).strip()
    enabled = await _setting(row.id, "zibal_enabled", "0") == "1"
    if not enabled or not merchant:
        await cb.answer("ابتدا مرچنت‌ایدی را ثبت و درگاه را فعال کنید.", show_alert=True)
        return
    callback_url = f"{await _callback_base(row.id)}/zibal/tenant/callback"
    await cb.answer("در حال بررسی اتصال زیبال...", show_alert=False)
    bot_label = f"@{row.username}" if row.username else f"#{row.id}"
    ok, result = await platform_zibal.request_payment(
        merchant, 10_000,
        f"تستِ اتصالِ درگاهِ زیبال — ربات {bot_label} (لطفاً پرداخت نکنید)",
        callback_url,
    )
    if ok:
        # === PATCH_GATEWAY_REDIRECT_LINK_V1 === لینکِ صفحهٔ پرداخت هم برایِ تست
        from tenant.gateways.gateway_redirect import startpay_for
        test_url = await startpay_for(row.id, "zibal", str(result))
        await cb.message.answer(
            "✅ <b>اتصال زیبال با موفقیت تأیید شد.</b>\n\n"
            "مرچنت‌ایدی و مسیر ارتباط با زیبال پاسخ صحیح دادند.\n\n"
            "👇 دکمهٔ زیر را باز کن تا مطمئن شوی <b>صفحهٔ پرداخت</b> هم باز می‌شود. "
            "اگر خطای دامنه/ارجاع‌دهنده دیدی، «🔗 لینک ریدایرکت» را تنظیم کن.\n\n"
            "⚠️ این فاکتورِ آزمایشی را پرداخت نکن.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔍 بازکردنِ صفحهٔ پرداختِ آزمایشی", url=test_url)]]),
        )
    else:
        await cb.message.answer(
            "❌ <b>تست اتصال زیبال ناموفق بود.</b>\n\n"
            f"جزئیات: <code>{result}</code>\n\n"
            "مرچنت‌ایدی و فعال‌بودنِ درگاه را بررسی کنید.",
            parse_mode="HTML",
        )


@zibal_admin_router.callback_query(F.data == "zbt:base")
async def zibal_admin_base(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(TenantZibalFSM.base)
    await cb.message.answer(
        "🌐 آدرسِ دامنهٔ اختصاصیِ callback را با https:// بفرست (برایِ برگشت به پیش‌فرض «/default» بفرست).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="zbt:base:cancel", style=ButtonStyle.DANGER)],
        ]))
    await cb.answer()


@zibal_admin_router.callback_query(F.data == "zbt:base:cancel")
async def zibal_admin_base_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    text, kb = await zibal_admin_text(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer("لغو شد")


_ZIBAL_BASE_NAV_TEXTS = {"🏠 بازگشت به خانه", "⬅️ بازگشت", "لغو", "/cancel"}


@zibal_admin_router.message(TenantZibalFSM.base, F.text)
async def zibal_admin_base_save(msg: Message, state: FSMContext, bot: Bot):
    from urllib.parse import urlparse
    from tenant import get_bot_row, set_setting
    row = await get_bot_row(bot)
    value = (msg.text or "").strip().rstrip("/")
    if not row:
        await state.clear()
        return
    if value in _ZIBAL_BASE_NAV_TEXTS:
        await state.clear()
        text, kb = await zibal_admin_text(row.id)
        await msg.answer("لغو شد.\n\n" + text, reply_markup=kb)
        return
    if value == "/default":
        value = ""
    else:
        parsed = urlparse(value)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(ch.isspace() for ch in value)):
            await msg.answer("❌ آدرس نامعتبر است. فقط یک آدرس HTTPS مانند https://pay.example.ir ارسال کنید.")
            return
    await set_setting(row.id, "zibal_webhook_base", value)
    await state.clear()
    text, kb = await zibal_admin_text(row.id)
    await msg.answer(
        ("✅ دامنهٔ اختصاصی ذخیره شد.\n\n" if value else "✅ برگشت به دامنهٔ پیش‌فرضِ مرکزی.\n\n") + text,
        reply_markup=kb)


@zibal_admin_router.callback_query(F.data == "zbt:toggle")
async def zibal_admin_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    current = await _setting(row.id, "zibal_enabled", "0")
    await set_setting(row.id, "zibal_enabled", "0" if current == "1" else "1")
    text, kb = await zibal_admin_text(row.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("✅ ذخیره شد")


@zibal_admin_router.callback_query(F.data == "zbt:merchant")
async def zibal_admin_merchant(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(TenantZibalFSM.merchant)
    await cb.message.answer("مرچنت‌ایدی زیبال همین ربات را ارسال کنید:")
    await cb.answer()


@zibal_admin_router.message(TenantZibalFSM.merchant, F.text)
async def zibal_admin_merchant_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting
    row = await get_bot_row(bot)
    value = (msg.text or "").strip()
    if not row or len(value) < 6 or len(value) > 64:
        await msg.answer("❌ مرچنت‌ایدی نامعتبر است.")
        return
    await set_setting(row.id, "zibal_merchant_id", value)
    await state.clear()
    text, kb = await zibal_admin_text(row.id)
    await msg.answer("✅ مرچنت‌ایدی ذخیره شد.\n\n" + text, reply_markup=kb)


# === PATCH_ZIBAL_FEE_V1 === تنظیمِ «انتقالِ کارمزد به خریدار»
def _fee_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 کارمزد درصدی", callback_data="zbt:feem:pct", style=ButtonStyle.PRIMARY),
         InlineKeyboardButton(text="💵 مبلغ ثابت", callback_data="zbt:feem:fixed", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⚪️ خاموش (کارمزد با من)", callback_data="zbt:feem:off", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="set:zibal", style=ButtonStyle.DANGER)],
    ])


@zibal_admin_router.callback_query(F.data == "zbt:fee")
async def zibal_admin_fee(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await cb.message.edit_text(
        "💸 <b>انتقالِ کارمزدِ درگاه به خریدار</b>\n\n"
        f"وضعیتِ فعلی: {await _fee_btn_text(row.id)}\n\n"
        "اگر روشن باشد، موقعِ شارژ کارمزد به مبلغ اضافه می‌شود و خریدار «مبلغِ شارژ + کارمزد» را می‌پردازد؛ "
        "ولی کیف‌پولش دقیقاً به‌اندازهٔ مبلغِ اصلی شارژ می‌شود و کارمزد از سهمِ تو کم نمی‌شود.\n\n"
        "• <b>درصدی</b>: مثلاً ۲ یعنی ۲٪ مبلغِ شارژ (به بالا گرد می‌شود)\n"
        "• <b>مبلغ ثابت</b>: مثلاً ۲٬۰۰۰ تومان برای هر شارژ",
        reply_markup=_fee_menu_kb())
    await cb.answer()


@zibal_admin_router.callback_query(F.data.startswith("zbt:feem:"))
async def zibal_admin_fee_mode(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    mode = cb.data.rsplit(":", 1)[1]
    if mode == "off":
        await set_setting(row.id, "zibal_fee_mode", "off")
        await set_setting(row.id, "zibal_fee_val", "0")
        await cb.answer("کارمزد خاموش شد")
        text, kb = await zibal_admin_text(row.id)
        await cb.message.edit_text(text, reply_markup=kb)
        return
    if mode not in ("pct", "fixed"):
        await cb.answer()
        return
    await state.set_state(TenantZibalFSM.fee_val)
    await state.update_data(fee_mode=mode)
    await cb.message.answer("درصدِ کارمزد را بفرست (مثلاً <code>2</code> یا <code>1.5</code>؛ حداکثر ۲۰):" if mode == "pct"
                            else "مبلغِ ثابتِ کارمزد را به تومان بفرست (مثلاً <code>2000</code>؛ حداکثر ۵۰٬۰۰۰):",
                            parse_mode="HTML")
    await cb.answer()


@zibal_admin_router.message(TenantZibalFSM.fee_val, F.text)
async def zibal_admin_fee_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting
    row = await get_bot_row(bot)
    if not row:
        return
    mode = (await state.get_data()).get("fee_mode", "pct")
    _tr = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    raw = (msg.text or "").translate(_tr).strip().replace("٫", ".").replace("،", "").replace(",", "")
    try:
        val = float(raw)
    except ValueError:
        val = 0.0
    lim = 20 if mode == "pct" else 50_000
    if not (0 < val <= lim):
        await msg.answer(f"❌ عددِ نامعتبر (بین ۰ و {lim:,}).")
        return
    await set_setting(row.id, "zibal_fee_mode", mode)
    await set_setting(row.id, "zibal_fee_val", (f"{val:g}" if mode == "pct" else str(int(val))))
    await state.clear()
    text, kb = await zibal_admin_text(row.id)
    await msg.answer("✅ کارمزدِ خریدار ذخیره شد.\n\n" + text, reply_markup=kb)
