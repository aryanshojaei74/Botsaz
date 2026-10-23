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

from sqlalchemy import select, update as sa_update

from config import Session
from models import ApayCharge, Bot as BotRow, BotUser, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

BASE_URL = "https://apaygate.shop"
_TIMEOUT = 15
MIN_AMOUNT = 10_000            # حداقلِ پیش‌فرضِ شارژ (تومان) — کفِ فنیِ APay ۱٬۰۰۰ تومان است
API_MIN_TOMAN = 1_000          # ۱۰٬۰۰۰ ریال
API_MAX_TOMAN = 100_000_000    # ۱٬۰۰۰٬۰۰۰٬۰۰۰ ریال
FEE_TOMAN = 1_500              # کارمزدِ هر پرداختِ موفق (fee=15000 ریال در پاسخِ آپی)
POLL_FOR_MINUTES = 30          # ۱۵ دقیقه اعتبار + ۱۰ دقیقه پیامکِ دیرهنگام + حاشیه
# === PATCH_APAY_ONE_OPEN_INVOICE_V1 === فاکتور آپی ۱۵ دقیقه اعتبار دارد؛ تا آن موقع «باز» حساب می‌شود
OPEN_INVOICE_MIN = 15


async def _get(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _set(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


def new_order_id() -> str:
    return "bs" + secrets.token_hex(8)


async def open_invoice(bot_id: int, tg_id: int):
    """فاکتور باز این کاربر در این ربات (در انتظار، لغو‌نشده و هنوز در مهلت اعتبار)؛ وگرنه None."""
    since = datetime.utcnow() - timedelta(minutes=OPEN_INVOICE_MIN)
    async with Session() as s:
        return await s.scalar(select(ApayCharge).where(
            ApayCharge.bot_id == int(bot_id), ApayCharge.user_telegram_id == int(tg_id),
            ApayCharge.status == "waiting", ApayCharge.created_at >= since
        ).order_by(ApayCharge.id.desc()))


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


async def cancel_payment(token: str, payment_id: str) -> tuple[int, dict]:
    """=== PATCH_APAY_REMOTE_CANCEL_V1 === POST /payments/:id/cancel — فاکتورِ پرداخت‌نشده را سمتِ آپی هم لغو می‌کند
    (مستندات: «cancel an unpaid invoice»؛ پرداخت‌شده ⇒ 409 invoice_not_payable)."""
    return await _api(token, "POST", f"/api/v1/payments/{payment_id}/cancel")


async def cancel_remote(bot_id: int, order_id: str) -> str:
    """لغوِ فاکتور در خودِ آپی. خروجی:
      "cancelled" — لغو شد؛ از این به بعد واریزِ هم‌مبلغ به این فاکتور تطبیق نمی‌خورد (جلوگیریِ شارژِ دوباره)؛
      "paid"      — آپی می‌گوید قبلاً پرداخت شده بود ⇒ همین‌جا تسویه (کیف پول شارژ) انجام شد؛
      "error"     — لغو ممکن نشد (شبکه/…)؛ رفتارِ قبلی (لغوِ محلی + پایش) ادامه دارد."""
    token = await _get(bot_id, "apay_token", "")
    async with Session() as s:
        ch = await s.scalar(select(ApayCharge).where(
            ApayCharge.bot_id == bot_id, ApayCharge.order_id == order_id))
        pid = ch.payment_id if ch else None
    if not token or not pid:
        return "error"
    code, res = await cancel_payment(token, pid)
    err = ((res or {}).get("error") or {}) if isinstance(res, dict) else {}
    if code in (200, 201, 204) and not err:
        return "cancelled"
    if code == 409 and err.get("code") == "invoice_not_payable":
        ok, _m, _a = await settle_charge(bot_id, order_id)
        return "paid" if ok else "error"
    logger.warning("apay: لغوِ سمتِ آپی ناموفق (bot=%s order=%s): %s %s", bot_id, order_id, code, str(res)[:200])
    return "error"


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
    if bal_t < FEE_TOMAN:
        msg += f"\n⚠️ موجودیِ کیف پولِ آپی کمتر از کارمزدِ هر پرداخت ({FEE_TOMAN:,} تومان) است؛ شارژش کن."
    return True, msg


# === PATCH_APAY_SITE_LINKS_V1 ===
# لینکِ شورتکاتِ امضاشدهٔ آیفون و APK را خودِ سایتِ آپی در /config.js منتشر می‌کند
# (window.APAY={"bot":…,"ios":…,"apk":…})؛ از همان‌جا می‌خوانیم تا هر بار که آپی
# فایل را عوض کرد، دکمه بدونِ آپدیتِ ربات به نسخهٔ تازه برود.
_SITE_CACHE: dict = {"at": 0.0, "data": {}}
_SITE_TTL_OK, _SITE_TTL_FAIL = 3600.0, 300.0


def _abs_site(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("/"):
        return BASE_URL + u
    return u if u.startswith("https://") else ""


async def site_links() -> dict:
    """{'ios': url|'', 'apk': url|''} — از کش؛ در خطا خالی (دکمه پنهان/پیش‌فرض)."""
    import json
    import re
    now = time.monotonic()
    ttl = _SITE_TTL_OK if _SITE_CACHE["data"] else _SITE_TTL_FAIL
    if _SITE_CACHE["at"] and now - _SITE_CACHE["at"] < ttl:
        return _SITE_CACHE["data"]
    data: dict = {}
    if aiohttp is not None:
        try:
            t = aiohttp.ClientTimeout(total=6)
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.get(f"{BASE_URL}/config.js") as r:
                    body = await r.text() if r.status == 200 else ""
            m = re.search(r"\{.*\}", body or "", re.S)
            cfg = json.loads(m.group(0)) if m else {}
            data = {"ios": _abs_site(cfg.get("ios", "")), "apk": _abs_site(cfg.get("apk", ""))}
        except Exception as e:
            logger.info("apay: خواندنِ config.js ِ سایت ناموفق بود: %s", e)
            data = {}
    _SITE_CACHE.update(at=now, data=data)
    return data


# === PATCH_APAY_INVOICE_CARD_V1 ===
def _digits(v) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def card_info(res: dict) -> tuple[str, str]:
    """(شمارهٔ کارت، نامِ صاحبِ کارت) از پاسخِ ساختِ پرداخت؛ اگر آپی نفرستاده بود رشتهٔ خالی.
    مستنداتِ آپی این فیلدها را صریح نام نبرده، پس چند شکلِ رایج پذیرفته می‌شود."""
    if not isinstance(res, dict):
        return "", ""
    card, holder = "", ""
    src = [res] + [res[k] for k in ("card", "bank_card", "destination", "device") if isinstance(res.get(k), dict)]
    for d in src:
        for k in ("card_number", "number", "pan", "card_no", "cardNumber"):
            n = _digits(d.get(k))
            if 16 <= len(n) <= 19:
                card = card or n
        # «name» فقط داخلِ شیءِ کارت معنیِ صاحبِ کارت دارد؛ در سطحِ اول ممکن است اسمِ فروشگاه باشد
        keys = ("card_holder", "holder", "holder_name", "owner", "owner_name", "cardHolder") + \
            (("name",) if d is not res else ())
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and v.strip() and not _digits(v):
                holder = holder or v.strip()[:60]
    if not card and isinstance(res.get("card"), str) and 16 <= len(_digits(res["card"])) <= 19:
        card = _digits(res["card"])
    return card, holder


async def card_for(token: str, res: dict) -> tuple[str, str]:
    """کارت/صاحبِ کارت از پاسخِ ساخت؛ اگر نبود یک‌بار از GET /payments/:id."""
    card, holder = card_info(res)
    # === PATCH_APAY_PUBLIC_CARD_V1 === API کارت را نمی‌دهد؛ صفحهٔ پرداختِ خودِ آپی آن را از
    # endpointِ عمومیِ /api/v1/public/payments/:id می‌خواند (card_number، card_holder).
    if not card and isinstance(res, dict) and res.get("id") and aiohttp is not None:
        try:
            t = aiohttp.ClientTimeout(total=6)
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.get(f"{BASE_URL}/api/v1/public/payments/{res['id']}") as r:
                    pub = await r.json(content_type=None) if r.status == 200 else None
            if isinstance(pub, dict):
                c2, h2 = card_info(pub.get("payment") if isinstance(pub.get("payment"), dict) else pub)
                card, holder = card or c2, holder or h2
        except Exception as e:
            logger.info("apay: خواندنِ کارتِ پرداخت از صفحهٔ عمومی ناموفق بود: %s", e)
    if not card:
        from log_throttle import should_log
        if should_log("apay:no_card_fields", 86400):
            logger.info("apay: پاسخِ پرداخت شمارهٔ کارت ندارد؛ فیلدها: %s",
                        sorted(res.keys()) if isinstance(res, dict) else type(res).__name__)
    return card, holder


# === PATCH_APAY_FEE_PAYER_V1 ===
FEE_PAYERS = {"seller": "فروشنده (از کیف پولِ آپیِ شما)", "buyer": "خریدار (رویِ مبلغِ فاکتور)"}


def fee_for_buyer(payer: str, fee_amount) -> int:
    """کارمزدی که باید رویِ فاکتورِ خریدار اضافه شود."""
    try:
        f = int(fee_amount or 0)
    except (TypeError, ValueError):
        f = 0
    return max(f, 0) if payer == "buyer" else 0


async def get_fee_cfg(bot_id: int) -> tuple[str, int]:
    payer = await _get(bot_id, "apay_fee_payer", "seller")
    try:
        amt = int(await _get(bot_id, "apay_fee_amount", str(FEE_TOMAN)))
    except (TypeError, ValueError):
        amt = FEE_TOMAN
    return (payer if payer in FEE_PAYERS else "seller"), max(amt, 0)


def invoice_view(*, title: str, pay_rial: int, card: str, holder: str, order_id: str,
                 pay_url: str, check_cb: str, cancel_cb: str, support_url: str = "",
                 fee_toman: int = 0, credit_toman: int = 0):
    """متن و کیبوردِ فاکتور — مشترکِ رباتِ مستأجر و بات‌ساز."""
    from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
    from aiogram.enums import ButtonStyle
    toman = pay_rial // 10 if pay_rial % 10 == 0 else pay_rial / 10
    lines = [f"🧾 <b>فاکتورِ شارژِ کیف پول — {title}</b>", "➖➖➖➖➖➖➖➖➖➖", ""]
    # === PATCH_APAY_INVOICE_TAP_COPY_V1 ===
    # کارت بدونِ فاصله: گروه‌هایِ ۴رقمیِ جداشده با فاصله در متنِ راست‌به‌چپ برعکس چیده می‌شدند.
    # مبلغ و کارت داخلِ <code> اند تا با یک لمس دقیقاً همان رقم‌ها (بدونِ ویرگول) کپی شوند.
    if pay_rial:
        lines.append(f"💳 مبلغِ دقیقِ واریز (ریال): <code>{pay_rial}</code>")
        lines.append(f"     (= {toman:,} تومان؛ برایِ کپی رویِ عدد بزن)")
    if fee_toman > 0:
        lines.append(f"ℹ️ شاملِ <b>{fee_toman:,}</b> تومان کارمزدِ درگاه؛ "
                     f"<b>{credit_toman:,}</b> تومان به کیف پولت اضافه می‌شود.")
    if card:
        lines.append(f"🏦 شمارهٔ کارت: <code>{card}</code>")
    if holder:
        lines.append(f"👤 به نامِ: <b>{holder}</b>")
    lines += [f"🔑 کدِ پیگیری: <code>{order_id}</code>", ""]
    if card:
        lines += ["📋 <b>مراحل:</b>",
                  "۱. <b>دقیقاً</b> همین مبلغ را به همین کارت واریز کن (رقم‌هایِ آخر مهم است)؛ "
                  "با دکمه‌هایِ کپی می‌توانی مستقیم از اپِ بانک پرداخت کنی، یا «💳 پرداخت» را بزن.",
                  "۲. کیف پولت خودکار شارژ می‌شود؛ اگر طول کشید «🔍 بررسیِ پرداخت» را بزن."]
    else:
        lines += ["📋 <b>مراحل:</b>",
                  "۱. «💳 پرداخت» را بزن؛ شمارهٔ کارت و مبلغ را می‌بینی.",
                  "۲. <b>دقیقاً</b> همان مبلغ را کارت‌به‌کارت کن (رقم‌هایِ آخر مهم است).",
                  "۳. کیف پولت خودکار شارژ می‌شود؛ اگر طول کشید «🔍 بررسیِ پرداخت» را بزن."]
    lines += ["", "⏳ این فاکتور ۱۵ دقیقه اعتبار دارد."]
    rows = []
    copy = []
    if card:
        copy.append(InlineKeyboardButton(text="📋 کپیِ شمارهٔ کارت", copy_text=CopyTextButton(text=card)))
    if pay_rial:
        copy.append(InlineKeyboardButton(text="📋 کپیِ مبلغِ دقیق (ریال)",
                                          copy_text=CopyTextButton(text=str(pay_rial))))
    if copy:
        rows.append(copy)
    rows.append([InlineKeyboardButton(text="💳 پرداخت", url=pay_url)])
    rows.append([InlineKeyboardButton(text="🔍 بررسیِ پرداخت", callback_data=check_cb,
                                      style=ButtonStyle.SUCCESS)])
    if support_url:
        rows.append([InlineKeyboardButton(text="💬 پیام به پشتیبانی (در صورت شارژ نشدن)", url=support_url)])
    rows.append([InlineKeyboardButton(text="❌ لغو و بازگشت", callback_data=cancel_cb,
                                      style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def webhook_url(bot_id: int) -> str:
    from tenant.gateways.custom_domain import get_bot_webhook_base
    base = (await get_bot_webhook_base(bot_id) or "").rstrip("/")
    return f"{base}/apay/webhook/{bot_id}" if base.startswith("https://") else ""


# ─────────────── تسویه ───────────────
# === PATCH_APAY_PAID_AMOUNT_GUARD_V1 ===
# نامِ فیلدِ «مبلغِ واقعاً واریزشده» در پاسخِ APay در مستنداتِ ما نیست؛ چند نامِ محتمل را می‌خوانیم. اگر هیچ‌کدام
# نبود، رفتارِ قبلی عیناً حفظ می‌شود (تنها تطبیقِ order_id و amount).
_PAID_AMOUNT_KEYS = ("paid_amount", "paid_rial", "paid_amount_rial", "received_amount", "received_rial",
                     "deposit_amount", "deposited_amount", "sms_amount", "bank_amount")


def paid_amount_mismatch(pay: dict, *, expected_pay_rial: int, expected_rial: int) -> str:
    """"" اگر مشکلی نیست؛ وگرنه شرحِ کوتاه. مبلغِ واریزی باید یا مبلغِ دقیقِ فاکتور (با رقمِ یکتا) باشد یا مبلغِ پایه."""
    ok = {int(v) for v in (expected_pay_rial, expected_rial) if v}
    for k in _PAID_AMOUNT_KEYS:
        v = pay.get(k)
        if v in (None, "", 0, "0"):
            continue
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            continue
        if ok and n not in ok:
            return f"{k}={n:,} ریال، فاکتور={int(expected_pay_rial or expected_rial):,} ریال"
    return ""


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
        expected = credit + int(getattr(ch, "fee_toman", 0) or 0)   # PATCH_APAY_FEE_PAYER_V1
        pay_rial_stored = int(getattr(ch, "pay_amount_rial", 0) or 0)   # PATCH_APAY_PAID_AMOUNT_GUARD_V1
        inv_chat, inv_msg = ch.chat_id, ch.msg_id
        ask_chat, ask_msg = ch.admin_ask_chat, ch.admin_ask_msg      # PATCH_APAY_ASK_CLEANUP_V1

    code, res = await verify_payment(token, payment_id)
    if code != 200 or not isinstance(res, dict):
        return False, err_text(code, res), 0
    pay = res.get("payment") or {}
    if not res.get("verified") or str(pay.get("status")) != "paid":
        st = str(pay.get("status") or "")
        return False, ("مهلتِ این فاکتور تمام شده و پرداختی ثبت نشد." if st == "expired"
                       else "هنوز پرداختی ثبت نشده است."), 0
    # تطبیق با سفارشِ خودمان — هرگز به مبلغِ اعلامی اعتماد نمی‌کنیم
    if str(pay.get("order_id")) != order_id or int(pay.get("amount") or 0) != expected * 10:
        logger.error("apay: عدمِ تطابقِ پرداخت (bot=%s order=%s): %s", bot_id, order_id, pay)
        return False, "اطلاعاتِ پرداخت با فاکتور نمی‌خواند؛ با پشتیبانی تماس بگیر.", 0
    # === PATCH_APAY_PAID_AMOUNT_GUARD_V1 ===
    # گزارشِ مالک: «مشتری کارت‌به‌کارتِ عادی با مبلغِ رند زد (نه مبلغِ دقیقِ فاکتورِ آپی) و آپی شارژش کرد».
    # اگر پاسخِ verify مبلغِ *واقعاً واریزشده* را هم بدهد، باید با مبلغِ دقیقِ فاکتور (با رقمِ یکتا) بخواند؛ وگرنه
    # شارژِ خودکار نمی‌کنیم و ادمین باید دستی تأیید کند (پیامِ «واریز شد؟» که از قبل می‌رود).
    mism = paid_amount_mismatch(pay, expected_pay_rial=pay_rial_stored, expected_rial=expected * 10)
    if mism:
        logger.error("apay: مبلغِ واریزشده با فاکتور نمی‌خواند (bot=%s order=%s): %s | %s", bot_id, order_id, mism, pay)
        return False, ("مبلغِ واریزشده با مبلغِ فاکتور یکی نیست (" + mism + ")؛ برایِ جلوگیری از شارژِ اشتباه "
                       "خودکار شارژ نشد — ادمین باید دستی بررسی کند."), 0

    async with Session() as s:
        ch = await s.scalar(select(ApayCharge).where(
            ApayCharge.bot_id == bot_id, ApayCharge.order_id == order_id).with_for_update())
        if not ch or ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        ch.status = "paid"
        ch.paid_at = datetime.utcnow()
        _for_order = getattr(ch, "for_order_id", None)      # PATCH_APAY_DIRECT_PURCHASE_V1
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
    await _drop_admin_ask(bot_id, ask_chat, ask_msg)       # PATCH_APAY_ASK_CLEANUP_V1
    if uid:
        try:
            await _notify_paid(bot_id, uid, credit, bal, inv_chat, inv_msg, for_order=_for_order)
        except Exception:
            logger.exception("apay: اطلاع به کاربر ناموفق بود")
        try:
            from tenant.gateways.payment_page import _tx_log_tenant
            await _tx_log_tenant(bot_id, uid, credit, bal, "apay")
        except Exception:
            logger.exception("apay: tx_log ناموفق بود")
        from tenant.gateways.apay_log import log_event as _alog     # PATCH_APAY_LOG_CHANNEL_V1
        await _alog(bot_id, "paid", user_tg=uid, amount_toman=credit, pay_rial=pay_rial_stored,
                    fee_toman=expected - credit, order_id=order_id, balance=bal, late=bool(pay.get("late")))
    return True, "پرداخت تأیید و کیف پول شارژ شد.", credit


def resume_button(for_order: int | None):
    """(متنِ دکمه، callback) برایِ «تکمیلِ خرید» بعد از پرداختِ مستقیم — همان رمزگذاریِ آبان‌گیت‌وی:
    >۰ سفارشِ فروشگاه (pay:wallet)، <۰ خریدِ دلخواهِ VPN (vc_pack) یا پلنِ VPN (−plan_id)."""
    if not for_order:
        return None
    if for_order > 0:
        return "🛒 تکمیلِ سفارش و دریافتِ سرویس", f"pay:wallet:{for_order}"
    from tenant.gateways.abangateway_admin import vc_unpack
    vc = vc_unpack(for_order)
    if vc:
        return "🛡 تکمیلِ خریدِ VPN و دریافتِ سرویس", f"vc:pay:{vc[0]}:{vc[1]}:{vc[2]}"
    return "🛡 تکمیلِ خریدِ VPN و دریافتِ سرویس", f"vpay:{-for_order}"


async def _notify_paid(bot_id: int, tg_id: int, amount: int, new_balance: float,
                       inv_chat: int | None = None, inv_msg: int | None = None,
                       for_order: int | None = None) -> None:
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
            _resume = resume_button(for_order)
            if _resume:
                # === PATCH_APAY_DIRECT_PURCHASE_V1 === پرداختِ مستقیم: به‌جایِ رسیدِ شارژ، دکمهٔ «تکمیلِ خرید»
                from aiogram.enums import ButtonStyle
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
                text = (f"✅ <b>{int(amount):,} تومان</b> پرداخت شد.\n"
                        f"موجودیِ فعلی: <b>{int(new_balance):,}</b> تومان\n\n"
                        "حالا برایِ نهایی‌شدنِ خرید، دکمهٔ زیر را بزن 👇")
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text=_resume[0], callback_data=_resume[1], style=ButtonStyle.SUCCESS)]])
            else:
                from tenant.gateways.wallet_receipt import build_wallet_charge_message_for_bot
                text, kb = await build_wallet_charge_message_for_bot(bot_id, amount, new_balance, "card")
            await tbot.send_message(tg_id, text, reply_markup=kb)
        except Exception:
            await tbot.send_message(tg_id, f"✅ {int(amount):,} تومان به کیفِ پولت اضافه شد.")
        if inv_chat and inv_msg:     # PATCH_APAY_FAST_WATCH_V1 — فاکتورِ پرداخت‌شده پاک می‌شود
            try:
                await tbot.delete_message(chat_id=inv_chat, message_id=inv_msg)
            except Exception:
                logger.info("apay: حذفِ پیامِ فاکتور ممکن نشد")
    finally:
        await tbot.session.close()


# ─────────────── پایشِ سریعِ هر فاکتور ───────────────
# === PATCH_APAY_FAST_WATCH_V1 ===
# حلقهٔ دقیقه‌ایِ worker و وب‌هوک کافی نبود («پرداخت کردم ولی ربات خودش چک نکرد»). بعد از
# ساختِ هر فاکتور، هر ۵ ثانیه وضعیت از endpointِ عمومیِ آپی (بدونِ توکن، همان که صفحهٔ
# پرداخت می‌خواند) خوانده می‌شود و فقط وقتی «paid» شد، تسویهٔ امن (verify + تطبیق) اجرا می‌شود.
WATCH_EVERY = 5
WATCH_FOR = 26 * 60          # ۱۵ دقیقه اعتبار + ۱۰ دقیقه پیامکِ دیرهنگام + حاشیه
_WATCH_TASKS: set = set()


async def public_status(payment_id: str) -> str:
    if aiohttp is None or not payment_id:
        return ""
    try:
        t = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(f"{BASE_URL}/api/v1/public/payments/{payment_id}") as r:
                d = await r.json(content_type=None) if r.status == 200 else None
        if isinstance(d, dict):
            d = d.get("payment") if isinstance(d.get("payment"), dict) else d
            return str(d.get("status") or "")
    except Exception as e:
        logger.debug("apay: وضعیتِ عمومیِ %s خوانده نشد: %s", payment_id, e)
    return ""


def spawn_watch(payment_id: str, is_open, settle) -> None:
    """is_open(): آیا فاکتور هنوز باز است (waiting/cancelled)؛ settle(): تسویهٔ امن."""
    import asyncio

    async def _run():
        waited = 0
        while waited < WATCH_FOR:
            await asyncio.sleep(WATCH_EVERY)
            waited += WATCH_EVERY
            try:
                if not await is_open():
                    return
                st = await public_status(payment_id)
                if st == "paid":
                    await settle()
                    return
                if st == "expired" and waited > 25 * 60:
                    return
            except Exception:
                logger.exception("apay: پایشِ فاکتورِ %s خطا داد", payment_id)
    task = asyncio.ensure_future(_run())
    _WATCH_TASKS.add(task)
    task.add_done_callback(_WATCH_TASKS.discard)


def watch_tenant_charge(bot_id: int, order_id: str, payment_id: str) -> None:
    async def is_open():
        async with Session() as s:
            st = await s.scalar(select(ApayCharge.status).where(
                ApayCharge.bot_id == bot_id, ApayCharge.order_id == order_id))
        return st in ("waiting", "cancelled")

    async def settle():
        await settle_charge(bot_id, order_id)
    spawn_watch(payment_id, is_open, settle)


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
    # === PATCH_APAY_OFF_MEANS_OFF_V1 === مالک درگاه را خاموش کرده ⇒ شارژِ خودکار هم نباید انجام شود
    if await _get(bot_id, "apay_enabled", "0") != "1":
        logger.info("apay: وب‌هوک برایِ رباتِ خاموش (bot=%s) نادیده گرفته شد", bot_id)
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


# ─────────────── پرسش از ادمین بعد از ۵ دقیقه ───────────────
# === PATCH_APAY_ADMIN_ASK_V1 ===
# مثلِ «کارت‌به‌کارتِ خودکار»ِ قبلی: اگر فاکتوری ۵ دقیقه بعد هنوز تسویه نشده، از ادمین
# پرسیده می‌شود «فلانی خواست شارژ کند؛ واریز شد؟» — با «تأیید» کیف پول دستی شارژ می‌شود.
ASK_AFTER_MIN = 5


def admin_ask_text(*, who: str, tg_id: int, amount: int, fee: int, pay_rial: int | None,
                   order_id: str, status: str, created=None) -> str:
    st = {"waiting": "⏳ در انتظارِ پرداخت", "cancelled": "🚫 کاربر لغو کرد",
          "expired": "⌛️ منقضی شد"}.get(status, status)
    return ("🔔 <b>درخواستِ شارژِ پرداخت‌نشده (کارت‌به‌کارتِ آنی)</b>\n\n"
            f"👤 کاربر: {who} (<code>{tg_id}</code>)\n"
            f"💰 مبلغِ شارژ: <b>{int(amount):,} تومان</b>\n"
            + (f"🧾 کارمزد با خریدار: {int(fee):,} تومان\n" if fee else "")
            + (f"💳 مبلغِ دقیقِ واریز: <code>{int(pay_rial)}</code> ریال\n" if pay_rial else "")
            + f"🆔 فاکتور: <code>{order_id}</code>\n"
            + _created_line(created)
            + f"📌 وضعیت: {st}\n\n"
            f"⏱ {ASK_AFTER_MIN} دقیقه گذشته و پرداختی ثبت نشده.\n"
            "<b>آیا این کاربر واقعاً واریز کرده است؟</b>\n"
            "با «تأیید» کیف پولِ کاربر همین الان شارژ می‌شود.")


def _created_line(created) -> str:
    if not created:
        return ""
    from tenant.gateways.wallet_receipt import tehran_date_time
    d, t = tehran_date_time(created)
    mins = max(0, int((datetime.utcnow() - created).total_seconds() // 60))
    return f"🕒 ثبتِ فاکتور: <b>{d}</b> ساعت <b>{t}</b> ({mins} دقیقه پیش)\n"


def admin_ask_kb(prefix: str, charge_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ واریز شد — شارژ کن", callback_data=f"{prefix}:ok:{charge_id}"),
        InlineKeyboardButton(text="❌ واریز نشده", callback_data=f"{prefix}:no:{charge_id}")]])


async def ask_admin_pending() -> int:
    """فاکتورهایِ تسویه‌نشدهٔ بیش از ۵ دقیقه را (یک‌بار) برایِ مالکِ ربات می‌فرستد."""
    now = datetime.utcnow()
    async with Session() as s:
        rows = (await s.scalars(select(ApayCharge).where(
            # === PATCH_APAY_NO_ASK_ON_CANCEL_V1 === فاکتورِ «لغوشده توسطِ کاربر» دیگر از ادمین نمی‌پرسد
            # (خودِ مشتری لغو کرده؛ اگر قبلش واریز کرده بود، وب‌هوک/پایشِ خودکار کیف پول را شارژ می‌کند).
            ApayCharge.status.in_(("waiting", "expired")),
            ApayCharge.admin_asked_at.is_(None),
            ApayCharge.created_at <= now - timedelta(minutes=ASK_AFTER_MIN),
            ApayCharge.created_at >= now - timedelta(hours=6)))).all()
        items = [r.id for r in rows]
    n = 0
    for cid in items:
        async with Session() as s:     # ادعایِ اتمیک: هر فاکتور فقط یک پیام
            claim = await s.execute(sa_update(ApayCharge).where(
                ApayCharge.id == cid, ApayCharge.admin_asked_at.is_(None),
                ApayCharge.status != "paid").values(admin_asked_at=now))
            await s.commit()
            if not claim.rowcount:
                continue
            ch = await s.get(ApayCharge, cid)
            brow = await s.get(BotRow, ch.bot_id)
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == ch.bot_id, BotUser.telegram_id == ch.user_telegram_id))
        if not brow or not brow.owner_id or int(brow.owner_id) == int(ch.user_telegram_id):
            continue
        who = ("@" + u.username) if u and u.username else ((u.full_name if u else "") or "—")
        try:
            _sent = await _send_owner(brow, admin_ask_text(
                who=who, tg_id=ch.user_telegram_id, amount=ch.amount_toman, fee=ch.fee_toman or 0,
                pay_rial=ch.pay_amount_rial, order_id=ch.order_id, status=ch.status,
                created=ch.created_at), admin_ask_kb("apadm", cid))
            if getattr(_sent, "message_id", None):
                async with Session() as s:
                    await s.execute(sa_update(ApayCharge).where(ApayCharge.id == cid).values(
                        admin_ask_chat=int(brow.owner_id), admin_ask_msg=_sent.message_id))
                    await s.commit()
            n += 1
        except Exception as e:
            logger.info("apay: پرسش از ادمینِ ربات %s ناموفق بود: %s", ch.bot_id, e)
    return n


async def _send_owner(brow, text: str, kb):
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        return await tbot.send_message(int(brow.owner_id), text, reply_markup=kb)
    finally:
        await tbot.session.close()


async def _drop_admin_ask(bot_id: int, chat_id, msg_id) -> None:
    """=== PATCH_APAY_ASK_CLEANUP_V1 === فاکتور خودکار تسویه شد → پیامِ «واریز شد؟» پاک می‌شود
    تا مالک دوباره رویش «تأیید» نزند."""
    if not (chat_id and msg_id):
        return
    try:
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        if not brow:
            return
        from aiogram import Bot
        tbot = Bot(token=brow.token)
        try:
            await tbot.delete_message(chat_id=int(chat_id), message_id=int(msg_id))
        finally:
            await tbot.session.close()
    except Exception:
        logger.info("apay: حذفِ پیامِ پرسش از ادمین ممکن نشد")


async def manual_settle(bot_id: int, charge_id: int, admin_uid: int) -> tuple[bool, str]:
    """ادمین تأیید کرد که واریز انجام شده → شارژِ اتمیک و یک‌بار (با تسویهٔ خودکار هم تداخل ندارد)."""
    async with Session() as s:
        ch = await s.scalar(select(ApayCharge).where(
            ApayCharge.id == charge_id, ApayCharge.bot_id == bot_id).with_for_update())
        if not ch:
            return False, "این فاکتور پیدا نشد."
        if ch.status == "paid":
            return False, "این فاکتور قبلاً شارژ شده است."
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == ch.user_telegram_id).with_for_update())
        if not u:
            return False, "کاربر پیدا نشد."
        credit = int(ch.amount_toman or 0)
        ch.status = "paid"
        ch.paid_at = datetime.utcnow()
        u.balance = float(u.balance or 0) + credit
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=credit, kind="apay_charge",
                        note=f"شارژِ آپی با تأییدِ دستیِ ادمین ({admin_uid})؛ {ch.order_id}"))
        uid, bal, kyc_uid, inv_chat, inv_msg = u.telegram_id, float(u.balance), u.id, ch.chat_id, ch.msg_id
        ch_order = ch.order_id
        await s.commit()
    try:
        from tenant.shared.kyc import after_charge as _ac
        await _ac(bot_id, kyc_uid)
    except Exception:
        logger.exception("apay: after_charge (KYC) ناموفق بود")
    try:
        await _notify_paid(bot_id, uid, credit, bal, inv_chat, inv_msg)
    except Exception:
        logger.exception("apay: اطلاع به کاربر ناموفق بود")
    try:
        from tenant.gateways.payment_page import _tx_log_tenant
        await _tx_log_tenant(bot_id, uid, credit, bal, "apay")
    except Exception:
        logger.exception("apay: tx_log ناموفق بود")
    from tenant.gateways.apay_log import log_event as _alog         # PATCH_APAY_LOG_CHANNEL_V1
    await _alog(bot_id, "paid", user_tg=uid, amount_toman=credit, order_id=ch_order,
                balance=bal, note=f"تأییدِ دستیِ ادمین ({admin_uid})")
    return True, f"✅ {credit:,} تومان به کیف پولِ کاربر اضافه شد."


# ─────────────── حلقهٔ استعلام ───────────────
async def poll_pending() -> int:
    """فاکتورهایِ بازِ ۳۰ دقیقهٔ اخیر را verify می‌کند (اگر وب‌هوک نرسید)؛ قدیمی‌ترها expired می‌شوند."""
    now = datetime.utcnow()
    async with Session() as s:
        rows = (await s.scalars(select(ApayCharge).where(
            ApayCharge.status.in_(("waiting", "cancelled")),
            ApayCharge.created_at >= now - timedelta(hours=6)))).all()
        # لغوشده‌ها هم تا ۳۰ دقیقه چک می‌شوند (شاید کاربر بعد از «لغو» واریز کرده باشد)
        items = [(r.bot_id, r.order_id, r.created_at) for r in rows
                 if r.status == "waiting" or (r.created_at and
                                              r.created_at > now - timedelta(minutes=POLL_FOR_MINUTES))]
    n = 0
    _enabled: dict[int, bool] = {}
    for bid, oid, created in items:
        try:
            if bid not in _enabled:       # PATCH_APAY_OFF_MEANS_OFF_V1
                _enabled[bid] = (await _get(bid, "apay_enabled", "0")) == "1"
            if _enabled[bid]:
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
                exp_info = None
                if ch:
                    ch.status = "expired"
                    ask_chat, ask_msg = ch.admin_ask_chat, ch.admin_ask_msg
                    exp_info = (ch.user_telegram_id, int(ch.amount_toman or 0), int(ch.pay_amount_rial or 0),
                                int(ch.fee_toman or 0))
                    await s.commit()
                else:
                    ask_chat = ask_msg = None
            if exp_info:                                          # PATCH_APAY_LOG_CHANNEL_V1
                from tenant.gateways.apay_log import log_event as _alog
                await _alog(bid, "expired", user_tg=exp_info[0], amount_toman=exp_info[1],
                            pay_rial=exp_info[2], fee_toman=exp_info[3], order_id=oid)
            # === PATCH_APAY_ASK_CLEANUP_V2 === منقضی و پرداخت‌نشده ⇒ پرسش بی‌پاسخ مالک پاک شود
            await _drop_admin_ask(bid, ask_chat, ask_msg)
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
        try:
            await ask_admin_pending()            # PATCH_APAY_ADMIN_ASK_V1
        except Exception:
            logger.exception("[APAY] پرسش از ادمین خطا داد")
        await asyncio.sleep(60)


# ─────────────── ساختِ فاکتور بدونِ پیامِ چت (برایِ مینی‌اپ) ───────────────
# === PATCH_APAY_MINIAPP_V1 === (ایلیا) شارژ با آپی داخلِ خودِ مینی‌اپ
async def create_charge_core(row, tg_id: int, amount: int) -> tuple[dict | None, str]:
    """(داده، پیامِ خطا). فاکتورِ شارژِ کیف پول می‌سازد و پایش را شروع می‌کند؛ هیچ پیامی به چت نمی‌فرستد.
    داده: order_id, pay_url, pay_rial, card, holder, fee, credit, expires_in. اگر کاربر فاکتورِ باز دارد، خطا + کدِ آن."""
    if not await is_available(row.id):
        return None, "درگاهِ آپی فعال نیست."
    token = await _get(row.id, "apay_token", "")
    min_amount = await get_min_amount(row.id)
    max_amount = min(await get_max_amount(row.id) or API_MAX_TOMAN, API_MAX_TOMAN)
    amount = int(amount or 0)
    if amount < min_amount:
        return None, f"حداقلِ مبلغ {min_amount:,} تومان است."
    if amount > max_amount:
        return None, f"حداکثرِ مبلغ {max_amount:,} تومان است."
    opened = await open_invoice(row.id, tg_id)
    if opened:
        return None, f"open:{opened.order_id}"
    oid = new_order_id()
    payer, famt = await get_fee_cfg(row.id)
    fee = fee_for_buyer(payer, famt)
    if amount + fee > API_MAX_TOMAN:
        return None, f"حداکثرِ مبلغ با کارمزد {API_MAX_TOMAN:,} تومان است."
    ret = f"https://t.me/{row.username}" if row.username else ""
    code, res = await create_payment(token, amount_toman=amount + fee, order_id=oid, description="شارژ کیف پول",
                                     callback_url=await webhook_url(row.id), return_url=ret)
    if code == 400 and isinstance(res, dict) and (res.get("error") or {}).get("code") == "invalid_url":
        code, res = await create_payment(token, amount_toman=amount + fee, order_id=oid, return_url=ret)
    if code not in (200, 201) or not isinstance(res, dict) or not res.get("pay_url"):
        logger.warning("apay(miniapp): ساختِ پرداخت ناموفق (bot=%s): %s %s", row.id, code, str(res)[:300])
        return None, err_text(code, res)
    pay_rial = int(res.get("payable_rial") or res.get("pay_amount") or 0)
    async with Session() as s:
        s.add(ApayCharge(bot_id=row.id, user_telegram_id=tg_id, order_id=oid, payment_id=str(res.get("id") or ""),
                         amount_toman=amount, fee_toman=fee, pay_amount_rial=pay_rial or None,
                         pay_url=str(res["pay_url"])[:300], status="waiting"))
        await s.commit()
    card, holder = await card_for(token, res)
    from tenant.gateways.apay_log import log_event as _alog
    await _alog(row.id, "created", user_tg=tg_id, amount_toman=amount, pay_rial=pay_rial, fee_toman=fee,
                order_id=oid, note="از مینی‌اپ")
    watch_tenant_charge(row.id, oid, str(res.get("id") or ""))
    return {"order_id": oid, "pay_url": str(res["pay_url"]), "pay_rial": pay_rial, "card": card, "holder": holder,
            "fee": fee, "credit": amount, "expires_in": OPEN_INVOICE_MIN * 60}, ""


_MINI_LAST_CHECK: dict[tuple[int, str], float] = {}


async def mini_status(bot_id: int, tg_id: int, order_id: str) -> dict | None:
    """وضعیتِ فاکتورِ این کاربر برایِ مینی‌اپ؛ برایِ فاکتورِ منتظر حداکثر هر ۸ ثانیه یک‌بار از آپی استعلام می‌کند."""
    import time as _t
    async with Session() as s:
        ch = await s.scalar(select(ApayCharge).where(ApayCharge.bot_id == bot_id, ApayCharge.order_id == order_id))
    if not ch or int(ch.user_telegram_id) != int(tg_id):
        return None
    if ch.status == "waiting" and await _get(bot_id, "apay_enabled", "0") == "1":
        now = _t.monotonic()
        if now - _MINI_LAST_CHECK.get((bot_id, order_id), 0.0) >= 8:
            _MINI_LAST_CHECK[(bot_id, order_id)] = now
            if len(_MINI_LAST_CHECK) > 2000:
                _MINI_LAST_CHECK.clear()
            try:
                await settle_charge(bot_id, order_id)
            except Exception:
                logger.exception("apay(miniapp): استعلامِ فاکتور ناموفق بود")
            async with Session() as s:
                ch = await s.scalar(select(ApayCharge).where(
                    ApayCharge.bot_id == bot_id, ApayCharge.order_id == order_id))
    bal = None
    if ch.status == "paid":
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
            bal = float(u.balance or 0) if u else None
    return {"status": ch.status, "credit": int(ch.amount_toman or 0), "balance": bal}
