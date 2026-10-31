"""
blupal.py — PATCH_BLUPAL_TENANT_V1

درگاهِ کارت‌به‌کارتِ خودکارِ «بلوپال» (blupal.top؛ مستندات: https://blupal.top/documentation) برایِ شارژِ کیفِ پولِ
رباتِ مشتری. (درگاهِ بلوپالِ خودِ بات‌ساز جداست: platform_blu.py.)

فلو (بدونِ وب‌هوک؛ همیشه خودمان از API می‌پرسیم — وب‌هوکِ بلو امضا ندارد و قابلِ جعل است):
  ۱) POST /v1/invoices/create {amount(ریال)}  →  invoice_id / final_amount / payment_link / card_number
  ۲) خریدار دقیقاً final_amount ریال را به card_number واریز می‌کند (final_amount = مبلغ + عددِ تصادفیِ ۰–۹۹۹ ریال)
  ۳) GET /v1/invoices/{id}  →  status=PAID  (PENDING / PAID / EXPIRED / CANCELED)
  ۴) کیفِ پول به‌اندازهٔ مبلغِ درخواستی (amount؛ نه final_amount) شارژ می‌شود

نکته‌ها:
  • مبلغِ API ریال است و کیفِ پول تومان؛ هر دو در دیتابیس ذخیره می‌شود (۱ تومان = ۱۰ ریال).
  • بازهٔ مجاز طبقِ مستندات ۱۰۰٬۰۰۰ تا ۵۰۰٬۰۰۰٬۰۰۰ ریال (۱۰٬۰۰۰ تا ۵۰٬۰۰۰٬۰۰۰ تومان).
  • شارژ فقط وقتی انجام می‌شود که خودِ بلو PAID بگوید و amount/final_amountِ پاسخ با رکوردِ ما بخواند.
  • فاکتورِ live تاریخِ انقضا ندارد؛ بعد از ۴۵ دقیقه فقط «منقضی» نشان داده می‌شود ولی تا ۲۴ ساعت همچنان استعلام می‌شود
    تا واریزِ دیرهنگامِ مشتری نسوزد.
  • تسویه idempotent است (قفلِ ردیف + وضعیت).
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
from models import BotUser, BluPalCharge, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

BASE_URL = "https://blupal.top/api"
_TIMEOUT = 20
RIAL = 10                                  # ۱ تومان = ۱۰ ریال
MIN_RIAL, MAX_RIAL = 100_000, 500_000_000
FLOOR_AMOUNT = MIN_RIAL // RIAL            # ۱۰٬۰۰۰ تومان
CEIL_AMOUNT = MAX_RIAL // RIAL             # ۵۰٬۰۰۰٬۰۰۰ تومان
MIN_AMOUNT = FLOOR_AMOUNT
EXPIRE_SHOW_MINUTES = 45                   # بعد از این مدت «منقضی» نشان داده می‌شود
POLL_WINDOW_HOURS = 24                     # تا این مدت همچنان استعلام می‌شود


async def _get(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _set(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


def new_order_id() -> str:
    return "bp" + secrets.token_hex(8)


def to_rial(toman: int) -> int:
    return int(toman) * RIAL


def fmt_rial(r: int) -> str:
    return f"{int(r):,} ریال"


def key_mode(api_key: str) -> str:
    k = (api_key or "").strip()
    return "sandbox" if k.startswith("blu_test_") else ("live" if k.startswith("blu_live_") else "unknown")


async def get_min_amount(bot_id: int) -> int:
    try:
        n = int(await _get(bot_id, "blupal_min_amount", str(MIN_AMOUNT)))
        return n if n >= FLOOR_AMOUNT else MIN_AMOUNT
    except (TypeError, ValueError):
        return MIN_AMOUNT


async def get_max_amount(bot_id: int) -> int:
    """سقفِ تومانیِ تنظیم‌شده؛ ۰ یعنی «سقفِ خودِ درگاه» (۵۰ میلیون تومان)."""
    try:
        n = max(int(await _get(bot_id, "blupal_max_amount", "0")), 0)
    except (TypeError, ValueError):
        n = 0
    return min(n, CEIL_AMOUNT) if n else 0


async def effective_max(bot_id: int) -> int:
    return await get_max_amount(bot_id) or CEIL_AMOUNT


async def is_available(bot_id: int) -> bool:
    return await _get(bot_id, "blupal_enabled", "0") == "1" and bool(await _get(bot_id, "blupal_api_key", ""))


async def get_display_name(bot_id: int) -> str:
    return (await _get(bot_id, "blupal_display_name", "")).strip() or "بلوپال"


async def replaces_card(bot_id: int) -> bool:
    """PATCH_BLUPAL_UX_V1: آیا بلوپال باید *جایگزینِ* کارت‌به‌کارتِ دستی شود؟ (مثلِ آبان گیت‌وی)
    وقتی روشن و درگاه فعال است، دکمهٔ «کارت‌به‌کارتِ دستی» در همهٔ صفحه‌هایِ پرداخت پنهان می‌شود و بلوپال همه‌جا
    می‌آید. درگاه غیرفعال ⇒ بی‌اثر (مالک هیچ‌وقت بدونِ روشِ پرداخت نمی‌ماند)."""
    if await _get(bot_id, "blupal_replace_card", "0") != "1":
        return False
    return await is_available(bot_id)


# ─────────────────── ویرایشِ همان پیامِ فاکتور (به‌جای پیامِ جدید) ───────────────────

async def edit_invoice_message(bot_id: int, chat_id, msg_id, text: str, kb=None) -> bool:
    """پیامِ فاکتور را با توکنِ خودِ همان ربات ویرایش می‌کند. True اگر ویرایش شد."""
    if not chat_id or not msg_id:
        return False
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from models import Bot as BotRow
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
    if not brow:
        return False
    tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await tbot.edit_message_text(text, chat_id=int(chat_id), message_id=int(msg_id), reply_markup=kb)
        return True
    except Exception as e:  # noqa: BLE001 — پیام پاک شده/تغییرنکرده: بی‌اهمیت؛ بالادست پیامِ جدید می‌فرستد
        logger.debug("blupal: ویرایشِ پیامِ فاکتور ممکن نشد: %s", e)
        return False
    finally:
        try:
            await tbot.session.close()
        except Exception:  # noqa: BLE001
            logger.debug("blupal: بستنِ نشستِ ربات ناموفق")


def code_of(ch) -> str:
    """کدِ پیگیریِ نمایشی: شمارهٔ ترتیبیِ ربات؛ برایِ فاکتورهایِ قدیمی همان order_id."""
    return str(getattr(ch, "seq_code", None) or getattr(ch, "order_id", "") or "")


def closed_text(ch_amount: int, code: str, status: str) -> str:
    if status == "cancelled":
        return (f"❌ <b>فاکتور لغو شد.</b>\n\n🔑 کدِ پیگیری: <code>{code}</code>\n💵 مبلغ: {int(ch_amount):,} تومان\n\n"
                "اگر واریز کردی ولی اینجا لغو شد، نگران نباش؛ تا ۲۴ ساعت اگر واریز برسد خودکار شارژ می‌شود، "
                "یا به پشتیبانی بگو با همین کدِ پیگیری بررسی کند.")
    return (f"⏳ <b>فاکتور منقضی شد.</b>\n\n🔑 کدِ پیگیری: <code>{code}</code>\n💵 مبلغ: {int(ch_amount):,} تومان\n\n"
            "اگر واریز کرده‌ای نگران نباش؛ تا ۲۴ ساعت بررسی می‌شود و در صورتِ رسیدنِ واریز کیفِ پولت خودکار شارژ می‌شود. "
            "در غیرِ این صورت فاکتورِ تازه بساز.")


def closed_kb():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ فاکتورِ تازه", callback_data="bpu:start")]])


# ─────────────────────────── API ───────────────────────────

async def _api(api_key: str, method: str, path: str, body: dict | None = None) -> dict:
    """تماسِ خام. خروجی همیشه dict با کلیدِ success است (در خطا: success=False + error/message + http)."""
    if aiohttp is None:
        return {"success": False, "http": 0, "error": "client", "message": "aiohttp نصب نیست"}
    headers = {"X-API-Key": api_key, "Accept": "application/json", "Content-Type": "application/json"}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.request(method, f"{BASE_URL}{path}", json=body, headers=headers) as r:
                out = await r.json(content_type=None)
                if not isinstance(out, dict):
                    return {"success": False, "http": r.status, "error": "bad_response", "message": "پاسخِ نامعتبر از درگاه"}
                out.setdefault("success", 200 <= r.status < 300)
                out["http"] = r.status
                return out
    except Exception as e:  # noqa: BLE001
        logger.warning("blupal: خطای شبکه در %s %s: %s", method, path, e)
        return {"success": False, "http": 0, "error": "network", "message": str(e) or type(e).__name__}


_ERR_FA = {
    "unauthorized": "کلید API نامعتبر است.",
    "amount_required": "مبلغ ارسال نشده است.",
    "amount_too_low": f"مبلغ کمتر از حداقلِ درگاه است ({FLOOR_AMOUNT:,} تومان).",
    "amount_too_high": f"مبلغ بیشتر از سقفِ درگاه است ({CEIL_AMOUNT:,} تومان).",
    "no_active_card": "در حسابِ بلوپال هیچ کارتِ فعالی نیست؛ از داشبوردِ بلوپال یک کارت اضافه و فعال کن.",
    "invalid_invoice_id": "شناسهٔ فاکتور نامعتبر است.",
    "not_found": "فاکتور پیدا نشد.",
    "mode_mismatch": "کلیدِ تست/اصلی با فاکتور هم‌حالت نیست.",
    "invalid_state": "فاکتور در وضعیتِ مناسب نیست.",
}


def _err(res: dict) -> str:
    code = str(res.get("error") or "")
    if code in _ERR_FA:
        return _ERR_FA[code]
    msg = str(res.get("message") or "").strip()
    if res.get("http") == 401:
        return _ERR_FA["unauthorized"]
    return msg or "پاسخِ نامشخص از درگاه"


async def create_invoice(api_key: str, amount_rial: int) -> dict:
    return await _api(api_key, "POST", "/v1/invoices/create", {"amount": int(amount_rial)})


async def get_invoice(api_key: str, invoice_id) -> dict:
    return await _api(api_key, "GET", f"/v1/invoices/{int(invoice_id)}")


async def validate_key(api_key: str) -> tuple[bool, str]:
    """تستِ کلید بدونِ ساختِ فاکتور: استعلامِ یک فاکتورِ ناموجود. 401 ⇒ کلید غلط؛ 404/400 ⇒ کلید درست است."""
    res = await _api(api_key, "GET", "/v1/invoices/1")
    if res.get("http") == 401 or res.get("error") == "unauthorized":
        return False, _ERR_FA["unauthorized"]
    if res.get("http") in (200, 400, 403, 404) or res.get("error") in ("not_found", "invalid_invoice_id", "mode_mismatch"):
        mode = {"sandbox": "حالتِ تست (Sandbox)", "live": "حالتِ اصلی (Live)"}.get(key_mode(api_key), "")
        return True, "کلید معتبر است و درگاه پاسخ داد." + (f" — {mode}" if mode else "")
    return False, _err(res)


# ─────────────────────────── تسویه ───────────────────────────

# === PATCH_BLUPAL_PAYER_CARD_V1 === کارتِ پرداخت‌کننده (اگر بلوپال در پاسخ بدهد). نامِ فیلد در مستندات ثابت نیست؛
# چند نامِ محتمل را می‌خوانیم، و اگر هیچ‌کدام نبود کلیدهای پاسخ یک‌بار در لاگ می‌آید تا نامِ درست مشخص شود.
_PAYER_CARD_KEYS = ("payer_card", "payer_card_number", "source_card", "source_card_number", "sender_card",
                    "from_card", "paid_card", "card_pan", "payer_pan", "masked_card", "payer_card_masked")


def payer_card_of(res: dict) -> str:
    for k in _PAYER_CARD_KEYS:
        v = res.get(k)
        if v:
            return str(v).strip()[:32]
    for sub in ("payer", "payment", "transaction"):
        d = res.get(sub)
        if isinstance(d, dict):
            for k in ("card", "card_number", "pan", "masked_card") + _PAYER_CARD_KEYS:
                if d.get(k):
                    return str(d[k]).strip()[:32]
    return ""


def _status(res: dict) -> str:
    return str(res.get("status") or "").upper()


async def settle_charge(bot_id: int, order_id: str) -> tuple[bool, str, int]:
    """استعلامِ یک فاکتور و در صورتِ پرداخت، شارژِ کیفِ پول. خروجی: (شارژ شد؟، پیام، مبلغِ شارژ‌شده به تومان)."""
    api_key = await _get(bot_id, "blupal_api_key", "")
    if not api_key:
        return False, "کلیدِ درگاه تنظیم نشده است.", 0
    async with Session() as s:
        ch = await s.scalar(select(BluPalCharge).where(BluPalCharge.bot_id == bot_id, BluPalCharge.order_id == order_id))
        if not ch:
            return False, "این فاکتور پیدا نشد.", 0
        if ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        inv_id, credit = ch.invoice_id, int(ch.amount_toman or 0)
        want_rial, want_final = int(ch.amount_rial or 0), int(ch.final_amount_rial or 0)
        prev_status, chat_id, msg_id, code = ch.status, ch.chat_id, ch.msg_id, code_of(ch)
    if not inv_id:
        return False, "این فاکتور شناسهٔ درگاه ندارد.", 0

    res = await get_invoice(api_key, inv_id)
    if not res.get("success"):
        return False, _err(res), 0
    st = _status(res)
    if st != "PAID":
        if st in ("EXPIRED", "CANCELED", "CANCELLED"):
            async with Session() as s:
                ch = await s.scalar(select(BluPalCharge).where(BluPalCharge.bot_id == bot_id, BluPalCharge.order_id == order_id,
                                                               BluPalCharge.status.in_(("waiting", "expired"))).with_for_update())
                if ch:
                    ch.status = "expired" if st == "EXPIRED" else "cancelled"
                    await s.commit()
                    new_status = ch.status
                else:
                    new_status = None
            if new_status and prev_status == "waiting":          # فقط لحظهٔ تغییرِ وضعیت؛ هر دورِ استعلام پیام را دست نمی‌زند
                await edit_invoice_message(bot_id, chat_id, msg_id, closed_text(credit, code, new_status), closed_kb())
            return False, "مهلتِ این فاکتور تمام شده یا لغو شده است.", 0
        return False, "هنوز پرداختی ثبت نشده است.", 0
    # مبلغِ فاکتور باید با رکوردِ خودمان بخواند
    api_amt, api_final = res.get("amount"), res.get("final_amount")
    if (api_amt is not None and int(float(api_amt)) != want_rial) or \
            (api_final is not None and want_final and int(float(api_final)) != want_final):
        logger.error("blupal: مبلغِ فاکتور با رکورد نمی‌خواند (bot=%s order=%s api=%s/%s db=%s/%s)",
                     bot_id, order_id, api_amt, api_final, want_rial, want_final)
        return False, "مبلغِ فاکتور با درخواست یکسان نیست؛ با پشتیبانی هماهنگ کن.", 0

    # ── تسویهٔ اتمیک ──
    async with Session() as s:
        ch = await s.scalar(select(BluPalCharge).where(BluPalCharge.bot_id == bot_id,
                                                       BluPalCharge.order_id == order_id).with_for_update())
        if not ch or ch.status == "paid":
            return False, "این فاکتور قبلاً تسویه شده است.", 0
        ch.status = "paid"
        ch.paid_at = datetime.utcnow()
        ch.tracking_code = str(res.get("transaction_id") or "")[:64] or None
        _pcard = payer_card_of(res)
        if not _pcard:
            logger.info("blupal: پاسخِ PAID فیلدِ کارتِ پرداخت‌کننده نداشت؛ کلیدها: %s", sorted(res.keys()))
        ch.payer_name = ((str(res.get("payer_name") or "") + (" | " + _pcard if _pcard else "")).strip(" |")[:80] or None)
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id,
                                                 BotUser.telegram_id == ch.user_telegram_id).with_for_update())
        _mismatch, _paid4 = False, ""
        if u:
            # === PATCH_BLUPAL_KYC_CARD_V1 === (SINA) همان قفلِ کارتِ زیبال/زرین‌پال: کاربرِ سطح ۱ با کارتِ غیرِ ثبت‌شده ⇒ شارژ می‌شود ولی محدود.
            # فقط وقتی ۴ رقمِ آخرِ کارت در پاسخِ بلوپال هست مقایسه می‌شود؛ پرداختِ «بلو به بلو» یا کارتِ نامشخص هرگز بی‌دلیل محدود نمی‌شود.
            _locked = "".join(c for c in str(u.kyc_card or "") if c.isdigit())
            _pd = "".join(c for c in str(_pcard or "") if c.isdigit())
            _paid4 = _pd[-4:] if len(_pd) >= 4 else ""
            _mismatch = bool(int(u.kyc_level or 0) == 1 and len(_locked) >= 4 and _paid4 and _locked[-4:] != _paid4)
            if _mismatch:
                u.kyc_blocked = True
                u.kyc_note = f"پرداخت با کارتِ غیرِثبت‌شده (…{_paid4})"
            u.balance = float(u.balance or 0) + credit
            s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=credit, kind="blupal_charge",
                            note=f"شارژ با بلوپال؛ ref={ch.tracking_code or ch.invoice_id}" + (f" کارت={_pcard}" if _pcard else "")))
            _uid, _bal, _kyc_uid = u.telegram_id, float(u.balance), u.id
        else:
            _uid = _bal = _kyc_uid = None
        _chat, _msg = ch.chat_id, ch.msg_id
        await s.commit()
    if _kyc_uid:
        from tenant.shared.kyc import after_charge as _ac
        await _ac(bot_id, _kyc_uid)
    if _mismatch and _uid:
        try:
            from tenant.gateways.zibal_wallet import _kyc_mismatch_notify
            await _kyc_mismatch_notify(bot_id, _uid, _paid4)
        except Exception:
            logger.exception("blupal: اطلاعِ عدمِ تطابقِ کارت ناموفق بود")
    if _uid:
        # PATCH_BLUPAL_UX_V1: همان پیامِ فاکتور به «پرداخت شد» تبدیل می‌شود؛ فقط اگر ویرایش ممکن نبود پیامِ جدید می‌رود
        edited = False
        try:
            from tenant.gateways.wallet_receipt import build_wallet_charge_message_for_bot
            _t, _k = await build_wallet_charge_message_for_bot(bot_id, credit, _bal, "card")
            edited = await edit_invoice_message(bot_id, _chat, _msg, _t, _k)
        except Exception:
            logger.exception("blupal: ساختِ پیامِ موفقیت ناموفق بود")
        if not edited:
            try:
                from tenant.gateways.uniquepay import _notify_paid
                await _notify_paid(bot_id, _uid, credit, _bal)
            except Exception:
                logger.exception("blupal: اطلاع به کاربر ناموفق بود")
        try:
            from tenant.gateways.payment_page import _tx_log_tenant
            await _tx_log_tenant(bot_id, _uid, credit, _bal, "blupal", card=_pcard)
        except Exception:
            logger.exception("blupal: tx_log ناموفق بود")
    return True, "پرداخت تأیید و کیفِ پول شارژ شد.", credit


async def poll_pending(bot_id: int | None = None, include_closed: bool = True) -> int:
    """استعلامِ دوره‌ایِ فاکتورهایِ باز و (اگر include_closed) منقضی/لغو‌شدهٔ ۲۴ ساعتِ اخیر — واریزِ دیرهنگام یا بعد از «لغو»
    نسوزد (بلو API لغو ندارد). فاکتورِ قدیمیِ پرداخت‌نشده «منقضی» می‌شود. خروجی: تعدادِ تسویه‌شده."""
    cutoff = datetime.utcnow() - timedelta(hours=POLL_WINDOW_HOURS)
    states = ("waiting", "expired", "cancelled") if include_closed else ("waiting",)
    async with Session() as s:
        q = select(BluPalCharge).where(BluPalCharge.status.in_(states), BluPalCharge.created_at >= cutoff)
        if bot_id:
            q = q.where(BluPalCharge.bot_id == bot_id)
        items = [(r.bot_id, r.order_id, r.created_at, r.status) for r in (await s.scalars(q)).all()]
    n = 0
    expire_before = datetime.utcnow() - timedelta(minutes=EXPIRE_SHOW_MINUTES)
    for bid, oid, created_at, status in items:
        try:
            ok, _msg, _amt = await settle_charge(bid, oid)
            if ok:
                n += 1
                continue
        except Exception:
            logger.exception("blupal: poll خطا (bot=%s order=%s)", bid, oid)
            continue
        if status == "waiting" and created_at and created_at <= expire_before:
            async with Session() as s:
                ch = await s.scalar(select(BluPalCharge).where(BluPalCharge.bot_id == bid, BluPalCharge.order_id == oid,
                                                               BluPalCharge.status == "waiting").with_for_update())
                if ch:
                    ch.status = "expired"
                    _x = (ch.chat_id, ch.msg_id, int(ch.amount_toman or 0), code_of(ch))
                    await s.commit()
                else:
                    _x = None
            if _x:                                              # PATCH_BLUPAL_UX_V1: همان پیامِ فاکتور «منقضی» می‌شود
                await edit_invoice_message(bid, _x[0], _x[1], closed_text(_x[2], _x[3], "expired"), closed_kb())
    return n


async def blupal_poll_loop():
    """هر ۲ دقیقه فاکتورهایِ باز را استعلام می‌کند و در صورتِ پرداخت، کیفِ پول را شارژ و به کاربر خبر می‌دهد."""
    import asyncio
    await asyncio.sleep(40)
    cycle = 0
    while True:
        try:
            cycle += 1
            n = await poll_pending(include_closed=(cycle % 5 == 0))    # باز: هر ۲ دقیقه؛ بسته‌شده‌ها: هر ۱۰ دقیقه
            if n:
                logger.info("[BLUPAL] %s فاکتور خودکار تسویه شد", n)
        except Exception:
            logger.exception("[BLUPAL] حلقهٔ استعلام خطا داد")
        await asyncio.sleep(120)
