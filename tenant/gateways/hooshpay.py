"""
hooshpay.py — PATCH_HOOSHPAY_GATEWAY_V1

درگاهِ کارت‌به‌کارتِ آنیِ «هوش‌پی» (hooshpay.xyz؛ مستندات: https://hooshpay.xyz/developers) برایِ شارژِ کیفِ پول.

فلو (مثلِ یونیک‌پی، بدونِ وب‌هوک؛ همیشه خودمان از API می‌پرسیم):
  ۱) POST /api/v1/invoices {amount, fee_mode, order_id}  →  data.uid / payment_url / payable_amount
  ۲) خریدار در payment_url دقیقاً payable_amount را کارت‌به‌کارت می‌کند
  ۳) POST /api/v1/invoices/{uid}/verify  →  paid
  ۴) کیفِ پول به‌اندازهٔ amount (مبلغِ درخواستی؛ نه payable_amount) شارژ می‌شود

نکته‌ها:
  • احراز: هدرِ X-API-KEY (کلید از بخشِ «توسعه» هوش‌پی). Secret فقط برایِ امضایِ وب‌هوک است که اینجا استفاده نمی‌شود.
  • کارمزد (پیش‌فرضِ حسابِ هوش‌پی ۲۰٪) با fee_mode: buyer = خریدار اضافه می‌دهد و کیفِ پولش کامل شارژ می‌شود (پیش‌فرضِ ما)،
    seller = از فروشنده کسر می‌شود، split = نصف‌نصف. مبلغی که به مشتری نشان می‌دهیم همیشه payable_amount است.
  • به بدنهٔ هیچ کال‌بکی اعتماد نمی‌کنیم؛ شارژ فقط وقتی انجام می‌شود که verify «paid» بگوید و amountِ فاکتور با رکوردِ خودمان بخواند.
  • مبلغ‌ها همه تومان و عددِ صحیح‌اند.
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
from models import Bot as BotRow, BotUser, HooshPayCharge, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

BASE_URL = "https://hooshpay.xyz/api/v1"
_TIMEOUT = 20
MIN_AMOUNT = 10_000          # حداقلِ پیش‌فرضِ شارژ (کفِ فنیِ هوش‌پی ۱٬۰۰۰ تومان)
FLOOR_AMOUNT = 1_000
FEE_MODES = ("buyer", "seller", "split")
FEE_MODE_FA = {"buyer": "خریدار", "seller": "فروشنده (من)", "split": "نصف‌نصف"}
DEFAULT_FEE_MODE = "buyer"
CANCEL_AFTER_MINUTES = 45     # فاکتورِ پرداخت‌نشده بعد از این مدت (یا انقضایِ خودِ هوش‌پی) بسته می‌شود


async def _get(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _set(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


def new_order_id() -> str:
    """شناسهٔ یکتایِ سفارش (order_id در API) — کوتاه و امن."""
    return "bs" + secrets.token_hex(8)


async def get_min_amount(bot_id: int) -> int:
    try:
        n = int(await _get(bot_id, "hooshpay_min_amount", str(MIN_AMOUNT)))
        return n if n >= FLOOR_AMOUNT else MIN_AMOUNT
    except (TypeError, ValueError):
        return MIN_AMOUNT


async def get_max_amount(bot_id: int) -> int:
    try:
        return max(int(await _get(bot_id, "hooshpay_max_amount", "0")), 0)
    except (TypeError, ValueError):
        return 0


async def get_fee_mode(bot_id: int) -> str:
    v = (await _get(bot_id, "hooshpay_fee_mode", DEFAULT_FEE_MODE) or "").strip().lower()
    return v if v in FEE_MODES else DEFAULT_FEE_MODE


async def is_available(bot_id: int) -> bool:
    return await _get(bot_id, "hooshpay_enabled", "0") == "1" and bool(await _get(bot_id, "hooshpay_api_key", ""))


async def get_display_name(bot_id: int) -> str:
    return (await _get(bot_id, "hooshpay_display_name", "")).strip() or "هوش‌پی"


# ─────────────────────────── API ───────────────────────────

async def _api(api_key: str, method: str, path: str, body: dict | None = None) -> dict:
    """تماسِ خام. خروجی همیشه dict با کلیدِ success است (در خطا: success=False + message + http)."""
    if aiohttp is None:
        return {"success": False, "http": 0, "message": "aiohttp نصب نیست"}
    headers = {"X-API-KEY": api_key, "Accept": "application/json"}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.request(method, f"{BASE_URL}{path}", json=body, headers=headers) as r:
                out = await r.json(content_type=None)
                if not isinstance(out, dict):
                    return {"success": False, "http": r.status, "message": "پاسخِ نامعتبر از درگاه"}
                out.setdefault("success", 200 <= r.status < 300)
                out["http"] = r.status
                return out
    except Exception as e:  # noqa: BLE001
        logger.warning("hooshpay: خطای شبکه در %s %s: %s", method, path, e)
        return {"success": False, "http": 0, "message": str(e) or type(e).__name__}


def _err(res: dict) -> str:
    http = res.get("http")
    base = {400: "ورودیِ نامعتبر (مثلاً مبلغ خارج از محدودهٔ درگاه).", 401: "کلید API نامعتبر است.",
            403: "حسابِ هوش‌پی مسدود است.", 404: "فاکتور پیدا نشد.",
            503: "درگاه غیرفعال است یا کارتی برایِ دریافت موجود نیست."}.get(http)
    msg = str(res.get("message") or res.get("error") or "").strip()
    return base or msg or "پاسخِ نامشخص از درگاه"


async def create_invoice(api_key: str, amount: int, order_id: str, fee_mode: str, description: str = "") -> dict:
    body = {"amount": int(amount), "fee_mode": fee_mode, "order_id": order_id}
    if description:
        body["description"] = description[:120]
    return await _api(api_key, "POST", "/invoices", body)


async def get_invoice(api_key: str, uid: str) -> dict:
    return await _api(api_key, "GET", f"/invoices/{uid}")


async def verify_invoice(api_key: str, uid: str) -> dict:
    return await _api(api_key, "POST", f"/invoices/{uid}/verify")


async def cancel_invoice(api_key: str, uid: str) -> dict:
    return await _api(api_key, "POST", f"/invoices/{uid}/cancel")


async def validate_key(api_key: str) -> tuple[bool, str]:
    """تستِ کلید با درخواستِ فقط‌خواندنیِ /account (فاکتوری ساخته نمی‌شود)."""
    res = await _api(api_key, "GET", "/account")
    if res.get("success"):
        return True, "کلید معتبر است و درگاه پاسخ داد."
    return False, _err(res)


# ─────────────────────────── تسویه ───────────────────────────

def _is_paid(res: dict) -> bool:
    d = res.get("data") if isinstance(res.get("data"), dict) else {}
    return bool(res.get("paid") is True or str(res.get("status") or d.get("status") or "").lower() == "paid")


async def settle_charge(bot_id: int, order_id: str, notify: bool = True) -> tuple[bool, str, int]:
    """استعلامِ یک فاکتور و در صورتِ پرداخت، شارژِ کیفِ پول. خروجی: (شارژ شد؟، پیام، مبلغِ شارژ‌شده به تومان)."""
    api_key = await _get(bot_id, "hooshpay_api_key", "")
    if not api_key:
        return False, "کلیدِ درگاه تنظیم نشده است.", 0
    async with Session() as s:
        ch = await s.scalar(select(HooshPayCharge).where(HooshPayCharge.bot_id == bot_id,
                                                         HooshPayCharge.order_id == order_id))
        if not ch:
            return False, "این فاکتور پیدا نشد.", 0
        if ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        uid, credit = ch.invoice_uid, int(ch.amount_toman or 0)
    if not uid:
        return False, "این فاکتور شناسهٔ درگاه ندارد.", 0

    res = await verify_invoice(api_key, uid)
    if not res.get("success"):
        return False, _err(res), 0
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    if not _is_paid(res):
        st = str(res.get("status") or data.get("status") or "").lower()
        if st in ("expired", "cancelled", "failed"):
            async with Session() as s:
                ch = await s.scalar(select(HooshPayCharge).where(HooshPayCharge.bot_id == bot_id,
                                                                 HooshPayCharge.order_id == order_id,
                                                                 HooshPayCharge.status == "waiting").with_for_update())
                if ch:
                    ch.status = "expired" if st == "expired" else "cancelled"
                    await s.commit()
            return False, "مهلتِ این فاکتور تمام شده یا لغو شده است.", 0
        return False, "هنوز پرداختی ثبت نشده است.", 0
    # مبلغِ فاکتور باید با رکوردِ خودمان بخواند (اگر درگاه amount داده باشد)
    paid_amt = data.get("amount")
    if paid_amt is not None and int(float(paid_amt)) != credit:
        logger.error("hooshpay: مبلغِ فاکتور با رکورد نمی‌خواند (bot=%s order=%s api=%s db=%s)", bot_id, order_id, paid_amt, credit)
        return False, "مبلغِ فاکتور با درخواست یکسان نیست؛ با پشتیبانی هماهنگ کن.", 0

    # ── تسویهٔ اتمیک ──
    async with Session() as s:
        ch = await s.scalar(select(HooshPayCharge).where(HooshPayCharge.bot_id == bot_id,
                                                         HooshPayCharge.order_id == order_id).with_for_update())
        if not ch or ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        ch.status = "paid"
        ch.paid_at = datetime.utcnow()
        ch.tracking_code = str(data.get("tracking_code") or res.get("tracking_code") or "")[:64] or None
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id,
                                                 BotUser.telegram_id == ch.user_telegram_id).with_for_update())
        if u:
            u.balance = float(u.balance or 0) + credit
            s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=credit, kind="hooshpay_charge",
                            note=f"شارژ با هوش‌پی؛ ref={ch.tracking_code or ch.invoice_uid}"))
            _uid, _bal, _kyc_uid = u.telegram_id, float(u.balance), u.id
        else:
            _uid = _bal = _kyc_uid = None
        await s.commit()
    if _kyc_uid:
        from tenant.shared.kyc import after_charge as _ac
        await _ac(bot_id, _kyc_uid)
    if _uid and notify:        # notify=False: کلیکِ «بررسی»؛ همان پیامِ فاکتور ویرایش می‌شود (PATCH_GATEWAY_INPLACE_V1)
        try:
            from tenant.gateways.uniquepay import _notify_paid
            await _notify_paid(bot_id, _uid, credit, _bal)
        except Exception:
            logger.exception("hooshpay: اطلاع به کاربر ناموفق بود")
        try:
            from tenant.gateways.payment_page import _tx_log_tenant
            await _tx_log_tenant(bot_id, _uid, credit, _bal, "hooshpay")
        except Exception:
            logger.exception("hooshpay: tx_log ناموفق بود")
    return True, "پرداخت تأیید و کیفِ پول شارژ شد.", credit


async def poll_pending(bot_id: int | None = None) -> int:
    """استعلامِ دوره‌ایِ فاکتورهایِ بازِ چند ساعتِ اخیر؛ فاکتورِ قدیمیِ پرداخت‌نشده بسته می‌شود. خروجی: تعدادِ تسویه‌شده."""
    cutoff = datetime.utcnow() - timedelta(hours=6)
    async with Session() as s:
        q = select(HooshPayCharge).where(HooshPayCharge.status == "waiting", HooshPayCharge.created_at >= cutoff)
        if bot_id:
            q = q.where(HooshPayCharge.bot_id == bot_id)
        items = [(r.bot_id, r.order_id, r.created_at, r.invoice_uid) for r in (await s.scalars(q)).all()]
    n = 0
    cancel_before = datetime.utcnow() - timedelta(minutes=CANCEL_AFTER_MINUTES)
    for bid, oid, created_at, uid in items:
        try:
            ok, _msg, _amt = await settle_charge(bid, oid)
            if ok:
                n += 1
                continue
        except Exception:
            logger.exception("hooshpay: poll خطا (bot=%s order=%s)", bid, oid)
            continue
        if created_at and created_at <= cancel_before:
            async with Session() as s:
                ch = await s.scalar(select(HooshPayCharge).where(
                    HooshPayCharge.bot_id == bid, HooshPayCharge.order_id == oid,
                    HooshPayCharge.status == "waiting").with_for_update())
                if ch:
                    ch.status = "expired"
                    await s.commit()
    return n


async def hooshpay_poll_loop():
    """هر ۲ دقیقه فاکتورهایِ باز را استعلام می‌کند و در صورتِ پرداخت، کیفِ پول را شارژ و به کاربر خبر می‌دهد."""
    import asyncio
    await asyncio.sleep(35)
    while True:
        try:
            n = await poll_pending()
            if n:
                logger.info("[HOOSHPAY] %s فاکتور خودکار تسویه شد", n)
        except Exception:
            logger.exception("[HOOSHPAY] حلقهٔ استعلام خطا داد")
        await asyncio.sleep(120)
