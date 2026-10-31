"""
platform_ton_direct.py — پرداختِ مستقیمِ TON به ولتِ خودِ بات‌ساز (بدونِ درگاه).

=== PATCH_TON_DIRECT_PLATFORM_V1 ===
همان منطقِ tenant/ton_direct.py، ولی برایِ کیفِ پولِ کاربرانِ خودِ رباتِ
بات‌ساز (نه یک تنانت). چون این‌جا فقط یک «مالک» (خودِ بات‌ساز) وجود دارد،
به‌جایِ ولتِ per-bot یک ولتِ سراسری در pset ذخیره می‌شود و تنظیماتش فقط
دستِ سوپرادمین (PLATFORM_ADMIN_ID) است.

«تونکیپر» یک کیفِ پولِ غیرحضانتی است، نه درگاه؛ کالبک و فاکتورِ فروشنده
ندارد. پس این ماژول هم مثلِ نسخهٔ تنانت، پرداخت را با خواندنِ خودِ
بلاک‌چین تشخیص می‌دهد:

  ۱) سوپرادمین آدرسِ ولتِ TON بات‌ساز را ثبت می‌کند.
  ۲) کاربر مبلغ را به تومان می‌دهد؛ با نرخِ زنده به TON تبدیل می‌شود و یک
     فاکتور با «memoِ یکتا» و «مبلغِ یکتا» ساخته می‌شود.
  ۳) لینکِ ton:// و لینکِ وبِ Tonkeeper به کاربر داده می‌شود.
  ۴) حلقهٔ پس‌زمینه تراکنش‌هایِ ورودیِ همان آدرس را از toncenter می‌خواند و
     هر تراکنشی که با فاکتور بخورد، کیفِ پولِ کاربر را در بات‌ساز شارژ می‌کند.

توابعِ عمومیِ بدونِ وابستگی به bot_id (قیمتِ لحظه‌ای TON، خواندنِ زنجیره،
ساختِ لینکِ پرداخت) مستقیم از tenant.gateways.ton_direct وارد می‌شوند تا منطقِ
شبکه فقط یک‌جا نوشته شده باشد.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import random
import re
import secrets
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ButtonStyle, ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select, update as sa_update

from config import Session
from models import PlatformTonDirectCharge, PlatformTxn, PlatformUser

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

# کلاینتِ شبکه و ثابت‌ها — مشترک با نسخهٔ تنانت
from tenant.gateways.ton_direct import (
    AMOUNT_TOLERANCE, FALLBACK_ABS_TOLERANCE, INVOICE_TTL_MINUTES, LATE_GRACE_HOURS, MAX_FEE_PERCENT,
    MIN_TON, NANO, fetch_incoming, payment_links, ton_usd_price,
)

platform_ton_direct_router = Router(name="platform_ton_direct")

DEFAULTS = DefaultBotProperties(parse_mode=ParseMode.HTML)

_ADDR_RE = re.compile(r"^(?:[EU]Q[A-Za-z0-9_\-]{46}|-?\d:[0-9a-fA-F]{64})$")

STALE_AFTER_MIN = 60


class PTonDirectFSM(StatesGroup):
    wallet = State()
    fee = State()
    manual_rate = State()
    amount = State()
    search = State()    # === PATCH_TON_DIRECT_INVOICES_V1 === جستجوی سوپرادمین با کد پیگیری
    track = State()     # === PATCH_TON_DIRECT_INVOICES_V1 === پیگیریِ کاربر با کد پیگیریِ خودش
    min_amount = State()  # === PATCH_TON_DIRECT_MINMAX_V1 ===
    max_amount = State()  # === PATCH_TON_DIRECT_MINMAX_V1 ===


def _rial(n) -> str:
    return f"{int(n):,} تومان"


def _new_memo() -> str:
    return "PB" + secrets.token_hex(3).upper()


def _uniquify(ton: Decimal) -> Decimal:
    base = ton.quantize(Decimal("0.001"))
    return (base + Decimal(random.randint(1, 999)) / Decimal(1_000_000)).quantize(
        Decimal("0.000001"))


async def _is_super(uid: int) -> bool:
    from config import PLATFORM_ADMIN_ID
    return uid == PLATFORM_ADMIN_ID


# ═══════════════════════ تنظیمات و نرخ ═══════════════════════

async def wallet_address() -> str:
    from platform_bot import get_pset
    return (await get_pset("pton_direct_wallet", "")).strip()


async def fee_percent() -> Decimal:
    from platform_bot import get_pset
    raw = await get_pset("pton_direct_fee_percent", "0")
    try:
        val = Decimal(str(raw or "0"))
    except Exception:
        return Decimal(0)
    if val < 0:
        return Decimal(0)
    return min(val, Decimal(MAX_FEE_PERCENT))


async def is_available() -> bool:
    from platform_bot import get_pset
    return (await get_pset("pton_direct_enabled", "0") == "1"
            and bool(await wallet_address()))


# === PATCH_TON_DIRECT_MINMAX_V1 ===
# محدودهٔ مبلغِ قابلِ‌شارژ (تومان) که سوپرادمین تعیین می‌کند. خالی/۰ یعنی
# محدودیتی تنظیم نشده — برای حداقل، به کفِ فنیِ MIN_TON برمی‌گردیم؛
# برای حداکثر، سقفی اعمال نمی‌شود.
async def min_toman_setting() -> int:
    from platform_bot import get_pset
    try:
        return int(await get_pset("pton_direct_min_toman", "0") or "0")
    except Exception:
        return 0


async def max_toman_setting() -> int:
    from platform_bot import get_pset
    try:
        return int(await get_pset("pton_direct_max_toman", "0") or "0")
    except Exception:
        return 0


_rate_cache: dict[str, tuple[Decimal, float]] = {}
_RATE_TTL = 300
_RATE_TIMEOUT = 6


async def ton_toman_rate(cached_only: bool = False) -> Decimal | None:
    """نرخِ هر TON به تومان برایِ بات‌ساز: نرخِ زنده → نرخِ دستیِ سوپرادمین → None."""
    import time
    from platform_bot import get_pset

    async def _manual() -> Decimal | None:
        raw = await get_pset("pton_direct_manual_price", "")
        try:
            val = Decimal(str(raw or "0"))
        except Exception:
            return None
        return val if val > 0 else None

    usd = await ton_usd_price(cached_only=cached_only)
    if not usd:
        return await _manual()

    cached = _rate_cache.get("usd_toman")
    if cached and (time.time() - cached[1]) < _RATE_TTL:
        usd_toman = cached[0]
    elif cached_only:
        return await _manual()
    else:
        try:
            from tenant.gateways.crypto_pay import get_rate_smart
            brs_key = await get_pset("brsapi_key", "")
            usd_toman = Decimal(str(await asyncio.wait_for(
                get_rate_smart(brs_key), timeout=10)))
        except asyncio.TimeoutError:
            logger.warning("platform ton_direct: گرفتنِ نرخِ دلار→تومان تایم‌اوت شد")
            return await _manual()
        except Exception:
            logger.warning("platform ton_direct: نرخِ دلار→تومان به دست نیامد", exc_info=True)
            return await _manual()
        if usd_toman <= 0:
            return await _manual()
        _rate_cache["usd_toman"] = (usd_toman, time.time())
    return usd * usd_toman


# ═══════════════ هستهٔ امن: تسویه و شارژ ═══════════════

async def _credit(charge_id: int, tx_hash: str) -> int:
    async with Session() as s:
        claim = await s.execute(
            sa_update(PlatformTonDirectCharge)
            .where(PlatformTonDirectCharge.id == charge_id,
                   PlatformTonDirectCharge.status != "paid")
            .values(status="paid", tx_hash=tx_hash))
        if claim.rowcount == 0:
            await s.rollback()
            return 0
        ch = await s.get(PlatformTonDirectCharge, charge_id)
        credit = int(ch.amount_toman or 0)
        payer = int(ch.user_telegram_id)
        u = await s.scalar(select(PlatformUser).where(
            PlatformUser.telegram_id == payer).with_for_update())
        if u:
            u.balance = float(u.balance or 0) + credit
            s.add(PlatformTxn(user_telegram_id=payer, amount=credit,
                              kind="ton_direct", note="شارژ با واریز مستقیم TON"))
        await s.commit()
    return credit


async def _match_charge(tx: dict) -> int | None:
    comment = (tx.get("comment") or "").strip().upper()
    value = Decimal(tx["value_nano"]) / NANO

    async with Session() as s:
        if comment:
            _late_from = _dt.datetime.utcnow() - _dt.timedelta(hours=LATE_GRACE_HOURS)
            rows = (await s.scalars(select(PlatformTonDirectCharge).where(
                (PlatformTonDirectCharge.status == "waiting") |
                ((PlatformTonDirectCharge.status == "expired") &
                 (PlatformTonDirectCharge.expires_at >= _late_from))))).all()
            for ch in rows:
                if ch.memo and ch.memo.upper() in comment:
                    if value >= Decimal(str(ch.ton_amount)) * AMOUNT_TOLERANCE:
                        return ch.id
                    logger.warning(
                        "platform ton_direct: memo خورد ولی مبلغ کم بود (فاکتور %s: "
                        "انتظار %s، دریافت %s)", ch.id, ch.ton_amount, value)
                    return None

        # === PATCH_TON_DIRECT_SAFE_FALLBACK_V1 ===
        # بدونِ کامنت: فقط اگر دقیقاً یک فاکتورِ بازِ هم‌مبلغ (در بازهٔ خیلی باریک)
        # وجود داشته باشد تسویه می‌شود؛ حدسِ «جدیدترین فاکتور» حذف شد چون یعنی
        # هر واریزِ نامرتبطِ هم‌مبلغ به این ولت، کیفِ پولِ یک کاربرِ دیگر را
        # مجانی شارژ می‌کرد (باگِ گزارش‌شده).
        rows = (await s.scalars(select(PlatformTonDirectCharge).where(
            PlatformTonDirectCharge.status == "waiting"))).all()
        candidates = [
            ch for ch in rows
            if Decimal(str(ch.ton_amount)) > 0
            and abs(value - Decimal(str(ch.ton_amount))) <= FALLBACK_ABS_TOLERANCE
        ]
        if len(candidates) == 1:
            return candidates[0].id
        if len(candidates) > 1:
            logger.warning(
                "platform ton_direct: تراکنشِ بدونِ memo (مبلغ %s) با %d فاکتورِ بازِ "
                "هم‌مبلغ برخورد کرد؛ برایِ جلوگیری از شارژِ اشتباه هیچ‌کدام خودکار "
                "تسویه نشد. فاکتورها: %s", value, len(candidates),
                [c.id for c in candidates])
    return None


async def settle(tbot: Bot | None = None) -> int:
    """یک دورِ بررسیِ زنجیره برایِ ولتِ بات‌ساز. خروجی: تعدادِ فاکتورهایِ تسویه‌شده."""
    addr = await wallet_address()
    if not addr:
        return 0
    async with Session() as s:
        _late_from = _dt.datetime.utcnow() - _dt.timedelta(hours=LATE_GRACE_HOURS)
        pending = await s.scalar(select(PlatformTonDirectCharge.id).where(
            (PlatformTonDirectCharge.status == "waiting") |
            ((PlatformTonDirectCharge.status == "expired") &
             (PlatformTonDirectCharge.expires_at >= _late_from))).limit(1))
    if not pending:
        return 0

    settled = 0
    for tx in await fetch_incoming(addr):
        tx_hash = tx.get("hash") or ""
        if not tx_hash:
            continue
        async with Session() as s:
            seen = await s.scalar(select(PlatformTonDirectCharge.id).where(
                PlatformTonDirectCharge.tx_hash == tx_hash))
        if seen:
            continue
        charge_id = await _match_charge(tx)
        if not charge_id:
            continue
        async with Session() as s:
            ch = await s.get(PlatformTonDirectCharge, charge_id)
            payer, memo = int(ch.user_telegram_id), ch.memo
        credit = await _credit(charge_id, tx_hash)
        if not credit:
            continue
        settled += 1
        logger.info("platform ton_direct: فاکتور %s با tx %s تسویه شد.", memo, tx_hash[:16])
        if tbot is not None:
            async with Session() as s:
                ch2 = await s.get(PlatformTonDirectCharge, charge_id)
                inv_chat, inv_msg = ch2.invoice_chat_id, ch2.invoice_message_id
            if inv_chat and inv_msg:
                try:
                    await tbot.delete_message(inv_chat, inv_msg)
                except Exception:
                    logger.info("platform ton_direct: حذفِ پیامِ فاکتور %s ممکن نشد (بی‌اهمیت)", memo)
            try:
                await tbot.send_message(
                    payer,
                    "✅ <b>پرداخت شما تایید شد</b>\n\n"
                    f"🧾 کد فاکتور: <code>{memo}</code>\n"
                    f"💰 کیف پول شما <b>{credit:,} تومان</b> شارژ شد.")
            except Exception:
                logger.exception("platform ton_direct: اطلاعِ شارژ به کاربر نرسید")
            # === PATCH_TON_DIRECT_ADMIN_NOTIFY_V1 ===
            # اعلانِ سوپرادمین: کی پرداخت کرد، با چه مشخصاتی، و چی خرید.
            try:
                from config import PLATFORM_ADMIN_ID
                async with Session() as s:
                    ch3 = await s.get(PlatformTonDirectCharge, charge_id)
                    u = await s.scalar(select(PlatformUser).where(
                        PlatformUser.telegram_id == payer))
                uname = f"@{u.username}" if u and u.username else "—"
                fname = (u.full_name if u else None) or "—"
                await tbot.send_message(
                    PLATFORM_ADMIN_ID,
                    "💎 <b>پرداخت جدید — TON مستقیم</b>\n\n"
                    "<blockquote>"
                    f"👤 نام: {fname}\n"
                    f"🔗 یوزرنیم: {uname}\n"
                    f"🆔 آیدی عددی: <code>{payer}</code>"
                    "</blockquote>\n\n"
                    f"🧾 کد پیگیری: <code>{memo}</code>\n"
                    f"🛒 خرید: شارژ کیف پول\n"
                    f"💰 مبلغ: <b>{credit:,} تومان</b>\n"
                    f"💎 مبلغ TON: <code>{ch3.ton_amount if ch3 else ''}</code>\n"
                    f"🔗 tx: <code>{tx_hash[:24]}…</code>")
            except Exception:
                logger.exception("platform ton_direct: اطلاعِ سوپرادمین ارسال نشد")
    return settled


async def pton_direct_loop(interval: int = 60, fast_interval: int = 8):
    """زنجیرهٔ ولتِ بات‌ساز را چک می‌کند: وقتی فاکتورِ بازی هست هر ~۸ ثانیه (شارژِ سریع بعد از
    واریز — === PATCH_TON_DIRECT_FAST_POLL_V1 ===)، وگرنه هر دقیقه."""
    from platform_bot import get_pset
    await asyncio.sleep(40)
    while True:
        _sleep = interval
        try:
            now = _dt.datetime.utcnow()
            async with Session() as s:
                await s.execute(
                    sa_update(PlatformTonDirectCharge)
                    .where(PlatformTonDirectCharge.status == "waiting",
                           PlatformTonDirectCharge.expires_at.isnot(None),
                           PlatformTonDirectCharge.expires_at < now)
                    .values(status="expired"))
                await s.commit()
            if await get_pset("pton_direct_enabled", "0") == "1":
                async with Session() as _sw:
                    if await _sw.scalar(select(PlatformTonDirectCharge.id).where(
                            PlatformTonDirectCharge.status == "waiting").limit(1)):
                        _sleep = fast_interval
                try:
                    await ton_toman_rate()
                except Exception:
                    logger.exception("platform ton_direct: پیش‌گرم‌کردنِ نرخ")
                from config import PLATFORM_BOT_TOKEN
                if PLATFORM_BOT_TOKEN:
                    tbot = Bot(token=PLATFORM_BOT_TOKEN, default=DEFAULTS)
                    try:
                        await settle(tbot)
                    finally:
                        try:
                            await tbot.session.close()
                        except Exception:
                            logger.exception("platform ton_direct: بستنِ سشنِ ربات")
        except Exception:
            logger.exception("pton_direct_loop error")
        await asyncio.sleep(_sleep)


async def poll_pending_once() -> None:
    """جابِ دوره‌ای — برایِ فراخوانی از worker جدا (سازگار با الگویِ tonpays/blu)."""
    await settle(None)


# ═══════════════════════ پنلِ سوپرادمین ═══════════════════════

async def _menu_text_kb() -> tuple[str, InlineKeyboardMarkup]:
    from platform_bot import get_pset
    from premium_emoji import hx_button
    addr = await wallet_address()
    on = (await get_pset("pton_direct_enabled", "0")) == "1"
    fee = await fee_percent()
    rate = await ton_toman_rate(cached_only=True)
    manual = await get_pset("pton_direct_manual_price", "")

    body = [
        "💎 <b>پرداخت مستقیم TON — بات‌ساز</b>\n",
        "کاربر مبلغ را مستقیم به ولتِ خودِ بات‌ساز واریز می‌کند و ربات پرداخت "
        "را از رویِ بلاک‌چین تشخیص می‌دهد؛ بدونِ درگاه و بدونِ کارمزدِ درگاه.\n",
        (f"👛 ولت مقصد: <code>{addr}</code>" if addr else "👛 ولت مقصد: <i>ثبت نشده</i>"),
        f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}",
        (f"➕ کارمزد پرداخت: <b>{fee:g}٪</b>" if fee > 0 else "➕ کارمزد پرداخت: <i>ندارد</i>"),
    ]
    if rate:
        body.append(f"📈 نرخ لحظه‌ای: هر TON حدود <b>{int(rate):,} تومان</b>")
    else:
        body.append("📈 نرخ لحظه‌ای: <i>برای دیدنش دکمهٔ «بررسی نرخ» را بزن</i>")
    if manual:
        body.append(f"💵 نرخ دستی: هر TON <b>{int(float(manual)):,} تومان</b>")
    body.append(f"\n⏳ هر فاکتور <b>{INVOICE_TTL_MINUTES} دقیقه</b> اعتبار دارد.")
    min_t = await min_toman_setting()
    max_t = await max_toman_setting()
    body.append(f"💵 حداقلِ مبلغِ شارژ: {(f'{min_t:,} تومان' if min_t > 0 else '<i>محدودیتی تنظیم نشده</i>')}")
    body.append(f"💵 حداکثرِ مبلغِ شارژ: {(f'{max_t:,} تومان' if max_t > 0 else '<i>محدودیتی تنظیم نشده</i>')}")

    rows = [[hx_button(("✏️ تغییر آدرس ولت" if addr else "👛 ثبت آدرس ولت TON"),
                       callback_data="ptond:setwallet")]]
    if addr:
        rows.append([hx_button(("🔴 غیرفعال کن" if on else "🟢 فعال کن"),
                               callback_data="ptond:toggle",
                               style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))])
    rows.append([hx_button(f"➕ کارمزد پرداخت ({fee:g}٪)", callback_data="ptond:setfee")])
    rows.append([hx_button("📈 بررسی نرخ لحظه‌ای", callback_data="ptond:rate")])
    rows.append([hx_button(("✏️ تغییر نرخ دستی TON" if manual else "💵 ثبت نرخ دستی TON"),
                           callback_data="ptond:setrate")])
    rows.append([hx_button("💵 حداقل مبلغ شارژ", callback_data="ptond:setmin"),
                 hx_button("💵 حداکثر مبلغ شارژ", callback_data="ptond:setmax")])
    rows.append([hx_button("🧾 وضعیت فاکتورها", callback_data="ptond:invoices")])
    rows.append([hx_button("⬅️ بازگشت", callback_data="adm:back", style=ButtonStyle.DANGER)])
    return "\n".join(body), InlineKeyboardMarkup(inline_keyboard=rows)


@platform_ton_direct_router.callback_query(F.data.in_({"set:pton_direct", "ptond:menu"}))
async def ptond_menu(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.clear()
    text, kb = await _menu_text_kb()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    try:
        await cb.answer()
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")


@platform_ton_direct_router.callback_query(F.data == "ptond:rate")
async def ptond_rate(cb: CallbackQuery):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await cb.answer("⏳ در حال گرفتن نرخ…")
    rate = await ton_toman_rate()
    if not rate:
        await cb.message.answer(
            "❌ نرخ لحظه‌ای در دسترس نیست.\n\n"
            "💵 راه‌حل: با دکمهٔ <b>ثبت نرخ دستی TON</b> خودت قیمت هر TON را "
            "به تومان وارد کن.")
        return
    text, kb = await _menu_text_kb()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@platform_ton_direct_router.callback_query(F.data == "ptond:setwallet")
async def ptond_setwallet(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.set_state(PTonDirectFSM.wallet)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("platform ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "👛 <b>آدرس ولت TON بات‌ساز</b>\n\n"
        "آدرس ولتی که می‌خواهی پول‌های کاربران به آن برود را بفرست (Tonkeeper، "
        "Tonhub، MyTonWallet یا هر ولت دیگر). از بخش <b>Receive</b> کپی‌اش کن.\n\n"
        "معمولاً با <code>UQ</code> یا <code>EQ</code> شروع می‌شود.\n\n"
        "🚫 <b>آدرس واریز صرافی نگذار.</b> آدرس دپوزیت صرافی‌ها بین هزاران کاربر "
        "مشترک است و صرافی برایِ تشخیصِ صاحب واریز memo مخصوص خودش را می‌خواهد؛ "
        "ولی این‌جا ربات memo خودش را می‌گذارد. نتیجه‌اش این می‌شود که پول به "
        "حساب ننشیند و برگرداندنش هم دردسرِ بزرگی باشد.\n\n"
        "⚠️ آدرس را با دقت بررسی کن؛ اگر اشتباه باشد پول به ولتِ شخصِ دیگری "
        "می‌رود و هیچ راهی برای برگرداندنش نیست.")
    await cb.answer()


@platform_ton_direct_router.message(PTonDirectFSM.wallet, F.text)
async def ptond_setwallet_save(msg: Message, state: FSMContext):
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if not _ADDR_RE.match(raw):
        await msg.answer(
            "❌ این آدرس معتبر به نظر نمی‌رسد.\n"
            "آدرس TON معمولاً ۴۸ کاراکتر است و با <code>UQ</code> یا "
            "<code>EQ</code> شروع می‌شود. دوباره بفرست:")
        return
    await state.update_data(pending_wallet=raw)
    await msg.answer(
        "🔎 <b>یک بار دیگر آدرس را چک کن</b>\n\n"
        f"<code>{raw}</code>\n\n"
        "<blockquote>با زدن دکمهٔ تایید، تصدیق می‌کنی که این آدرس <b>ولتِ خودِ "
        "بات‌ساز</b> است و آدرس واریزِ صرافی نیست.\n\n"
        "پول‌های کاربران مستقیم به همین آدرس می‌رود.</blockquote>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ تایید می‌کنم",
                                  callback_data="ptond:wok", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="ptond:wcancel",
                                  style=ButtonStyle.DANGER)],
        ]))


@platform_ton_direct_router.callback_query(F.data == "ptond:wcancel")
async def ptond_wallet_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await ptond_menu(cb, state)


@platform_ton_direct_router.callback_query(F.data == "ptond:wok")
async def ptond_wallet_confirm(cb: CallbackQuery, state: FSMContext):
    from platform_bot import set_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    addr = (await state.get_data()).get("pending_wallet") or ""
    if not _ADDR_RE.match(addr):
        await cb.answer("آدرس پیدا نشد؛ دوباره ثبتش کن.", show_alert=True)
        return
    await state.clear()
    await set_pset("pton_direct_wallet", addr)
    await cb.answer("✅ ذخیره شد")
    text, kb = await _menu_text_kb()
    try:
        await cb.message.edit_text("✅ آدرس ولت ذخیره شد.\n\n" + text, reply_markup=kb,
                                   parse_mode="HTML")
    except Exception:
        await cb.message.answer("✅ آدرس ولت ذخیره شد.\n\n" + text, reply_markup=kb,
                                parse_mode="HTML")


@platform_ton_direct_router.callback_query(F.data == "ptond:setrate")
async def ptond_setrate(cb: CallbackQuery, state: FSMContext):
    from platform_bot import get_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    cur = await get_pset("pton_direct_manual_price", "")
    live = await ton_toman_rate(cached_only=True)
    await state.set_state(PTonDirectFSM.manual_rate)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("platform ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "💵 <b>نرخ دستی TON</b>\n\n"
        "قیمت هر <b>۱ TON</b> را به <b>تومان</b> بفرست (مثلاً <code>180000</code>).\n\n"
        "این نرخ فقط وقتی استفاده می‌شود که نرخ زنده در دسترس نباشد.\n\n"
        + (f"نرخ دستی فعلی: <b>{int(float(cur)):,} تومان</b>\n" if cur else "")
        + (f"نرخ زندهٔ فعلی: <b>{int(live):,} تومان</b>\n" if live else "")
        + "\nبرای حذف نرخ دستی <code>0</code> بفرست.")
    await cb.answer()


@platform_ton_direct_router.message(PTonDirectFSM.manual_rate, F.text)
async def ptond_setrate_save(msg: Message, state: FSMContext):
    from platform_bot import fa_to_int, set_pset
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    val = fa_to_int((msg.text or "").strip())
    if val is None or val < 0:
        await msg.answer("❌ عدد نامعتبر است. قیمت هر TON را به تومان بفرست:")
        return
    if val == 0:
        await set_pset("pton_direct_manual_price", "")
        await state.clear()
        text, kb = await _menu_text_kb()
        await msg.answer("🗑 نرخ دستی حذف شد.\n\n" + text, reply_markup=kb, parse_mode="HTML")
        return
    if val < 1000:
        await msg.answer("❌ این عدد خیلی کم است. قیمت هر TON به <b>تومان</b> است. دوباره بفرست:")
        return
    await set_pset("pton_direct_manual_price", str(val))
    await state.clear()
    text, kb = await _menu_text_kb()
    await msg.answer(f"✅ نرخ دستی روی <b>{val:,} تومان</b> برای هر TON تنظیم شد.\n\n" + text,
                     reply_markup=kb, parse_mode="HTML")


# === PATCH_TON_DIRECT_MINMAX_V1 ===
@platform_ton_direct_router.callback_query(F.data == "ptond:setmin")
async def ptond_setmin(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    cur = await min_toman_setting()
    await state.set_state(PTonDirectFSM.min_amount)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("platform ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "💵 <b>حداقلِ مبلغِ شارژ (تومان)</b>\n\n"
        "حداقلِ مبلغی که کاربر می‌تواند برای شارژِ TON مستقیم وارد کند را به "
        "<b>تومان</b> بفرست (برای حذفِ محدودیت <code>0</code> بفرست).\n\n"
        f"مقدارِ فعلی: {(f'{cur:,} تومان' if cur > 0 else 'تنظیم نشده')}")
    await cb.answer()


@platform_ton_direct_router.message(PTonDirectFSM.min_amount, F.text)
async def ptond_setmin_save(msg: Message, state: FSMContext):
    from platform_bot import fa_to_int, set_pset
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    val = fa_to_int((msg.text or "").strip())
    if val is None or val < 0:
        await msg.answer("❌ عدد نامعتبر است. حداقل مبلغ را به تومان بفرست:")
        return
    max_t = await max_toman_setting()
    if val > 0 and max_t > 0 and val > max_t:
        await msg.answer(
            f"❌ حداقل نمی‌تواند از حداکثرِ فعلی (<b>{max_t:,} تومان</b>) بیشتر باشد. "
            "دوباره بفرست:")
        return
    await set_pset("pton_direct_min_toman", str(val))
    await state.clear()
    text, kb = await _menu_text_kb()
    note = "🗑 حداقل حذف شد." if val == 0 else f"✅ حداقل روی <b>{val:,} تومان</b> تنظیم شد."
    await msg.answer(note + "\n\n" + text, reply_markup=kb, parse_mode="HTML")


@platform_ton_direct_router.callback_query(F.data == "ptond:setmax")
async def ptond_setmax(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    cur = await max_toman_setting()
    await state.set_state(PTonDirectFSM.max_amount)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("platform ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "💵 <b>حداکثرِ مبلغِ شارژ (تومان)</b>\n\n"
        "حداکثرِ مبلغی که کاربر می‌تواند برای شارژِ TON مستقیم وارد کند را به "
        "<b>تومان</b> بفرست (برای حذفِ محدودیت <code>0</code> بفرست).\n\n"
        f"مقدارِ فعلی: {(f'{cur:,} تومان' if cur > 0 else 'تنظیم نشده')}")
    await cb.answer()


@platform_ton_direct_router.message(PTonDirectFSM.max_amount, F.text)
async def ptond_setmax_save(msg: Message, state: FSMContext):
    from platform_bot import fa_to_int, set_pset
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    val = fa_to_int((msg.text or "").strip())
    if val is None or val < 0:
        await msg.answer("❌ عدد نامعتبر است. حداکثر مبلغ را به تومان بفرست:")
        return
    min_t = await min_toman_setting()
    if val > 0 and min_t > 0 and val < min_t:
        await msg.answer(
            f"❌ حداکثر نمی‌تواند از حداقلِ فعلی (<b>{min_t:,} تومان</b>) کمتر باشد. "
            "دوباره بفرست:")
        return
    await set_pset("pton_direct_max_toman", str(val))
    await state.clear()
    text, kb = await _menu_text_kb()
    note = "🗑 حداکثر حذف شد." if val == 0 else f"✅ حداکثر روی <b>{val:,} تومان</b> تنظیم شد."
    await msg.answer(note + "\n\n" + text, reply_markup=kb, parse_mode="HTML")


@platform_ton_direct_router.callback_query(F.data == "ptond:setfee")
async def ptond_setfee(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    cur = await fee_percent()
    await state.set_state(PTonDirectFSM.fee)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("platform ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "➕ <b>کارمزد پرداخت TON</b>\n\n"
        "درصدی که می‌خواهی روی هر پرداخت اضافه شود را بفرست (مثلاً <code>3</code>).\n\n"
        f"کارمزد فعلی: <b>{cur:g}٪</b>\n"
        f"حداکثر مجاز: <b>{MAX_FEE_PERCENT}٪</b> — برای حذف کارمزد <code>0</code> بفرست.")
    await cb.answer()


@platform_ton_direct_router.message(PTonDirectFSM.fee, F.text)
async def ptond_setfee_save(msg: Message, state: FSMContext):
    from platform_bot import set_pset
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    raw = (msg.text or "").strip().translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    ).replace("٪", "").replace("%", "").replace("/", ".").strip()
    try:
        val = Decimal(raw)
    except Exception:
        await msg.answer("❌ عدد نامعتبر است. یک عدد بفرست (مثلاً 3 یا 2.5):")
        return
    if val < 0 or val > MAX_FEE_PERCENT:
        await msg.answer(f"❌ کارمزد باید بین <b>0</b> تا <b>{MAX_FEE_PERCENT}</b> درصد باشد:")
        return
    await set_pset("pton_direct_fee_percent", str(val.normalize()))
    await state.clear()
    text, kb = await _menu_text_kb()
    note = ("✅ کارمزد حذف شد." if val == 0 else f"✅ کارمزد روی <b>{val:g}٪</b> تنظیم شد.")
    await msg.answer(note + "\n\n" + text, reply_markup=kb, parse_mode="HTML")


@platform_ton_direct_router.callback_query(F.data == "ptond:toggle")
async def ptond_toggle(cb: CallbackQuery, state: FSMContext):
    from platform_bot import get_pset, set_pset
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    if not await wallet_address():
        await cb.answer("اول آدرس ولت را ثبت کن.", show_alert=True)
        return
    cur = await get_pset("pton_direct_enabled", "0")
    await set_pset("pton_direct_enabled", "0" if cur == "1" else "1")
    await ptond_menu(cb, state)


# === PATCH_TON_DIRECT_INVOICES_V1 ===
# پنلِ وضعیتِ فاکتورها: لیستِ آخرین فاکتورها + جستجو با کدِ پیگیری، برایِ
# سوپرادمین. مشخصاتِ پرداخت‌کننده (نام/یوزرنیم/آیدی) از PlatformUser خوانده
# می‌شود؛ همان جدولی که یوزرنیم را مستقل از محدودیت‌هایِ تلگرام ذخیره می‌کند.

INVOICE_LIST_LIMIT = 8
_STATUS_LABEL = {"paid": "✅ پرداخت‌شده", "waiting": "⏳ در انتظار", "expired": "⌛️ منقضی‌شده"}


async def _invoice_row(ch: "PlatformTonDirectCharge", s) -> str:
    u = await s.scalar(select(PlatformUser).where(
        PlatformUser.telegram_id == ch.user_telegram_id))
    who = (f"@{u.username}" if u and u.username else
           (u.full_name if u and u.full_name else str(ch.user_telegram_id)))
    icon = {"paid": "✅", "waiting": "⏳", "expired": "⌛️"}.get(ch.status, "❔")
    return f"{icon} <code>{ch.memo}</code> — {int(ch.amount_toman or 0):,} ت — {who}"


async def _invoice_detail_text(ch: "PlatformTonDirectCharge", s) -> str:
    u = await s.scalar(select(PlatformUser).where(
        PlatformUser.telegram_id == ch.user_telegram_id))
    uname = f"@{u.username}" if u and u.username else "—"
    fname = (u.full_name if u else None) or "—"
    return (
        "🧾 <b>جزئیات فاکتور</b>\n\n"
        "<blockquote>"
        f"👤 نام: {fname}\n"
        f"🔗 یوزرنیم: {uname}\n"
        f"🆔 آیدی عددی: <code>{ch.user_telegram_id}</code>"
        "</blockquote>\n\n"
        f"کد پیگیری: <code>{ch.memo}</code>\n"
        f"وضعیت: <b>{_STATUS_LABEL.get(ch.status, ch.status)}</b>\n"
        f"🛒 خرید: شارژ کیف پول\n"
        f"💰 مبلغ: <b>{int(ch.amount_toman or 0):,} تومان</b>\n"
        f"💎 مبلغ TON: <code>{ch.ton_amount}</code>\n"
        + (f"🔗 tx: <code>{ch.tx_hash[:24]}…</code>\n" if ch.tx_hash else "")
        + f"🕒 ساخته‌شده: {ch.created_at:%Y-%m-%d %H:%M}")


@platform_ton_direct_router.callback_query(F.data == "ptond:invoices")
async def ptond_invoices(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.clear()
    async with Session() as s:
        rows = (await s.scalars(select(PlatformTonDirectCharge).order_by(
            PlatformTonDirectCharge.id.desc()).limit(INVOICE_LIST_LIMIT))).all()
        lines = [await _invoice_row(ch, s) for ch in rows]
    text = ("🧾 <b>وضعیت فاکتورهای TON مستقیم</b>\n"
            f"(آخرین {INVOICE_LIST_LIMIT} فاکتور)\n\n"
            + ("\n".join(lines) if lines else "فاکتوری ثبت نشده.")
            + "\n\nبرای دیدنِ جزئیاتِ کاملِ هر فاکتور (مشخصاتِ پرداخت‌کننده)، "
            "کدِ پیگیری‌اش را با دکمهٔ زیر جستجو کن.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 جستجو با کد پیگیری", callback_data="ptond:searchinv")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ptond:menu", style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_ton_direct_router.callback_query(F.data == "ptond:searchinv")
async def ptond_searchinv(cb: CallbackQuery, state: FSMContext):
    if not await _is_super(cb.from_user.id):
        await cb.answer("فقط سوپرادمین.", show_alert=True)
        return
    await state.set_state(PTonDirectFSM.search)
    await cb.message.answer("🔍 کدِ پیگیریِ فاکتور را بفرست (مثلاً <code>PBA1B2C3</code>):")
    await cb.answer()


@platform_ton_direct_router.message(PTonDirectFSM.search, F.text)
async def ptond_search_save(msg: Message, state: FSMContext):
    if not await _is_super(msg.from_user.id):
        await state.clear()
        return
    memo = (msg.text or "").strip().upper()
    await state.clear()
    async with Session() as s:
        ch = await s.scalar(select(PlatformTonDirectCharge).where(
            PlatformTonDirectCharge.memo == memo))
        if not ch:
            await msg.answer("❌ فاکتوری با این کد پیدا نشد.")
            return
        text = await _invoice_detail_text(ch, s)
    await msg.answer(text)


@platform_ton_direct_router.callback_query(F.data == "ptond:track")
async def ptond_track_start(cb: CallbackQuery, state: FSMContext):
    """پیگیریِ کاربرِ عادی — فقط فاکتورهایِ خودش را می‌بیند."""
    if not await is_available():
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    await state.set_state(PTonDirectFSM.track)
    text = ("🔍 <b>پیگیریِ فاکتور TON</b>\n\n"
            "کدِ پیگیری‌ای که موقعِ ساختِ فاکتور به تو داده شد را بفرست:")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ptond:back_charge")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_ton_direct_router.message(PTonDirectFSM.track, F.text)
async def ptond_track_save(msg: Message, state: FSMContext):
    memo = (msg.text or "").strip().upper()
    await state.clear()
    async with Session() as s:
        ch = await s.scalar(select(PlatformTonDirectCharge).where(
            PlatformTonDirectCharge.memo == memo,
            PlatformTonDirectCharge.user_telegram_id == msg.from_user.id))
    if not ch:
        await msg.answer("❌ فاکتوری با این کد برایِ حسابِ تو پیدا نشد.")
        return
    await msg.answer(
        "🧾 <b>وضعیتِ فاکتور</b>\n\n"
        f"کد پیگیری: <code>{ch.memo}</code>\n"
        f"وضعیت: <b>{_STATUS_LABEL.get(ch.status, ch.status)}</b>\n"
        f"💰 مبلغ: <b>{int(ch.amount_toman or 0):,} تومان</b>\n"
        f"💎 مبلغ TON: <code>{ch.ton_amount}</code>")


# ═══════════════════════ جریانِ کاربر ═══════════════════════

async def _charge_entry_text_kb() -> tuple[str, InlineKeyboardMarkup]:
    """متن+کیبوردِ صفحهٔ ورودیِ TON مستقیم — هم برایِ ورودِ اول، هم برایِ بازگشت."""
    rate = await ton_toman_rate()
    fee = await fee_percent()
    min_t = await min_toman_setting()
    max_t = await max_toman_setting()

    lines = ["💎 <b>شارژ کیف پول با واریز مستقیم TON</b>\n",
             "مبلغ را به تومان می‌دهی، به TON تبدیل می‌شود و یک فاکتور با آدرس "
             "و مبلغِ دقیق برایت ساخته می‌شود. همین‌که واریز را روی زنجیره ببینیم، "
             "کیف پولت خودکار شارژ می‌شود."]
    if rate:
        lines.append(f"\n📈 نرخ لحظه‌ای: هر TON حدود <b>{int(rate):,} تومان</b>")
    if fee > 0:
        lines.append(f"➕ کارمزد پرداخت: <b>{fee:g}٪</b>")
    if min_t > 0:
        lines.append(f"💵 حداقل شارژ: <b>{min_t:,} تومان</b>")
    if max_t > 0:
        lines.append(f"💵 حداکثر شارژ: <b>{max_t:,} تومان</b>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 پرداخت", callback_data="ptond:pay")],
        [InlineKeyboardButton(text="🔍 پیگیری فاکتورِ TON با کد", callback_data="ptond:track")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ptond:back_wallet")],
    ])
    return "\n".join(lines), kb


@platform_ton_direct_router.callback_query(F.data == "charge_pton_direct")
async def charge_pton_direct(cb: CallbackQuery, state: FSMContext):
    """صفحهٔ ورودیِ TON مستقیم: توضیح + دکمهٔ پرداخت + دکمهٔ پیگیریِ فاکتور.

    === PATCH_TON_DIRECT_INPLACE_BACK_V1 ===
    به‌جایِ فرستادنِ پیامِ جدید، همون پیامِ منویِ کیف‌پول edit می‌شود؛ این‌طوری
    دکمهٔ «بازگشت» هم می‌تونه همین پیام رو دوباره edit کنه و برگردونه به
    کیف‌پول، بدونِ این‌که یه پیامِ کیف‌پولِ تکراری بفرسته.
    """
    if not await is_available():
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    await state.clear()
    text, kb = await _charge_entry_text_kb()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_ton_direct_router.callback_query(F.data == "ptond:back_wallet")
async def ptond_back_wallet(cb: CallbackQuery, state: FSMContext):
    """بازگشت از صفحهٔ TON مستقیم به منویِ کیف‌پول — همون پیام edit می‌شود."""
    await state.clear()
    from platform_bot import _platform_wallet_kb
    kb = await _platform_wallet_kb()
    try:
        await cb.message.edit_text("👛 <b>کیف پول</b>", reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("platform ton_direct: حذفِ صفحهٔ TON مستقیم ممکن نشد")
        await cb.message.answer("👛 <b>کیف پول</b>", reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_ton_direct_router.callback_query(F.data == "ptond:back_charge")
async def ptond_back_charge(cb: CallbackQuery, state: FSMContext):
    """بازگشت از «پرداخت»/«پیگیری» به صفحهٔ ورودیِ TON مستقیم — همون پیام edit می‌شود."""
    await state.clear()
    text, kb = await _charge_entry_text_kb()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("platform ton_direct: حذفِ صفحهٔ پرداخت/پیگیری ممکن نشد")
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_ton_direct_router.callback_query(F.data == "ptond:pay")
async def ptond_pay(cb: CallbackQuery, state: FSMContext):
    if not await is_available():
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    rate = await ton_toman_rate()
    if not rate:
        await cb.answer("نرخ لحظه‌ای TON در دسترس نیست، کمی بعد امتحان کن.", show_alert=True)
        return
    fee = await fee_percent()
    floor_toman = int((MIN_TON * rate * Decimal(100) / (Decimal(100) + fee)
                       ).to_integral_value())
    min_t = await min_toman_setting()
    max_t = await max_toman_setting()
    effective_min = max(floor_toman, min_t) if min_t > 0 else floor_toman

    await state.set_state(PTonDirectFSM.amount)
    limits = f"حداقل حدود <b>{effective_min:,} تومان</b>"
    if max_t > 0:
        limits += f" و حداکثر <b>{max_t:,} تومان</b>"
    text = ("💳 <b>پرداخت</b>\n\n"
            "مبلغی که می‌خواهی شارژ کنی را <b>به تومان</b> بفرست "
            f"({limits}):")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ptond:back_charge")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@platform_ton_direct_router.message(PTonDirectFSM.amount, F.text)
async def ptond_amount(msg: Message, state: FSMContext):
    from platform_bot import fa_to_int
    raw = (msg.text or "").strip()
    amount = fa_to_int(raw)
    if amount is None or amount <= 0:
        await msg.answer("مبلغ نامعتبر است. یک عدد به <b>تومان</b> بفرست:")
        return

    rate = await ton_toman_rate()
    if not rate:
        await state.clear()
        await msg.answer("❌ نرخ لحظه‌ای در دسترس نیست. کمی بعد دوباره امتحان کن.")
        return
    fee = await fee_percent()

    min_t = await min_toman_setting()
    max_t = await max_toman_setting()
    if min_t > 0 and amount < min_t:
        await msg.answer(f"❌ حداقلِ مبلغِ شارژ <b>{min_t:,} تومان</b> است. مبلغِ بیشتری بفرست:")
        return
    if max_t > 0 and amount > max_t:
        await msg.answer(f"❌ حداکثرِ مبلغِ شارژ <b>{max_t:,} تومان</b> است. مبلغِ کمتری بفرست:")
        return

    payable_toman = (Decimal(amount) * (Decimal(100) + fee) / Decimal(100))
    ton = payable_toman / rate
    if ton < MIN_TON:
        await msg.answer(
            f"❌ مبلغ خیلی کم است (حدود {ton:.4f} TON می‌شود).\n"
            f"حداقل <b>{MIN_TON} TON</b> لازم است. مبلغ بیشتری بفرست:")
        return

    addr = await wallet_address()
    ton_final = _uniquify(ton)
    memo = _new_memo()
    now = _dt.datetime.utcnow()
    async with Session() as s:
        s.add(PlatformTonDirectCharge(
            user_telegram_id=msg.from_user.id, memo=memo,
            amount_toman=int(amount), ton_amount=float(ton_final),
            rate_toman=int(rate), wallet_address=addr, status="waiting",
            expires_at=now + _dt.timedelta(minutes=INVOICE_TTL_MINUTES)))
        await s.commit()
    await state.clear()

    deep, web = payment_links(addr, ton_final, memo)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 پرداخت با Tonkeeper", url=web)],
        [InlineKeyboardButton(text=f"📋 کپی کد فاکتور ({memo})",
                              copy_text=CopyTextButton(text=memo))],
        [InlineKeyboardButton(text="🔄 بررسی وضعیت پرداخت",
                              callback_data=f"ptond:check:{memo}",
                              style=ButtonStyle.PRIMARY)],
    ])
    sent = await msg.answer(
        "💎 <b>فاکتور پرداخت TON</b>\n\n"
        f"🧾 کد فاکتور: <code>{memo}</code>\n"
        f"💰 مبلغ شارژ: <b>{amount:,} تومان</b>\n"
        + (f"➕ کارمزد پرداخت ({fee:g}٪): <b>{int(payable_toman) - amount:,} تومان</b>\n"
           f"🧮 مجموع قابل پرداخت: <b>{int(payable_toman):,} تومان</b>\n" if fee > 0 else "")
        + f"💎 مبلغ دقیق: <code>{ton_final}</code> TON\n"
        f"👛 آدرس مقصد:\n<code>{addr}</code>\n\n"
        "<blockquote>روی دکمهٔ زیر بزن تا کیف پولت با مبلغ و کد آماده باز شود.\n"
        "اگر دستی واریز می‌کنی، حتماً <b>همین مبلغ دقیق</b> را بفرست و کد "
        f"<code>{memo}</code> را در قسمت <b>Comment/Memo</b> بنویس.</blockquote>\n\n"
        f"⏳ این فاکتور تا <b>{INVOICE_TTL_MINUTES} دقیقه</b> اعتبار دارد.\n"
        "بعد از واریز، تایید معمولاً کمتر از یک دقیقه طول می‌کشد.\n\n"
        f"🔗 لینک مستقیم: <code>{deep}</code>",
        reply_markup=kb)

    async with Session() as s:
        await s.execute(
            sa_update(PlatformTonDirectCharge)
            .where(PlatformTonDirectCharge.memo == memo)
            .values(invoice_chat_id=sent.chat.id, invoice_message_id=sent.message_id))
        await s.commit()


@platform_ton_direct_router.callback_query(F.data.startswith("ptond:check:"))
async def ptond_check(cb: CallbackQuery, bot: Bot):
    memo = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(PlatformTonDirectCharge).where(
            PlatformTonDirectCharge.memo == memo))
        status = ch.status if ch else None
    if status == "paid":
        await cb.answer("✅ این فاکتور قبلاً تایید و شارژ شده است.", show_alert=True)
        return
    if status is None:
        await cb.answer("فاکتور پیدا نشد.", show_alert=True)
        return
    await cb.answer("⏳ در حال بررسی زنجیره…")
    await settle(bot)
    async with Session() as s:
        ch = await s.scalar(select(PlatformTonDirectCharge).where(
            PlatformTonDirectCharge.memo == memo))
    if ch and ch.status == "paid":
        return
    if ch and ch.status == "expired":
        await cb.message.answer(
            "⌛️ مهلت این فاکتور تمام شد.\n"
            "اگر واریز کرده‌ای نگران نباش — با پشتیبانی تماس بگیر و همین کد "
            f"<code>{memo}</code> را بده. اگر هنوز واریز نکرده‌ای، یک فاکتور "
            "تازه با نرخ روز بساز.")
        return
    await cb.message.answer(
        "⏳ هنوز واریزی برای این فاکتور روی زنجیره دیده نشد.\n"
        "اگر همین الان پرداخت کرده‌ای، چند لحظه صبر کن و دوباره بررسی بزن.")
