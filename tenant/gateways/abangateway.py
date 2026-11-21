"""
abangateway.py — PATCH_ABANGATEWAY_V1

درگاهِ کارت‌به‌کارتِ خودکارِ «آبان گیت‌وی» (abangateway.ir) برایِ شارژِ کیفِ پول
و پرداختِ مستقیمِ محصول — با تأییدِ آنیِ خودکار.

فلو (طبقِ مستنداتِ رسمی):
  ۱) POST /api/v1/invoices با amount_rial  →  invoice_id + payment_url +
     payable_rial (چند ریال بیشتر از amount_rial، برایِ تشخیصِ خودکارِ
     واریز از رویِ پیامکِ بانک — کاربر باید دقیقاً همین عدد را واریز کند)
     + card_number/card_holder (کارتِ مقصد برایِ همین فاکتور)
  ۲) کاربر کارت‌به‌کارت می‌کند
  ۳) GET /api/v1/invoices/{id}  →  status (pending/partially_paid/paid/
     expired/cancelled) — هر تعداد بار قابلِ فراخوانی، بدونِ عارضه
  ۴) اگر status == paid: POST /api/v1/invoices/{id}/verify — این تنها
     مسیریست که واقعاً «تحویل/شارژ» را قفل می‌کند: طبقِ مستندات فقط اولین
     فراخوانیِ آن موفق است (verified: true)، فراخوانیِ بعدی ۴۰۹
     (already_verified) می‌گیرد. پس دقیقاً مثلِ یک قفلِ یک‌بارمصرف عمل
     می‌کند و ما رویِ آن (به‌همراهِ رزروِ ردیفِ خودمان با with_for_update)
     دوبار-شارژ نمی‌شویم.

نکتهٔ امنیتی: هیچ‌وقت به بدنهٔ وب‌هوک/کال‌بک اعتماد نمی‌کنیم؛ همیشه مستقیم
از verify (که خودش یک‌بارمصرف و امن است) تأیید می‌گیریم.

نکتهٔ واحدِ مبلغ: طبقِ مستندات، API فقط ریالِ عددِ صحیح می‌گیرد — تومان فقط
برایِ نمایش است. همه‌جایِ ربات (نمایش، ورودی، شارژ، کیفِ پول) تومان است؛
تبدیل به ریال فقط همین‌جا و فقط برایِ ارسال به API انجام می‌شود.
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
from models import Bot as BotRow, BotUser, AbanGatewayCharge, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

BASE_URL = "https://abangateway.ir"
_TIMEOUT = 20
MIN_AMOUNT = 10_000          # حداقلِ مبلغِ پیش‌فرضِ شارژ (تومان) — قابلِ‌تغییر توسطِ مالکِ ربات


def new_order_id() -> str:
    """شناسهٔ یکتایِ فاکتورِ ما — به‌عنوانِ order_id به آبان گیت‌وی فرستاده می‌شود
    (حداکثر ۱۲۸ کاراکتر طبقِ مستندات؛ خیلی کوتاه‌تر از این کافی است)."""
    return "ab" + secrets.token_hex(8)


async def _get(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _set(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


async def get_min_amount(bot_id: int) -> int:
    v = await _get(bot_id, "abangateway_min_amount", str(MIN_AMOUNT))
    try:
        n = int(v)
        return n if n > 0 else MIN_AMOUNT
    except (TypeError, ValueError):
        return MIN_AMOUNT


async def get_max_amount(bot_id: int) -> int:
    """حداکثرِ مبلغِ قابلِ‌تنظیم توسطِ مالکِ ربات (تومان). ۰ یعنی بدونِ سقف."""
    v = await _get(bot_id, "abangateway_max_amount", "0")
    try:
        return max(int(v), 0)
    except (TypeError, ValueError):
        return 0


# === PATCH_ABANGATEWAY_DISPLAY_NAME_V1 ===
DEFAULT_DISPLAY_NAME = "آبان گیت‌وی"


async def get_display_name(bot_id: int) -> str:
    """اسمی که به‌جایِ «آبان گیت‌وی» به کاربر نشان داده می‌شود (تویِ کیفِ پول و
    پرداختِ محصول) — مالکِ ربات می‌تواند از پنلِ تنظیماتِ همین درگاه، هر اسمی
    که دوست دارد رویش بگذارد (مثلاً برایِ هماهنگی با برندِ خودِ ربات).
    خودِ اتصال/API همچنان با abangateway.ir کار می‌کند؛ فقط برچسبِ نمایشی
    برایِ کاربرِ نهایی تغییر می‌کند."""
    v = await _get(bot_id, "abangateway_display_name", "")
    v = (v or "").strip()
    return v[:40] if v else DEFAULT_DISPLAY_NAME


async def is_available(bot_id: int) -> bool:
    """آیا درگاهِ آبان گیت‌وی برایِ این ربات فعال و آماده‌به‌استفاده است."""
    on = await _get(bot_id, "abangateway_enabled", "0") == "1"
    tok = await _get(bot_id, "abangateway_token", "")
    return on and bool(tok)


# === PATCH_ABANGATEWAY_REPLACE_CARD_V1 ===
async def replaces_card(bot_id: int) -> bool:
    """آیا آبان گیت‌وی باید *جایگزینِ* کارت‌به‌کارتِ دستی شود؟

    گزارشِ ادمین (ایلیا): «امکانِ استفاده از آبان‌گیت‌وی به‌جایِ شماره‌کارت
    برایِ خریدِ سرویس یا شارژِ کیف‌پول». وقتی این کلید روشن است (و خودِ
    درگاه هم فعال و متصل است)، دکمه‌هایِ «کارت‌به‌کارتِ دستی» (شمارهٔ کارتِ
    ثابتِ ادمین + ارسالِ رسید) در همهٔ صفحه‌هایِ پرداخت پنهان می‌شوند و
    آبان گیت‌وی همه‌جا نمایش داده می‌شود، حتی اگر تیک‌هایِ «نمایش در
    کیف‌پول/خرید» خاموش باشند. اگر درگاه غیرفعال باشد، هیچ اثری ندارد —
    یعنی ادمین هیچ‌وقت بدونِ روشِ پرداخت نمی‌ماند."""
    if await _get(bot_id, "abangateway_replace_card", "0") != "1":
        return False
    return await is_available(bot_id)


async def _api(token: str, method: str, path: str, data: dict | None = None) -> tuple[int, dict]:
    """تماسِ خام با API. خروجی: (کدِ HTTP, بدنهٔ dict — حتی در خطا)."""
    if aiohttp is None:
        return 0, {"error": {"code": "no_aiohttp", "message": "aiohttp نصب نیست"}}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.request(method, f"{BASE_URL}{path}", json=data,
                                 headers=headers) as r:
                try:
                    out = await r.json(content_type=None)
                except Exception:
                    out = None
                if not isinstance(out, dict):
                    out = {"error": {"code": "invalid_response",
                                     "message": "پاسخِ نامعتبر از درگاه"}}
                return r.status, out
    except Exception as e:
        logger.exception("abangateway: خطای شبکه در %s %s", method, path)
        return 0, {"error": {"code": "network_error", "message": str(e)}}


def _err(body: dict) -> tuple[str, str]:
    """کدِ ماشین‌خوان و پیامِ خطا را از بدنهٔ پاسخ درمی‌آورد.

    === PATCH_ABANGATEWAY_ERROR_VISIBILITY_V1 ===
    قبلاً اگه بدنهٔ پاسخ دقیقاً شکلِ مستندشدهٔ {"error": {"code","message"}}
    نبود (مثلاً یه خطایِ فریم‌ورکی/اعتبارسنجیِ غیرمنتظره از خودِ درگاه)،
    همیشه پیامِ عمومیِ «خطایِ نامشخص از درگاه» نشون داده می‌شد و هیچ ردی
    از خودِ متنِ واقعیِ خطا باقی نمی‌موند — تشخیصِ مشکلِ واقعی غیرممکن بود.
    حالا حتی وقتی شکلِ استاندارد نباشه، سعی می‌کنیم هرچی از متنِ خطا که تو
    بدنه پیدا می‌شه رو نشون بدیم، به‌جایِ قورت‌دادنِ کاملش."""
    err = body.get("error")
    if isinstance(err, dict):
        code = str(err.get("code") or "")
        msg = err.get("message")
        if msg:
            return code, str(msg)
    elif isinstance(err, str) and err.strip():
        return "", err.strip()
    # فرمت‌هایِ رایجِ دیگرِ خطا (مثلاً فریم‌ورک‌هایِ عمومی): message/errors در ریشه
    for key in ("message", "detail", "error_description"):
        v = body.get(key)
        if isinstance(v, str) and v.strip():
            return "", v.strip()
    if body:
        # آخرین راه‌حل: خودِ بدنه رو (کوتاه‌شده) نشون بده تا لااقل چیزی
        # برایِ عیب‌یابی داشته باشیم، نه یه پیامِ کاملاً کور.
        import json as _json
        try:
            dump = _json.dumps(body, ensure_ascii=False)[:300]
        except Exception:
            dump = str(body)[:300]
        return "", f"پاسخِ غیرمنتظره از درگاه: {dump}"
    return "", "پاسخِ خالی از درگاه (بدونِ هیچ توضیحی)."


_ERROR_LABELS = {
    "not_yet_paid": "هنوز واریزی برایِ این فاکتور تشخیص داده نشده.",
    "insufficient_fee_wallet": "کیفِ‌پولِ کارمزدِ حسابِ آبان گیت‌وی خالی است؛ به مالکِ ربات اطلاع بده.",
    "sandbox_only": "این عملیات فقط با توکنِ آزمایشی کار می‌کند.",
    "invoice_not_found": "این فاکتور پیدا نشد یا مالِ این حساب نیست.",
    "already_verified": "این فاکتور قبلاً تأیید و تحویل داده شده است.",
    "invoice_not_payable": "وضعیتِ فعلیِ این فاکتور اجازهٔ این عملیات را نمی‌دهد.",
    "no_card_registered": "هیچ کارتِ فعالی رویِ حسابِ درگاه ثبت نشده؛ به مالکِ ربات اطلاع بده.",
    "invoice_expired": "مهلتِ پرداختِ این فاکتور تمام شده.",
    "amount_not_allowed": "این مبلغ خارج از محدودهٔ مجازِ درگاه است.",
    "unsafe_callback_url": "آدرسِ بازگشتِ تنظیم‌شده پذیرفته نشد.",
    "capacity_full": "ظرفیتِ همزمانِ کارتِ مقصد پر است؛ چند دقیقهٔ دیگر دوباره امتحان کن.",
}


async def create_invoice(token: str, amount_rial: int, order_id: str = "",
                         description: str = "", callback_url: str = "",
                         expiry_minutes: int | None = None) -> tuple[int, dict]:
    d = {"amount_rial": int(amount_rial)}
    if order_id:
        d["order_id"] = order_id
    if description:
        d["description"] = description[:1000]
    if callback_url:
        d["callback_url"] = callback_url
    if expiry_minutes:
        d["expiry_minutes"] = int(expiry_minutes)
    return await _api(token, "POST", "/api/v1/invoices", d)


async def get_invoice(token: str, invoice_id: str) -> tuple[int, dict]:
    return await _api(token, "GET", f"/api/v1/invoices/{invoice_id}")


async def verify_invoice(token: str, invoice_id: str) -> tuple[int, dict]:
    return await _api(token, "POST", f"/api/v1/invoices/{invoice_id}/verify")


async def cancel_invoice(token: str, invoice_id: str) -> tuple[int, dict]:
    return await _api(token, "POST", f"/api/v1/invoices/{invoice_id}/cancel")


async def validate_token(bot_id: int, token: str, bot_username: str = "") -> tuple[bool, str]:
    """تستِ توکن: یک فاکتورِ کوچک می‌سازد و بلافاصله لغوش می‌کند (تا چیزی
    رزروشده رویِ حسابِ ادمین باقی نماند)."""
    if not (token.startswith("ag_live_") or token.startswith("ag_test_")):
        return False, "توکنِ آبان گیت‌وی باید با ag_live_ یا ag_test_ شروع شود."
    amount_toman = await get_min_amount(bot_id)
    redirect = f"https://t.me/{bot_username}" if bot_username else ""
    status, body = await create_invoice(
        token, amount_toman * 10, order_id=new_order_id(),
        description="تستِ اتصالِ بات‌ساز", callback_url=redirect)
    if status == 201 and body.get("invoice_id"):
        inv_id = body["invoice_id"]
        try:
            await cancel_invoice(token, inv_id)
        except Exception:
            logger.exception("abangateway: لغوِ فاکتورِ تستی ناموفق بود (بی‌اهمیت)")
        return True, "✅ توکن معتبر است و درگاه پاسخ داد."
    code, msg = _err(body)
    # === PATCH_ABANGATEWAY_FEE_WALLET_TOKEN_ACCEPT_V1 ===
    # درخواستِ ادمین: «insufficient_fee_wallet» یعنی توکن کاملاً معتبره و
    # احرازِهویت با موفقیت انجام شده — فقط خودِ حسابِ آبان گیت‌وی (نه کیفِ
    # پولِ کاربرانِ ربات) موجودیِ کارمزد نداره تا فاکتورِ تستی ساخته بشه.
    # این یه مشکلِ «توکن نامعتبره» نیست، پس دیگه نباید جلویِ ذخیره‌شدنِ
    # توکن رو بگیره — فقط باید به ادمین هشدار بده که قبل از استفادهٔ
    # واقعی، کیفِ‌پولِ کارمزد رو شارژ کنه.
    if code == "insufficient_fee_wallet":
        return True, ("✅ توکن معتبر است و ذخیره شد.\n\n"
                      "⚠️ ولی کیفِ‌پولِ کارمزدِ حسابِ آبان گیت‌وی خالی است — تا "
                      "شارژش نکنی، فاکتورهایِ واقعی ساخته نمی‌شن. از پنلِ "
                      "abangateway.ir یا ربات @Abangw_bot شارژش کن.")
    if status == 403:
        return False, "توکن ارسال نشد یا نامعتبر است."
    if status == 404:
        return False, "حسابی با این توکن پیدا نشد."
    # === PATCH_ABANGATEWAY_ERROR_VISIBILITY_V1 ===
    # اگه کدِ خطا تو جدولِ شناخته‌شده‌ها نبود، به‌جایِ قورت‌دادنش، خودِ
    # کدِ HTTP و پیامِ خامِ درگاه رو هم نشون بده تا بشه فهمید دقیقاً چی
    # پسِ‌زمینه اتفاق افتاده (مثلاً یه خطایِ اعتبارسنجیِ مستندنشده).
    logger.warning("abangateway: پاسخِ غیرمنتظرهٔ تستِ توکن — status=%s body=%r", status, body)
    label = _ERROR_LABELS.get(code)
    if label:
        return False, label
    return False, f"خطا از درگاه (HTTP {status}): {msg}"


async def create_invoice_safe(bot_id: int, token: str, amount_toman: int,
                              order_id: str, bot_username: str = "") -> tuple[bool, str, dict]:
    """ساختِ فاکتورِ واقعیِ کاربر. خروجی: (موفق؟, پیامِ خطا در صورتِ شکست, بدنهٔ پاسخ)."""
    redirect = f"https://t.me/{bot_username}" if bot_username else ""
    status, body = await create_invoice(
        token, amount_toman * 10, order_id=order_id,
        description=f"شارژِ کیفِ‌پول — {order_id}", callback_url=redirect)
    if status == 201 and body.get("invoice_id"):
        return True, "", body
    code, msg = _err(body)
    # === PATCH_ABANGATEWAY_ERROR_VISIBILITY_V1 ===
    logger.warning("abangateway: ساختِ فاکتور ناموفق — status=%s body=%r", status, body)
    label = _ERROR_LABELS.get(code)
    return False, (label or f"خطا از درگاه (HTTP {status}): {msg}"), body


# === PATCH_ABANGATEWAY_INVOICE_UX_V1 ===
def deadline_minutes(body: dict) -> int | None:
    """مهلتِ باقی‌ماندهٔ فاکتور (دقیقه) اگر درگاه در پاسخ اعلام کرده باشد؛ وگرنه None
    (در آن صورت خطِ «مهلت» نمایش داده نمی‌شود، نه عددِ حدسی)."""
    try:
        for k in ("expires_in", "expires_in_seconds", "ttl_seconds"):
            if body.get(k) not in (None, ""):
                return max(1, round(int(body[k]) / 60))
        for k in ("expires_at", "expire_at", "expiry"):
            v = body.get(k)
            if v:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.utcnow()
                return max(1, round((dt - now).total_seconds() / 60))
    except Exception:
        logger.debug("abangateway: مهلتِ فاکتور خوانده نشد (بی‌اهمیت)")
    return None


async def remember_invoice_msg(bot_id: int, order_id: str, chat_id: int, message_id: int) -> None:
    await _set(bot_id, f"abu_msg_{order_id}", f"{chat_id}:{message_id}")


async def delete_invoice_msg(bot_id: int, order_id: str) -> None:
    """بعد از شارژِ موفق، پیامِ فاکتور را (با توکنِ خودِ همان ربات) پاک می‌کند."""
    raw = await _get(bot_id, f"abu_msg_{order_id}", "")
    if not raw or ":" not in raw:
        return
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    chat_s, mid_s = raw.split(":", 1)
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
    if not brow:
        return
    tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await tbot.delete_message(chat_id=int(chat_s), message_id=int(mid_s))
    except Exception:
        logger.debug("abangateway: حذفِ پیامِ فاکتور ممکن نشد (بی‌اهمیت)")
    finally:
        try:
            await tbot.session.close()
        except Exception:
            logger.debug("abangateway: بستنِ سشن (بی‌اهمیت)")
    await _set(bot_id, f"abu_msg_{order_id}", "")


async def settle_charge(bot_id: int, order_id: str) -> tuple[bool, str, int]:
    """استعلامِ یک فاکتور و در صورتِ پرداختِ کامل، تأییدِ یک‌بارمصرف + شارژِ
    کیفِ پول.

    خروجی: (شارژ شد؟, پیام, مبلغِ شارژ‌شده به تومان)
    """
    token = await _get(bot_id, "abangateway_token", "")
    if not token:
        return False, "توکنِ درگاه تنظیم نشده است.", 0

    async with Session() as s:
        ch = await s.scalar(select(AbanGatewayCharge).where(
            AbanGatewayCharge.bot_id == bot_id,
            AbanGatewayCharge.order_id == order_id))
        if not ch:
            return False, "این فاکتور پیدا نشد.", 0
        if ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        # === PATCH_ABANGATEWAY_LATE_PAID_V1 ===
        # قبلاً «expired/cancelled» این‌جا نهایی بود و دیگر هیچ‌وقت از درگاه
        # استعلام نمی‌شد. ولی وقتی پیامکِ بانک دیر می‌رسد، فاکتور منقضی
        # می‌شود و بعد مالک در پنلِ آبان «دستی» تأییدش می‌کند — فاکتور
        # روی درگاه paid می‌شود ولی ما هرگز نمی‌فهمیدیم و کیفِ پول شارژ
        # نمی‌شد. حالا فقط «rejected» (ردِ صریحِ ادمینِ خودِ ربات) نهایی است
        # و بقیه دوباره از درگاه پرسیده می‌شوند.
        if ch.status == "rejected":
            return False, "این فاکتور توسطِ ادمین رد شده است.", 0
        invoice_id = ch.invoice_id
        credit = int(ch.amount_toman or 0)

    if not invoice_id:
        return False, "این فاکتور هنوز رویِ درگاه ساخته نشده.", 0

    status, inv_body = await get_invoice(token, invoice_id)
    if status != 200:
        code, msg = _err(inv_body)
        return False, _ERROR_LABELS.get(code, msg or "استعلام ناموفق بود."), 0
    inv_status = inv_body.get("status")
    if inv_status in ("expired", "cancelled"):
        async with Session() as s:
            cc = await s.scalar(select(AbanGatewayCharge).where(
                AbanGatewayCharge.bot_id == bot_id,
                AbanGatewayCharge.order_id == order_id).with_for_update())
            if cc and cc.status == "waiting":
                cc.status = inv_status
                await s.commit()
        return False, ("مهلتِ این فاکتور تمام شده است." if inv_status == "expired"
                       else "این فاکتور لغو شده است."), 0
    if inv_status != "paid":
        return False, "هنوز پرداختی برایِ این فاکتور تأیید نشده است.", 0

    # ── تأییدِ یک‌بارمصرف ──
    v_status, v_body = await verify_invoice(token, invoice_id)
    if v_status == 409:
        code, _msg = _err(v_body)
        if code == "already_verified":
            # === PATCH_ABANGATEWAY_LATE_PAID_V1 ===
            # قبلاً ردیف بی‌صدا «paid» می‌شد *بدونِ شارژِ کیفِ پول*. دو حالت
            # پول را گم می‌کرد: (۱) verify بیرون از ربات انجام شده (تأییدِ
            # دستی در پنلِ آبان)، (۲) مسابقه با فراخوانیِ موازی — آن یکی
            # verify=200 گرفته ولی هنوز commit نکرده؛ این‌جا paid می‌شد و آن
            # یکی در مرحلهٔ اتمیک «قبلاً تسویه شده» می‌دید و شارژ نمی‌کرد.
            # حالا به ردیف دست نمی‌زنیم مگر اینکه شارژ نشده باشد؛ در آن صورت
            # به waiting برمی‌گردد و به ادمین هشدارِ «تأیید و شارژِ دستی» می‌رود.
            async with Session() as s:
                cc = await s.scalar(select(AbanGatewayCharge).where(
                    AbanGatewayCharge.bot_id == bot_id,
                    AbanGatewayCharge.order_id == order_id).with_for_update())
                need_alert = False
                if cc and cc.status != "paid":
                    if cc.status != "waiting":
                        # ردیفِ منقضی/لغوشده: هشدارِ قبلی (اگر بوده) دکمه‌اش
                        # دیگر کار نمی‌کرد، پس یک هشدارِ تازه لازم است.
                        cc.admin_notified = False
                    cc.status = "waiting"
                    need_alert = not cc.admin_notified
                    await s.commit()
            if need_alert:
                try:
                    await _send_stale_claim_alert(bot_id, order_id, verified_elsewhere=True)
                except Exception:
                    logger.exception("abangateway: هشدارِ فاکتورِ تأییدشده بیرون از ربات ناموفق بود")
            return False, ("این پرداخت روی درگاه تأیید شده و برایِ شارژِ کیفِ پول "
                           "به ادمینِ ربات ارجاع شد."), 0
    if v_status != 200 or not v_body.get("verified"):
        code, msg = _err(v_body)
        return False, _ERROR_LABELS.get(code, msg or "تأییدِ پرداخت ناموفق بود."), 0

    # ── تسویهٔ اتمیک ──
    async with Session() as s:
        ch = await s.scalar(select(AbanGatewayCharge).where(
            AbanGatewayCharge.bot_id == bot_id,
            AbanGatewayCharge.order_id == order_id).with_for_update())
        if not ch or ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        ch.status = "paid"
        ch.paid_at = datetime.utcnow()
        ch.ref_id = str(v_body.get("invoice_id") or invoice_id)[:64]
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id,
            BotUser.telegram_id == ch.user_telegram_id).with_for_update())
        if u:
            u.balance = float(u.balance or 0) + credit
            s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=credit,
                            kind="abangateway_charge",
                            note=f"شارژ با آبان گیت‌وی؛ فاکتور={invoice_id}"))
            _uid, _bal = u.telegram_id, float(u.balance)
            _kyc_uid = u.id
        else:
            _uid = _bal = None
            _kyc_uid = None
        # === PATCH_GATEWAY_PAY_FOR_ORDER_V1 ===
        _for_order = ch.for_order_id
        await s.commit()

    # === PATCH_KYC_GATE_ALL_PATHS_V1 ===
    if _kyc_uid:
        from tenant.shared.kyc import after_charge as _ac
        await _ac(bot_id, _kyc_uid)

    if _uid:
        try:
            await delete_invoice_msg(bot_id, order_id)   # PATCH_ABANGATEWAY_INVOICE_UX_V1
        except Exception:
            logger.exception("abangateway: حذفِ پیامِ فاکتور ناموفق بود")
        try:
            await _notify_paid(bot_id, _uid, credit, _bal, for_order=_for_order)
        except Exception:
            logger.exception("abangateway: اطلاع به کاربر ناموفق بود")
        try:
            from tenant.gateways.payment_page import _tx_log_tenant
            await _tx_log_tenant(bot_id, _uid, credit, _bal, "abangateway")
        except Exception:
            logger.exception("abangateway: tx_log ناموفق بود")
        # === PATCH_ABANGATEWAY_ADMIN_NOTIFY_V1 ===
        try:
            await _notify_admin_paid(bot_id, _uid, credit, order_id)
        except Exception:
            logger.exception("abangateway: اطلاع به ادمین ناموفق بود")

    return True, "پرداخت تأیید و کیفِ پول شارژ شد.", credit


CLAIM_ALERT_AFTER_MINUTES = 5


# === PATCH_ABANGATEWAY_ADMIN_NOTIFY_V1 ===
async def _notify_admin_paid(bot_id: int, tg_id: int, amount: int, order_id: str) -> None:
    """پیامِ خصوصی به مالک/ادمینِ ربات: «فلان کاربر با آبان گیت‌وی X تومان
    شارژ کرد» — مستقل از کانالِ لاگِ تراکنش‌ها (که اختیاری/تنظیمی است)."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
    if not brow:
        return
    who = f"@{u.username}" if (u and u.username) else (u.full_name if (u and u.full_name) else None)
    who_line = f"{who} (<code>{tg_id}</code>)" if who else f"<code>{tg_id}</code>"
    from tenant import _jalali_date_time
    d, t = _jalali_date_time(datetime.utcnow())
    text = (
        "💳 <b>شارژِ آبان گیت‌وی</b>\n\n"
        f"👤 کاربر: {who_line}\n"
        f"💵 مبلغ: <b>{amount:,}</b> تومان\n"
        f"🔖 کدِ فاکتور: <code>{order_id}</code>\n"
        f"🕒 {d} — {t}"
    )
    tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        from tenant import _notify_admins
        await _notify_admins(tbot, brow, text)
    finally:
        try:
            await tbot.session.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


async def mark_claimed(bot_id: int, order_id: str) -> None:
    """کاربر رویِ «بررسیِ پرداخت» زده ولی هنوز paid نشده — اولین‌بار زمانش را
    ثبت می‌کنیم تا اگر بعدِ چند دقیقه هنوز waiting بود، به ادمین هشدار بدهیم.
    کلیک‌هایِ بعدی چیزی را ریست نمی‌کنند (وگرنه کاربر می‌تواند با کلیکِ
    مکرر هشدار را همیشه عقب بیندازد)."""
    async with Session() as s:
        ch = await s.scalar(select(AbanGatewayCharge).where(
            AbanGatewayCharge.bot_id == bot_id,
            AbanGatewayCharge.order_id == order_id).with_for_update())
        if ch and ch.status == "waiting" and ch.claimed_at is None:
            ch.claimed_at = datetime.utcnow()
            await s.commit()


async def check_stale_claims(bot_id: int | None = None) -> int:
    """فاکتورهایی که کاربر ادعایِ پرداخت کرده (claimed_at ثبت شده) ولی بعدِ
    ~۵ دقیقه هنوز waiting مانده‌اند را به ادمینِ ربات هشدار می‌دهد — با
    دکمهٔ تأییدِ دستی/ردِ صریح. خروجی: تعدادِ هشدارِ ارسال‌شده."""
    cutoff = datetime.utcnow() - timedelta(minutes=CLAIM_ALERT_AFTER_MINUTES)
    async with Session() as s:
        q = select(AbanGatewayCharge).where(
            AbanGatewayCharge.status == "waiting",
            AbanGatewayCharge.admin_notified == False,  # noqa: E712
            AbanGatewayCharge.claimed_at.isnot(None),
            AbanGatewayCharge.claimed_at <= cutoff)
        if bot_id:
            q = q.where(AbanGatewayCharge.bot_id == bot_id)
        rows = (await s.scalars(q)).all()
        items = [(r.bot_id, r.order_id) for r in rows]

    n = 0
    for bid, oid in items:
        try:
            # آخرین شانس: شاید همین الان از خودِ درگاه تأیید بیاید — اگر
            # بیاید، دیگر لازم نیست مزاحمِ ادمین شویم.
            ok, _msg, _amt = await settle_charge(bid, oid)
            if ok:
                continue
            if await _send_stale_claim_alert(bid, oid):
                n += 1
        except Exception:
            logger.exception("abangateway: بررسیِ ادعایِ پرداخت خطا داد (bot=%s order=%s)", bid, oid)
    return n


async def _send_stale_claim_alert(bot_id: int, order_id: str,
                                  verified_elsewhere: bool = False) -> bool:
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    async with Session() as s:
        ch = await s.scalar(select(AbanGatewayCharge).where(
            AbanGatewayCharge.bot_id == bot_id,
            AbanGatewayCharge.order_id == order_id).with_for_update())
        if not ch or ch.status != "waiting" or ch.admin_notified:
            return False
        ch.admin_notified = True
        tg_id, amount, claimed_at = ch.user_telegram_id, int(ch.amount_toman or 0), ch.claimed_at
        if verified_elsewhere and claimed_at is None:  # === PATCH_ABANGATEWAY_LATE_PAID_V1 ===
            claimed_at = datetime.utcnow()
        await s.commit()
        brow = await s.get(BotRow, bot_id)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == tg_id))
    if not brow:
        return False
    who = f"@{u.username}" if (u and u.username) else (u.full_name if (u and u.full_name) else None)
    who_line = f"{who} (<code>{tg_id}</code>)" if who else f"<code>{tg_id}</code>"
    from tenant import _jalali_date_time
    d, t = _jalali_date_time(claimed_at or datetime.utcnow())
    text = (
        "⚠️ <b>ادعایِ پرداختِ تأییدنشده — آبان گیت‌وی</b>\n\n"
        f"👤 کاربر: {who_line}\n"
        f"💵 مبلغ: <b>{amount:,}</b> تومان\n"
        f"🔖 کدِ فاکتور: <code>{order_id}</code>\n"
        f"🕒 زمانِ اعلامِ پرداخت: {d} — {t}\n\n"
        + (
            # === PATCH_ABANGATEWAY_LATE_PAID_V1 ===
            "این فاکتور روی خودِ درگاهِ آبان <b>تأیید شده</b> (احتمالاً دستی از "
            "پنلِ آبان)، ولی ربات نتوانست خودکار شارژش کند. اگر در پنلِ آبان "
            "پرداختش را می‌بینی، «تأیید و شارژِ دستی» را بزن."
            if verified_elsewhere else
            f"این کاربر دکمهٔ «بررسیِ پرداخت» را زده ولی حدودِ {CLAIM_ALERT_AFTER_MINUTES} "
            "دقیقه گذشته و هنوز از خودِ درگاه تأییدی نیامده. اگر خودت مطمئنی که پول "
            "رسیده (مثلاً رسیدِ بانکی را دیدی)، تأیید کن تا کیفِ‌پولش دستی شارژ شود؛ "
            "وگرنه رد کن.")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تأیید و شارژِ دستی", callback_data=f"ab:claim:ok:{order_id}"),
        InlineKeyboardButton(text="❌ رد کن", callback_data=f"ab:claim:no:{order_id}"),
    ]])
    tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        from tenant import _notify_admins
        await _notify_admins(tbot, brow, text, kb)
    finally:
        try:
            await tbot.session.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return True


async def poll_pending(bot_id: int | None = None, include_closed: bool = False) -> int:
    """استعلامِ دوره‌ایِ فاکتورهایِ بازِ چند ساعتِ اخیر. خروجی: تعدادِ تسویه‌شده.

    === PATCH_ABANGATEWAY_LATE_PAID_V1 ===
    با include_closed=True، فاکتورهایِ منقضی/لغوشدهٔ ۲۴ ساعتِ اخیر هم
    استعلام می‌شوند — برایِ وقتی که پیامکِ بانک دیر رسیده و مالک بعداً
    فاکتور را در پنلِ آبان دستی تأیید کرده."""
    from sqlalchemy import or_, and_
    cutoff = datetime.utcnow() - timedelta(hours=6)
    late_cutoff = datetime.utcnow() - timedelta(hours=24)
    cond = and_(AbanGatewayCharge.status == "waiting",
                AbanGatewayCharge.created_at >= cutoff)
    if include_closed:
        cond = or_(cond, and_(AbanGatewayCharge.status.in_(("expired", "cancelled")),
                              AbanGatewayCharge.created_at >= late_cutoff))
    async with Session() as s:
        q = select(AbanGatewayCharge).where(cond)
        if bot_id:
            q = q.where(AbanGatewayCharge.bot_id == bot_id)
        rows = (await s.scalars(q)).all()
        items = [(r.bot_id, r.order_id) for r in rows]

    n = 0
    for bid, oid in items:
        try:
            ok, _msg, _amt = await settle_charge(bid, oid)
            if ok:
                n += 1
        except Exception:
            logger.exception("abangateway: poll خطا (bot=%s order=%s)", bid, oid)
    return n


# === PATCH_ABANGATEWAY_V1 ===
async def abangateway_poll_loop():
    """هر ۳ دقیقه فاکتورهایِ باز را استعلام می‌کند و در صورتِ پرداخت،
    کیفِ پول را شارژ کرده و به کاربر خبر می‌دهد. اگر آبان گیت‌وی خودش
    وب‌هوک را هم صدا بزند، همان لحظه (از طریقِ دکمهٔ «بررسیِ پرداخت» یا
    مسیرِ وب‌هوک) تسویه می‌شود؛ این حلقه فقط شبکهٔ ایمنیِ عقب‌افتادن است."""
    import asyncio
    await asyncio.sleep(30)
    _tick = 0
    while True:
        _tick += 1
        try:
            # === PATCH_ABANGATEWAY_LATE_PAID_V1 === هر ~۱۵ دقیقه منقضی‌ها هم
            n = await poll_pending(include_closed=(_tick % 5 == 1))
            if n:
                logger.info("[ABANGATEWAY] %s فاکتور خودکار تسویه شد", n)
        except Exception:
            logger.exception("[ABANGATEWAY] حلقهٔ استعلام خطا داد")
        # === PATCH_ABANGATEWAY_ADMIN_NOTIFY_V1 ===
        try:
            m = await check_stale_claims()
            if m:
                logger.info("[ABANGATEWAY] %s هشدارِ ادعایِ پرداخت به ادمین ارسال شد", m)
        except Exception:
            logger.exception("[ABANGATEWAY] بررسیِ ادعاهایِ پرداخت خطا داد")
        await asyncio.sleep(180)


async def _notify_paid(bot_id: int, tg_id: int, amount: int, new_balance: float,
                       for_order: int | None = None) -> None:
    """پیامِ «کیفِ پولت شارژ شد» با توکنِ خودِ همان ربات.

    === PATCH_GATEWAY_PAY_FOR_ORDER_V1 ===
    اگر این شارژ برایِ یک سفارشِ مشخص بوده، به‌جایِ رهاکردنِ کاربر وسطِ
    کیفِ پول، یک دکمهٔ مستقیمِ «تکمیلِ سفارش» می‌دهیم که همان مسیرِ
    همیشگیِ pay:wallet را صدا می‌زند. عمداً خودکار تسویه نمی‌کنیم:
    آن مسیر تحویلِ سرویس و عودتِ خودکار را هم مدیریت می‌کند و دوباره‌نویسی‌اش
    یعنی ریسکِ کسرِ پول بدونِ تحویل.
    """
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
        if for_order:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            from aiogram.enums import ButtonStyle
            # === PATCH_ABANGATEWAY_VPN_PLAN_RESUME_V1 ===
            # گزارشِ ادمین (کسری): خریدِ پلنِ VPN اصلاً Order نمی‌سازد
            # (مستقیم کیف‌پول کسر می‌شود)، پس کاربر بعدِ شارژ باید دستی
            # برمی‌گشت و دوباره پلن را می‌خرید. به‌جایِ افزودنِ ستونِ تازه
            # به دیتابیس (که migration می‌خواهد)، چون Orderهایِ واقعی
            # همیشه مثبت‌اند، همین ستونِ for_order_id را برایِ «رزومِ
            # خریدِ پلن» با یک عددِ *منفی* (−plan_id) پر می‌کنیم و اینجا
            # فقط بر اساسِ علامت تصمیم می‌گیریم کدام دکمه ساخته شود.
            if for_order > 0:
                resume_cb = f"pay:wallet:{for_order}"
                btn_text = "🛒 تکمیلِ سفارش و دریافتِ سرویس"
            else:
                # === PATCH_ABANGATEWAY_VC_DIRECT_V1 === خریدِ دلخواه (gb/روز/تعداد رمزگذاری‌شده)
                from tenant.gateways.abangateway_admin import vc_unpack
                _vc = vc_unpack(for_order)
                if _vc:
                    resume_cb = f"vc:pay:{_vc[0]}:{_vc[1]}:{_vc[2]}"
                else:
                    resume_cb = f"vpay:{-for_order}"
                btn_text = "🛡 تکمیلِ خریدِ VPN و دریافتِ سرویس"
            text = (f"✅ <b>{int(amount):,} تومان</b> پرداخت شد.\n"
                    f"موجودیِ فعلی: <b>{int(new_balance):,}</b> تومان\n\n"
                    "حالا برایِ نهایی‌شدنِ خرید، دکمهٔ زیر را بزن 👇")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=btn_text, callback_data=resume_cb, style=ButtonStyle.SUCCESS)]])
        else:
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
