"""
HesabPay gateway for tenant wallets.

The gateway is disabled until the tenant owner configures an API key, a fixed
AFN-to-Toman rate, and explicitly enables it. The internal wallet remains in
Toman; HesabPay receives AFN. Credit is applied only after a verified,
 idempotent webhook.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import Session, WEBHOOK_BASE
from logging_setup import get_logger as _get_logger
from models import Bot as BotRow, BotUser, HesabPayCharge, Setting, WalletTxn

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

logger = _get_logger(__name__)
hesabpay_router = Router()
hesabpay_admin_router = Router()

API_PRODUCTION = "https://api.hesab.com"
API_SANDBOX = "https://api-sandbox.hesab.com"
DEFAULT_RATE = Decimal("0")
_RATE_TTL_SECONDS = 900


class HesabPayFSM(StatesGroup):
    amount = State()


class HesabPayAdminFSM(StatesGroup):
    api_key = State()
    rate = State()
    webhook_base = State()


def _toman(value: int | float | Decimal) -> str:
    return f"{int(value):,} تومان"


def _afn(value: Decimal) -> str:
    value = value.quantize(Decimal("0.01"))
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text} افغانی"


def _digits(text: str) -> str:
    return (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


def _parse_int(text: str) -> int | None:
    try:
        value = _digits(text).replace(",", "").replace("،", "").strip()
        return int(value)
    except (TypeError, ValueError):
        return None


async def _setting(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def is_available(bot_id: int) -> bool:
    enabled = await _setting(bot_id, "hesabpay_enabled", "0") == "1"
    api_key = (await _setting(bot_id, "hesabpay_api_key", "")).strip()
    return enabled and bool(api_key) and await _rate(bot_id) > 0


async def _api_base(bot_id: int) -> str:
    mode = (await _setting(bot_id, "hesabpay_mode", "sandbox")).strip().lower()
    return API_PRODUCTION if mode == "live" else API_SANDBOX


async def _api_key(bot_id: int) -> str:
    return (await _setting(bot_id, "hesabpay_api_key", "")).strip()


async def _webhook_base(bot_id: int) -> str:
    override = (await _setting(bot_id, "hesabpay_webhook_base", "")).strip().rstrip("/")
    return override or (WEBHOOK_BASE or "https://expresspanel.ir").rstrip("/")


async def _save_setting(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


async def _manual_rate(bot_id: int) -> Decimal:
    try:
        return Decimal((await _setting(bot_id, "hesabpay_afn_toman_rate", "0")).strip())
    except (InvalidOperation, ValueError):
        return DEFAULT_RATE


async def _fetch_auto_rate(bot_id: int) -> Decimal | None:
    """AFN→تومان را از AFN/USD و USD/تومان محاسبه و cache می‌کند."""
    if aiohttp is None:
        return None
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get("https://open.er-api.com/v6/latest/USD") as response:
                data = await response.json(content_type=None)
        afn_per_usd = Decimal(str((data.get("rates") or {}).get("AFN") or "0"))
        if afn_per_usd <= 0:
            return None
        from tenant.gateways.crypto_pay import get_usd_to_toman_rate
        brs_key = await _setting(bot_id, "brsapi_key", "")
        usd_toman = Decimal(str(await asyncio.wait_for(
            get_usd_to_toman_rate(brs_key), timeout=12)))
        if usd_toman <= 0:
            return None
        rate = (usd_toman / afn_per_usd).quantize(Decimal("0.01"))
        if rate <= 0:
            return None
        await _save_setting(bot_id, "hesabpay_afn_toman_rate", str(rate))
        await _save_setting(bot_id, "hesabpay_rate_updated_at", str(int(time.time())))
        await _save_setting(bot_id, "hesabpay_rate_source", "AFN/USD + USD/تومان")
        return rate
    except Exception:
        logger.exception("hesabpay: دریافت نرخ خودکار ناموفق بود")
        return None


async def _rate(bot_id: int) -> Decimal:
    manual = await _manual_rate(bot_id)
    mode = (await _setting(bot_id, "hesabpay_rate_mode", "auto")).strip().lower()
    if mode != "auto":
        return manual
    cached = manual
    try:
        updated = int(await _setting(bot_id, "hesabpay_rate_updated_at", "0"))
    except (TypeError, ValueError):
        updated = 0
    if cached > 0 and updated and time.time() - updated < _RATE_TTL_SECONDS:
        return cached
    fresh = await _fetch_auto_rate(bot_id)
    return fresh or cached


def _toman_to_afn(amount_toman: int, rate: Decimal) -> Decimal:
    if amount_toman <= 0 or rate <= 0:
        raise ValueError("مبلغ یا نرخ تبدیل نامعتبر است")
    return (Decimal(amount_toman) / rate).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _reference(bot_id: int, telegram_id: int) -> str:
    return f"hb-{bot_id}-{telegram_id}-{secrets.token_hex(8)}"


def _user_markup(charge: HesabPayCharge) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇦🇫 پرداخت با حساب‌پی", url=charge.checkout_url or "", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🔄 وضعیت پرداخت", callback_data=f"hb:status:{charge.id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="❌ لغو فاکتور", callback_data=f"hb:cancel:{charge.id}", style=ButtonStyle.DANGER)],
    ])


def _charge_text(charge: HesabPayCharge) -> str:
    mode = "آزمایشی" if charge.mode == "sandbox" else "اصلی"
    return (
        "🇦🇫 <b>شارژ کیف پول با افغانی</b>\n\n"
        f"💰 اعتبار کیف پول: <b>{_toman(charge.amount_toman)}</b>\n"
        f"💵 مبلغ پرداخت: <b>{_afn(Decimal(str(charge.amount_afn)))}</b>\n"
        f"📊 نرخ محاسبه: هر ۱ افغانی = <b>{_toman(charge.rate_toman_per_afn)}</b>\n"
        f"🧾 شناسه فاکتور: <code>{charge.reference}</code>\n"
        f"🧪 حالت درگاه: {mode}\n\n"
        "پس از پرداخت، کیف پول فقط با تأیید امن و خودکار درگاه شارژ می‌شود."
    )


async def _create_session(bot_id: int, charge: HesabPayCharge) -> dict[str, Any]:
    if aiohttp is None:
        raise RuntimeError("کتابخانهٔ ارتباط با درگاه روی سرویس نصب نیست")
    api_key = await _api_key(bot_id)
    base = await _api_base(bot_id)
    webhook_base = await _webhook_base(bot_id)
    payload = {
        "user_id": charge.reference,
        "items": [{
            "id": charge.reference,
            "name": "شارژ کیف پول ربات",
            "price": float(Decimal(str(charge.amount_afn))),
        }],
        "redirect_success_url": f"{webhook_base}/hesabpay/success",
        "redirect_failure_url": f"{webhook_base}/hesabpay/failure",
    }
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"Authorization": f"API-KEY {api_key}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async with client.post(f"{base}/api/v1/payment/create-session", json=payload, headers=headers) as response:
            data = await response.json(content_type=None)
            if response.status >= 400 or not data.get("success") or not data.get("url"):
                raise RuntimeError(str(data.get("message") or "درگاه فاکتور را نپذیرفت"))
            return data


async def _start_amount(target: Message, bot: Bot, state: FSMContext, amount_toman: int,
                        from_user=None) -> None:
    # === PATCH_PAYER_IDENTITY_V1 === (cb.message = پیامِ خودِ ربات؛ پرداخت‌کننده باید صریحاً داده شود)
    from tenant.gateways.payer_identity import resolve_payer, PayerIdentityError, PAYER_ERROR_TEXT
    try:
        payer = resolve_payer(target, from_user)
    except PayerIdentityError:
        await target.answer(PAYER_ERROR_TEXT)
        await state.clear()
        return
    row = await _row_for_bot(bot)
    if not row or not await is_available(row.id):
        await target.answer("❌ درگاه حساب‌پی برای این ربات هنوز توسط مدیر تنظیم و فعال نشده است.")
        await state.clear()
        return
    # === PATCH_WALLET_LIMITS_V1 === نقطهٔ مشترکِ دکمهٔ مبلغِ ثابت و مبلغِ دلخواه
    from tenant.gateways import wallet_limits as _wl
    if not await _wl.guard(target, row.id, "hesabpay", amount_toman):
        await state.clear()
        return
    rate = await _rate(row.id)
    try:
        amount_afn = _toman_to_afn(amount_toman, rate)
    except ValueError:
        await target.answer("❌ نرخ تبدیل یا مبلغ معتبر نیست.")
        await state.clear()
        return
    reference = _reference(row.id, payer.id)
    mode = (await _setting(row.id, "hesabpay_mode", "sandbox")).strip().lower()
    async with Session() as s:
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == row.id, BotUser.telegram_id == payer.id))
        if not user:
            from tenant import get_or_create_user
            user = await get_or_create_user(row.id, payer.id,
                                            payer.full_name or "",
                                            payer.username or "")
        charge = HesabPayCharge(
            bot_id=row.id,
            user_telegram_id=payer.id,
            amount_toman=amount_toman,
            amount_afn=amount_afn,
            rate_toman_per_afn=rate,
            reference=reference,
            mode=mode if mode in ("sandbox", "live") else "sandbox",
            status="creating",
        )
        s.add(charge)
        await s.commit()
        await s.refresh(charge)
    try:
        data = await _create_session(row.id, charge)
    except Exception as exc:
        logger.exception("hesabpay: ساخت session ناموفق بود")
        async with Session() as s:
            failed = await s.get(HesabPayCharge, charge.id)
            if failed:
                failed.status = "failed"
                await s.commit()
        await target.answer("❌ ساخت فاکتور حساب‌پی ناموفق بود. لطفاً بعداً دوباره تلاش کنید.")
        await state.clear()
        return
    async with Session() as s:
        saved = await s.get(HesabPayCharge, charge.id)
        saved.session_id = str(data.get("session_id") or data.get("id") or "")[:120]
        saved.checkout_url = str(data["url"])[:1000]
        saved.status = "waiting"
        await s.commit()
        await s.refresh(saved)
    await target.answer(_charge_text(saved), reply_markup=_user_markup(saved), parse_mode="HTML")
    await state.clear()


async def _row_for_bot(bot: Bot) -> BotRow | None:
    from tenant import get_bot_row
    return await get_bot_row(bot)


async def _amount_menu(cb: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    row = await _row_for_bot(bot)
    if not row or not await is_available(row.id):
        await cb.answer("درگاه حساب‌پی هنوز فعال نشده است.", show_alert=True)
        return
    # === PATCH_WALLET_LIMITS_V1 === دکمه‌هایِ خارج از بازهٔ مجاز نشان داده نمی‌شوند
    from tenant.gateways import wallet_limits as _wl
    presets = await _wl.allowed_presets(row.id, "hesabpay",
                                        (50_000, 100_000, 200_000, 500_000, 1_000_000, 2_000_000))
    rows = []
    for pair in _wl.chunk_rows(presets, 2):
        rows.append([InlineKeyboardButton(text=_toman(a), callback_data=f"hb:amount:{a}", style=ButtonStyle.SUCCESS)
                     for a in pair])
    rows.append([InlineKeyboardButton(text="✏️ مبلغ دلخواه به تومان", callback_data="hb:custom", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="w:back", style=ButtonStyle.DANGER)])
    hint = f"\n({await _wl.range_hint(row.id, 'hesabpay')})" if await _wl.customized(row.id, "hesabpay") else ""
    await cb.message.answer("🇦🇫 مبلغ شارژ کیف پول را انتخاب کنید:" + hint, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@hesabpay_router.callback_query(F.data == "w:hesabpay")
async def hesabpay_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await _amount_menu(cb, bot, state)


@hesabpay_router.callback_query(F.data.startswith("hb:amount:"))
async def hesabpay_amount(cb: CallbackQuery, bot: Bot, state: FSMContext):
    amount = _parse_int(cb.data.rsplit(":", 1)[1])
    if not amount:
        await cb.answer("مبلغ نامعتبر است.", show_alert=True)
        return
    await _start_amount(cb.message, bot, state, amount, from_user=cb.from_user)   # PATCH_PAYER_IDENTITY_V1
    await cb.answer()


@hesabpay_router.callback_query(F.data == "hb:custom")
async def hesabpay_custom(cb: CallbackQuery, state: FSMContext, bot: Bot):
    from tenant.gateways import wallet_limits as _wl   # === PATCH_WALLET_LIMITS_V1 ===
    await state.set_state(HesabPayFSM.amount)
    row = await _row_for_bot(bot)
    if row and await _wl.customized(row.id, "hesabpay"):
        await cb.message.answer(f"💰 مبلغ شارژ را به تومان بفرست ({await _wl.range_hint(row.id, 'hesabpay')}):")
    else:
        await cb.message.answer("💰 مبلغ شارژ را به تومان بفرست (مثلاً ۱۰۰۰۰۰):")
    await cb.answer()


@hesabpay_router.message(HesabPayFSM.amount, F.text)
async def hesabpay_amount_text(msg: Message, bot: Bot, state: FSMContext):
    amount = _parse_int(msg.text or "")
    if not amount or amount < 1_000:
        await msg.answer("❌ مبلغ نامعتبر است. حداقل مبلغ ۱٬۰۰۰ تومان است.")
        return
    # === PATCH_WALLET_LIMITS_V1 === (خودِ _start_amount هم بررسی می‌کند؛ این‌جا برایِ ماندنِ کاربر در همان مرحله)
    from tenant.gateways import wallet_limits as _wl
    _row = await _row_for_bot(bot)
    if _row and not await _wl.guard(msg, _row.id, "hesabpay", amount):
        return
    await _start_amount(msg, bot, state, amount, from_user=msg.from_user)


async def wallet_hesabpay_text(msg: Message, bot: Bot, state: FSMContext):
    await _amount_menu(_TextCallback(msg), bot, state)


class _TextCallback:
    def __init__(self, message: Message):
        self.message = message
        self.from_user = message.from_user

    async def answer(self, *args, **kwargs):
        return None


@hesabpay_router.callback_query(F.data.startswith("hb:status:"))
async def hesabpay_status(cb: CallbackQuery):
    try:
        charge_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        await cb.answer("شناسه نامعتبر است.", show_alert=True)
        return
    async with Session() as s:
        charge = await s.get(HesabPayCharge, charge_id)
    if not charge or charge.user_telegram_id != cb.from_user.id:
        await cb.answer("فاکتور پیدا نشد.", show_alert=True)
        return
    labels = {"waiting": "در انتظار تأیید درگاه است.", "paid": "پرداخت تأیید و کیف پول شارژ شده است ✅", "failed": "فاکتور ناموفق شده است.", "cancelled": "فاکتور لغو شده است."}
    await cb.answer(labels.get(charge.status, "در حال بررسی است."), show_alert=True)


@hesabpay_router.callback_query(F.data.startswith("hb:cancel:"))
async def hesabpay_cancel(cb: CallbackQuery):
    try:
        charge_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        await cb.answer("شناسه نامعتبر است.", show_alert=True)
        return
    async with Session() as s:
        charge = await s.scalar(select(HesabPayCharge).where(HesabPayCharge.id == charge_id).with_for_update())
        if not charge or charge.user_telegram_id != cb.from_user.id:
            await cb.answer("فاکتور پیدا نشد.", show_alert=True)
            return
        if charge.status == "waiting":
            charge.status = "cancelled"
            await s.commit()
        else:
            await cb.answer("این فاکتور دیگر قابل لغو نیست.", show_alert=True)
            return
    try:
        await cb.message.edit_text("❌ فاکتور شارژ لغو شد.")
    except Exception:
        await cb.message.answer("❌ فاکتور شارژ لغو شد.")
    await cb.answer("لغو شد")


async def verify_signature(bot_id: int, signature: str, timestamp: str) -> bool:
    if aiohttp is None or not signature or not timestamp:
        return False
    key = await _api_key(bot_id)
    if not key:
        return False
    base = await _api_base(bot_id)
    timeout = aiohttp.ClientTimeout(total=12)
    headers = {"Authorization": f"API-KEY {key}", "Content-Type": "application/json"}
    payload = {"signature": signature, "timestamp": timestamp}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(f"{base}/api/v1/hesab/webhooks/verify-signature", json=payload, headers=headers) as response:
                data = await response.json(content_type=None)
                return response.status < 400 and data.get("success") is True
    except Exception:
        logger.exception("hesabpay: تأیید امضا ناموفق بود")
        return False


async def settle_webhook(reference: str, payload: dict[str, Any]) -> tuple[str, str, int]:
    transaction_id = str(payload.get("transaction_id") or "").strip()
    try:
        gateway_amount = Decimal(str(payload.get("amount")))
    except (InvalidOperation, TypeError):
        return "error", "مبلغ درگاه نامعتبر است.", 0
    if not reference or not transaction_id or not payload.get("success"):
        return "ignored", "پرداخت موفق نیست.", 0
    async with Session() as s:
        charge = await s.scalar(select(HesabPayCharge).where(HesabPayCharge.reference == reference).with_for_update())
        if not charge:
            return "error", "فاکتور پیدا نشد.", 0
        if charge.status == "paid":
            return "already", "این پرداخت قبلاً ثبت شده است.", int(charge.amount_toman)
        if gateway_amount.quantize(Decimal("0.01")) != Decimal(str(charge.amount_afn)).quantize(Decimal("0.01")):
            charge.status = "failed"
            await s.commit()
            return "error", "مبلغ پرداخت با فاکتور مطابقت ندارد.", 0
        duplicate = await s.scalar(select(HesabPayCharge).where(
            HesabPayCharge.transaction_id == transaction_id,
            HesabPayCharge.id != charge.id))
        if duplicate:
            return "error", "تراکنش قبلاً برای فاکتور دیگری ثبت شده است.", 0
        user = await s.scalar(select(BotUser).where(
            BotUser.bot_id == charge.bot_id,
            BotUser.telegram_id == charge.user_telegram_id).with_for_update())
        if not user:
            return "error", "کاربر فاکتور پیدا نشد.", 0
        user.balance = float(user.balance or 0) + int(charge.amount_toman)
        charge.transaction_id = transaction_id
        charge.status = "paid"
        charge.paid_at = datetime.utcnow()
        s.add(WalletTxn(bot_id=charge.bot_id, user_id=user.id, amount=int(charge.amount_toman),
                        kind="hesabpay_charge", note=f"حساب‌پی؛ تراکنش {transaction_id}"))
        new_balance = int(user.balance)
        await s.commit()
    return "credited", "پرداخت تأیید و کیف پول شارژ شد.", new_balance


# ═══════════════════════ Admin settings ═══════════════════════

def _admin_kb(bot_id: int, enabled: bool, mode: str, rate: str, rate_mode: str,
              show_purchase: bool, show_wallet: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("🔴 غیرفعال‌کردن" if enabled else "🟢 فعال‌کردن"), callback_data="hba:toggle", style=ButtonStyle.DANGER if enabled else ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🔑 ثبت/تغییر کلید API", callback_data="hba:key", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🧪 حالت درگاه: {'اصلی' if mode == 'live' else 'آزمایشی'}", callback_data="hba:mode", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"📊 نرخ هر ۱ افغانی: {rate or 'تنظیم نشده'} تومان", callback_data="hba:rate", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🛰 منبع نرخ: {'خودکار' if rate_mode == 'auto' else 'دستی'}", callback_data="hba:rate_mode", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🛒 نمایش در پرداخت محصولات: {'✅ فعال' if show_purchase else '❌ غیرفعال'}", callback_data="hba:purchase_toggle", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text=f"👛 نمایش در کیف پول: {'✅ فعال' if show_wallet else '❌ غیرفعال'}", callback_data="hba:wallet_toggle", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="📚 راهنمای اتصال حساب‌پی", callback_data="hba:guide", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance", style=ButtonStyle.DANGER)],
    ])


async def _admin_allowed(cb: CallbackQuery, bot: Bot) -> BotRow | None:
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, cb.from_user.id, row.owner_id):
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return None
    return row


async def hesabpay_admin_start(cb: CallbackQuery, bot: Bot):
    row = await _admin_allowed(cb, bot)
    if not row:
        return
    enabled = await _setting(row.id, "hesabpay_enabled", "0") == "1"
    mode = await _setting(row.id, "hesabpay_mode", "sandbox")
    rate_mode = (await _setting(row.id, "hesabpay_rate_mode", "auto")).strip().lower()
    rate_value = await _rate(row.id)
    rate = f"{rate_value:,.2f}" if rate_value > 0 else ""
    show_purchase = await _setting(row.id, "hesabpay_show_purchase", "0") == "1"
    show_wallet = await _setting(row.id, "hesabpay_show_wallet", "1") == "1"
    updated_at = await _setting(row.id, "hesabpay_rate_updated_at", "")
    updated_display = ""
    if updated_at:
        try:
            from tenant import _fmt_txn_time
            updated_display = _fmt_txn_time(
                datetime.fromtimestamp(int(updated_at), tz=timezone.utc).replace(tzinfo=None)
            )
        except Exception:
            logger.exception("تبدیل زمان بروزرسانی نرخ حساب‌پی ناموفق بود")
            updated_display = "نامشخص"
    text = (
        "🇦🇫 <b>مدیریت درگاه حساب‌پی</b>\n\n"
        f"وضعیت: {'✅ فعال' if enabled else '❌ خاموش'}\n"
        f"حالت: {'آزمایشی' if mode != 'live' else 'اصلی'}\n"
        f"کلید API: {'✅ ثبت شده' if await _api_key(row.id) else '❌ ثبت نشده'}\n"
        f"نرخ تبدیل: {rate or 'تنظیم نشده'} تومان برای هر افغانی\n"
        f"منبع نرخ: {'خودکار' if rate_mode == 'auto' else 'دستی'}"
        + (f" — آخرین بروزرسانی: {updated_display} به وقت تهران" if updated_display else "") + "\n\n"
        "درگاه فقط وقتی قابل نمایش به کاربر است که کلید API، نرخ تبدیل و فعال‌سازی انجام شده باشد.\n"
        "موجودی داخلی ربات به تومان باقی می‌ماند و پرداخت کاربر با افغانی انجام می‌شود."
    )
    await cb.message.answer(text, reply_markup=_admin_kb(row.id, enabled, mode, rate, rate_mode, show_purchase, show_wallet), parse_mode="HTML")
    await cb.answer()


@hesabpay_admin_router.callback_query(F.data == "set:hesabpay")
async def hesabpay_admin_callback(cb: CallbackQuery, bot: Bot):
    await hesabpay_admin_start(cb, bot)


@hesabpay_admin_router.callback_query(F.data == "hba:toggle")
async def hesabpay_toggle(cb: CallbackQuery, bot: Bot):
    row = await _admin_allowed(cb, bot)
    if not row:
        return
    enabled = await _setting(row.id, "hesabpay_enabled", "0")
    await _set_setting(row.id, "hesabpay_enabled", "0" if enabled == "1" else "1")
    await cb.answer("ذخیره شد")
    await hesabpay_admin_start(cb, bot)


@hesabpay_admin_router.callback_query(F.data == "hba:mode")
async def hesabpay_mode(cb: CallbackQuery, bot: Bot):
    row = await _admin_allowed(cb, bot)
    if not row:
        return
    mode = await _setting(row.id, "hesabpay_mode", "sandbox")
    await _set_setting(row.id, "hesabpay_mode", "live" if mode != "live" else "sandbox")
    await cb.answer("حالت درگاه تغییر کرد")
    await hesabpay_admin_start(cb, bot)


@hesabpay_admin_router.callback_query(F.data == "hba:key")
async def hesabpay_key_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _admin_allowed(cb, bot)
    if not row:
        return
    await state.set_state(HesabPayAdminFSM.api_key)
    await cb.message.answer("🔑 کلید API حساب‌پی را ارسال کنید. برای لغو: لغو")
    await cb.answer()


@hesabpay_admin_router.message(HesabPayAdminFSM.api_key, F.text)
async def hesabpay_key_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _row_for_bot(bot)
    if not row:
        await state.clear()
        return
    if (msg.text or "").strip() == "لغو":
        await state.clear()
        await msg.answer("لغو شد.")
        return
    await _set_setting(row.id, "hesabpay_api_key", msg.text.strip())
    await state.clear()
    await msg.answer("✅ کلید API ذخیره شد. قبل از فعال‌سازی، نرخ تبدیل را نیز ثبت کنید.")


@hesabpay_admin_router.callback_query(F.data == "hba:rate")
async def hesabpay_rate_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _admin_allowed(cb, bot)
    if not row:
        return
    await state.set_state(HesabPayAdminFSM.rate)
    await cb.message.answer("📊 نرخ تبدیل را بفرستید؛ یعنی هر ۱ افغانی چند تومان است (مثلاً ۱۱۰۰):")
    await cb.answer()


@hesabpay_admin_router.message(HesabPayAdminFSM.rate, F.text)
async def hesabpay_rate_save(msg: Message, bot: Bot, state: FSMContext):
    row = await _row_for_bot(bot)
    if not row:
        await state.clear()
        return
    try:
        rate = Decimal(_digits(msg.text or "").replace(",", "").replace("،", "").strip())
    except (InvalidOperation, ValueError):
        await msg.answer("❌ نرخ نامعتبر است.")
        return
    if rate <= 0 or rate > Decimal("10000000"):
        await msg.answer("❌ نرخ باید بیشتر از صفر و منطقی باشد.")
        return
    await _set_setting(row.id, "hesabpay_afn_toman_rate", str(rate.quantize(Decimal("0.01"))))
    await _set_setting(row.id, "hesabpay_rate_mode", "manual")
    await state.clear()
    await msg.answer("✅ نرخ تبدیل ذخیره شد.")


@hesabpay_admin_router.callback_query(F.data == "hba:rate_mode")
async def hesabpay_rate_mode(cb: CallbackQuery, bot: Bot):
    row = await _admin_allowed(cb, bot)
    if not row:
        return
    current = (await _setting(row.id, "hesabpay_rate_mode", "auto")).strip().lower()
    await _set_setting(row.id, "hesabpay_rate_mode", "manual" if current == "auto" else "auto")
    await cb.answer("منبع نرخ تغییر کرد")
    await hesabpay_admin_start(cb, bot)


@hesabpay_admin_router.callback_query(F.data == "hba:purchase_toggle")
async def hesabpay_purchase_toggle(cb: CallbackQuery, bot: Bot):
    row = await _admin_allowed(cb, bot)
    if not row:
        return
    current = await _setting(row.id, "hesabpay_show_purchase", "0")
    await _set_setting(row.id, "hesabpay_show_purchase", "0" if current == "1" else "1")
    await cb.answer("تنظیم نمایش در پرداخت محصولات ذخیره شد")
    await hesabpay_admin_start(cb, bot)


@hesabpay_admin_router.callback_query(F.data == "hba:wallet_toggle")
async def hesabpay_wallet_toggle(cb: CallbackQuery, bot: Bot):
    row = await _admin_allowed(cb, bot)
    if not row:
        return
    current = await _setting(row.id, "hesabpay_show_wallet", "1")
    await _set_setting(row.id, "hesabpay_show_wallet", "0" if current == "1" else "1")
    await cb.answer("تنظیم نمایش در کیف پول ذخیره شد")
    await hesabpay_admin_start(cb, bot)


@hesabpay_admin_router.callback_query(F.data == "hba:guide")
async def hesabpay_guide(cb: CallbackQuery, bot: Bot):
    row = await _admin_allowed(cb, bot)
    if not row:
        return
    base = await _webhook_base(row.id)
    await cb.message.answer(
        "📚 <b>راهنمای اتصال حساب‌پی</b>\n\n"
        "۱) در حساب Merchant حساب‌پی، کلید API محیط آزمایشی را دریافت کنید.\n"
        "۲) کلید را در همین صفحه ثبت کنید.\n"
        "۳) نرخ تبدیل هر ۱ افغانی به تومان را ثبت کنید.\n"
        "۴) ابتدا حالت آزمایشی را تست کنید؛ سپس برای پرداخت واقعی حالت اصلی را روشن کنید.\n\n"
        f"نشانی webhook برای ثبت در پنل درگاه:\n<code>{base}/hesabpay/webhook</code>\n\n"
        "پرداخت پس از webhook معتبر و تأیید امضای درگاه، فقط یک‌بار به موجودی تومانی اضافه می‌شود.",
        parse_mode="HTML")
    await cb.answer()


async def _set_setting(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)
