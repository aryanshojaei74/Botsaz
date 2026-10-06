"""
ton_direct.py — پرداختِ مستقیمِ TON به ولتِ خودِ مالکِ ربات (بدونِ درگاه).

=== PATCH_TON_DIRECT_V1 ===
«تونکیپر» یک کیفِ پولِ غیرحضانتی است، نه درگاه — API فروشنده، فاکتور و
کال‌بکِ «پرداخت شد» ندارد. پس این ماژول درگاه نیست؛ کارِ درگاه را با
خواندنِ خودِ بلاک‌چین انجام می‌دهد:

  ۱) مالکِ ربات آدرسِ ولتِ TON خودش را ثبت می‌کند (Tonkeeper یا هر ولتِ دیگر).
  ۲) کاربر مبلغ را به تومان می‌دهد؛ ربات با نرخِ زنده آن را به TON تبدیل
     می‌کند و یک فاکتور با «memoِ یکتا» و «مبلغِ یکتا» می‌سازد.
  ۳) به کاربر لینکِ ton:// (و لینکِ وبِ Tonkeeper) داده می‌شود که مبلغ و
     کامنت را از قبل پر می‌کند.
  ۴) حلقهٔ پس‌زمینه تراکنش‌هایِ ورودیِ همان آدرس را از toncenter می‌خواند و
     هر تراکنشی که با فاکتور بخورد، کیفِ پول را شارژ می‌کند.

پول در هیچ لحظه‌ای دستِ بات‌ساز یا واسطه نیست؛ مستقیم به ولتِ فروشنده
می‌رود. در عوض، مسئولیتِ نگه‌داریِ کلیدِ ولت هم کاملاً با خودِ اوست.

تشخیصِ پرداخت دو لایه دارد چون کاربران همیشه از ولتِ شخصی واریز نمی‌کنند:
  • memo: وقتی کاربر از لینکِ ربات استفاده کند، کامنت خودکار پر می‌شود.
  • مبلغِ یکتا: سه رقمِ آخرِ اعشارِ TON تصادفی است، پس حتی وقتی صرافی کامنت
    را حذف کرده باشد، مبلغ خودش مثلِ اثرِ انگشت عمل می‌کند.
"""
from __future__ import annotations

import asyncio
import datetime as _dt_late
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
from sqlalchemy import or_, select, update as sa_update

from config import Session
from models import Bot as BotRow, BotUser, TonDirectCharge, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

ton_direct_router = Router(name="ton_direct")

DEFAULTS = DefaultBotProperties(parse_mode=ParseMode.HTML)

TONCENTER_BASE = "https://toncenter.com/api/v2"
TONAPI_RATES = "https://tonapi.io/v2/rates?tokens=ton&currencies=usd"
_TIMEOUT = 20

NANO = 1_000_000_000            # ۱ TON = ۱۰^۹ نانوتون
INVOICE_TTL_MINUTES = 20        # مهلتِ فاکتور — نرخِ TON نوسان دارد
MIN_TON = Decimal("0.05")       # زیرِ این مقدار کارمزدِ شبکه بی‌معنی‌اش می‌کند
LATE_GRACE_HOURS = 24   # === PATCH_TON_DIRECT_LATE_PAY_V1 === پرداختِ دیرهنگام (بعد از انقضایِ فاکتور) تا این مدت هنوز شارژ می‌شود
AMOUNT_TOLERANCE = Decimal("0.98")   # ۲٪ ارفاق بابتِ کارمزدِ شبکه — فقط وقتی memo هم خورده باشد

# === PATCH_TON_DIRECT_SAFE_FALLBACK_V1 ===
# پنجرهٔ تطبیقِ «بدونِ memo»: باید مطلق و خیلی باریک باشد، نه درصدی.
# چون به هر فاکتورِ بازِ دیگر (حتی برایِ کاربرِ دیگر) هم اجازهٔ برخورد می‌دهد،
# اگر بازه‌اش وسیع باشد یک واریزِ کاملاً نامرتبط (یا حتی بدونِ واریز واقعی —
# مثلاً تراکنشِ دیگری که تصادفاً هم‌مبلغ است) به‌جایِ فاکتورِ درست تسویه می‌شود.
FALLBACK_ABS_TOLERANCE = Decimal("0.0015")  # فقط اختلافِ زیرِ این مقدار TON

# آدرسِ TON: یا فرمتِ کاربرپسند (EQ/UQ + ۴۶ کاراکترِ base64url) یا خامِ 0:hex
_ADDR_RE = re.compile(r"^(?:[EU]Q[A-Za-z0-9_\-]{46}|-?\d:[0-9a-fA-F]{64})$")

# === PATCH_COIN_DIRECT_MULTI_ASSET_V1 ===
# جدولِ ton_direct_charges حالا فاکتورهایِ TRX و USDT را هم نگه می‌دارد
# (مالکشان tenant/tron_direct.py است). بدونِ این فیلتر، یک واریزِ TON
# می‌توانست از رویِ «مبلغِ یکتا» به فاکتورِ ترون بخورد و کاربرِ اشتباه را
# شارژ کند. ردیف‌هایِ قدیمی asset ندارند، پس NULL هم یعنی TON.
def _ton_only():
    return or_(TonDirectCharge.asset.is_(None), TonDirectCharge.asset == "TON")



class TonDirectFSM(StatesGroup):
    wallet = State()    # ادمین: آدرسِ ولت
    fee = State()       # ادمین: درصدِ کارمزد
    manual_rate = State()   # ادمین: نرخِ دستیِ هر TON به تومان
    amount = State()    # کاربر: مبلغِ شارژ به تومان
    min_amount = State()    # === PATCH_TON_DIRECT_MINMAX_V1 === ادمین: حداقلِ مبلغ
    max_amount = State()    # === PATCH_TON_DIRECT_MINMAX_V1 === ادمین: حداکثرِ مبلغ
    search = State()    # === PATCH_TON_DIRECT_INVOICES_V1 === ادمین: جستجو با کد پیگیری
    track = State()     # === PATCH_TON_DIRECT_INVOICES_V1 === کاربر: پیگیریِ فاکتورِ خودش


# === PATCH_TON_DIRECT_FEE_V1 ===
# مالکِ ربات می‌تواند رویِ هر پرداخت درصدی کارمزد بگذارد. کاربر همان مبلغی
# که خواسته شارژ می‌شود، ولی معادلِ TON را کمی بیشتر می‌پردازد. چرا لازم است:
# کارمزدِ شبکهٔ TON، نوسانِ قیمت بینِ ساختِ فاکتور و واریز، و اختلافِ نرخِ
# تبدیل هنگامِ نقد کردن — بدونِ این، فروشنده سرِ هر تراکنش کمی ضرر می‌کند.
MAX_FEE_PERCENT = 30


async def fee_percent(bot_id: int) -> Decimal:
    from tenant import get_setting
    raw = await get_setting(bot_id, "ton_direct_fee_percent", "0")
    try:
        val = Decimal(str(raw or "0"))
    except Exception:
        return Decimal(0)
    if val < 0:
        return Decimal(0)
    return min(val, Decimal(MAX_FEE_PERCENT))


# === PATCH_TON_DIRECT_MINMAX_V1 ===
# مالکِ ربات می‌تواند حداقل/حداکثرِ مبلغِ شارژ (به تومان) را خودش تعیین کند —
# مثلاً برایِ جلوگیری از فاکتورهایِ خیلی کوچک (که کارمزدِ شبکه بی‌صرفه‌اش
# می‌کند) یا خیلی بزرگ (که ریسکِ نگه‌داریِ TON در ولتِ شخصی را بالا می‌برد).
async def min_toman_setting(bot_id: int) -> int:
    from tenant import get_setting
    try:
        return max(0, int(await get_setting(bot_id, "ton_direct_min_toman", "0") or "0"))
    except Exception:
        return 0


async def max_toman_setting(bot_id: int) -> int:
    from tenant import get_setting
    try:
        return max(0, int(await get_setting(bot_id, "ton_direct_max_toman", "0") or "0"))
    except Exception:
        return 0


# ═══════════════════════ نرخ ═══════════════════════

_rate_cache: dict[str, tuple[Decimal, float]] = {}
_RATE_TTL = 300   # ۵ دقیقه — نرخِ TON نوسانِ سریع دارد

# === PATCH_TON_DIRECT_MENU_NONBLOCKING_V1 ===
# باگِ گزارش‌شده: بازشدنِ منو ۴۰ ثانیه طول می‌کشید. علتش این بود که
# _menu() نرخِ زنده را می‌گرفت — دو درخواستِ شبکه‌ایِ پشتِ‌هم با تایم‌اوتِ
# ۲۰ ثانیه‌ایِ عمومی. روی سروری که دسترسیِ خارج کند یا مسدود است، این
# یعنی منو تا تایم‌اوتِ هر دو قفل می‌ماند.
# دو اصلاح: (۱) درخواست‌هایِ نرخ تایم‌اوتِ کوتاهِ خودشان را دارند،
# (۲) منو فقط از کش می‌خواند و هیچ‌وقت به شبکه دست نمی‌زند.
_RATE_TIMEOUT = 6


async def ton_usd_price(cached_only: bool = False) -> Decimal | None:
    """قیمتِ هر TON به دلار از tonapi (بدونِ نیاز به کلید).

    cached_only=True یعنی «اگر کش نداری، None بده» — برایِ جاهایی مثلِ
    رندرِ منو که نباید منتظرِ شبکه بمانند.
    """
    import time
    cached = _rate_cache.get("ton_usd")
    if cached and (time.time() - cached[1]) < _RATE_TTL:
        return cached[0]
    if cached_only or aiohttp is None:
        return None
    try:
        t = aiohttp.ClientTimeout(total=_RATE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(TONAPI_RATES) as r:
                data = await r.json(content_type=None)
        price = data["rates"]["TON"]["prices"]["USD"]
        val = Decimal(str(price))
        if val <= 0:
            return None
        _rate_cache["ton_usd"] = (val, time.time())
        return val
    except Exception:
        logger.warning("ton_direct: گرفتنِ قیمتِ TON از tonapi ناموفق بود", exc_info=True)
        return None


async def ton_toman_rate(bot_id: int, cached_only: bool = False) -> Decimal | None:
    """نرخِ هر TON به تومان.

    === PATCH_TON_DIRECT_RATE_FALLBACK_V1 ===
    گزارشِ ادمین: «نرخ لحظه‌ای در دسترس نیست». دو ایراد داشت:
      ۱) دلار→تومان از get_usd_to_toman_rate گرفته می‌شد که منابعِ ایرانی را
         پشتِ‌هم امتحان می‌کند؛ روی سرورِ فیلترشده همه‌شان می‌افتند. حالا از
         get_rate_smart استفاده می‌شود که اول والکس (آدرسِ خارجی) را می‌زند،
         سقفِ زمانیِ خودش را دارد و در نهایت به کشِ قدیمی سقوط می‌کند.
      ۲) اگر tonapi هم در دسترس نبود، هیچ راهِ جایگزینی نبود. حالا مالکِ ربات
         می‌تواند «نرخِ دستیِ TON» ثبت کند و قابلیت مستقل از اینترنتِ خارج
         کار کند.

    ترتیب: نرخِ زنده → نرخِ دستیِ مالک → None
    """
    import time
    from tenant import get_setting

    async def _manual() -> Decimal | None:
        raw = await get_setting(bot_id, "ton_direct_manual_price", "")
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
            brs_key = await get_setting(bot_id, "brsapi_key", "")
            usd_toman = Decimal(str(await asyncio.wait_for(
                get_rate_smart(brs_key), timeout=10)))
        except asyncio.TimeoutError:
            logger.warning("ton_direct: گرفتنِ نرخِ دلار→تومان تایم‌اوت شد")
            return await _manual()
        except Exception:
            logger.warning("ton_direct: نرخِ دلار→تومان به دست نیامد", exc_info=True)
            return await _manual()
        if usd_toman <= 0:
            # منبعِ دستیِ خودِ ربات (همان کلیدی که درگاهِ ارز دیجیتال هم دارد)
            try:
                manual_usd = Decimal(str(await get_setting(bot_id, "toman_per_usd", "0")))
            except Exception:
                manual_usd = Decimal(0)
            if manual_usd > 0:
                usd_toman = manual_usd
            else:
                return await _manual()
        _rate_cache["usd_toman"] = (usd_toman, time.time())
    return usd * usd_toman


# ═══════════════════════ کلاینتِ زنجیره ═══════════════════════

_FETCH_CACHE: dict = {}
_TC_DOWN_UNTIL = [0.0]   # PATCH_TONCENTER_BACKOFF_V1
_TC_FAILS = [0]
_TC_LAST_WARN = [-1e9]


async def fetch_incoming(address: str, limit: int = 40) -> list[dict]:
    """تراکنش‌هایِ اخیرِ یک آدرس از toncenter.

    خروجی: [{"hash":…, "value_nano":int, "comment":str, "source":str}, …]
    خطا → لیستِ خالی (هیچ‌وقت استثنا به حلقه نمی‌دهد).
    """
    if aiohttp is None:
        return []
    url = f"{TONCENTER_BASE}/getTransactions"
    params = {"address": address, "limit": str(limit), "archival": "false"}
    # === PATCH_TON_DIRECT_FETCH_FIX_V1 ===
    # گزارش (ایلیا): «با TON می‌زنن شارژ نمی‌شه». عللِ محتمل که قبلاً کاملاً بی‌صدا بودند:
    #  ۱) کلیدِ TONCENTER_API_KEY اینجا به‌کار نمی‌رفت؛ toncenter بدونِ کلید ۱ درخواست در ثانیه
    #     می‌دهد و با چند ربات/فاکتورِ هم‌زمان ۴۲۹ برمی‌گرداند.
    #  ۲) پاسخِ ناموفق (ok=false / ۴۲۹ / ۵xx) بدونِ هیچ لاگی «لیستِ خالی» حساب می‌شد.
    # حالا کلید ارسال می‌شود، ۴۲۹ یک‌بار با تأخیر دوباره امتحان می‌شود و هر خطا لاگ می‌شود.
    # کشِ کوتاه: چند کاربر که هم‌زمان «بررسی» می‌زنند یا چند فاکتورِ هم‌زمان، toncenter را نمی‌کوبند
    import time as _time
    _ck = (address, limit)
    _hit = _FETCH_CACHE.get(_ck)
    if _hit and _time.monotonic() - _hit[0] < 4:
        return list(_hit[1])
    # === PATCH_TONCENTER_BACKOFF_V1 ===
    # لاگِ سرور: ۱۴۶۴ هشدارِ «HTTP 500 LITE_SERVER_UNAVAILABLE» در ۲۴ ساعت. وقتی خودِ toncenter
    # از دسترس خارج است، حلقهٔ سریع هر چند ثانیه دوباره می‌کوبید و لاگ را پر می‌کرد. حالا بعد از
    # خطای ۵xx/ok=false چند ثانیه صبر می‌کنیم و هشدار حداکثر هر ۵ دقیقه یک‌بار ثبت می‌شود.
    if _time.monotonic() < _TC_DOWN_UNTIL[0]:
        return []
    try:
        from config import TONCENTER_API_KEY as _TCK
    except Exception:
        _TCK = ""
    headers = {"X-Api-Key": _TCK} if _TCK else {}
    data = None
    for _attempt in (1, 2):
        try:
            t = aiohttp.ClientTimeout(total=_TIMEOUT)
            async with aiohttp.ClientSession(timeout=t, headers=headers) as s:
                async with s.get(url, params=params) as r:
                    _status = r.status
                    data = await r.json(content_type=None)
        except Exception:
            logger.warning("ton_direct: toncenter پاسخ نداد (%s)", address, exc_info=True)
            return []
        if _status == 429 and _attempt == 1:
            await asyncio.sleep(1.6)
            continue
        break
    if not isinstance(data, dict) or not data.get("ok"):
        _TC_DOWN_UNTIL[0] = _time.monotonic() + (20 if (_status or 0) >= 500 else 4)
        _TC_FAILS[0] += 1
        if _time.monotonic() - _TC_LAST_WARN[0] > 300:
            logger.warning("ton_direct: toncenter پاسخِ ناموفق داد — HTTP %s | %s | (کلید %s) | %d خطا از آخرین گزارش",
                           _status, str(data)[:200], "دارد" if _TCK else "ندارد؛ TONCENTER_API_KEY را بگذار",
                           _TC_FAILS[0])
            _TC_LAST_WARN[0] = _time.monotonic()
            _TC_FAILS[0] = 0
        return []
    out: list[dict] = []
    for tx in data.get("result") or []:
        in_msg = tx.get("in_msg") or {}
        try:
            value = int(in_msg.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            continue   # تراکنشِ خروجی یا پیامِ داخلی، نه واریز
        out.append({
            "hash": ((tx.get("transaction_id") or {}).get("hash") or "")[:80],
            "value_nano": value,
            "comment": (in_msg.get("message") or "").strip(),
            "source": in_msg.get("source") or "",
        })
    _FETCH_CACHE[_ck] = (_time.monotonic(), list(out))
    return out


# ═══════════════════════ ساختِ فاکتور ═══════════════════════

def _new_memo() -> str:
    """کامنتِ کوتاه و یکتا — کوتاه است تا کاربر بتواند دستی هم تایپش کند."""
    return "BS" + secrets.token_hex(3).upper()   # مثلاً BS9F2A71


def _uniquify(ton: Decimal) -> Decimal:
    """سه رقمِ آخرِ اعشار را تصادفی می‌کند تا مبلغ عملاً یکتا شود.

    همیشه رو به بالا، تا مبلغِ دریافتی از چیزی که انتظار داریم کمتر نشود.
    """
    base = ton.quantize(Decimal("0.001"))
    return (base + Decimal(random.randint(1, 999)) / Decimal(1_000_000)).quantize(
        Decimal("0.000001"))


def payment_links(address: str, ton: Decimal, memo: str) -> tuple[str, str]:
    """(لینکِ ton://، لینکِ وبِ Tonkeeper) — هر دو مبلغ و کامنت را پر می‌کنند."""
    nano = int((ton * NANO).to_integral_value())
    q = f"amount={nano}&text={memo}"
    return (f"ton://transfer/{address}?{q}",
            f"https://app.tonkeeper.com/transfer/{address}?{q}")


async def wallet_address(bot_id: int) -> str:
    from tenant import get_setting
    return (await get_setting(bot_id, "ton_direct_wallet", "")).strip()


# === PATCH_TON_DIRECT_PRO_ONLY_V1 ===
# طبقِ خواستهٔ مدیر این قابلیت فقط پلنِ حرفه‌ای است. گیت هم در پنلِ مالک
# چک می‌شود و هم اینجا — یعنی حتی اگر رباتی قبلاً روشنش کرده باشد و بعد
# پلنش پایین بیاید، دکمه‌اش در کیفِ پولِ کاربر دیگر ظاهر نمی‌شود.
async def plan_allows(bot_id: int) -> bool:
    try:
        from plans import Feature, has_feature
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        return bool(brow and has_feature(brow.plan, Feature.TON_DIRECT))
    except Exception:
        logger.exception("ton_direct: بررسیِ پلن ناموفق بود")
        return False


async def is_available(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, "ton_direct_enabled", "0") == "1"
            and bool(await wallet_address(bot_id))
            and await plan_allows(bot_id))


# ═══════════════════ هستهٔ امن: تسویه و شارژ ═══════════════════

async def _credit(bot_id: int, charge_id: int, tx_hash: str) -> int:
    """شارژِ اتمیِ کیفِ پول برایِ یک فاکتور. خروجی: مبلغِ شارژشده (۰ یعنی نشد).

    تنها نقطه‌ای است که موجودی را زیاد می‌کند. با UPDATE شرطی قفل می‌شود تا
    اگر دو تراکنش هم‌زمان به یک فاکتور بخورند، فقط یکی شارژ شود.
    """
    async with Session() as s:
        claim = await s.execute(
            sa_update(TonDirectCharge)
            .where(TonDirectCharge.id == charge_id,
                   TonDirectCharge.status != "paid")
            .values(status="paid", tx_hash=tx_hash))
        if claim.rowcount == 0:
            await s.rollback()
            return 0
        ch = await s.get(TonDirectCharge, charge_id)
        credit = int(ch.amount_toman or 0)
        payer = int(ch.user_telegram_id)
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id,
            BotUser.telegram_id == payer).with_for_update())
        if u:
            u.balance = float(u.balance or 0) + credit
            s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=credit,
                            kind="ton_direct", note="شارژ با واریز مستقیم TON"))
        await s.commit()
    return credit


async def _match_charge(bot_id: int, tx: dict) -> int | None:
    """فاکتورِ متناظرِ یک تراکنش را پیدا می‌کند (اول memo، بعد مبلغ)."""
    comment = (tx.get("comment") or "").strip().upper()
    value = Decimal(tx["value_nano"]) / NANO

    async with Session() as s:
        if comment:
            # کامنت ممکن است چیزهایِ دیگری هم داشته باشد؛ memo داخلش می‌گردیم
            _late_from = _dt_late.datetime.utcnow() - _dt_late.timedelta(hours=LATE_GRACE_HOURS)
            rows = (await s.scalars(select(TonDirectCharge).where(
                TonDirectCharge.bot_id == bot_id, _ton_only(),
                (TonDirectCharge.status == "waiting") |
                ((TonDirectCharge.status == "expired") & (TonDirectCharge.expires_at >= _late_from))))).all()
            for ch in rows:
                if ch.memo and ch.memo.upper() in comment:
                    if value >= Decimal(str(ch.ton_amount)) * AMOUNT_TOLERANCE:
                        return ch.id
                    logger.warning(
                        "ton_direct: memo خورد ولی مبلغ کم بود (فاکتور %s: "
                        "انتظار %s، دریافت %s)", ch.id, ch.ton_amount, value)
                    return None

        # بدونِ کامنت (مثلاً برداشت از صرافی) → فقط اگر دقیقاً یک فاکتورِ بازِ
        # این بات با این مبلغِ یکتا (در بازهٔ خیلی باریک) وجود داشته باشد، تسویه شود.
        # اگر چند فاکتور هم‌زمان با این مبلغ جور دربیایند، به‌جایِ حدس‌زدنِ «جدیدترین»،
        # هیچ‌کدام خودکار شارژ نمی‌شود — چون حدس اشتباه یعنی شارژِ رایگانِ کاربرِ اشتباه.
        rows = (await s.scalars(select(TonDirectCharge).where(
            TonDirectCharge.bot_id == bot_id, _ton_only(),
            TonDirectCharge.status == "waiting"))).all()
        candidates = [
            ch for ch in rows
            if Decimal(str(ch.ton_amount)) > 0
            and abs(value - Decimal(str(ch.ton_amount))) <= FALLBACK_ABS_TOLERANCE
        ]
        if len(candidates) == 1:
            return candidates[0].id
        if len(candidates) > 1:
            logger.warning(
                "ton_direct: تراکنشِ بدونِ memo (bot_id=%s, مبلغ %s) با %d فاکتورِ بازِ "
                "هم‌مبلغ برخورد کرد؛ برایِ جلوگیری از شارژِ اشتباه هیچ‌کدام خودکار "
                "تسویه نشد. فاکتورها: %s", bot_id, value, len(candidates),
                [c.id for c in candidates])
    return None


async def settle_bot(bot_id: int, tbot: Bot | None = None) -> int:
    """یک دورِ بررسیِ زنجیره برایِ یک ربات. خروجی: تعدادِ فاکتورهایِ تسویه‌شده."""
    addr = await wallet_address(bot_id)
    if not addr:
        return 0
    async with Session() as s:
        _late_from = _dt_late.datetime.utcnow() - _dt_late.timedelta(hours=LATE_GRACE_HOURS)
        pending = await s.scalar(select(TonDirectCharge.id).where(
            TonDirectCharge.bot_id == bot_id, _ton_only(),
            (TonDirectCharge.status == "waiting") |
            ((TonDirectCharge.status == "expired") & (TonDirectCharge.expires_at >= _late_from))).limit(1))
    if not pending:
        return 0   # فاکتورِ بازی نیست؛ بی‌خود به زنجیره دست نمی‌زنیم

    settled = 0
    for tx in await fetch_incoming(addr):
        tx_hash = tx.get("hash") or ""
        if not tx_hash:
            continue
        async with Session() as s:
            seen = await s.scalar(select(TonDirectCharge.id).where(
                TonDirectCharge.tx_hash == tx_hash))
        if seen:
            continue   # این تراکنش قبلاً استفاده شده
        charge_id = await _match_charge(bot_id, tx)
        if not charge_id:
            continue
        async with Session() as s:
            ch = await s.get(TonDirectCharge, charge_id)
            payer, memo = int(ch.user_telegram_id), ch.memo
        credit = await _credit(bot_id, charge_id, tx_hash)
        if not credit:
            continue
        settled += 1
        logger.info("ton_direct: فاکتور %s (bot_id=%s) با tx %s تسویه شد.",
                    memo, bot_id, tx_hash[:16])
        if tbot is not None:
            # === PATCH_TON_DIRECT_AUTODELETE_INVOICE_V1 ===
            # پیامِ فاکتور (که مبلغ و آدرس رویش بود) بعد از تاییدِ پرداخت
            # دیگر لازم نیست در چت بماند؛ حذفش می‌کنیم و اگر حذف ممکن نبود
            # (مثلاً کاربر پیام را خودش پاک کرده یا بیش از ۴۸ ساعت گذشته)
            # بی‌سروصدا رد می‌شویم — مهم‌تر این است که کیفِ پول شارژ شده باشد.
            async with Session() as s:
                ch2 = await s.get(TonDirectCharge, charge_id)
                inv_chat, inv_msg = ch2.invoice_chat_id, ch2.invoice_message_id
            if inv_chat and inv_msg:
                try:
                    await tbot.delete_message(inv_chat, inv_msg)
                except Exception:
                    logger.info("ton_direct: حذفِ پیامِ فاکتور %s ممکن نشد (بی‌اهمیت)", memo)
            try:
                await tbot.send_message(
                    payer,
                    "✅ <b>پرداخت شما تایید شد</b>\n\n"
                    f"🧾 کد فاکتور: <code>{memo}</code>\n"
                    f"💰 کیف پول شما <b>{credit:,} تومان</b> شارژ شد.")
            except Exception:
                logger.exception("ton_direct: اطلاعِ شارژ به کاربر نرسید")
            # === PATCH_TON_DIRECT_OWNER_NOTIFY_V1 ===
            # اطلاعِ مالکِ ربات: کی پرداخت کرد، با چه مشخصاتی، و چی خرید.
            try:
                async with Session() as s:
                    brow = await s.get(BotRow, bot_id)
                    ch3 = await s.get(TonDirectCharge, charge_id)
                    bu = await s.scalar(select(BotUser).where(
                        BotUser.bot_id == bot_id, BotUser.telegram_id == payer))
                owner_id = brow.owner_id if brow else None
                if owner_id:
                    uname = f"@{bu.username}" if bu and bu.username else "—"
                    fname = (bu.full_name if bu else None) or "—"
                    from tenant.shared.tx_channel import notify_log_or_owner as _notify_log
                    await _notify_log(
                        tbot, bot_id, owner_id,
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
                logger.exception("ton_direct: اطلاعِ مالکِ ربات ارسال نشد")
    return settled


async def ton_direct_loop(interval: int = 60, fast_interval: int = 10):
    """زنجیره را برایِ ربات‌هایی که فاکتورِ باز دارند چک می‌کند: با فاکتورِ باز هر ~۱۰ ثانیه
    (=== PATCH_TON_DIRECT_FAST_POLL_V1 ===)، وگرنه هر دقیقه."""
    from tenant import get_setting
    await asyncio.sleep(35)
    while True:
        _sleep = interval
        try:
            now = _dt.datetime.utcnow()
            # فاکتورهایِ سررسیدگذشته بسته می‌شوند تا لیستِ تطبیق کوچک بماند
            async with Session() as s:
                await s.execute(
                    sa_update(TonDirectCharge)
                    .where(TonDirectCharge.status == "waiting",
                           TonDirectCharge.expires_at.isnot(None),
                           TonDirectCharge.expires_at < now)
                    .values(status="expired"))
                await s.commit()
                bot_ids = list((await s.scalars(select(TonDirectCharge.bot_id).where(
                    TonDirectCharge.status == "waiting",
                    _ton_only()).distinct())).all())
            if bot_ids:
                _sleep = fast_interval
            # === PATCH_TON_DIRECT_RATE_TIMEOUT_V1 ===
            # کشِ نرخ را همین‌جا گرم نگه می‌داریم تا مسیرِ کاربر (ساختِ
            # فاکتور) تقریباً همیشه به کش بخورد و منتظرِ شبکه نماند.
            if bot_ids:
                try:
                    await ton_toman_rate(bot_ids[0])
                except Exception:
                    logger.exception("ton_direct: پیش‌گرم‌کردنِ نرخ")
            for bot_id in bot_ids:
                try:
                    if await get_setting(bot_id, "ton_direct_enabled", "0") != "1":
                        continue
                    async with Session() as s:
                        brow = await s.get(BotRow, bot_id)
                        token = brow.token if (brow and brow.status == "active") else None
                    if not token:
                        continue
                    tbot = Bot(token=token, default=DEFAULTS)
                    try:
                        await settle_bot(bot_id, tbot)
                    finally:
                        try:
                            await tbot.session.close()
                        except Exception:
                            logger.exception("ton_direct: بستنِ سشنِ ربات")
                except Exception:
                    logger.exception("ton_direct: خطا در ربات %s", bot_id)
                # toncenter بدونِ کلید نرخ‌محدود است؛ بینِ ربات‌ها مکث می‌کنیم
                await asyncio.sleep(1.2)
        except Exception:
            logger.exception("ton_direct_loop error")
        await asyncio.sleep(_sleep)


# ═══════════════════════ پنلِ مالکِ ربات ═══════════════════════

async def _menu(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from tenant import get_setting
    addr = await wallet_address(bot_id)
    on = await get_setting(bot_id, "ton_direct_enabled", "0") == "1"
    in_wallet = await get_setting(bot_id, "ton_direct_show_wallet", "1") == "1"

    body = [
        "💎 <b>پرداخت مستقیم TON</b>", "",
        "کاربر مبلغ را مستقیم به <b>ولت خودت</b> واریز می‌کند و ربات پرداخت را "
        "از روی بلاک‌چین تشخیص می‌دهد. هیچ درگاه و واسطه‌ای در کار نیست، "
        "کارمزد درگاه هم ندارد.",
        "هر ولت TON قبول است (Tonkeeper، Tonhub، MyTonWallet، ولت تلگرام و…) — "
        "فقط آدرس واریزِ <b>صرافی</b> نگذار.", "",
        (f"👛 ولت مقصد: <code>{addr}</code>" if addr else "👛 ولت مقصد: <i>ثبت نشده</i>"),
        f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}",
    ]
    fee = await fee_percent(bot_id)
    body.append(f"➕ کارمزد پرداخت: <b>{fee:g}٪</b>" if fee > 0
                else "➕ کارمزد پرداخت: <i>ندارد</i>")
    # فقط از کش — منو هیچ‌وقت منتظرِ شبکه نمی‌ماند
    rate = await ton_toman_rate(bot_id, cached_only=True)
    if rate:
        body.append(f"📈 نرخ لحظه‌ای: هر TON حدود <b>{int(rate):,} تومان</b>")
    else:
        body.append("📈 نرخ لحظه‌ای: <i>برای دیدنش دکمهٔ «بررسی نرخ» را بزن</i>")
    manual = await get_setting(bot_id, "ton_direct_manual_price", "")
    if manual:
        body.append(f"💵 نرخ دستی: هر TON <b>{int(float(manual)):,} تومان</b> "
                    "(وقتی نرخ زنده در دسترس نباشد)")
    # === PATCH_TON_DIRECT_MINMAX_V1 ===
    min_t = await min_toman_setting(bot_id)
    max_t = await max_toman_setting(bot_id)
    body.append(f"💵 حداقل شارژ: <b>{min_t:,} تومان</b>" if min_t > 0
                else "💵 حداقل شارژ: <i>پیش‌فرض (فقط طبقِ کارمزدِ شبکه)</i>")
    body.append(f"💵 حداکثر شارژ: <b>{max_t:,} تومان</b>" if max_t > 0
                else "💵 حداکثر شارژ: <i>نامحدود</i>")
    body += [
        "",
        "⚠️ مسئولیت نگهداری کلید/عبارت بازیابی ولت کاملاً با خودت است. "
        "بات‌ساز به ولت تو دسترسی ندارد و نمی‌تواند تراکنشی را برگرداند.",
        f"⏳ هر فاکتور <b>{INVOICE_TTL_MINUTES} دقیقه</b> اعتبار دارد "
        "(چون قیمت TON نوسان دارد).",
    ]

    rows = [[InlineKeyboardButton(
        text=("✏️ تغییر آدرس ولت" if addr else "👛 ثبت آدرس ولت TON"),
        callback_data="tond:setwallet", style=ButtonStyle.PRIMARY)]]
    if addr:
        rows.append([InlineKeyboardButton(
            text=("🔴 غیرفعال کن" if on else "🟢 فعال کن"),
            callback_data="tond:toggle",
            style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))])
    rows.append([InlineKeyboardButton(
        text=f"➕ کارمزد پرداخت ({fee:g}٪)",
        callback_data="tond:setfee", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text="📈 بررسی نرخ لحظه‌ای", callback_data="tond:rate",
        style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text=("✏️ تغییر نرخ دستی TON" if manual else "💵 ثبت نرخ دستی TON"),
        callback_data="tond:setrate", style=ButtonStyle.PRIMARY)])
    # === PATCH_TON_DIRECT_MINMAX_V1 ===
    rows.append([
        InlineKeyboardButton(text="💵 حداقل مبلغ شارژ", callback_data="tond:setmin",
                             style=ButtonStyle.PRIMARY),
        InlineKeyboardButton(text="💵 حداکثر مبلغ شارژ", callback_data="tond:setmax",
                             style=ButtonStyle.PRIMARY),
    ])
    # === PATCH_TON_DIRECT_INVOICES_V1 ===
    rows.append([InlineKeyboardButton(
        text="🧾 وضعیت فاکتورها", callback_data="tond:invoices",
        style=ButtonStyle.PRIMARY)])
    if on:
        rows.append([InlineKeyboardButton(
            text="👛 نمایش در کیف پول: " + ("✅ فعال" if in_wallet else "❌ غیرفعال"),
            callback_data="tond:vis", style=ButtonStyle.PRIMARY)])
    # === PATCH_COIN_DIRECT_MULTI_ASSET_V1 === یک پله بالاتر حالا هابِ ارزهاست
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cdir:hub",
                                      style=ButtonStyle.DANGER)])
    return "\n".join(body), InlineKeyboardMarkup(inline_keyboard=rows)


async def set_ton_direct_start(cb: CallbackQuery, bot: Bot, state: FSMContext | None = None):
    """ورودیِ منو — هم از دکمهٔ شیشه‌ای و هم از کیبوردِ پایین صدا زده می‌شود."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    # === PATCH_TON_DIRECT_PRO_ONLY_V1 ===
    if not await plan_allows(row.id):
        await cb.answer()
        await cb.message.answer(
            "🔒 <b>پرداخت مستقیم TON — ویژهٔ پلن حرفه‌ای</b>\n\n"
            "با این قابلیت، کاربرانت مستقیم به ولت TON خودت (Tonkeeper یا هر "
            "ولت دیگر) واریز می‌کنند و ربات پرداخت را از روی بلاک‌چین تشخیص "
            "می‌دهد — بدون درگاه، بدون واسطه و بدون کارمزد درگاه.\n\n"
            "برای فعال‌سازی، پلن ربات را به <b>حرفه‌ای</b> ارتقا بده.")
        return
    text, kb = await _menu(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@ton_direct_router.callback_query(F.data.in_({"set:ton_direct", "tond:menu"}))
async def tond_menu(cb: CallbackQuery, bot: Bot):
    await set_ton_direct_start(cb, bot)


# === PATCH_TON_DIRECT_MENU_NONBLOCKING_V1 ===
@ton_direct_router.callback_query(F.data == "tond:rate")
async def tond_rate(cb: CallbackQuery, bot: Bot):
    """گرفتنِ نرخِ زنده — عمداً جدا از منو، چون به شبکه وصل می‌شود."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await cb.answer("⏳ در حال گرفتن نرخ…")
    rate = await ton_toman_rate(row.id)
    if not rate:
        await cb.message.answer(
            "❌ نرخ لحظه‌ای در دسترس نیست.\n\n"
            "سرور به <code>tonapi.io</code> یا سرویس نرخ دلار دسترسی ندارد.\n\n"
            "💵 راه‌حل: با دکمهٔ <b>ثبت نرخ دستی TON</b> خودت قیمت هر TON را "
            "به تومان وارد کن. تا وقتی نرخ زنده برنگردد، ربات از همان استفاده "
            "می‌کند و کاربران بدون مشکل فاکتور می‌سازند.\n\n"
            "⚠️ یادت باشد نرخ دستی را هر چند وقت به‌روز کنی، وگرنه با نوسان "
            "قیمت TON ضرر می‌کنی.")
        return
    text, kb = await _menu(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@ton_direct_router.callback_query(F.data == "tond:setwallet")
async def tond_setwallet(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(TonDirectFSM.wallet)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "👛 <b>آدرس ولت TON</b>\n\n"
        "آدرس ولتی که می‌خواهی پول‌ها به آن برود را بفرست. هر ولت TON قبول "
        "است — Tonkeeper، Tonhub، MyTonWallet، ولت داخل تلگرام یا هر ولت "
        "دیگر. از بخش <b>Receive</b> کپی‌اش کن.\n\n"
        "معمولاً با <code>UQ</code> یا <code>EQ</code> شروع می‌شود.\n\n"
        "🚫 <b>آدرس واریز صرافی نگذار.</b> آدرس دپوزیت صرافی‌ها (نوبیتکس، "
        "بیت‌پین، OKX و…) بین هزاران کاربر مشترک است و صرافی برای تشخیص "
        "صاحب واریز، memo مخصوص خودش را می‌خواهد؛ ولی ربات memo خودش را "
        "می‌گذارد. نتیجه‌اش این است که پول به حسابت ننشیند و برگرداندنش هم "
        "دردسر بزرگی باشد. حتماً ولت شخصی خودت را بده.\n\n"
        "⚠️ آدرس را با دقت بررسی کن؛ اگر اشتباه باشد پول‌ها به ولتِ شخصِ "
        "دیگری می‌رود و هیچ راهی برای برگرداندنش نیست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@ton_direct_router.message(TonDirectFSM.wallet, F.text)
async def tond_setwallet_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, set_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _menu(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    if not _ADDR_RE.match(raw):
        await msg.answer(
            "❌ این آدرس معتبر به نظر نمی‌رسد.\n"
            "آدرس TON معمولاً ۴۸ کاراکتر است و با <code>UQ</code> یا "
            "<code>EQ</code> شروع می‌شود. دوباره بفرست:",
            reply_markup=await smart_cancel_kb(bot))
        return
    # === PATCH_TON_DIRECT_WALLET_CONFIRM_V1 ===
    # آدرسِ دپوزیتِ صرافی از نظرِ فنی با ولتِ شخصی هیچ فرقی ندارد، پس کد
    # نمی‌تواند تشخیصش بدهد. تنها دفاع، یک تاییدِ آگاهانهٔ خودِ مالک است:
    # آدرس تا وقتی این دکمه زده نشود ذخیره نمی‌شود.
    # آدرس در state نگه داشته می‌شود، نه در callback_data: آدرسِ خامِ
    # «0:hex» ۶۶ کاراکتر است و از سقفِ ۶۴ بایتیِ callback_data رد می‌شود.
    await state.update_data(pending_wallet=raw)
    await msg.answer(
        "🔎 <b>یک بار دیگر آدرس را چک کن</b>\n\n"
        f"<code>{raw}</code>\n\n"
        "کاراکترهای ابتدا و انتهای آن را با چیزی که در ولتت می‌بینی مقایسه کن.\n\n"
        "<blockquote>با زدن دکمهٔ تایید، تصدیق می‌کنی که این آدرس <b>ولت شخصی "
        "خودت</b> است و آدرس واریزِ صرافی نیست.\n\n"
        "پول‌های کاربران مستقیم به همین آدرس می‌رود. اگر اشتباه یا آدرس صرافی "
        "باشد، بات‌ساز هیچ راهی برای بازگرداندن آن ندارد.</blockquote>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ تایید می‌کنم؛ ولت شخصی خودم است",
                callback_data="tond:wok", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="tond:wcancel",
                                  style=ButtonStyle.DANGER)],
        ]))


@ton_direct_router.callback_query(F.data == "tond:wcancel")
async def tond_wallet_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await tond_menu(cb, bot)


@ton_direct_router.callback_query(F.data == "tond:wok")
async def tond_wallet_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """ذخیرهٔ آدرس فقط بعد از تاییدِ صریحِ مالک."""
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    addr = (await state.get_data()).get("pending_wallet") or ""
    if not _ADDR_RE.match(addr):
        await cb.answer("آدرس پیدا نشد؛ دوباره ثبتش کن.", show_alert=True)
        return
    await state.clear()
    await set_setting(row.id, "ton_direct_wallet", addr)
    await cb.answer("✅ ذخیره شد")
    text, kb = await _menu(row.id)
    try:
        await cb.message.edit_text("✅ آدرس ولت ذخیره شد.\n\n" + text, reply_markup=kb)
    except Exception:
        await cb.message.answer("✅ آدرس ولت ذخیره شد.\n\n" + text, reply_markup=kb)


# === PATCH_TON_DIRECT_RATE_FALLBACK_V1 ===
@ton_direct_router.callback_query(F.data == "tond:setrate")
async def tond_setrate(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, get_setting, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "ton_direct_manual_price", "")
    live = await ton_toman_rate(row.id, cached_only=True)
    await state.set_state(TonDirectFSM.manual_rate)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "💵 <b>نرخ دستی TON</b>\n\n"
        "قیمت هر <b>۱ TON</b> را به <b>تومان</b> بفرست (مثلاً <code>180000</code>).\n\n"
        "این نرخ فقط زمانی استفاده می‌شود که نرخ زنده در دسترس نباشد — "
        "مثلاً وقتی سرور به سرویس‌های قیمت دسترسی ندارد. تا وقتی نرخ زنده "
        "کار کند، همان اولویت دارد.\n\n"
        + (f"نرخ دستی فعلی: <b>{int(float(cur)):,} تومان</b>\n" if cur else "")
        + (f"نرخ زندهٔ فعلی: <b>{int(live):,} تومان</b>\n" if live else "")
        + "\n⚠️ اگر عدد را خیلی پایین بزنی، عملاً TON را ارزان می‌فروشی. "
        "قیمت روز را از یک صرافی چک کن.\n"
        "برای حذف نرخ دستی <code>0</code> بفرست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@ton_direct_router.message(TonDirectFSM.manual_rate, F.text)
async def tond_setrate_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, set_setting, fa_to_int, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _menu(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    val = fa_to_int(raw)
    if val is None or val < 0:
        await msg.answer("❌ عدد نامعتبر است. قیمت هر TON را به تومان بفرست:",
                         reply_markup=await smart_cancel_kb(bot))
        return
    if val == 0:
        await set_setting(row.id, "ton_direct_manual_price", "")
        await state.clear()
        text, kb = await _menu(row.id)
        await msg.answer("🗑 نرخ دستی حذف شد.\n\n" + text, reply_markup=kb)
        return
    if val < 1000:
        await msg.answer(
            "❌ این عدد خیلی کم است. قیمت هر TON به <b>تومان</b> است، "
            "نه به دلار. دوباره بفرست:", reply_markup=await smart_cancel_kb(bot))
        return
    await set_setting(row.id, "ton_direct_manual_price", str(val))
    await state.clear()
    text, kb = await _menu(row.id)
    await msg.answer(f"✅ نرخ دستی روی <b>{val:,} تومان</b> برای هر TON تنظیم شد.\n\n"
                     + text, reply_markup=kb)


# === PATCH_TON_DIRECT_FEE_V1 ===
@ton_direct_router.callback_query(F.data == "tond:setfee")
async def tond_setfee(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await fee_percent(row.id)
    await state.set_state(TonDirectFSM.fee)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "➕ <b>کارمزد پرداخت TON</b>\n\n"
        "درصدی که می‌خواهی روی هر پرداخت اضافه شود را بفرست (مثلاً <code>3</code>).\n\n"
        "کاربر همان مبلغی که خواسته شارژ می‌شود، ولی معادلِ TON را به‌اندازهٔ "
        "این درصد بیشتر می‌پردازد. برای جبرانِ کارمزد شبکه، نوسان قیمت TON "
        "و اختلاف نرخ هنگام نقد کردن.\n\n"
        f"کارمزد فعلی: <b>{cur:g}٪</b>\n"
        f"حداکثر مجاز: <b>{MAX_FEE_PERCENT}٪</b> — برای حذف کارمزد <code>0</code> بفرست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@ton_direct_router.message(TonDirectFSM.fee, F.text)
async def tond_setfee_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, set_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _menu(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    raw = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
                        ).replace("٪", "").replace("%", "").replace("/", ".").strip()
    try:
        val = Decimal(raw)
    except Exception:
        await msg.answer("❌ عدد نامعتبر است. یک عدد بفرست (مثلاً 3 یا 2.5):",
                         reply_markup=await smart_cancel_kb(bot))
        return
    if val < 0 or val > MAX_FEE_PERCENT:
        await msg.answer(
            f"❌ کارمزد باید بین <b>0</b> تا <b>{MAX_FEE_PERCENT}</b> درصد باشد:",
            reply_markup=await smart_cancel_kb(bot))
        return
    await set_setting(row.id, "ton_direct_fee_percent", str(val.normalize()))
    await state.clear()
    text, kb = await _menu(row.id)
    note = ("✅ کارمزد حذف شد." if val == 0 else f"✅ کارمزد روی <b>{val:g}٪</b> تنظیم شد.")
    await msg.answer(note + "\n\n" + text, reply_markup=kb)


# === PATCH_TON_DIRECT_MINMAX_V1 ===
@ton_direct_router.callback_query(F.data == "tond:setmin")
async def tond_setmin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await min_toman_setting(row.id)
    await state.set_state(TonDirectFSM.min_amount)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "💵 <b>حداقلِ مبلغِ شارژ</b>\n\n"
        "حداقل مبلغی که کاربر می‌تواند به تومان شارژ کند را بفرست.\n"
        + (f"حداقلِ فعلی: <b>{cur:,} تومان</b>\n" if cur > 0 else "") +
        "برای حذفِ محدودیت (فقط طبقِ کارمزدِ شبکه) <code>0</code> بفرست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@ton_direct_router.message(TonDirectFSM.min_amount, F.text)
async def tond_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, set_setting, fa_to_int, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _menu(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    val = fa_to_int(raw)
    if val is None or val < 0:
        await msg.answer("❌ عدد نامعتبر است. یک عدد به تومان بفرست:",
                         reply_markup=await smart_cancel_kb(bot))
        return
    max_t = await max_toman_setting(row.id)
    if val > 0 and max_t > 0 and val > max_t:
        await msg.answer(
            f"❌ حداقل نمی‌تواند از حداکثرِ فعلی (<b>{max_t:,} تومان</b>) بیشتر باشد:",
            reply_markup=await smart_cancel_kb(bot))
        return
    await set_setting(row.id, "ton_direct_min_toman", str(val))
    await state.clear()
    text, kb = await _menu(row.id)
    note = "✅ حداقل حذف شد." if val == 0 else f"✅ حداقلِ شارژ روی <b>{val:,} تومان</b> تنظیم شد."
    await msg.answer(note + "\n\n" + text, reply_markup=kb)


@ton_direct_router.callback_query(F.data == "tond:setmax")
async def tond_setmax(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await max_toman_setting(row.id)
    await state.set_state(TonDirectFSM.max_amount)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "💵 <b>حداکثرِ مبلغِ شارژ</b>\n\n"
        "حداکثر مبلغی که کاربر می‌تواند به تومان شارژ کند را بفرست.\n"
        + (f"حداکثرِ فعلی: <b>{cur:,} تومان</b>\n" if cur > 0 else "") +
        "برای حذفِ محدودیت (نامحدود) <code>0</code> بفرست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@ton_direct_router.message(TonDirectFSM.max_amount, F.text)
async def tond_setmax_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, set_setting, fa_to_int, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _menu(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    val = fa_to_int(raw)
    if val is None or val < 0:
        await msg.answer("❌ عدد نامعتبر است. یک عدد به تومان بفرست:",
                         reply_markup=await smart_cancel_kb(bot))
        return
    min_t = await min_toman_setting(row.id)
    if val > 0 and min_t > 0 and val < min_t:
        await msg.answer(
            f"❌ حداکثر نمی‌تواند از حداقلِ فعلی (<b>{min_t:,} تومان</b>) کمتر باشد:",
            reply_markup=await smart_cancel_kb(bot))
        return
    await set_setting(row.id, "ton_direct_max_toman", str(val))
    await state.clear()
    text, kb = await _menu(row.id)
    note = "✅ حداکثر حذف شد." if val == 0 else f"✅ حداکثرِ شارژ روی <b>{val:,} تومان</b> تنظیم شد."
    await msg.answer(note + "\n\n" + text, reply_markup=kb)


# === PATCH_TON_DIRECT_INVOICES_V1 ===
# پنلِ وضعیتِ فاکتورها برایِ مالکِ ربات: لیستِ آخرین فاکتورها + جستجو با کدِ
# پیگیری، با مشخصاتِ خریدار (نام/یوزرنیم/آیدی) از رویِ BotUser.
INVOICE_LIST_LIMIT = 8
_STATUS_LABEL = {"paid": "✅ پرداخت‌شده", "waiting": "⏳ در انتظار", "expired": "⌛️ منقضی‌شده"}


async def _invoice_row(ch: "TonDirectCharge", s) -> str:
    bu = await s.scalar(select(BotUser).where(
        BotUser.bot_id == ch.bot_id, BotUser.telegram_id == ch.user_telegram_id))
    who = (f"@{bu.username}" if bu and bu.username else
          (bu.full_name if bu and bu.full_name else str(ch.user_telegram_id)))
    icon = {"paid": "✅", "waiting": "⏳", "expired": "⌛️"}.get(ch.status, "❔")
    return f"{icon} <code>{ch.memo}</code> — {int(ch.amount_toman or 0):,} ت — {who}"


async def _invoice_detail_text(ch: "TonDirectCharge", s) -> str:
    bu = await s.scalar(select(BotUser).where(
        BotUser.bot_id == ch.bot_id, BotUser.telegram_id == ch.user_telegram_id))
    uname = f"@{bu.username}" if bu and bu.username else "—"
    fname = (bu.full_name if bu else None) or "—"
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


@ton_direct_router.callback_query(F.data == "tond:invoices")
async def tond_invoices(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    async with Session() as s:
        rows_ = (await s.scalars(select(TonDirectCharge).where(
            TonDirectCharge.bot_id == row.id, _ton_only()).order_by(
                TonDirectCharge.id.desc()).limit(INVOICE_LIST_LIMIT))).all()
        lines = [await _invoice_row(ch, s) for ch in rows_]
    text = ("🧾 <b>وضعیت فاکتورهای TON مستقیم</b>\n"
            f"(آخرین {INVOICE_LIST_LIMIT} فاکتور)\n\n"
            + ("\n".join(lines) if lines else "فاکتوری ثبت نشده.")
            + "\n\nبرای دیدنِ جزئیاتِ کاملِ هر فاکتور (مشخصاتِ خریدار)، کدِ "
            "پیگیری‌اش را با دکمهٔ زیر جستجو کن.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 جستجو با کد پیگیری", callback_data="tond:searchinv")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tond:menu",
                              style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@ton_direct_router.callback_query(F.data == "tond:searchinv")
async def tond_searchinv(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.set_state(TonDirectFSM.search)
    await cb.message.answer("🔍 کدِ پیگیریِ فاکتور را بفرست (مثلاً <code>BS9F2A71</code>):",
                            reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@ton_direct_router.message(TonDirectFSM.search, F.text)
async def tond_search_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _menu(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    memo = raw.upper()
    await state.clear()
    async with Session() as s:
        ch = await s.scalar(select(TonDirectCharge).where(
            TonDirectCharge.bot_id == row.id, TonDirectCharge.memo == memo))
        if not ch:
            await msg.answer("❌ فاکتوری با این کد پیدا نشد.")
            return
        text = await _invoice_detail_text(ch, s)
    await msg.answer(text)


@ton_direct_router.callback_query(F.data == "tond:toggle")
async def tond_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await wallet_address(row.id):
        await cb.answer("اول آدرس ولت را ثبت کن.", show_alert=True)
        return
    cur = await get_setting(row.id, "ton_direct_enabled", "0")
    await set_setting(row.id, "ton_direct_enabled", "0" if cur == "1" else "1")
    await tond_menu(cb, bot)


@ton_direct_router.callback_query(F.data == "tond:vis")
async def tond_vis(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    cur = await get_setting(row.id, "ton_direct_show_wallet", "1")
    await set_setting(row.id, "ton_direct_show_wallet", "0" if cur == "1" else "1")
    await tond_menu(cb, bot)


# ═══════════════════════ جریانِ کاربر ═══════════════════════

async def _charge_entry_text_kb(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """متن+کیبوردِ صفحهٔ ورودیِ TON مستقیم — هم برایِ ورودِ اول، هم برایِ بازگشت."""
    rate = await ton_toman_rate(bot_id, cached_only=True)
    fee = await fee_percent(bot_id)
    min_t = await min_toman_setting(bot_id)
    max_t = await max_toman_setting(bot_id)

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

    # === PATCH_COIN_DIRECT_MULTI_ASSET_V1 ===
    # اگر ارزِ مستقیمِ دیگری (ترون/تتر) هم فعال باشد، کاربر از صفحهٔ انتخابِ
    # ارز به اینجا رسیده؛ «بازگشت» باید یک پله بالا برود نه تا کیفِ پول.
    back_cb = "w:back"
    try:
        from tenant.gateways.tron_direct import visible_assets as _vis
        if len(await _vis(bot_id)) > 1:
            back_cb = "w:cdirect"
    except Exception:
        logger.exception("ton_direct: تعیینِ مقصدِ دکمهٔ بازگشت")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 پرداخت", callback_data="tond:pay")],
        [InlineKeyboardButton(text="🔍 پیگیری فاکتورِ TON با کد", callback_data="tond:track")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb)],
    ])
    return "\n".join(lines), kb


async def wallet_ton_direct_text(msg: Message, bot: Bot, state: FSMContext):
    """شروعِ شارژ — از هر دو حالتِ منو (شیشه‌ای و کیبوردی) صدا زده می‌شود."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or not await is_available(row.id):
        await msg.answer("این روش پرداخت در حال حاضر در دسترس نیست.")
        return
    await state.clear()
    text, kb = await _charge_entry_text_kb(row.id)
    await msg.answer(text, reply_markup=kb)


@ton_direct_router.callback_query(F.data == "w:ton_direct")
async def w_ton_direct(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or not await is_available(row.id):
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    await state.clear()
    text, kb = await _charge_entry_text_kb(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@ton_direct_router.callback_query(F.data == "tond:back_charge")
async def tond_back_charge(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """بازگشت از «پرداخت»/«پیگیری» به صفحهٔ ورودیِ TON مستقیم — همون پیام edit می‌شود."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    await state.clear()
    text, kb = await _charge_entry_text_kb(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@ton_direct_router.callback_query(F.data == "tond:pay")
async def w_ton_direct_pay(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row or not await is_available(row.id):
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    rate = await ton_toman_rate(row.id)
    if not rate:
        await cb.answer("نرخ لحظه‌ای TON در دسترس نیست، کمی بعد امتحان کن.", show_alert=True)
        return
    fee = await fee_percent(row.id)
    floor_toman = int((MIN_TON * rate * Decimal(100) / (Decimal(100) + fee)
                       ).to_integral_value())
    min_t = await min_toman_setting(row.id)
    max_t = await max_toman_setting(row.id)
    effective_min = max(floor_toman, min_t) if min_t > 0 else floor_toman

    await state.set_state(TonDirectFSM.amount)
    limits = f"حداقل حدود <b>{effective_min:,} تومان</b>"
    if max_t > 0:
        limits += f" و حداکثر <b>{max_t:,} تومان</b>"
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        f"💳 <b>پرداخت</b>\n\nمبلغی که می‌خواهی شارژ کنی را به <b>تومان</b> "
        f"بفرست ({limits}):",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@ton_direct_router.callback_query(F.data == "tond:track")
async def w_ton_direct_track(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """پیگیریِ کاربرِ عادی — فقط فاکتورهایِ خودش را می‌بیند."""
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row or not await is_available(row.id):
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    await state.set_state(TonDirectFSM.track)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("ton_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "🔍 <b>پیگیریِ فاکتور TON</b>\n\n"
        "کدِ پیگیری‌ای که موقعِ ساختِ فاکتور به تو داده شد را بفرست:",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@ton_direct_router.message(TonDirectFSM.track, F.text)
async def w_ton_direct_track_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _charge_entry_text_kb(row.id)
        await msg.answer(text, reply_markup=kb)
        return
    memo = raw.upper()
    await state.clear()
    async with Session() as s:
        ch = await s.scalar(select(TonDirectCharge).where(
            TonDirectCharge.bot_id == row.id, TonDirectCharge.memo == memo,
            TonDirectCharge.user_telegram_id == msg.from_user.id))
    if not ch:
        await msg.answer("❌ فاکتوری با این کد برایِ حسابِ تو پیدا نشد.")
        return
    await msg.answer(
        "🧾 <b>وضعیتِ فاکتور</b>\n\n"
        f"کد پیگیری: <code>{ch.memo}</code>\n"
        f"وضعیت: <b>{_STATUS_LABEL.get(ch.status, ch.status)}</b>\n"
        f"💰 مبلغ: <b>{int(ch.amount_toman or 0):,} تومان</b>\n"
        f"💎 مبلغ TON: <code>{ch.ton_amount}</code>")


@ton_direct_router.message(TonDirectFSM.amount, F.text)
async def tond_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, fa_to_int, inline_back_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await inline_back_kb(row.id))
        return
    amount = fa_to_int(raw)
    if amount is None or amount <= 0:
        await msg.answer("مبلغ نامعتبر است. یک عدد به تومان بفرست:")
        return

    # === PATCH_TON_DIRECT_MINMAX_V1 ===
    min_t = await min_toman_setting(row.id)
    max_t = await max_toman_setting(row.id)
    if min_t > 0 and amount < min_t:
        await msg.answer(f"❌ حداقل مبلغِ شارژ <b>{min_t:,} تومان</b> است. عددِ بزرگ‌تری بفرست:")
        return
    if max_t > 0 and amount > max_t:
        await msg.answer(f"❌ حداکثر مبلغِ شارژ <b>{max_t:,} تومان</b> است. عددِ کوچک‌تری بفرست:")
        return

    rate = await ton_toman_rate(row.id)
    if not rate:
        await state.clear()
        await msg.answer("❌ نرخ لحظه‌ای در دسترس نیست. کمی بعد دوباره امتحان کن.")
        return
    # === PATCH_TON_DIRECT_FEE_V1 ===
    # کارمزد رویِ مبلغِ پرداختی سوار می‌شود، نه رویِ شارژ: کاربر همان
    # amount تومان شارژ می‌شود ولی معادلِ TONِ amount+کارمزد را می‌پردازد.
    fee = await fee_percent(row.id)
    payable_toman = (Decimal(amount) * (Decimal(100) + fee) / Decimal(100))
    ton = payable_toman / rate
    if ton < MIN_TON:
        await msg.answer(
            f"❌ مبلغ خیلی کم است (حدود {ton:.4f} TON می‌شود).\n"
            f"حداقل <b>{MIN_TON} TON</b> لازم است، چون کارمزد شبکه "
            "مبلغ‌های کوچک را بی‌صرفه می‌کند. مبلغ بیشتری بفرست:")
        return

    addr = await wallet_address(row.id)
    ton_final = _uniquify(ton)
    memo = _new_memo()
    now = _dt.datetime.utcnow()
    async with Session() as s:
        s.add(TonDirectCharge(
            bot_id=row.id, user_telegram_id=msg.from_user.id, memo=memo,
            asset="TON",   # === PATCH_COIN_DIRECT_MULTI_ASSET_V1 ===
            amount_toman=int(amount), ton_amount=float(ton_final),
            rate_toman=int(rate), wallet_address=addr, status="waiting",
            expires_at=now + _dt.timedelta(minutes=INVOICE_TTL_MINUTES)))
        await s.commit()
    await state.clear()

    deep, web = payment_links(addr, ton_final, memo)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 پرداخت با Tonkeeper", url=web)],
        # === PATCH_TON_DIRECT_COPY_MEMO_V1 ===
        # قبلاً کاربر باید رویِ <code>{memo}</code> لمسِ طولانی می‌کرد تا
        # کپی شود — خیلی‌ها این حرکت را نمی‌شناسند، مخصوصاً کسی که دارد
        # هم‌زمان بینِ ربات و اپِ کیف‌پول جابه‌جا می‌شود. با یک تپِ ساده روی
        # این دکمه، کدِ فاکتور مستقیم در کلیپ‌بورد کپی می‌شود.
        [InlineKeyboardButton(text=f"📋 کپی کدِ Comment ({memo})",
                              copy_text=CopyTextButton(text=memo))],
        [InlineKeyboardButton(text="🔄 بررسی وضعیت پرداخت",
                              callback_data=f"tond:check:{memo}",
                              style=ButtonStyle.PRIMARY)],
    ])
    sent = await msg.answer(
        "💎 <b>فاکتور پرداخت TON</b>\n\n"
        # === PATCH_TON_DIRECT_MEMO_LABEL_CLARITY_V1 ===
        # گزارشِ ادمین: برچسبِ «کد فاکتور» توی همین خطِ اول باعث می‌شد کاربر
        # فکر کنه این فقط یه شمارهٔ پیگیریِ داخلیه، نه چیزی که باید تویِ
        # قسمتِ Comment/Memoِ کیف‌پول بنویسه — با اینکه پایین‌تر تو
        # توضیحات هم اومده بود، خیلی‌ها همون خطِ اول رو می‌خوندن و رد
        # می‌شدن. حالا از همون خطِ اول مشخصه که این کد باید کجا نوشته بشه.
        f"🧾 کدِ Comment (حتماً موقعِ واریز بفرست): <code>{memo}</code>\n"
        f"💰 مبلغ شارژ: <b>{amount:,} تومان</b>\n"
        + (f"➕ کارمزد پرداخت ({fee:g}٪): <b>{int(payable_toman) - amount:,} تومان</b>\n"
           f"🧮 مجموع قابل پرداخت: <b>{int(payable_toman):,} تومان</b>\n" if fee > 0 else "")
        + f"💎 مبلغ دقیق: <code>{ton_final}</code> TON\n"
        f"👛 آدرس مقصد:\n<code>{addr}</code>\n\n"
        "<blockquote>روی دکمهٔ زیر بزن تا کیف پولت با مبلغ و کد آماده باز شود.\n"
        "اگر دستی واریز می‌کنی، حتماً <b>همین مبلغ دقیق</b> را بفرست و کد "
        f"<code>{memo}</code> را در قسمت <b>Comment/Memo</b> بنویس — با دکمهٔ "
        "«📋 کپی کدِ Comment» زیرِ همین پیام می‌توانی کدش را کپی کنی.</blockquote>\n\n"
        f"⏳ این فاکتور تا <b>{INVOICE_TTL_MINUTES} دقیقه</b> اعتبار دارد.\n"
        "بعد از واریز، تایید معمولاً کمتر از یک دقیقه طول می‌کشد و خودم خبرت می‌کنم.\n\n"
        f"🔗 لینک مستقیم: <code>{deep}</code>",
        reply_markup=kb)

    # === PATCH_TON_DIRECT_AUTODELETE_INVOICE_V1 ===
    async with Session() as s:
        await s.execute(
            sa_update(TonDirectCharge)
            .where(TonDirectCharge.memo == memo)
            .values(invoice_chat_id=sent.chat.id, invoice_message_id=sent.message_id))
        await s.commit()


@ton_direct_router.callback_query(F.data.startswith("tond:check:"))
async def tond_check(cb: CallbackQuery, bot: Bot):
    """دکمهٔ «بررسی وضعیت» — همان مسیرِ امنِ حلقه را دستی اجرا می‌کند."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    memo = cb.data.split(":", 2)[2]
    async with Session() as s:
        ch = await s.scalar(select(TonDirectCharge).where(
            TonDirectCharge.bot_id == row.id, TonDirectCharge.memo == memo))
        status = ch.status if ch else None
    if status == "paid":
        await cb.answer("✅ این فاکتور قبلاً تایید و شارژ شده است.", show_alert=True)
        return
    if status is None:
        await cb.answer("فاکتور پیدا نشد.", show_alert=True)
        return
    await cb.answer("⏳ در حال بررسی زنجیره…")
    await settle_bot(row.id, bot)
    async with Session() as s:
        ch = await s.scalar(select(TonDirectCharge).where(
            TonDirectCharge.bot_id == row.id, TonDirectCharge.memo == memo))
    if ch and ch.status == "paid":
        return   # پیامِ تاییدِ شارژ را خودِ settle_bot فرستاده است
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
