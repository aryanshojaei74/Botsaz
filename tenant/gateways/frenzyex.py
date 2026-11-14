"""
frenzyex.py — PATCH_FRENZYEX_GATEWAY_V1

درگاهِ «فرنزی‌اکس» (FrenzyEx؛ https://frenzy.fastsnap.info) برایِ شارژِ کیفِ پول — کارت‌به‌کارت (و در صورتِ فعال‌بودن، کریپتو).
مخصوصِ پلنِ حرفه‌ای. مستندات: «FrenzyEx Merchant Payment Gateway Guide v5».

فلو (بدونِ وب‌هوک؛ همیشه خودمان با کلیدِ Bearer از API می‌پرسیم — مسیرِ «authenticated status polling» مستندات):
  ۱) POST /api/v1/payment-requests {amount, amount_ccy:"TMN", payment_method, order_ref, description}
        → request_id / checkout_url / bot_pay_url
  ۲) مشتری در checkout_url (ربات فرنزی‌اکس) «شروع پرداخت» را می‌زند و مبلغِ دقیقِ نمایش‌داده‌شده را کارت‌به‌کارت می‌کند
  ۳) GET /api/v1/payment-requests/{request_id} → status=paid
  ۴) کیفِ پول به‌اندازهٔ amount (مبلغِ فاکتورِ ما؛ نه مبلغی که مشتری با کارمزد می‌دهد) شارژ می‌شود

نکته‌هایِ مهمِ مستندات که رعایت شده:
  • ساختِ فاکتور idempotency ندارد ⇒ POST را هرگز کورکورانه تکرار نمی‌کنیم (نتیجهٔ نامطمئن = خطا به کاربر، نه تلاشِ دوباره).
  • سقفِ ساخت: ۱۰ فاکتور در ۶۰ ثانیه برای هر مرچنت (HTTP 429).
  • انقضا/لغو قطعی نیست: تا ۲ ساعت بعد هم ممکن است پرداخت کامل شود ⇒ فاکتورهایِ منقضی/لغوشده تا ۳ ساعت استعلام می‌شوند
    و اگر paid شدند شارژ می‌شوند.
  • لغو با HTTP 409 (payment_received یا در حالِ بررسی) رد می‌شود ⇒ فاکتور باز می‌ماند و شارژِ بعدی انجام می‌شود.
  • تصمیمِ شارژ فقط: status=paid + یکسان‌بودنِ order_ref و مبلغ (amount/price_amount) با رکوردِ خودمان + اولین تسویهٔ اتمیک.
  • کلیدِ API فقط سمتِ سرور نگه‌داری می‌شود و هیچ‌جا در پیامِ کاربر/لاگ نمی‌آید.
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
from models import BotUser, FrenzyExCharge, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

BASE_URL = "https://frenzy.fastsnap.info"
_TIMEOUT = 20
MIN_AMOUNT = 10_000
FLOOR_AMOUNT = 1_000
METHODS = ("auto", "standard")
METHOD_FA = {"auto": "خودکار (کارت‌به‌کارت + کریپتو اگر فعال باشد)", "standard": "فقط کارت‌به‌کارت"}
DEFAULT_METHOD = "auto"
POLL_WINDOW_HOURS = 3          # فاکتورِ پرداخت‌نشده تا این مدت استعلام می‌شود (مستندات: تا ۲ ساعت بعد از انقضا هم ممکن است کامل شود)
CCY = "TMN"


async def _get(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _set(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


def new_order_id() -> str:
    """order_ref یکتا؛ کوتاه و امن."""
    return "fx" + secrets.token_hex(8)


async def get_min_amount(bot_id: int) -> int:
    try:
        n = int(await _get(bot_id, "frenzyex_min_amount", str(MIN_AMOUNT)))
        return n if n >= FLOOR_AMOUNT else MIN_AMOUNT
    except (TypeError, ValueError):
        return MIN_AMOUNT


async def get_max_amount(bot_id: int) -> int:
    try:
        return max(int(await _get(bot_id, "frenzyex_max_amount", "0")), 0)
    except (TypeError, ValueError):
        return 0


async def get_method(bot_id: int) -> str:
    v = (await _get(bot_id, "frenzyex_method", DEFAULT_METHOD) or "").strip().lower()
    return v if v in METHODS else DEFAULT_METHOD


async def _pro_ok(bot_id: int) -> bool:
    """فقط پلنِ حرفه‌ای (اگر پایین آمد، درگاه از دسترس خارج می‌شود ولی تنظیمات می‌ماند)."""
    try:
        from tenant import get_bot_row_by_id
        from plans import Feature, has_feature
        r = await get_bot_row_by_id(bot_id)
        return bool(r and has_feature(r.plan, Feature.GATEWAY_FRENZYEX))
    except Exception:
        logger.exception("frenzyex: خواندنِ پلنِ ربات ناموفق")
        return False


async def is_available(bot_id: int) -> bool:
    return (await _get(bot_id, "frenzyex_enabled", "0") == "1"
            and bool(await _get(bot_id, "frenzyex_api_key", ""))
            and await _pro_ok(bot_id))


async def get_display_name(bot_id: int) -> str:
    return (await _get(bot_id, "frenzyex_display_name", "")).strip() or "فرنزی‌اکس"


# ─────────────────────────── API ───────────────────────────

async def _api(api_key: str, method: str, path: str, body: dict | None = None) -> dict:
    """تماسِ خام. خروجی همیشه dict با کلیدِ success است (در خطا: success=False + message + http + detail)."""
    if aiohttp is None:
        return {"success": False, "http": 0, "message": "aiohttp نصب نیست"}
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.request(method, f"{BASE_URL}{path}", json=body, headers=headers, allow_redirects=False) as r:
                try:
                    out = await r.json(content_type=None)
                except Exception:   # noqa: BLE001
                    out = None
                ok = 200 <= r.status < 300
                if not isinstance(out, dict):
                    return {"success": ok, "http": r.status, "message": "" if ok else "پاسخِ نامعتبر از درگاه"}
                out["http"] = r.status
                out["success"] = ok
                return out
    except Exception as e:  # noqa: BLE001
        logger.warning("frenzyex: خطای شبکه در %s %s: %s", method, path, type(e).__name__)
        return {"success": False, "http": 0, "message": type(e).__name__, "network": True}


def _detail(res: dict) -> str:
    d = res.get("detail")
    if isinstance(d, dict):
        d = d.get("error") or d.get("detail") or d.get("message")
    if isinstance(d, list):          # خطایِ 422 (schema)
        d = "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in d[:3])
    return str(d or res.get("message") or res.get("error") or "").strip()


def _err(res: dict) -> str:
    http = res.get("http")
    base = {400: "درخواستِ نامعتبر (مبلغ/روشِ پرداخت/آدرسِ بازگشت).", 401: "کلیدِ API نامعتبر است.",
            403: "حسابِ مرچنتِ فرنزی‌اکس غیرفعال است؛ با پشتیبانیِ فرنزی‌اکس تماس بگیر.",
            404: "فاکتور پیدا نشد.", 409: "این فاکتور الان قابلِ لغو نیست (پرداخت دریافت شده یا در حالِ بررسی است).",
            422: "مبلغ نامعتبر است (باید بیشتر از ۰ باشد).",
            429: "درخواست‌هایِ ساختِ فاکتور بیش از حد بود؛ چند دقیقه بعد دوباره امتحان کن."}.get(http)
    if res.get("network"):
        return "اتصال به درگاه برقرار نشد؛ کمی بعد دوباره امتحان کن."
    return base or _detail(res) or "پاسخِ نامشخص از درگاه"


async def create_request(api_key: str, amount: int, order_ref: str, method: str, description: str = "") -> dict:
    """ساختِ فاکتور — idempotent نیست؛ فراخوان نباید در خطایِ نامطمئن دوباره صدایش کند."""
    body = {"amount": int(amount), "amount_ccy": CCY, "payment_method": method, "order_ref": order_ref}
    if description:
        body["description"] = description[:120]
    return await _api(api_key, "POST", "/api/v1/payment-requests", body)


async def get_request(api_key: str, request_id: str) -> dict:
    return await _api(api_key, "GET", f"/api/v1/payment-requests/{request_id}")


async def cancel_request(api_key: str, request_id: str) -> dict:
    return await _api(api_key, "POST", f"/api/v1/payment-requests/{request_id}/cancel")


async def validate_key(api_key: str) -> tuple[bool, str]:
    """تستِ کلید بدونِ ساختنِ فاکتور: یک request_idِ ساختگی را می‌پرسد.
    کلیدِ درست ⇒ 404 (فاکتور پیدا نشد)؛ کلیدِ غلط ⇒ 401؛ حسابِ غیرفعال ⇒ 403."""
    res = await _api(api_key, "GET", "/api/v1/payment-requests/keycheck0000")
    http = res.get("http")
    if http in (404, 200):
        return True, "کلید معتبر است و درگاه پاسخ داد."
    if res.get("network"):
        return False, _err(res)
    if http == 401:
        return False, "کلیدِ API نامعتبر است (باید با frx_live_ شروع شود)."
    return False, _err(res)


# ─────────────────────────── تسویه ───────────────────────────

def _status_of(res: dict) -> str:
    return str(res.get("status") or "").strip().lower()


async def settle_charge(bot_id: int, order_id: str, notify: bool = True) -> tuple[bool, str, int]:
    """استعلامِ یک فاکتور و در صورتِ پرداخت، شارژِ کیفِ پول. خروجی: (شارژ شد؟، پیام، مبلغِ شارژ‌شده به تومان)."""
    api_key = await _get(bot_id, "frenzyex_api_key", "")
    if not api_key:
        return False, "کلیدِ درگاه تنظیم نشده است.", 0
    async with Session() as s:
        ch = await s.scalar(select(FrenzyExCharge).where(FrenzyExCharge.bot_id == bot_id,
                                                         FrenzyExCharge.order_id == order_id))
        if not ch:
            return False, "این فاکتور پیدا نشد.", 0
        if ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        rid, credit = ch.request_id, int(ch.amount_toman or 0)
    if not rid:
        return False, "این فاکتور شناسهٔ درگاه ندارد.", 0

    res = await get_request(api_key, rid)
    if not res.get("success"):
        return False, _err(res), 0
    st = _status_of(res)
    if st != "paid":
        if st == "canceled" or (st == "expired"):
            return False, "این فاکتور منقضی/لغو شده است (اگر واریز کرده‌ای، تا ۲ ساعت ممکن است هنوز تأیید شود).", 0
        return False, "هنوز پرداختی ثبت نشده است.", 0

    # ── اعتبارسنجیِ پاسخ با رکوردِ خودمان (مستندات: request/order/amount باید بخواند) ──
    ref = str(res.get("order_ref") or res.get("order_id") or "")
    if ref and ref != order_id:
        logger.error("frenzyex: order_ref نمی‌خواند (bot=%s order=%s api=%s)", bot_id, order_id, ref)
        return False, "شناسهٔ سفارش با فاکتور یکسان نیست؛ با پشتیبانی هماهنگ کن.", 0
    ccy = str(res.get("amount_ccy") or res.get("price_currency") or CCY).upper()
    paid_amt = res.get("price_amount", res.get("amount"))
    try:
        mismatch = ccy != CCY or (paid_amt is not None and int(float(paid_amt)) != credit)
    except (TypeError, ValueError):
        mismatch = True
    if mismatch:
        logger.error("frenzyex: مبلغِ فاکتور با رکورد نمی‌خواند (bot=%s order=%s api=%s %s db=%s)",
                     bot_id, order_id, paid_amt, ccy, credit)
        return False, "مبلغِ فاکتور با درخواست یکسان نیست؛ با پشتیبانی هماهنگ کن.", 0

    # ── تسویهٔ اتمیک (یک‌بار) ──
    async with Session() as s:
        ch = await s.scalar(select(FrenzyExCharge).where(FrenzyExCharge.bot_id == bot_id,
                                                         FrenzyExCharge.order_id == order_id).with_for_update())
        if not ch or ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        ch.status = "paid"
        ch.paid_at = datetime.utcnow()
        ch.tracking_code = str(res.get("tx_hash") or "")[:80] or None
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id,
                                                 BotUser.telegram_id == ch.user_telegram_id).with_for_update())
        if u:
            u.balance = float(u.balance or 0) + credit
            s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=credit, kind="frenzyex_charge",
                            note=f"شارژ با فرنزی‌اکس؛ ref={ch.tracking_code or ch.request_id}"))
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
            logger.exception("frenzyex: اطلاع به کاربر ناموفق بود")
        try:
            from tenant.gateways.payment_page import _tx_log_tenant
            await _tx_log_tenant(bot_id, _uid, credit, _bal, "frenzyex")
        except Exception:
            logger.exception("frenzyex: tx_log ناموفق بود")
    return True, "پرداخت تأیید و کیفِ پول شارژ شد.", credit


async def poll_pending(bot_id: int | None = None) -> int:
    """استعلامِ دوره‌ایِ فاکتورهایِ تسویه‌نشدهٔ ۳ ساعتِ اخیر (شاملِ منقضی/لغوشده، چون ممکن است دیرتر کامل شوند).
    خروجی: تعدادِ تسویه‌شده."""
    cutoff = datetime.utcnow() - timedelta(hours=POLL_WINDOW_HOURS)
    async with Session() as s:
        q = select(FrenzyExCharge).where(FrenzyExCharge.status.in_(("waiting", "expired", "cancelled")),
                                         FrenzyExCharge.created_at >= cutoff)
        if bot_id:
            q = q.where(FrenzyExCharge.bot_id == bot_id)
        items = [(r.bot_id, r.order_id, r.created_at, r.status) for r in (await s.scalars(q)).all()]
    n = 0
    expire_before = datetime.utcnow() - timedelta(minutes=70)   # فاکتور ۶۰ دقیقه اعتبار دارد
    for bid, oid, created_at, status in items:
        try:
            if not await _get(bid, "frenzyex_api_key", ""):
                continue
            ok, _msg, _amt = await settle_charge(bid, oid)
            if ok:
                n += 1
                continue
        except Exception:
            logger.exception("frenzyex: poll خطا (bot=%s order=%s)", bid, oid)
            continue
        if status == "waiting" and created_at and created_at <= expire_before:
            async with Session() as s:
                ch = await s.scalar(select(FrenzyExCharge).where(
                    FrenzyExCharge.bot_id == bid, FrenzyExCharge.order_id == oid,
                    FrenzyExCharge.status == "waiting").with_for_update())
                if ch:
                    ch.status = "expired"      # فقط برچسب؛ تا ۳ ساعت هنوز استعلام می‌شود
                    await s.commit()
    return n


async def frenzyex_poll_loop():
    """هر ۲ دقیقه فاکتورهایِ باز را استعلام می‌کند و در صورتِ پرداخت، کیفِ پول را شارژ و به کاربر خبر می‌دهد."""
    import asyncio
    await asyncio.sleep(40)
    while True:
        try:
            n = await poll_pending()
            if n:
                logger.info("[FRENZYEX] %s فاکتور خودکار تسویه شد", n)
        except Exception:
            logger.exception("[FRENZYEX] حلقهٔ استعلام خطا داد")
        await asyncio.sleep(120)
