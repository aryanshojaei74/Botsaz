from __future__ import annotations
from urllib.parse import urlparse

from aiogram import F, Bot, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, update

from config import Session
from models import Bot as BotRow, BotUser, Order, Setting, WalletTxn
import platform_zarinpal

# === PATCH_ZARINPAL_WALLET_MISSING_LOGGER_V1 ===
# فایل در چند جا (kyc mismatch / اطلاعِ ادمین) از logger.exception استفاده
# می‌کرد بدونِ اینکه logger اصلاً import یا تعریف شده باشه — دقیقاً همون‌جایی
# که پرداخت charge شده ولی اطلاع‌رسانی fail می‌کنه، به‌جایِ لاگِ خطای واقعی یک
# NameError جدید می‌ترکید.
from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

zarinpal_wallet_router = Router()
zarinpal_admin_router = Router()

# === PATCH_ZARINPAL_HARDCODED_CALLBACK_DOMAIN_V1 ===
# قبلاً از WEBHOOK_BASE (که از env سرور خونده می‌شه) استفاده می‌کردیم؛ ولی اون
# env روی سرور خالی/متفاوت بود و زرین‌پال با خطای «دامنهٔ callback با دامنهٔ
# ثبت‌شدهٔ پذیرنده مطابقت ندارد» رد می‌کرد. این همون دامنه‌ایه که با
# test_zarinpal.py تستش کردیم و زرین‌پال قبولش کرد؛ برای این‌که دیگه به
# تنظیمِ env وابسته نباشه، مستقیم این‌جا ثابت شده.
# === PATCH_GATEWAY_OWN_DOMAIN_REQUIRED_V1 ===
# ثابتِ دامنهٔ پیش‌فرض حذف شد: دامنه‌ای که این‌جا بود مالِ این پلتفرم نبود و
# هر مستأجرِ بدونِ دامنه، callbackِ پرداختش به سرورِ شخصِ ثالث می‌رفت.
# حالا هر ربات باید دامنهٔ خودش را ثبت کند (اختصاصیِ تاییدشده یا ورودِ دستی)
# و اگر نکرد، درگاه فعال نمی‌شود. هیچ fallbackی عمداً وجود ندارد.

_PRESETS = (50_000, 100_000, 200_000, 500_000, 1_000_000)


def _rial(value: int | float) -> str:
    return f"{int(value):,} تومان"


class TenantZarinpalFSM(StatesGroup):
    amount = State()
    merchant = State()
    base = State()


async def _setting(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


# === PATCH_ZARINPAL_PER_BOT_DOMAIN_OVERRIDE_V1 ===
# فرض اولیه این بود که مرچنت‌ایدیِ همهٔ ربات‌های مستأجر روی یک دامنهٔ
# مرکزی (expresspanel.ir) توی پنلِ زرین‌پال ثبت شده — برای بیشترشون درست
# بود، ولی معلوم شد بعضی صاحبانِ ربات مرچنتِ خودشون را با دامنهٔ اختصاصیِ
# خودشان ثبت کرده‌اند. برای آن‌ها callback روی دامنهٔ مرکزی همیشه با خطای
# «دامنهٔ callback با دامنهٔ ثبت‌شدهٔ پذیرنده مطابقت ندارد» رد می‌شود.
# راه‌حل: دامنهٔ مرکزی هنوز پیش‌فرض است (اکثریت نیازی به تنظیم ندارند)،
# ولی هر ربات می‌تواند override اش کند.
async def _callback_base(bot_id: int) -> str:
    override = (await _setting(bot_id, "zarinpal_webhook_base", "")).strip().rstrip("/")
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
        logger.exception("zarinpal_wallet: خواندنِ دامنهٔ اختصاصیِ ربات ناموفق بود")
    # === PATCH_GATEWAY_OWN_DOMAIN_REQUIRED_V1 ===
    # عمداً هیچ پیش‌فرضی برنمی‌گردانیم. تا دیروز این‌جا یک دامنهٔ ثابت بود
    # که اصلاً مالِ این پلتفرم نبود؛ یعنی callbackِ پرداختِ هر مستأجری که
    # دامنه نداشت به سرورِ شخصِ ثالث می‌رفت — هم نشتِ اطلاعات، هم وابستگی
    # به سروری که هر لحظه می‌توانست بخوابد و پرداختِ همه را بشکند.
    # رشتهٔ خالی یعنی «هنوز دامنه‌ای ثبت نشده» و صدازننده باید جلویش را
    # بگیرد، نه اینکه لینکِ خراب بسازد.
    return ""


# === PATCH_GATEWAY_REDIRECT_LINK_V1 === (منطق در tenant/gateway_redirect.py)
async def startpay_url_for_bot(bot_id: int, authority: str) -> str:
    from tenant.gateways.gateway_redirect import startpay_for
    return await startpay_for(bot_id, "zarinpal", authority)


async def _configured(bot_id: int) -> bool:
    # === PATCH_GATEWAY_OWN_DOMAIN_REQUIRED_V1 ===
    # بدونِ دامنه، فاکتور ساخته می‌شود ولی کاربر بعد از پرداخت به هیچ‌جا
    # برنمی‌گردد؛ پس درگاه «پیکربندی‌شده» حساب نمی‌شود.
    return ((await _setting(bot_id, "zarinpal_enabled", "0")) == "1"
            and bool((await _setting(bot_id, "zarinpal_merchant_id", "")).strip())
            and bool(await _callback_base(bot_id)))


# === PATCH_ZARINPAL_MANUAL_VERIFY_V1 ===
# نسخهٔ محلیِ همون پارسِ receipt_text که main_tenant.py تو کالبکِ HTTP داره
# (زرین‌پال؛authority=X یا ...|fee=N). این‌جا هم لازمش داریم چون این فایل
# نباید main_tenant.py رو ایمپورت کنه (تا سرویسِ تننت مستقل بمونه).
def _zp_parse_receipt(txt: str) -> tuple[str, int]:
    raw = (txt or "").strip()
    fee = 0
    if "|fee=" in raw:
        raw, _, fee_s = raw.partition("|fee=")
        try:
            fee = max(0, int(fee_s.strip()))
        except ValueError:
            fee = 0
    return raw.replace("zarinpal_authority=", "", 1).strip(), fee


async def _settle_zarinpal_order(order_id: int) -> tuple[str, str, int, int | None, int | None, float | None, str | None]:
    """تلاش برای استعلام و شارژِ یک سفارشِ زرین‌پالِ کیفِ پول از رویِ id.

    idempotent است — صدا زدنِ چندباره روی سفارشی که همین الان شارژ شده مشکلی
    ایجاد نمی‌کند (وضعیتِ paid را دوباره چک می‌کند). هم دکمهٔ «تأیید پرداخت»
    داخل ربات و هم حلقهٔ پس‌زمینه از همین یک تابع استفاده می‌کنند تا منطق
    دو جا تکرار/دچار واگرایی نشود.

    خروجی: (status, message, amount, bot_id, telegram_id, new_balance, ref_id)
      status: "credited" (تازه شارژ شد؛ ref_id کدِ پیگیریِ زرین‌پال است) /
              "already" (قبلاً شارژ شده بود) /
              "not_paid" (طبقِ استعلامِ زرین‌پال هنوز تأیید نشده) /
              "error" (سفارش/تنظیمات نامعتبر است)
    """
    async with Session() as s:
        order = await s.scalar(select(Order).where(
            Order.id == order_id, Order.pay_method == "zarinpal_wallet",
        ).with_for_update())
        if not order:
            return "error", "سفارش پیدا نشد.", 0, None, None, None, None
        bot_id = order.bot_id
        if order.status == "paid":
            return "already", "این پرداخت قبلاً تأیید و به کیف پول اضافه شده است.", int(order.amount or 0), bot_id, None, None, None
        authority, fee = _zp_parse_receipt(order.receipt_text or "")
        if not authority:
            return "error", "اطلاعاتِ این سفارش ناقص است.", 0, bot_id, None, None, None
        merchant_rec = await s.scalar(select(Setting).where(
            Setting.bot_id == bot_id, Setting.key == "zarinpal_merchant_id"))
        merchant = (merchant_rec.value if merchant_rec else "").strip()
        if not merchant:
            return "error", "مرچنت‌ایدی این ربات تنظیم نشده است.", 0, bot_id, None, None, None
        amount = int(order.amount or 0)
        gateway_amount = amount + fee
        ok, ref_or_msg = await platform_zarinpal.verify_payment(merchant, gateway_amount, authority)
        # === PATCH_ZP_CARD_PAN_V1 ===
        _card_pan = ""
        try:
            _card_pan = platform_zarinpal.pop_card_pan(authority)
        except Exception:
            logger.exception("zarinpal: خواندنِ شمارهٔ کارت ناموفق بود")
        if not ok:
            return "not_paid", ref_or_msg, amount, bot_id, None, None, None
        claim = await s.execute(update(Order).where(
            Order.id == order.id, Order.status != "paid").values(status="paid"))
        if claim.rowcount == 0:
            await s.rollback()
            return "already", "این پرداخت قبلاً تأیید شده است.", amount, bot_id, None, None, None
        user = await s.scalar(select(BotUser).where(BotUser.id == order.user_id).with_for_update())
        if not user:
            await s.rollback()
            return "error", "کاربرِ این سفارش پیدا نشد.", amount, bot_id, None, None, None
        # === PATCH_KYC_CARD_LOCK_V1 ===
        # کاربرِ سطح ۱ فقط با همان کارتِ ثبت‌شده مجاز است. اگر کارتِ دیگری
        # بود، شارژ *انجام می‌شود* (پول کاربر حیف نشود و عودتِ دستی لازم
        # نباشد) ولی حساب مسدود می‌شود تا احرازِ سطح ۲ را انجام دهد.
        _locked = "".join(ch for ch in str(user.kyc_card or "") if ch.isdigit())
        _paid4 = "".join(ch for ch in str(_card_pan or "") if ch.isdigit())[-4:]
        _mismatch = bool(int(user.kyc_level or 0) == 1 and _locked and _paid4
                         and _locked[-4:] != _paid4)
        if _mismatch:
            user.kyc_blocked = True
            user.kyc_note = f"پرداخت با کارتِ غیرِثبت‌شده (…{_paid4})"

        user.balance = float(user.balance or 0) + amount
        s.add(WalletTxn(bot_id=bot_id, user_id=user.id, amount=amount,
                        kind="zarinpal_charge",
                        note=f"زرین‌پال؛ ref_id={ref_or_msg}"
                             + (f"؛ card={_card_pan}" if _card_pan else "")))
        telegram_id = user.telegram_id
        new_balance = float(user.balance)
        _kyc_uid = user.id
        await s.commit()
    # === PATCH_KYC_GATE_ALL_PATHS_V1 ===
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


# === PATCH_KYC_CARD_LOCK_V1 ===
async def _kyc_mismatch_notify(bot_id: int, telegram_id: int, last4: str) -> None:
    """به کاربر و ادمینها خبر می‌دهد که چرا حساب محدود شد."""
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
        await invoice_msg.finalize(tbot, bot_id, "zp", order_id, amount, ref_id)
    except Exception:
        logger.exception("zarinpal: ادیتِ پیامِ فاکتور ناموفق بود")
    try:
        from tenant.gateways.wallet_receipt import build_wallet_charge_message
        text, kb = build_wallet_charge_message(amount, new_balance, "zarinpal", ref_id=ref_id)
        await tbot.send_message(telegram_id, text, reply_markup=kb)
    except Exception:
        logger.exception("zarinpal: ارسالِ پیامِ شارژ به کاربر ناموفق بود")
    # === PATCH_TX_LOG_CHANNEL_V1 ===
    try:
        from tenant.shared.tx_channel import tx_log
        async with Session() as s2:
            _u = await s2.scalar(select(BotUser).where(
                BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        _ref, _, _card = (ref_id or "").partition("|")
        await tx_log(tbot, bot_id, buyer=_u, amount=amount, gateway="zarinpal",
                     old_balance=(float(new_balance) - float(amount)),
                     new_balance=new_balance, ref=_ref, card=_card)
    except Exception:
        logger.exception("zarinpal: ثبتِ تراکنش در کانالِ گزارش ناموفق بود")
    try:
        await tbot.session.close()
    except Exception:
        logger.exception("zarinpal: بستنِ سشنِ ربات ناموفق بود")


# === PATCH_ZARINPAL_PENDING_POLL_V1 ===
_STALE_AFTER_HOURS = 6


async def zarinpal_poll_once() -> None:
    """همهٔ فاکتورهایِ بازِ شارژِ کیفِ پولِ زرین‌پال (تازه‌تر از چند ساعتِ اخیر)
    را یک دور استعلام می‌کند و در صورتِ پرداختِ موفق، خودکار تسویه می‌کند.

    فاکتورهایِ قدیمی‌تر دیگر پیگیری نمی‌شوند (نه رد می‌شوند، فقط دیگر پول
    را برای هرکدام هدر نمی‌دهیم)؛ کاربر همیشه می‌تواند از دکمهٔ «تأیید
    پرداخت» زیرِ همان فاکتور دستی هم امتحان کند.
    """
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(hours=_STALE_AFTER_HOURS)
    async with Session() as s:
        pend = (await s.scalars(select(Order).where(
            Order.pay_method == "zarinpal_wallet",
            Order.status == "pending",
            Order.created_at >= cutoff,
        ))).all()
        order_ids = [o.id for o in pend]
    for oid in order_ids:
        try:
            status, _msg, amount, bot_id, telegram_id, new_balance, ref_id = await _settle_zarinpal_order(oid)
        except Exception:
            continue
        if status == "credited" and bot_id and telegram_id:
            try:
                await _notify_credited(bot_id, telegram_id, amount, new_balance, ref_id, order_id=oid)
            except Exception:
                logger.exception("zarinpal: اطلاعِ شارژِ دسته‌ای به کاربر ناموفق بود")


def _amount_kb(presets=_PRESETS) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(presets), 2):
        rows.append([InlineKeyboardButton(text=_rial(x), callback_data=f"wzp:amt:{x}", style=ButtonStyle.SUCCESS) for x in presets[i:i+2]])
    rows.append([InlineKeyboardButton(text="✏️ مبلغ دلخواه", callback_data="wzp:custom", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="w:back", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@zarinpal_wallet_router.callback_query(F.data == "w:zarinpal")
async def zarinpal_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or not await _configured(row.id):
        await cb.answer("درگاه زرین‌پال برای این ربات فعال نشده است.", show_alert=True)
        return
    await state.clear()
    # === PATCH_WALLET_LIMITS_V1 === دکمه‌هایِ خارج از بازهٔ مجاز اصلاً نشان داده نمی‌شوند
    from tenant.gateways import wallet_limits as _wl
    presets = await _wl.allowed_presets(row.id, "zarinpal", _PRESETS)
    hint = f"\n({await _wl.range_hint(row.id, 'zarinpal')})" if await _wl.customized(row.id, "zarinpal") else ""
    await cb.message.answer("💳 مبلغ شارژ کیف پول را انتخاب کنید:" + hint, reply_markup=_amount_kb(presets))
    await cb.answer()


@zarinpal_wallet_router.callback_query(F.data == "wzp:custom")
async def zarinpal_custom(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant import get_bot_row
    from tenant.gateways import wallet_limits as _wl   # === PATCH_WALLET_LIMITS_V1 ===
    await state.set_state(TenantZarinpalFSM.amount)
    row = await get_bot_row(bot)
    if row and await _wl.customized(row.id, "zarinpal"):
        await cb.message.answer(f"مبلغ شارژ را به تومان و به عدد بفرستید ({await _wl.range_hint(row.id, 'zarinpal')}):")
    else:
        await cb.message.answer("مبلغ شارژ را به تومان و به عدد بفرستید (حداقل ۲۰٬۰۰۰ تومان):")
    await cb.answer()


@zarinpal_wallet_router.message(TenantZarinpalFSM.amount, F.text)
async def zarinpal_custom_amount(msg: Message, state: FSMContext, bot: Bot):
    from tenant import fa_to_int, get_bot_row
    from tenant.gateways import wallet_limits as _wl   # === PATCH_WALLET_LIMITS_V1 ===
    # === MARKER_ZARINPAL_MINAMOUNT_V1 ===
    amount = fa_to_int(msg.text)
    row = await get_bot_row(bot)
    if not amount or not row:
        await msg.answer("❌ مبلغ نامعتبر است؛ یک عدد به تومان بفرستید.")
        return
    if not await _wl.guard(msg, row.id, "zarinpal", amount):
        return
    await state.clear()
    await _create_payment(msg, bot, amount, from_user=msg.from_user)


@zarinpal_wallet_router.callback_query(F.data.startswith("wzp:amt:"))
async def zarinpal_fixed_amount(cb: CallbackQuery, bot: Bot, state: FSMContext):
    try:
        amount = int(cb.data.rsplit(":", 1)[1])
    except Exception:
        await cb.answer("مبلغ نامعتبر است.", show_alert=True)
        return
    await state.clear()
    # === PATCH_PAYER_IDENTITY_V1 ===
    # cb.message پیامِ خودِ *ربات* است (from_user = ربات)؛ پرداخت‌کننده کسی است که دکمه را زده.
    await _create_payment(cb.message, bot, amount, from_user=cb.from_user)
    await cb.answer()


async def _create_payment(message: Message, bot: Bot, amount: int, from_user=None):
    from tenant import get_bot_row
    # === PATCH_PAYER_IDENTITY_V1 === پرداخت‌کنندهٔ واقعی؛ اگر ربات/نامشخص بود پیش از هر کاری متوقف می‌شود
    from tenant.gateways.payer_identity import resolve_payer, PayerIdentityError, PAYER_ERROR_TEXT
    try:
        payer = resolve_payer(message, from_user)
    except PayerIdentityError:
        await message.answer(PAYER_ERROR_TEXT)
        return
    row = await get_bot_row(bot)
    if not row or not await _configured(row.id):
        await message.answer("❌ درگاه زرین‌پال برای این ربات آماده نیست.")
        return
    # === PATCH_WALLET_LIMITS_V1 === نقطهٔ مشترکِ دکمهٔ مبلغِ ثابت و مبلغِ دلخواه (و دکمهٔ قدیمیِ باقی‌مانده در چت)
    from tenant.gateways import wallet_limits as _wl
    if not await _wl.guard(message, row.id, "zarinpal", amount):
        return
    merchant = (await _setting(row.id, "zarinpal_merchant_id", "")).strip()
    # === PATCH_ZARINPAL_PER_BOT_DOMAIN_OVERRIDE_V1 ===
    callback_url = f"{await _callback_base(row.id)}/zarinpal/tenant/callback"

    # === PATCH_ZARINPAL_FEE_V1 ===
    # اگر مالکِ ربات «کارمزد با پرداخت‌کننده» را روشن کرده باشد، کارمزد را
    # از خودِ زرین‌پال استعلام می‌کنیم و به مبلغِ درگاه اضافه می‌کنیم؛ ولی
    # کیفِ پول همان مبلغِ اصلی شارژ می‌شود، نه مبلغِ با کارمزد.
    fee = 0
    if (await _setting(row.id, "zarinpal_fee_on_payer", "0")) == "1":
        ok_fee, fee = await platform_zarinpal.calc_fee(merchant, amount)
        if not ok_fee:
            fee = 0   # استعلام نشد؛ خرید نباید متوقف شود
    gateway_amount = amount + fee

    # === PATCH_ZARINPAL_INVOICE_DESCRIPTION_V1 ===
    # قبلاً توضیحاتِ فاکتور «شارژ کیف پول کاربر {عددی} در ربات {عددی}» بود —
    # دو تا شناسهٔ خامِ عددی که برای کاربرِ نهایی که تویِ صفحهٔ زرین‌پال
    # می‌بینتش هیچ معنایی نداره. حالا اسمِ رباتِ خودش (یوزرنیمِ تلگرامی، اگر
    # موجود بود) رو نشون می‌ده که هم حرفه‌ای‌تره هم قابلِ‌شناسایی‌تر.
    bot_label = f"@{row.username}" if row.username else f"#{row.id}"
    # === PATCH_PAYER_IDENTITY_V1 ===
    # درخواستِ ادمین: شناسهٔ عددیِ کاربر در توضیحات بیاید تا در پنلِ زرین‌پال معلوم باشد پرداخت مالِ چه کسی
    # است (بسیاری یوزرنیم ندارند). شناسهٔ خودِ کاربر است و چیزِ محرمانه‌ای نیست.
    description = f"افزایش موجودیِ کیف‌پول کاربر {payer.id} در ربات {bot_label}"

    ok, result = await platform_zarinpal.request_payment(
        merchant, gateway_amount, description, callback_url)
    if not ok:
        await message.answer(f"❌ ساخت پرداخت ناموفق بود:\n{result}")
        return
    async with Session() as s:
        user = await s.scalar(select(BotUser).where(BotUser.bot_id == row.id, BotUser.telegram_id == payer.id))
    if not user:
        # کاربر همین حالا از طریق ربات وارد جریان پرداخت شده است؛ اگر به‌دلیل
        # /start قدیمی، تغییر منو یا هم‌زمانی، ردیف او وجود نداشت، همان الگوی
        # ثبت استاندارد کاربران را اجرا کن و پرداخت را بی‌دلیل متوقف نکن.
        from tenant import get_or_create_user
        user = await get_or_create_user(
            row.id, payer.id,
            getattr(payer, "full_name", "") or "",
            getattr(payer, "username", "") or "")
        if not user:
            await message.answer("❌ ثبت حساب کاربری انجام نشد؛ لطفاً دوباره تلاش کن.")
            return
    # === PATCH_KYC_CAP_V1 ===
    # سقفِ احرازِ هویت باید *قبل* از ساختِ فاکتور بررسی شود تا کاربر پول
    # ندهد و بعد رد شود.
    try:
        from tenant.shared.kyc import check_charge
        _ok, _why = await check_charge(row.id, user, amount)
        if not _ok:
            await message.answer(_why)
            return
    except Exception:
        logger.exception("kyc: بررسیِ سقف ناموفق بود (zarinpal)")

    async with Session() as s:
        # amount همان مبلغی است که به کیفِ پول اضافه می‌شود (بدونِ کارمزد)،
        # وگرنه کاربر بابتِ کارمزد هم اعتبار می‌گرفت.
        order = Order(bot_id=row.id, user_id=user.id, volume_gb=0, days=0, count=1,
                      amount=amount, pay_method="zarinpal_wallet", status="pending",
                      receipt_text=f"zarinpal_authority={result}"
                                   + (f"|fee={fee}" if fee else ""))
        s.add(order)
        await s.commit()
    # === PATCH_ZARINPAL_DIRECT_STARTPAY_V1 ===
    # قبلاً دکمه به یک صفحهٔ میانیِ فاکتور روی دامنهٔ خودمان
    # (/zarinpal/tenant/pay/{id}) لینک می‌شد که آن صفحه بعداً کاربر را به
    # StartPay هدایت می‌کرد. این لایهٔ اضافه وابسته به روتینگِ درستِ آن
    # دامنه بود و اگر آن مسیر پشتِ ری‌ورس‌پروکسی/هاست به main_tenant.py
    # نمی‌رسید (یا اصلاً دیپلوی/ری‌استارت نشده بود)، کاربر همیشه ۴۰۴ عمومیِ
    # سایت را می‌دید و پرداخت هرگز شروع نمی‌شد — دقیقاً همان چیزی که ادمین‌ها
    # گزارش کردند. طبق مستندِ رسمیِ زرین‌پال
    # (zarinpal.com/docs/paymentGateway/connectToGateway.html) کاری که باید
    # انجام شود همین است: بعد از گرفتنِ Authority، کاربر مستقیماً به
    # startpay_url(authority) فرستاده شود؛ هیچ صفحهٔ میانیِ اختصاصی لازم نیست.
    pay_url = await startpay_url_for_bot(row.id, result)  # PATCH_GATEWAY_REDIRECT_LINK_V1
    _inv_msg = await message.answer(
        f"✅ فاکتور شارژ کیف پول ساخته شد.\n\nمبلغ: <b>{_rial(amount)}</b>\n"
        + (f"کارمزد درگاه: <b>{_rial(fee)}</b>\n"
           f"مبلغ قابل پرداخت: <b>{_rial(gateway_amount)}</b>\n" if fee else "")
        + "\n"
        "روی دکمهٔ زیر بزنید و پرداخت را در درگاه زرین‌پال انجام دهید. کیف پول معمولاً خودکار شارژ می‌شود؛ "
        "اگر بعد از پرداخت شارژ نشد، دکمهٔ «✅ بررسی و تأیید پرداخت» را بزنید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔐 پرداخت در درگاه زرین‌پال", url=pay_url, style=ButtonStyle.SUCCESS)],
            # === PATCH_ZARINPAL_MANUAL_VERIFY_V1 ===
            [InlineKeyboardButton(text="✅ بررسی و تأیید پرداخت", callback_data=f"wzp:verify:{order.id}", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="w:back", style=ButtonStyle.DANGER)],
        ]))
    # === PATCH_INVOICE_MSG_EDIT_V1 === شناسهٔ پیامِ فاکتور را نگه می‌داریم تا بعد از تأیید ادیت شود
    try:
        from tenant.gateways import invoice_msg
        await invoice_msg.remember(row.id, "zp", order.id, _inv_msg.chat.id, _inv_msg.message_id)
    except Exception:
        logger.exception("zarinpal: ذخیرهٔ پیامِ فاکتور ناموفق بود")


# === PATCH_ZARINPAL_MANUAL_VERIFY_V1 ===
@zarinpal_wallet_router.callback_query(F.data.startswith("wzp:verify:"))
async def zarinpal_manual_verify(cb: CallbackQuery):
    try:
        order_id = int(cb.data.rsplit(":", 1)[1])
    except Exception:
        await cb.answer("شناسهٔ فاکتور نامعتبر است.", show_alert=True)
        return
    await cb.answer("در حال بررسیِ وضعیتِ پرداخت...", show_alert=False)
    status, msg, amount, bot_id, telegram_id, new_balance, ref_id = await _settle_zarinpal_order(order_id)
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
                logger.exception("zarinpal: اطلاعِ شارژ (بررسیِ دستی) به کاربر ناموفق بود")
    elif status == "already":
        # === PATCH_INVOICE_MSG_EDIT_V1 === فاکتورِ قدیمی/تکراری: دکمه‌ها برداشته می‌شود
        from tenant.gateways import invoice_msg
        if not await invoice_msg.edit_message_confirmed(cb.message, amount, already=True):
            await cb.message.answer("✅ این فاکتور قبلاً تأیید و به کیف پول اضافه شده است.")
    elif status == "not_paid":
        await cb.message.answer(
            "⏳ هنوز طبقِ استعلام از زرین‌پال، پرداختی برای این فاکتور تأیید نشده.\n"
            f"جزئیات: <code>{msg}</code>\n\n"
            "اگر همین الان پرداخت کردی، چند لحظه صبر کن و دوباره همین دکمه را بزن.")
    else:
        await cb.message.answer(f"❌ بررسی ناموفق بود: {msg}")


async def zarinpal_admin_text(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await _setting(bot_id, "zarinpal_enabled", "0") == "1"
    merchant = await _setting(bot_id, "zarinpal_merchant_id", "")
    base_override = (await _setting(bot_id, "zarinpal_webhook_base", "")).strip().rstrip("/")
    fee_on = await _setting(bot_id, "zarinpal_fee_on_payer", "0") == "1"
    from tenant.gateways.gateway_redirect import status_line as _gwr_status  # PATCH_GATEWAY_REDIRECT_LINK_V1
    _gwr_line = await _gwr_status(bot_id, "zarinpal")
    _base = await _callback_base(bot_id)   # PATCH_GATEWAY_OWN_DOMAIN_REQUIRED_V1
    text = ("🔐 <b>درگاه زرین‌پال ربات</b>\n\n"
            f"وضعیت درگاه: {'✅ فعال' if enabled else '❌ غیرفعال'}\n"
            f"مرچنت‌ایدی: {'✅ ثبت شده' if merchant else '⚠️ ثبت نشده'}\n"
            f"دامنهٔ callback: {_base or '⛔ ثبت نشده'}\n"
            f"{_gwr_line}\n"
            # === PATCH_ZARINPAL_FEE_V1 ===
            f"کارمزد درگاه: <b>{'پرداخت‌کننده' if fee_on else 'فروشنده (شما)'}</b>")
    # === PATCH_GATEWAY_OWN_DOMAIN_REQUIRED_V1 ===
    if not _base:
        text += (
            "\n\n⛔ <b>تا دامنه ثبت نشود این درگاه کار نمی‌کند.</b>\n\n"
            "زرین‌پال بعد از پرداخت، کاربر را به دامنهٔ تو برمی‌گرداند؛ بدونِ "
            "آن پرداخت تأیید نمی‌شود و پولِ مشتری معلق می‌ماند.\n\n"
            "<b>دو راه داری:</b>\n"
            "• دامنهٔ اختصاصیِ رباتت را ثبت و تأیید کن (توصیه‌شده) — بعد از آن "
            "این‌جا خودکار پر می‌شود.\n"
            "• یا دامنهٔ خودت را دستی وارد کن.\n\n"
            "<i>⚠️ همین دامنه باید در پنلِ زرین‌پال هم ثبت شده باشد.</i>")
    rows = [
        [InlineKeyboardButton(text="🔴 غیرفعال کن" if enabled else "🟢 فعال کن", callback_data="zpt:toggle", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text="✏️ ثبت / ویرایش مرچنت‌ایدی", callback_data="zpt:merchant", style=ButtonStyle.PRIMARY)],
        # === PATCH_ZARINPAL_PER_BOT_DOMAIN_OVERRIDE_V1 ===
        [InlineKeyboardButton(text="🌐 ثبت/ویرایشِ دامنهٔ callback", callback_data="zpt:base", style=ButtonStyle.PRIMARY)],
        # === PATCH_GATEWAY_REDIRECT_LINK_V1 ===
        [InlineKeyboardButton(text="🔗 لینک ریدایرکت", callback_data="gwr:menu:zarinpal", style=ButtonStyle.PRIMARY)],
        # === PATCH_ZARINPAL_FEE_V1 ===
        [InlineKeyboardButton(
            text=("💸 کارمزد: پرداخت‌کننده 🟢" if fee_on else "💸 کارمزد: فروشنده ⚪️"),
            callback_data="zpt:fee",
            style=ButtonStyle.SUCCESS if fee_on else ButtonStyle.PRIMARY)],
    ]
    if enabled and merchant.strip():
        rows.append([InlineKeyboardButton(text="🧪 تست اتصال زرین‌پال", callback_data="zpt:test", style=ButtonStyle.SUCCESS)])
    rows.extend([
        [InlineKeyboardButton(text="📖 راهنمای اتصال زرین‌پال", callback_data="zpt:guide", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, kb


# === PATCH_ZARINPAL_FEE_V1 ===
@zarinpal_admin_router.callback_query(F.data == "zpt:fee")
async def zarinpal_admin_fee_toggle(cb: CallbackQuery, bot: Bot):
    """کارمزدِ درگاه را از فروشنده به پرداخت‌کننده منتقل می‌کند و برعکس.

    زرین‌پال گزینهٔ «کارمزد از خریدار» را از پنلش برداشته و گفته به‌جایش
    از APIِ استعلامِ کارمزد استفاده کنید؛ یعنی موقعِ ساختِ تراکنش، کارمزد
    را استعلام و به مبلغ اضافه می‌کنیم. کیفِ پولِ کاربر همان مبلغِ اصلی
    شارژ می‌شود، نه مبلغِ با کارمزد.
    """
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await _setting(row.id, "zarinpal_fee_on_payer", "0") == "1"
    await set_setting(row.id, "zarinpal_fee_on_payer", "0" if cur else "1")
    await cb.answer("کارمزد با فروشنده" if cur else "کارمزد با پرداخت‌کننده")
    text, kb = await zarinpal_admin_text(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    if not cur:
        await cb.message.answer(
            "💸 <b>کارمزد با پرداخت‌کننده شد.</b>\n\n"
            "از این پس موقعِ پرداخت، کارمزد از خودِ زرین‌پال استعلام و به "
            "مبلغ اضافه می‌شود. کاربر مبلغِ بیشتری می‌پردازد ولی دقیقاً "
            "همان مبلغِ اصلی به کیف پولش اضافه می‌شود.\n\n"
            "⚠️ اگر استعلامِ کارمزد به هر دلیلی جواب ندهد، پرداخت بدونِ "
            "کارمزد انجام می‌شود تا خریدِ کاربر متوقف نشود.")


@zarinpal_admin_router.callback_query(F.data == "set:zarinpal")
async def zarinpal_admin_start(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    text, kb = await zarinpal_admin_text(row.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@zarinpal_admin_router.callback_query(F.data == "zpt:guide")
async def zarinpal_admin_guide(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    text = ("📖 <b>راهنمای اتصال زرین‌پال</b>\n\n"
            "۱) Merchant ID همین ربات را از پنل زرین‌پال دریافت کنید.\n"
            "۲) از گزینهٔ «✏️ ثبت / ویرایش مرچنت‌ایدی» همان کد را ذخیره کنید.\n"
            "۳) درگاه را فعال کنید و «🧪 تست اتصال زرین‌پال» را بزنید.\n\n"
            "دامنهٔ callback به‌صورت پیش‌فرض همان دامنهٔ مرکزیِ بات‌ساز است و "
            "برای بیشترِ ربات‌ها نیازی به تنظیم ندارد. فقط اگر مرچنت‌ایدی‌تان را "
            "در پنلِ زرین‌پال با یک دامنهٔ دیگر (مثلاً دامنهٔ خودتان) ثبت کرده‌اید، "
            "از «🌐 ثبت/ویرایشِ دامنهٔ callback» همان دامنه را وارد کنید — وگرنه "
            "زرین‌پال با خطای «عدم تطابقِ دامنه» رد می‌کند.\n\n"
            "رمز کارت یا اطلاعات بانکی را در ربات وارد نکنید.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت به تنظیمات زرین‌پال", callback_data="set:zarinpal", style=ButtonStyle.DANGER)],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@zarinpal_admin_router.callback_query(F.data == "zpt:test")
async def zarinpal_admin_test(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    merchant = (await _setting(row.id, "zarinpal_merchant_id", "")).strip()
    enabled = await _setting(row.id, "zarinpal_enabled", "0") == "1"
    if not enabled or not merchant:
        await cb.answer("ابتدا مرچنت‌ایدی را ثبت و درگاه را فعال کنید.", show_alert=True)
        return
    callback_url = f"{await _callback_base(row.id)}/zarinpal/tenant/callback"
    await cb.answer("در حال بررسی اتصال زرین‌پال...", show_alert=False)
    bot_label = f"@{row.username}" if row.username else f"#{row.id}"
    # MARKER_ZARINPAL_MINAMOUNT_V1: زرین‌پال حداقل ۱۵٬۰۰۰ تومان می‌خواهد؛ ۱۰٬۰۰۰ همیشه رد می‌شد
    ok, result = await platform_zarinpal.request_payment(
        merchant,
        20_000,
        f"تستِ اتصالِ درگاهِ زرین‌پال — ربات {bot_label} (لطفاً پرداخت نکنید)",
        callback_url,
    )
    if ok:
        # === PATCH_GATEWAY_REDIRECT_LINK_V1 ===
        # ساختِ فاکتور موفق بودن یعنی نیمی از راه؛ صفحهٔ پرداخت هم باید باز
        # شود (مثلاً با زیردامنهٔ اختصاصی). لینک را می‌دهیم تا مالک ببیند.
        test_url = await startpay_url_for_bot(row.id, result)
        await cb.message.answer(
            "✅ <b>اتصال زرین‌پال با موفقیت تأیید شد.</b>\n\n"
            "مرچنت‌ایدی و مسیر ارتباط با زرین‌پال پاسخ صحیح دادند.\n\n"
            "👇 دکمهٔ زیر را باز کن تا مطمئن شوی <b>صفحهٔ پرداخت</b> هم باز می‌شود. "
            "اگر خطای «دسترسی از این دامنه مجاز نمی‌باشد» دیدی، زرین‌پال "
            "ارجاع از داخلِ تلگرام را قبول نمی‌کند و باید «🔗 لینک ریدایرکت» "
            "را تنظیم کنی.\n\n"
            "⚠️ این فاکتورِ آزمایشی را پرداخت نکن.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔍 بازکردنِ صفحهٔ پرداختِ آزمایشی", url=test_url)]]),
        )
    else:
        hint = (
            "این مرچنت‌ایدی احتمالاً با یک دامنهٔ دیگر (نه expresspanel.ir) توی پنلِ زرین‌پال ثبت شده. "
            "از «🌐 ثبت/ویرایشِ دامنهٔ callback» همون دامنه رو ثبت کن.\n\n"
            if "domain" in str(result).lower() or "دامنه" in str(result)
            else ""
        )
        await cb.message.answer(
            "❌ <b>تست اتصال زرین‌پال ناموفق بود.</b>\n\n"
            f"جزئیات: <code>{result}</code>\n\n"
            f"{hint}"
            "مرچنت‌ایدی، فعال‌بودن درگاه و تطابق دامنهٔ ثبت‌شده را بررسی کنید.",
            parse_mode="HTML",
        )


@zarinpal_admin_router.callback_query(F.data == "zpt:base")
async def zarinpal_admin_base(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(TenantZarinpalFSM.base)
    await cb.message.answer(
        "🌐 دامنه‌ای که این مرچنت‌ایدی توی پنلِ زرین‌پال روش ثبت شده رو با https:// بفرست.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="zpt:base:cancel", style=ButtonStyle.DANGER)],
        ]))
    await cb.answer()


# === PATCH_ZARINPAL_BASE_INPUT_CANCEL_V1 ===
@zarinpal_admin_router.callback_query(F.data == "zpt:base:cancel")
async def zarinpal_admin_base_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    text, kb = await zarinpal_admin_text(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer("لغو شد")


# متن‌هایِ ناوبریِ سراسری که ممکن است هنگامِ منتظرِ آدرس‌بودن زده شوند —
# اگر توی این حالت گیر بماند، این کیبوردِ پایین همیشگی برای کاربر بی‌اثر
# به‌نظر می‌رسد (هر متنی «آدرس نامعتبر» می‌گیرد). این را هم مثلِ زدنِ دکمهٔ
# «❌ لغو» بالا در نظر می‌گیریم.
_ZARINPAL_BASE_NAV_TEXTS = {"🏠 بازگشت به خانه", "⬅️ بازگشت", "لغو", "/cancel"}


@zarinpal_admin_router.message(TenantZarinpalFSM.base, F.text)
async def zarinpal_admin_base_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting
    row = await get_bot_row(bot)
    value = (msg.text or "").strip().rstrip("/")
    if not row:
        await state.clear()
        return
    if value in _ZARINPAL_BASE_NAV_TEXTS:
        await state.clear()
        text, kb = await zarinpal_admin_text(row.id)
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
    await set_setting(row.id, "zarinpal_webhook_base", value)
    await state.clear()
    text, kb = await zarinpal_admin_text(row.id)
    await msg.answer(
        ("✅ دامنهٔ اختصاصی ذخیره شد.\n\n" if value else "✅ برگشت به دامنهٔ پیش‌فرضِ مرکزی.\n\n") + text,
        reply_markup=kb)


@zarinpal_admin_router.callback_query(F.data == "zpt:toggle")
async def zarinpal_admin_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    current = await _setting(row.id, "zarinpal_enabled", "0")
    await set_setting(row.id, "zarinpal_enabled", "0" if current == "1" else "1")
    text, kb = await zarinpal_admin_text(row.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("✅ ذخیره شد")


@zarinpal_admin_router.callback_query(F.data == "zpt:merchant")
async def zarinpal_admin_merchant(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(TenantZarinpalFSM.merchant)
    await cb.message.answer("مرچنت‌ایدی زرین‌پال همین ربات را ارسال کنید:")
    await cb.answer()


@zarinpal_admin_router.message(TenantZarinpalFSM.merchant, F.text)
async def zarinpal_admin_merchant_save(msg: Message, state: FSMContext, bot: Bot):
    from tenant import get_bot_row, set_setting
    row = await get_bot_row(bot)
    value = (msg.text or "").strip()
    if not row or len(value) < 10 or len(value) > 64:
        await msg.answer("❌ مرچنت‌ایدی نامعتبر است.")
        return
    await set_setting(row.id, "zarinpal_merchant_id", value)
    await state.clear()
    text, kb = await zarinpal_admin_text(row.id)
    await msg.answer("✅ مرچنت‌ایدی ذخیره شد.\n\n" + text, reply_markup=kb)
