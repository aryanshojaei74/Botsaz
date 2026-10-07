"""
apay.py — PATCH_APAY_GATEWAY_V1

درگاهِ کارت‌به‌کارتِ آنیِ «آپی» (APay — apaygate.shop) برایِ شارژِ کیف پولِ ربات‌هایِ مستأجر.

فلو (طبقِ مستنداتِ رسمی /docs):
  ۱) POST /api/v1/payments  {amount(ریال), order_id, callback_url, return_url}  →  pay_url، pay_amount
  ۲) مشتری در صفحهٔ pay_url دقیقاً pay_amount را کارت‌به‌کارت می‌کند (رقمِ یکتایِ ۱ تا ۹۹ تومان اضافه دارد)
  ۳) گوشیِ فروشنده پیامکِ بانک را به APay می‌دهد، APay پرداخت را paid می‌کند و وب‌هوک می‌زند
  ۴) ما (از وب‌هوک یا حلقهٔ استعلام) POST /payments/:id/verify می‌زنیم و فقط با verified=true و
     تطبیقِ order_id و مبلغ با رکوردِ خودمان، کیف پول را شارژ می‌کنیم.

نکته‌هایِ امنیتی:
  • بدنهٔ وب‌هوک هرگز به‌تنهایی پذیرفته نمی‌شود؛ امضایِ HMAC (اگر secret ثبت شده) فقط فیلترِ اولیه است و
    تأییدِ نهایی همیشه از verify ِ خودِ APay و مقایسه با دیتابیسِ ماست.
  • شارژ اتمیک است (قفلِ ردیف) و هر پرداخت فقط یک‌بار تسویه می‌شود (وب‌هوکِ at-least-once).
  • واحد: همهٔ ربات تومان؛ API ریال می‌گیرد (×۱۰ فقط این‌جا).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta

try:
    import aiohttp
except ImportError:      # pragma: no cover
    aiohttp = None

from sqlalchemy import select

from config import Session
from models import ApayCharge, Bot as BotRow, BotUser, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

BASE_URL = "https://apaygate.shop"
_TIMEOUT = 15
MIN_AMOUNT = 10_000            # حداقلِ پیش‌فرضِ شارژ (تومان) — کفِ فنیِ APay ۱٬۰۰۰ تومان است
API_MIN_TOMAN = 1_000          # ۱۰٬۰۰۰ ریال
API_MAX_TOMAN = 100_000_000    # ۱٬۰۰۰٬۰۰۰٬۰۰۰ ریال
POLL_FOR_MINUTES = 30          # ۱۵ دقیقه اعتبار + ۱۰ دقیقه پیامکِ دیرهنگام + حاشیه


async def _get(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _set(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


def new_order_id() -> str:
    return "bs" + secrets.token_hex(8)


async def get_min_amount(bot_id: int) -> int:
    try:
        n = int(await _get(bot_id, "apay_min_amount", str(MIN_AMOUNT)))
    except (TypeError, ValueError):
        n = MIN_AMOUNT
    return max(n, API_MIN_TOMAN) if n > 0 else MIN_AMOUNT


async def get_max_amount(bot_id: int) -> int:
    """۰ یعنی بدونِ سقف (سقفِ فنیِ APay همچنان اعمال می‌شود)."""
    try:
        return max(int(await _get(bot_id, "apay_max_amount", "0")), 0)
    except (TypeError, ValueError):
        return 0


async def is_available(bot_id: int) -> bool:
    on = await _get(bot_id, "apay_enabled", "0") == "1"
    return on and bool(await _get(bot_id, "apay_token", ""))


# ─────────────── API ───────────────
async def _api(token: str, method: str, path: str, data: dict | None = None) -> tuple[int, dict]:
    """(کدِ HTTP، بدنه). در خطایِ شبکه کد ۰ و بدنهٔ {"error": {...}}."""
    if aiohttp is None:
        return 0, {"error": {"code": "no_aiohttp", "message": "aiohttp نصب نیست"}}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.request(method, f"{BASE_URL}{path}", json=data, headers=headers) as r:
                try:
                    out = await r.json(content_type=None)
                except Exception:
                    out = None
                if not isinstance(out, (dict, list)):
                    out = {"error": {"code": "bad_response", "message": f"پاسخِ نامعتبر (HTTP {r.status})"}}
                return r.status, out
    except Exception as e:
        logger.warning("apay: خطای شبکه در %s %s: %s", method, path, e)
        return 0, {"error": {"code": "network", "message": str(e)[:200]}}


_ERR_FA = {
    "unauthorized": "توکنِ API اشتباه است یا ثبت نشده.",
    "insufficient_balance": "موجودیِ کیف پولِ حسابِ آپی (برایِ کارمزد) کافی نیست؛ از رباتِ آپی شارژش کن.",
    "suspended": "حسابِ آپی غیرفعال یا مسدود است.",
    "not_active": "ثبت‌نامِ حسابِ آپی کامل نشده است.",
    "no_device": "هیچ کارتِ فعال و تأییدشده‌ای در آپی نداری؛ اول در رباتِ آپی کارت اضافه کن.",
    "invalid_amount": "مبلغ خارج از محدودهٔ مجازِ آپی است.",
    "busy": "تعدادِ پرداخت‌هایِ باز با این مبلغ زیاد است؛ کمی بعد دوباره امتحان کن.",
    "rate_limited": "درخواست‌ها بیش از حد است؛ کمی بعد دوباره امتحان کن.",
    "duplicate_order": "این سفارش قبلاً ثبت شده است.",
    "invalid_url": "آدرسِ بازگشت/وب‌هوک معتبر نیست.",
}


def err_text(code: int, body) -> str:
    e = (body or {}).get("error") if isinstance(body, dict) else None
    if isinstance(e, dict):
        c = str(e.get("code") or "")
        return _ERR_FA.get(c) or str(e.get("message") or c or f"خطا (HTTP {code})")
    return f"خطا (HTTP {code})"


async def create_payment(token: str, *, amount_toman: int, order_id: str, description: str = "",
                         callback_url: str = "", return_url: str = "") -> tuple[int, dict]:
    d: dict = {"amount": int(amount_toman) * 10, "order_id": order_id}
    if description:
        d["description"] = description[:200]
    if callback_url:
        d["callback_url"] = callback_url
    if return_url:
        d["return_url"] = return_url
    return await _api(token, "POST", "/api/v1/payments", d)


async def verify_payment(token: str, payment_id: str) -> tuple[int, dict]:
    return await _api(token, "POST", f"/api/v1/payments/{payment_id}/verify")


async def wallet(token: str) -> tuple[int, dict]:
    return await _api(token, "GET", "/api/v1/wallet")


async def devices(token: str) -> tuple[int, dict | list]:
    return await _api(token, "GET", "/api/v1/devices")


async def validate_token(token: str) -> tuple[bool, str]:
    """تستِ بی‌هزینه: موجودیِ کیف پول و کارت‌هایِ متصل (هیچ پرداختی ساخته نمی‌شود)."""
    code, body = await wallet(token)
    if code != 200 or not isinstance(body, dict) or "balance" not in body:
        return False, err_text(code, body)
    bal_t = int(body.get("balance") or 0) // 10
    dcode, dev = await devices(token)
    n_dev = len(dev) if (dcode == 200 and isinstance(dev, list)) else None
    msg = f"توکن معتبر است. موجودیِ کیف پولِ آپی: {bal_t:,} تومان"
    if n_dev is not None:
        msg += f" · کارت/گوشیِ متصل: {n_dev}"
        if n_dev == 0:
            msg += "\n⚠️ هنوز کارتی در آپی اضافه نکرده‌ای؛ تا کارت اضافه نشود پرداختی ساخته نمی‌شود."
    if bal_t < 1_000:
        msg += "\n⚠️ موجودیِ کیف پولِ آپی کمتر از کارمزدِ هر پرداخت (۱٬۰۰۰ تومان) است؛ شارژش کن."
    return True, msg


async def webhook_url(bot_id: int) -> str:
    from tenant.gateways.custom_domain import get_bot_webhook_base
    base = (await get_bot_webhook_base(bot_id) or "").rstrip("/")
    return f"{base}/apay/webhook/{bot_id}" if base.startswith("https://") else ""


# ─────────────── تسویه ───────────────
async def settle_charge(bot_id: int, order_id: str) -> tuple[bool, str, int]:
    """استعلام از APay و در صورتِ تأیید، شارژِ اتمیکِ کیف پول. خروجی: (شارژ شد؟، پیام، مبلغ)."""
    token = await _get(bot_id, "apay_token", "")
    if not token:
        return False, "توکنِ درگاه تنظیم نشده است.", 0
    async with Session() as s:
        ch = await s.scalar(select(ApayCharge).where(
            ApayCharge.bot_id == bot_id, ApayCharge.order_id == order_id))
        if not ch:
            return False, "این فاکتور پیدا نشد.", 0
        if ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        if not ch.payment_id:
            return False, "این فاکتور شناسهٔ پرداخت ندارد.", 0
        payment_id, credit = ch.payment_id, int(ch.amount_toman or 0)

    code, res = await verify_payment(token, payment_id)
    if code != 200 or not isinstance(res, dict):
        return False, err_text(code, res), 0
    pay = res.get("payment") or {}
    if not res.get("verified") or str(pay.get("status")) != "paid":
        st = str(pay.get("status") or "")
        return False, ("مهلتِ این فاکتور تمام شده و پرداختی ثبت نشد." if st == "expired"
                       else "هنوز پرداختی ثبت نشده است."), 0
    # تطبیق با سفارشِ خودمان — هرگز به مبلغِ اعلامی اعتماد نمی‌کنیم
    if str(pay.get("order_id")) != order_id or int(pay.get("amount") or 0) != credit * 10:
        logger.error("apay: عدمِ تطابقِ پرداخت (bot=%s order=%s): %s", bot_id, order_id, pay)
        return False, "اطلاعاتِ پرداخت با فاکتور نمی‌خواند؛ با پشتیبانی تماس بگیر.", 0

    async with Session() as s:
        ch = await s.scalar(select(ApayCharge).where(
            ApayCharge.bot_id == bot_id, ApayCharge.order_id == order_id).with_for_update())
        if not ch or ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        ch.status = "paid"
        ch.paid_at = datetime.utcnow()
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == ch.user_telegram_id).with_for_update())
        uid = bal = kyc_uid = None
        if u:
            u.balance = float(u.balance or 0) + credit
            s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=credit, kind="apay_charge",
                            note=f"شارژ با آپی؛ {payment_id}" + (" (پیامکِ دیرهنگام)" if pay.get("late") else "")))
            uid, bal, kyc_uid = u.telegram_id, float(u.balance), u.id
        await s.commit()
    if kyc_uid:
        try:
            from tenant.shared.kyc import after_charge as _ac
            await _ac(bot_id, kyc_uid)
        except Exception:
            logger.exception("apay: after_charge (KYC) ناموفق بود")
    if uid:
        try:
            await _notify_paid(bot_id, uid, credit, bal)
        except Exception:
            logger.exception("apay: اطلاع به کاربر ناموفق بود")
        try:
            from tenant.gateways.payment_page import _tx_log_tenant
            await _tx_log_tenant(bot_id, uid, credit, bal, "apay")
        except Exception:
            logger.exception("apay: tx_log ناموفق بود")
    return True, "پرداخت تأیید و کیف پول شارژ شد.", credit


async def _notify_paid(bot_id: int, tg_id: int, amount: int, new_balance: float) -> None:
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
    if not brow:
        return
    tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        try:
            from tenant.gateways.wallet_receipt import build_wallet_charge_message_for_bot
            text, kb = await build_wallet_charge_message_for_bot(bot_id, amount, new_balance, "card")
            await tbot.send_message(tg_id, text, reply_markup=kb)
        except Exception:
            await tbot.send_message(tg_id, f"✅ {int(amount):,} تومان به کیفِ پولت اضافه شد.")
    finally:
        await tbot.session.close()


# ─────────────── وب‌هوک ───────────────
def signature_ok(secret: str, raw: bytes, ts: str, sig: str, now: float | None = None) -> bool:
    """HMAC_SHA256(secret, ts + "." + rawBody) — و timestamp حداکثر ۵ دقیقه اختلاف."""
    try:
        if abs((now or time.time()) - int(ts)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    exp = hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(exp, (sig or "").strip().lower())


async def handle_webhook(bot_id: int, raw: bytes, ts: str, sig: str) -> int:
    """کدِ HTTPِ پاسخ را برمی‌گرداند. تسویه همیشه با verify ِ خودِ APay است، نه بدنهٔ وب‌هوک."""
    import json
    secret = (await _get(bot_id, "apay_secret", "") or "").strip()
    if secret and not signature_ok(secret, raw, ts, sig):
        logger.warning("apay: وب‌هوک با امضایِ نامعتبر رد شد (bot=%s)", bot_id)
        return 401
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return 400
    pay = (body or {}).get("payment") or {}
    order_id = str(pay.get("order_id") or "")
    payment_id = str(pay.get("id") or "")
    if not order_id:
        return 200
    async with Session() as s:
        ch = await s.scalar(select(ApayCharge).where(
            ApayCharge.bot_id == bot_id, ApayCharge.order_id == order_id))
    if not ch or (payment_id and ch.payment_id and ch.payment_id != payment_id):
        return 200      # مالِ ما نیست/قبلاً — 2xx تا APay دوباره نفرستد
    if ch.status != "paid":
        try:
            await settle_charge(bot_id, order_id)
        except Exception:
            logger.exception("apay: تسویه از وب‌هوک ناموفق بود (bot=%s order=%s)", bot_id, order_id)
    return 200


# ─────────────── حلقهٔ استعلام ───────────────
async def poll_pending() -> int:
    """فاکتورهایِ بازِ ۳۰ دقیقهٔ اخیر را verify می‌کند (اگر وب‌هوک نرسید)؛ قدیمی‌ترها expired می‌شوند."""
    now = datetime.utcnow()
    async with Session() as s:
        rows = (await s.scalars(select(ApayCharge).where(
            ApayCharge.status == "waiting",
            ApayCharge.created_at >= now - timedelta(hours=6)))).all()
        items = [(r.bot_id, r.order_id, r.created_at) for r in rows]
    n = 0
    for bid, oid, created in items:
        try:
            ok, _m, _a = await settle_charge(bid, oid)
            if ok:
                n += 1
                continue
        except Exception:
            logger.exception("apay: poll خطا (bot=%s order=%s)", bid, oid)
            continue
        if created and created <= now - timedelta(minutes=POLL_FOR_MINUTES):
            async with Session() as s:
                ch = await s.scalar(select(ApayCharge).where(
                    ApayCharge.bot_id == bid, ApayCharge.order_id == oid,
                    ApayCharge.status == "waiting").with_for_update())
                if ch:
                    ch.status = "expired"
                    await s.commit()
    return n


async def apay_poll_loop():
    import asyncio
    await asyncio.sleep(40)
    while True:
        try:
            n = await poll_pending()
            if n:
                logger.info("[APAY] %s فاکتور خودکار تسویه شد", n)
        except Exception:
            logger.exception("[APAY] حلقهٔ استعلام خطا داد")
        await asyncio.sleep(60)
