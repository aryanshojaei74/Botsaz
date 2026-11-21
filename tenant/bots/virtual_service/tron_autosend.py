"""tron_autosend.py — «ارسالِ خودکارِ ترون و تتر» برایِ فروشِ رمزارز (مثلِ ton_autosend.py برایِ تون).

=== PATCH_TRON_AUTOSEND_V1 ===
بعد از پرداختِ مشتری، ربات TRX یا USDT-TRC20 را از **کیف‌پولِ ترونِ اختصاصیِ همین ربات** به آدرسِ مشتری می‌فرستد.

ایمنی (پایِ پولِ واقعی — محافظه‌کارانه، مثلِ تون):
  • پیش‌فرض خاموش؛ فقط مالک روشنش می‌کند؛ سقفِ هر سفارش جدا برایِ TRX و USDT.
  • کیلِ خصوصی با wallet_crypto.py (Fernet؛ کلیدِ WALLET_ENC_KEY در .env) رمزنگاری می‌شود؛ بدونِ آن ساختِ کیف‌پول اصلاً انجام نمی‌شود.
  • فقط سفارشِ awaiting_admin با آدرسِ معتبرِ ترون و شبکهٔ TRC20؛ «قفلِ ردیف» (status=sending) تا دو بار نرود؛ هر ربات یک قفل.
  • txID *پیش از انتشار* روی سفارش ذخیره می‌شود (tx_hash)؛ اگر خطا/تایم‌اوت بعد از انتشار بود، ادمین با همان هش روی بلاک‌چین چک می‌کند.
  • هر خطایِ «قبل از انتشار» ⇒ برگشت به مسیرِ دستی؛ هر خطایِ «ممکن است رفته باشد» ⇒ status=sending می‌ماند و ۱۵ دقیقه ارسالِ خودکار می‌ایستد.
  • موجودیِ لازم: TRX ≥ مبلغ + ذخیرهٔ کارمزد؛ USDT ≥ مبلغ و TRX ≥ ۳۰ (برایِ کارمزدِ انرژی).
"""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from config import Session
from models import GiftOrder

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

CENTI = 100
DEFAULT_CAP = {"trx": 5000.0, "usdt": 500.0}
TRX_RESERVE = 2.0                  # کارمزدِ bandwidth/فعال‌سازیِ حسابِ مقصد
MIN_TRX_FOR_USDT = 30.0            # انرژیِ انتقالِ USDT (حدودِ ۱۳ تا ۲۷ TRX سوزانده می‌شود)
PAUSE_S = 900

_LOCKS: dict[int, asyncio.Lock] = {}
_PAUSED_UNTIL: dict[int, float] = {}


def _lock(bot_id: int) -> asyncio.Lock:
    return _LOCKS.setdefault(int(bot_id), asyncio.Lock())


def paused(bot_id: int) -> bool:
    return _PAUSED_UNTIL.get(int(bot_id), 0) > time.monotonic()


async def _gs(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _ss(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


def _f(v, d: float) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return d


async def settings(bot_id: int) -> dict:
    """هر خطایِ خواندن ⇒ «خاموش» (ارسالِ پول هرگز به‌خاطرِ خطا روشن نمی‌ماند)."""
    try:
        return {
            "enabled": (await _gs(bot_id, "cx_tron_auto", "0")) == "1",
            "cap_trx": max(0.0, _f(await _gs(bot_id, "cx_tron_cap_trx", str(DEFAULT_CAP["trx"])), DEFAULT_CAP["trx"])),
            "cap_usdt": max(0.0, _f(await _gs(bot_id, "cx_tron_cap_usdt", str(DEFAULT_CAP["usdt"])), DEFAULT_CAP["usdt"])),
            "api_key": (await _gs(bot_id, "cx_tron_apikey", "")).strip(),
        }
    except Exception:      # noqa: BLE001
        logger.exception("tron_autosend: reading settings failed (bot=%s)", bot_id)
        return {"enabled": False, "cap_trx": DEFAULT_CAP["trx"], "cap_usdt": DEFAULT_CAP["usdt"], "api_key": ""}


# ───────────── کیف‌پولِ اختصاصی ─────────────
async def wallet_address(bot_id: int) -> str | None:
    a = (await _gs(bot_id, "cx_tron_addr", "")).strip()
    return a or None


async def create_wallet(bot_id: int) -> tuple[bool, str]:
    """کیف‌پولِ جدید می‌سازد (فقط اگر نباشد). خروجی: (موفق?, آدرس یا پیامِ خطا)."""
    from tenant.shared import tron_chain as TC
    if await wallet_address(bot_id):
        return False, "کیف‌پول قبلاً ساخته شده است."
    try:
        from wallet_crypto import encrypt_secret
        priv = TC.new_private_key()
        enc = encrypt_secret(format(priv, "064x"))
    except Exception as e:      # noqa: BLE001 — از جمله WalletCryptoNotConfigured
        logger.exception("tron_autosend: wallet creation failed")
        return False, f"ساختِ کیف‌پول ممکن نشد ({type(e).__name__}). کلیدِ WALLET_ENC_KEY در .env تنظیم نیست؟"
    addr = TC.address_from_private(priv)
    await _ss(bot_id, "cx_tron_key", enc)           # اول کلید، بعد آدرس: آدرس بدونِ کلید هرگز ثبت نمی‌شود
    await _ss(bot_id, "cx_tron_addr", addr)
    return True, addr


async def reveal_private_key(bot_id: int) -> str | None:
    try:
        from wallet_crypto import decrypt_secret
        enc = await _gs(bot_id, "cx_tron_key", "")
        return decrypt_secret(enc) if enc else None
    except Exception:      # noqa: BLE001
        logger.exception("tron_autosend: key reveal failed (bot=%s)", bot_id)
        return None


async def balances(bot_id: int) -> tuple[float | None, float | None]:
    """(TRX, USDT) زنده؛ هر کدام که خوانده نشد None."""
    from tenant.shared import tron_chain as TC
    addr = await wallet_address(bot_id)
    if not addr:
        return None, None
    key = (await settings(bot_id))["api_key"]
    out = []
    for fn, div in ((TC.trx_balance, TC.SUN), (TC.usdt_balance, TC.SUN)):
        try:
            out.append(await fn(addr, key) / div)
        except Exception:      # noqa: BLE001
            out.append(None)
    return out[0], out[1]


# ───────────── ارسال ─────────────
async def _load_priv(bot_id: int) -> int | None:
    k = await reveal_private_key(bot_id)
    try:
        return int(k, 16) if k else None
    except ValueError:
        return None


async def send(bot_id: int, order_id: int, coin: str, to: str, centi: int, cfg: dict) -> tuple[str, str, str | None]:
    """ارسالِ TRX/USDT. خروجی: ("ok"|"fail"|"unconfirmed", پیام, txid|None).
    fail = قطعاً چیزی منتشر نشده (امن برایِ برگشت به دستی)؛ unconfirmed = ممکن است رفته باشد."""
    from tenant.shared import tron_chain as TC
    priv = await _load_priv(bot_id)
    if priv is None:
        return "fail", "کیف‌پولِ ترونِ ربات ساخته نشده یا کلیدش خوانده نشد.", None
    frm = TC.address_from_private(priv)
    if frm != await wallet_address(bot_id):
        return "fail", "آدرسِ ذخیره‌شده با کلید نمی‌خواند؛ ارسالِ خودکار متوقف شد.", None
    key = cfg["api_key"]
    amount = centi / CENTI
    try:
        trx = await TC.trx_balance(frm, key) / TC.SUN
        if coin == "trx":
            if trx < amount + TRX_RESERVE:
                return "fail", f"موجودیِ TRX کافی نیست ({trx:.2f}؛ لازم: {amount + TRX_RESERVE:.2f}).", None
        else:
            usdt = await TC.usdt_balance(frm, key) / TC.SUN
            if usdt < amount:
                return "fail", f"موجودیِ USDT کافی نیست ({usdt:.2f}؛ لازم: {amount:.2f}).", None
            if trx < MIN_TRX_FOR_USDT:
                return "fail", f"برایِ کارمزدِ انتقالِ USDT حداقل {MIN_TRX_FOR_USDT:g} TRX لازم است (الان {trx:.2f}).", None
    except Exception as e:      # noqa: BLE001
        return "fail", f"موجودیِ کیف‌پول از شبکه خوانده نشد ({type(e).__name__}).", None
    try:
        txid, signed = await TC.prepare_transfer(coin, priv, to, centi, key)
    except TC.TronError as e:
        return "fail", f"ساختِ تراکنش ناموفق: {e}", None
    except Exception as e:      # noqa: BLE001
        logger.exception("tron_autosend: prepare failed (bot=%s)", bot_id)
        return "fail", f"ساختِ تراکنش ناموفق ({type(e).__name__}).", None
    # txID را پیش از انتشار روی سفارش ثبت کن: اگر وسطِ راه خطا شد، ادمین با همین هش می‌تواند چک کند
    async with Session() as s:
        o = await s.get(GiftOrder, order_id, with_for_update=True)
        if o is None or o.status != "sending":
            return "fail", "وضعیتِ سفارش عوض شده؛ ارسال انجام نشد.", None
        o.tx_hash = txid
        await s.commit()
    try:
        accepted, note = await TC.broadcast(signed, key)
    except TC.TronAmbiguous as e:
        return "unconfirmed", f"پاسخِ شبکه به انتشار نامشخص بود: {e}", txid
    if not accepted:
        # رد شدنِ صریح؛ برایِ اطمینان یک بار وضعیتِ هش را هم می‌پرسیم (شاید قبلاً رفته باشد)
        st = await TC.tx_status(txid, key)
        if st == "success":
            return "ok", "تراکنش در شبکه ثبت شد.", txid
        if st == "failed":
            return "fail", "تراکنش در شبکه ناموفق شد (کارمزد سوخته؛ توکن منتقل نشد).", None
        return "fail", f"گرهٔ ترون تراکنش را نپذیرفت: {note}", None
    st = await TC.wait_confirmed(txid, key)
    if st == "success":
        return "ok", "تراکنش در شبکه ثبت شد.", txid
    if st == "failed":
        return "fail", "تراکنش در شبکه ناموفق شد (کارمزد سوخته؛ توکن منتقل نشد).", None
    return "unconfirmed", "تراکنش منتشر شد ولی تأییدِ ثبت در بلاک نیامد.", txid


async def deliver_order(bot, row, order_id: int, force: bool = False) -> tuple[str, str]:
    """تلاش برایِ تحویلِ خودکارِ یک سفارشِ cxsell_. هرگز استثنا نمی‌دهد.
    خروجی: ("disabled"|"manual"|"sent"|"unconfirmed", پیام). manual/unconfirmed ⇒ فراخوان ادمین را خبر کند."""
    from tenant.shared import tron_chain as TC
    try:
        cfg = await settings(row.id)
        if not cfg["enabled"] and not force:
            return "disabled", ""
        if paused(row.id) and not force:
            return "manual", "ارسالِ خودکار به‌خاطرِ یک خطایِ نامشخصِ اخیر موقتاً متوقف است؛ دستی بفرست."
        async with _lock(row.id):
            async with Session() as s:
                o = await s.get(GiftOrder, order_id, with_for_update=True)
                if (not o or o.bot_id != row.id or not (o.gift_id or "").startswith("cxsell_")
                        or o.status != "awaiting_admin"):
                    return "manual", "سفارش در وضعیتِ قابلِ‌ارسال نیست."
                parts = (o.gift_id or "").split("_")
                coin = parts[1] if len(parts) > 2 and parts[1] in ("trx", "usdt") else ""
                centi = int(o.star_count or 0)
                addr = (o.dest_label or "").strip()
                net = (o.dest_memo or "").replace("net:", "")
                if not coin or net != "TRC20":
                    return "manual", "ارسالِ خودکار فقط برایِ شبکهٔ ترون (TRC20) است."
                cap = cfg["cap_trx"] if coin == "trx" else cfg["cap_usdt"]
                if centi <= 0 or centi / CENTI > cap:
                    return "manual", f"مقدار ({centi / CENTI:g}) از سقفِ ارسالِ خودکار ({cap:g}) بیشتر است."
                if not TC.is_valid_address(addr):
                    return "manual", "آدرسِ مقصد برایِ ارسالِ خودکار معتبر نیست."
                o.status = "sending"          # قفلِ ردیف: از این لحظه هیچ مسیرِ دیگری ارسال نمی‌کند
                o.deliver_via = "auto"
                await s.commit()
            res, note, txid = await send(row.id, order_id, coin, addr, centi, cfg)
            async with Session() as s:
                o = await s.get(GiftOrder, order_id, with_for_update=True)
                if o is not None and o.status == "sending":
                    if res == "ok":
                        o.status = "sent"
                        o.fail_reason = None
                        o.tx_hash = txid
                    elif res == "fail":
                        o.status = "awaiting_admin"        # قطعاً نرفته ⇒ مسیرِ دستی
                        o.deliver_via = ""
                        o.fail_reason = note[:500]
                        o.tx_hash = None                   # هشِ ساخته‌شدهٔ هرگز-منتشرنشده نماند
                    else:
                        o.fail_reason = ("⚠️ نامشخص: " + note)[:500]     # status=sending می‌ماند تا ادمین چک کند
                    await s.commit()
            if res == "unconfirmed":
                _PAUSED_UNTIL[int(row.id)] = time.monotonic() + PAUSE_S
            if res == "ok":
                return "sent", note
            return ("manual" if res == "fail" else "unconfirmed"), note
    except Exception as e:      # noqa: BLE001
        logger.exception("tron_autosend: unexpected error (bot=%s order=%s)", getattr(row, "id", None), order_id)
        sending = False
        try:
            async with Session() as s:
                o = await s.get(GiftOrder, order_id)
                sending = bool(o and o.status == "sending")
        except Exception:      # noqa: BLE001
            logger.exception("tron_autosend: reading the order status after the error failed")
        return ("unconfirmed" if sending else "manual"), f"خطایِ غیرمنتظره: {type(e).__name__}"
