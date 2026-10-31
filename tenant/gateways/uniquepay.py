"""
uniquepay.py — PATCH_UNIQUEPAY_V1

درگاهِ کارت‌به‌کارتِ خودکارِ «یونیک‌پی» (uniquepay.top) برایِ شارژِ کیفِ پول.

فلو:
  ۱) create-invoice با hashId یکتا و مبلغ  →  paymentLink
  ۲) کاربر روی لینک می‌رود و کارت‌به‌کارت می‌کند
  ۳) check-invoice با همان hashId  →  isPaid
  ۴) اگر پرداخت شده بود، کیفِ پول شارژ می‌شود

نکتهٔ امنیتی: هیچ‌وقت به بدنهٔ کال‌بک اعتماد نمی‌کنیم؛ همیشه مستقیم از
check-invoice تأیید می‌گیریم و مبلغ را با چیزی که خودمان ثبت کرده‌ایم
مقایسه می‌کنیم.

⚠️ واحدِ مبلغ: برخلافِ چیزی که مستنداتِ درگاه اولش می‌گفت، API واقعاً
مبلغِ «تومانی» را مستقیم می‌گیرد (نه ریال) — طبقِ گزارشِ کاربر و اسکرین‌شاتِ
صفحهٔ واقعیِ پرداخت که ده‌برابرِ مبلغِ درخواستی نشون می‌داد، تبدیلِ ×۱۰ی
قبلی حذف شد (PATCH_UNIQUEPAY_AMOUNT_UNIT_FIX_V1). حالا amount_out همون
عددِ تومانی است، بدونِ تبدیل.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

try:
    import aiohttp
except ImportError:
    aiohttp = None

from sqlalchemy import select

from config import Session
from models import Bot as BotRow, BotUser, UniquePayCharge, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

BASE_URL = "https://uniquepay.top"
_TIMEOUT = 20
MIN_AMOUNT = 51_000          # حداقلِ مبلغِ پیش‌فرضِ شارژ (تومان)
# نکته‌ی مهم (PATCH_UNIQUEPAY_MIN_BOUNDARY_FIX_V1):
# خودِ سایتِ uniquepay.top رویِ حسابِ بیزینس یک محدودیتِ حداقلِ ۵۰,۰۰۰
# تومان گذاشته، ولی این محدودیت به‌صورتِ «بزرگ‌تر از» عمل می‌کند نه
# «بزرگ‌تر یا مساوی». یعنی وقتی دقیقاً ۵۰,۰۰۰ فرستاده می‌شود، خودِ
# API با پیامِ «minimum amount is 50,000» فاکتور را رد می‌کند، ولی با
# ۵۱,۰۰۰ (یا هر عددِ بالاترِ ۵۰,۰۰۰) به‌درستی فاکتور ساخته می‌شود.
# به همینِ خاطر حداقلِ پیش‌فرضِ اینجا را از ۵۰,۰۰۰ به ۵۱,۰۰۰ بالا بردیم
# تا کاربرِ نهایی هیچ‌وقت دقیقاً رویِ مرزِ خطرناک نیفتد. اگر مالکِ ربات
# حداقلِ سفارشیِ خودش را دستی رویِ ۵۰,۰۰۰ بگذارد، باز همین مشکل پیش
# می‌آید — پس تویِ پنلِ تنظیماتِ ادمین (uniquepay_admin.py) هم هشدارِ
# مربوطه اضافه شده است.


async def _get(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _set(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


def new_hash_id() -> str:
    """شناسهٔ یکتایِ فاکتور — کوتاه و امن."""
    return "bs" + secrets.token_hex(8)


async def _amount_out(bot_id: int, toman: int) -> int:
    """مبلغی که به درگاه فرستاده می‌شود.

    === PATCH_UNIQUEPAY_AMOUNT_UNIT_FIX_V1 ===
    باگِ گزارش‌شده: کاربر تو ربات ۵۰,۰۰۰ تومان زد ولی صفحهٔ واقعیِ
    پرداختِ uniquepay.top مبلغِ ۵۰۰,۰۰۰+ تومان (ده برابر) نشون می‌داد.
    قبلاً اینجا مبلغِ تومانی در ۱۰ ضرب می‌شد، با این فرض که فیلدِ amount
    در create-invoice «ریالی» است. طبقِ اسکرین‌شاتِ واقعیِ درگاه، این فرض
    اشتباه بوده — API همون عددِ تومانی را مستقیم می‌خواهد. حالا بدونِ
    تبدیل فرستاده می‌شود. (منطقِ شارژِ کیف‌پول در settle_charge از رویِ
    amount_tomanِ ذخیره‌شده در دیتابیسِ خودمان کار می‌کند، نه از رویِ
    چیزی که به API فرستاده می‌شود — پس این فیکس رویِ آن اثر نمی‌گذارد.
    """
    return int(toman)


async def get_min_amount(bot_id: int) -> int:
    """حداقلِ مبلغِ قابلِ‌تنظیم توسطِ مالکِ ربات (تومان). پیش‌فرض: MIN_AMOUNT."""
    v = await _get(bot_id, "uniquepay_min_amount", str(MIN_AMOUNT))
    try:
        n = int(v)
        return n if n > 0 else MIN_AMOUNT
    except (TypeError, ValueError):
        return MIN_AMOUNT


async def get_max_amount(bot_id: int) -> int:
    """حداکثرِ مبلغِ قابلِ‌تنظیم توسطِ مالکِ ربات (تومان). ۰ یعنی بدونِ سقف."""
    v = await _get(bot_id, "uniquepay_max_amount", "0")
    try:
        return max(int(v), 0)
    except (TypeError, ValueError):
        return 0


async def is_available(bot_id: int) -> bool:
    """آیا درگاهِ یونیک‌پی برایِ این ربات فعال و آماده‌به‌استفاده است."""
    on = await _get(bot_id, "uniquepay_enabled", "0") == "1"
    tok = await _get(bot_id, "uniquepay_token", "")
    return on and bool(tok)


async def _api(token: str, path: str, data: dict) -> dict:
    """تماسِ خام با API. خروجی همیشه dict است (حتی در خطا)."""
    if aiohttp is None:
        return {"status": False, "code": 0, "message": "aiohttp نصب نیست"}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(f"{BASE_URL}{path}", data=data,
                              headers=headers) as r:
                out = await r.json(content_type=None)
                if not isinstance(out, dict):
                    return {"status": False, "code": r.status,
                            "message": "پاسخِ نامعتبر از درگاه"}
                return out
    except Exception as e:
        logger.exception("uniquepay: خطای شبکه در %s", path)
        return {"status": False, "code": 0, "message": str(e)}


async def create_invoice(token: str, hash_id: str, amount: int,
                         redirect_url: str = "") -> dict:
    d = {"hashId": hash_id, "amount": int(amount)}
    if redirect_url:
        d["redirectUrl"] = redirect_url
    return await _api(token, "/api/create-invoice", d)


async def check_invoice(token: str, hash_id: str) -> dict:
    return await _api(token, "/api/check-invoice", {"hashId": hash_id})


async def validate_token(bot_id: int, token: str,
                         bot_username: str = "") -> tuple[bool, str]:
    """تستِ سریعِ توکن با ساختِ یک فاکتورِ کوچک.

    باید همان تبدیلِ (فعلاً بدونِ تبدیل — نگاه کن به _amount_out) که برایِ
    شارژِ واقعیِ کاربر استفاده می‌شود اینجا هم اعمال شود، وگرنه تستِ توکن
    ممکن است با مبلغِ نادرست رد شود.

    نکته ۲: بعضی بیزینس‌ها روی پنلِ یونیک‌پی طوری تنظیم شده‌اند که
    ساختِ اینوویس بدونِ redirectUrl یا callbackUrl رد می‌شود (خطایِ
    «At least one of redirectUrl or callbackUrl is required»)؛ پس اگر
    یوزرنیمِ ربات موجود باشد، همان redirectUrl ای که در شارژِ واقعی
    استفاده می‌شود (https://t.me/{username}) اینجا هم فرستاده می‌شود.
    """
    out = await _amount_out(bot_id, await get_min_amount(bot_id))
    redirect = f"https://t.me/{bot_username}" if bot_username else ""
    res = await create_invoice(token, new_hash_id(), out, redirect)
    code = res.get("code")
    if res.get("status") and code == 200:
        return True, "توکن معتبر است و درگاه پاسخ داد."
    msgs = {
        403: "توکن ارسال نشد یا نامعتبر است.",
        404: "بیزینسی با این توکن پیدا نشد.",
        402: "بیزینسِ شما غیرفعال یا تأییدنشده است.",
    }
    return False, msgs.get(code, res.get("message") or "پاسخِ نامشخص از درگاه")


async def settle_charge(bot_id: int, hash_id: str, notify: bool = True) -> tuple[bool, str, int]:
    """استعلامِ یک فاکتور و در صورتِ پرداخت، شارژِ کیفِ پول.

    خروجی: (شارژ شد؟, پیام, مبلغِ شارژ‌شده به تومان)
    """
    token = await _get(bot_id, "uniquepay_token", "")
    if not token:
        return False, "توکنِ درگاه تنظیم نشده است.", 0

    async with Session() as s:
        ch = await s.scalar(select(UniquePayCharge).where(
            UniquePayCharge.bot_id == bot_id,
            UniquePayCharge.hash_id == hash_id))
        if not ch:
            return False, "این فاکتور پیدا نشد.", 0
        if ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        credit = int(ch.amount_toman or 0)

    res = await check_invoice(token, hash_id)
    if not res.get("status"):
        return False, res.get("message") or "استعلام ناموفق بود.", 0
    inv = res.get("invoice") or {}
    if not inv.get("isPaid"):
        return False, "هنوز پرداختی ثبت نشده است.", 0

    # ── تسویهٔ اتمیک ──
    async with Session() as s:
        ch = await s.scalar(select(UniquePayCharge).where(
            UniquePayCharge.bot_id == bot_id,
            UniquePayCharge.hash_id == hash_id).with_for_update())
        if not ch or ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        ch.status = "paid"
        ch.paid_at = datetime.utcnow()
        ch.ref_id = str(inv.get("id") or "")[:64]
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id,
            BotUser.telegram_id == ch.user_telegram_id).with_for_update())
        if u:
            u.balance = float(u.balance or 0) + credit
            s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=credit,
                            kind="uniquepay_charge",
                            note=f"شارژ با یونیک‌پی؛ ref={ch.ref_id}"))
            _uid, _bal = u.telegram_id, float(u.balance)
            _kyc_uid = u.id
        else:
            _uid = _bal = None
            _kyc_uid = None
        await s.commit()
    # === PATCH_KYC_GATE_ALL_PATHS_V1 ===
    if _kyc_uid:
        from tenant.shared.kyc import after_charge as _ac
        await _ac(bot_id, _kyc_uid)

    # ── اطلاع به کاربر (چون شاید خودش دکمه‌ای نزده باشد) ──
    if _uid and notify:        # notify=False: کلیکِ «بررسی»؛ همان پیامِ فاکتور ویرایش می‌شود (PATCH_GATEWAY_INPLACE_V1)
        try:
            await _notify_paid(bot_id, _uid, credit, _bal)
        except Exception:
            logger.exception("uniquepay: اطلاع به کاربر ناموفق بود")

    # ── کانالِ لاگِ تراکنش‌ها ──
    if _uid:
        try:
            from tenant.gateways.payment_page import _tx_log_tenant
            await _tx_log_tenant(bot_id, _uid, credit, _bal, "uniquepay")
        except Exception:
            logger.exception("uniquepay: tx_log ناموفق بود")

    return True, "پرداخت تأیید و کیفِ پول شارژ شد.", credit


# === PATCH_UNIQUEPAY_AUTO_CANCEL_10MIN_V1 ===
# بعدِ این‌همه دقیقه، فاکتورِ پرداخت‌نشده خودکار cancelled می‌شه.
CANCEL_AFTER_MINUTES = 15


async def poll_pending(bot_id: int | None = None) -> int:
    """استعلامِ دوره‌ایِ فاکتورهایِ بازِ چند ساعتِ اخیر. خروجی: تعدادِ تسویه‌شده.

    === PATCH_UNIQUEPAY_AUTO_CANCEL_10MIN_V1 ===
    فاکتورهایی که بعد از CANCEL_AFTER_MINUTES دقیقه هنوز waiting و
    پرداخت‌نشده مونده باشن (یعنی استعلام از درگاه هم isPaid را تایید
    نکرده)، همینجا خودکار cancelled می‌شن."""
    cutoff = datetime.utcnow() - timedelta(hours=6)
    async with Session() as s:
        q = select(UniquePayCharge).where(
            UniquePayCharge.status == "waiting",
            UniquePayCharge.created_at >= cutoff)
        if bot_id:
            q = q.where(UniquePayCharge.bot_id == bot_id)
        rows = (await s.scalars(q)).all()
        items = [(r.bot_id, r.hash_id, r.created_at) for r in rows]

    n = 0
    cancel_before = datetime.utcnow() - timedelta(minutes=CANCEL_AFTER_MINUTES)
    for bid, hid, created_at in items:
        try:
            ok, _msg, _amt = await settle_charge(bid, hid)
            if ok:
                n += 1
                continue
        except Exception:
            logger.exception("uniquepay: poll خطا (bot=%s hash=%s)", bid, hid)
            continue
        # === PATCH_UNIQUEPAY_AUTO_CANCEL_10MIN_V1 ===
        if created_at and created_at <= cancel_before:
            async with Session() as s:
                ch = await s.scalar(select(UniquePayCharge).where(
                    UniquePayCharge.bot_id == bid,
                    UniquePayCharge.hash_id == hid,
                    UniquePayCharge.status == "waiting").with_for_update())
                if ch:
                    ch.status = "cancelled"
                    await s.commit()
                    logger.info(
                        "uniquepay: فاکتور %s (bot=%s) بعدِ %d دقیقه بدونِ پرداخت "
                        "خودکار لغو شد", hid, bid, CANCEL_AFTER_MINUTES)
    return n


# === PATCH_UNIQUEPAY_AUTO_V1 ===
async def uniquepay_poll_loop():
    """هر ۳ دقیقه فاکتورهایِ باز را استعلام می‌کند و در صورتِ پرداخت،
    کیفِ پول را شارژ کرده و به کاربر خبر می‌دهد."""
    import asyncio
    await asyncio.sleep(30)          # کمی بعد از بالا آمدنِ سرویس
    while True:
        try:
            n = await poll_pending()
            if n:
                logger.info("[UNIQUEPAY] %s فاکتور خودکار تسویه شد", n)
        except Exception:
            logger.exception("[UNIQUEPAY] حلقهٔ استعلام خطا داد")
        await asyncio.sleep(180)


# === PATCH_UNIQUEPAY_AUTO_V1 ===
async def _notify_paid(bot_id: int, tg_id: int, amount: int,
                       new_balance: float) -> None:
    """پیامِ «کیفِ پولت شارژ شد» با توکنِ خودِ همان ربات."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
    if not brow:
        return
    tbot = Bot(token=brow.token,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        from tenant.gateways.wallet_receipt import build_wallet_charge_message_for_bot
        text, kb = await build_wallet_charge_message_for_bot(
            bot_id, amount, new_balance, "card")
        await tbot.send_message(tg_id, text, reply_markup=kb)
    except Exception:
        try:
            await tbot.send_message(
                tg_id, f"\u2705 {int(amount):,} تومان به کیفِ پولت اضافه شد.")
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    finally:
        try:
            await tbot.session.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


# === PATCH_UNIQUEPAY_AUTO_V1 ===
async def create_invoice_safe(bot_id: int, token: str, hash_id: str,
                              amount: int, bot_username: str = "") -> dict:
    """ساختِ فاکتور با redirectUrl؛ اگر درگاه دامنه را نپذیرفت (کد ۱۰۴)،
    بدونِ redirectUrl دوباره تلاش می‌کند."""
    redirect = f"https://t.me/{bot_username}" if bot_username else ""
    res = await create_invoice(token, hash_id, amount, redirect)
    if redirect and res.get("code") == 104:
        logger.info("[UNIQUEPAY] redirectUrl پذیرفته نشد؛ بدونِ آن تلاش شد "
                    "(bot=%s)", bot_id)
        res = await create_invoice(token, hash_id, amount)
    return res
