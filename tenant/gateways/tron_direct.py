"""
tron_direct.py — پرداختِ مستقیمِ TRX و USDT (TRC20) به ولتِ خودِ مالکِ ربات.

=== PATCH_COIN_DIRECT_MULTI_ASSET_V1 ===
این ماژول دقیقاً همان کاری را می‌کند که `tenant/ton_direct.py` برایِ TON
می‌کند، فقط رویِ شبکهٔ TRON:

  ۱) مالکِ ربات آدرسِ TRON خودش را ثبت می‌کند (Tonkeeper، TronLink، Trust،
     OKX Wallet یا هر ولتِ دیگر). برایِ TRX و USDT دو اسلاتِ جداگانه هست تا
     بتواند پول‌ها را در دو ولتِ مختلف بگیرد؛ اگر بخواهد می‌تواند هر دو را
     یک آدرس بزند (یک آدرسِ TRON هم TRX می‌گیرد هم TRC20).
  ۲) کاربر مبلغ را به تومان می‌دهد؛ ربات با نرخِ زنده آن را به TRX/USDT
     تبدیل می‌کند و فاکتوری با «مبلغِ یکتا» می‌سازد.
  ۳) حلقهٔ پس‌زمینه تراکنش‌هایِ ورودیِ همان آدرس را از TronGrid می‌خواند و
     هر تراکنشی که با فاکتور بخورد، کیفِ پول را شارژ می‌کند.

تفاوتِ مهم با TON — و دلیلِ اینکه این فایل کپیِ صرفِ ton_direct نیست:

  • انتقالِ **TRC20** (تتر) اصلاً فیلدِ memo/comment ندارد. پس برایِ تتر
    تنها راهِ تشخیص، «مبلغِ یکتا» است. به همین خاطر یکتاکننده اینجا چهار
    رقمِ اعشار را تصادفی می‌کند (نه سه رقم مثلِ TON) و پنجرهٔ تطبیق هم
    باریک‌تر است.
  • انتقالِ **TRX** فیلدِ `data` دارد که بعضی ولت‌ها به‌عنوانِ Note نشان
    می‌دهند؛ اگر پر شده باشد به‌عنوانِ memo استفاده می‌شود، ولی رویش
    حساب باز نمی‌کنیم.
  • TRON لینکِ پرداختِ استانداردِ فراگیر (مثلِ `ton://`) ندارد. به‌جایش
    دکمه‌هایِ «کپیِ آدرس» و «کپیِ مبلغِ دقیق» می‌گذاریم که در همهٔ ولت‌ها
    و صرافی‌ها کار می‌کند.

فاکتورها در همان جدولِ `TonDirectCharge` ذخیره می‌شوند و با ستونِ `asset`
از فاکتورهایِ TON جدا می‌شوند (`asset` خالی = TON، برایِ ردیف‌هایِ قدیمی).
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
from sqlalchemy import or_, select, update as sa_update

from config import Session
from models import Bot as BotRow, BotUser, TonDirectCharge, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

tron_direct_router = Router(name="tron_direct")

DEFAULTS = DefaultBotProperties(parse_mode=ParseMode.HTML)

TRONGRID_BASE = "https://api.trongrid.io"
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"   # قراردادِ رسمیِ Tether رویِ TRON
_TIMEOUT = 20
_RATE_TIMEOUT = 6

SUN = 1_000_000                 # هم TRX و هم USDT رویِ TRON شش رقمِ اعشار دارند
INVOICE_TTL_MINUTES = 20        # مهلتِ فاکتور — نرخ نوسان دارد

# ═══════════════════ مشخصاتِ هر ارز ═══════════════════

ASSETS = ("TRX", "USDT")

ASSET_LABEL = {"TRX": "ترون (TRX)", "USDT": "تتر (USDT · TRC20)"}
ASSET_SHORT = {"TRX": "ترون", "USDT": "تتر"}
ASSET_EMOJI = {"TRX": "🔺", "USDT": "💵"}

# حداقلِ منطقیِ هر واریز — زیرِ این مقدار کارمزدِ شبکه بی‌معنی‌اش می‌کند.
# انتقالِ TRC20 برایِ فرستنده انرژی/TRX می‌سوزاند، پس کفِ تتر بالاتر است.
# === PATCH_TRX_MIN_UNIT_LOWER_V1 ===
# قبلاً کفِ TRX عددِ ۵ بود (خیلی محتاطانه‌تر از چیزی که واقعاً لازمه —
# کارمزدِ یک تراکنشِ سادهٔ TRX رویِ شبکه معمولاً خیلی کمتر از ۱ TRXه).
# گزارشِ چندتا ادمین: مشتری‌ها می‌خوان بتونن با ۱ یا ۲ TRX هم شارژ کنن.
# کفِ فنی رو به ۱ کاهش دادیم؛ ادمینی که خودش می‌خواد سخت‌گیرانه‌تر باشه
# می‌تونه از همون تنظیمِ «حداقل مبلغ به تومان» (min_toman_setting، از
# قبل تو پنلِ مدیریتِ هر ربات موجوده) یه کفِ بالاتر هم روش بذاره.
MIN_UNIT = {"TRX": Decimal("1"), "USDT": Decimal("1")}

# ارفاقِ کارمزد — فقط وقتی memo هم خورده باشد (عملاً فقط TRX)
AMOUNT_TOLERANCE = Decimal("0.98")

# === پنجرهٔ تطبیقِ «بدونِ memo» ===
# مثلِ ton_direct مطلق است نه درصدی. چون برایِ تتر تنها راهِ تشخیص است،
# عمداً باریک‌تر از TON گرفته شده و در عوض یکتاکننده چهار رقمِ اعشار را
# تصادفی می‌کند (۱۰٬۰۰۰ حالت) تا برخوردِ تصادفی عملاً ناممکن شود.
FALLBACK_ABS_TOLERANCE = Decimal("0.00005")

# آدرسِ TRON (base58): با T شروع می‌شود و ۳۴ کاراکتر است.
_TRON_ADDR_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")

MAX_FEE_PERCENT = 30


class TronDirectFSM(StatesGroup):
    wallet = State()        # ادمین: آدرسِ ولتِ TRON
    fee = State()           # ادمین: درصدِ کارمزد
    manual_rate = State()   # ادمین: نرخِ دستیِ هر واحد به تومان
    min_amount = State()    # ادمین: حداقلِ مبلغ
    max_amount = State()    # ادمین: حداکثرِ مبلغ
    search = State()        # ادمین: جستجویِ فاکتور با کد
    amount = State()        # کاربر: مبلغِ شارژ به تومان
    track = State()         # کاربر: پیگیریِ فاکتورِ خودش


# ═══════════════════ تنظیمات ═══════════════════

def _k(asset: str, suffix: str) -> str:
    """کلیدِ تنظیماتِ هر ارز — مثلاً trx_direct_wallet / usdt_direct_fee_percent."""
    return f"{asset.lower()}_direct_{suffix}"


def _norm_asset(raw: str | None) -> str:
    a = (raw or "").strip().upper()
    return a if a in ASSETS else "TRX"


async def wallet_address(bot_id: int, asset: str) -> str:
    from tenant import get_setting
    return (await get_setting(bot_id, _k(asset, "wallet"), "")).strip()


async def fee_percent(bot_id: int, asset: str) -> Decimal:
    from tenant import get_setting
    raw = await get_setting(bot_id, _k(asset, "fee_percent"), "0")
    try:
        val = Decimal(str(raw or "0"))
    except Exception:
        return Decimal(0)
    if val < 0:
        return Decimal(0)
    return min(val, Decimal(MAX_FEE_PERCENT))


async def min_toman_setting(bot_id: int, asset: str) -> int:
    from tenant import get_setting
    try:
        return max(0, int(await get_setting(bot_id, _k(asset, "min_toman"), "0") or "0"))
    except Exception:
        return 0


async def max_toman_setting(bot_id: int, asset: str) -> int:
    from tenant import get_setting
    try:
        return max(0, int(await get_setting(bot_id, _k(asset, "max_toman"), "0") or "0"))
    except Exception:
        return 0


async def plan_allows(bot_id: int) -> bool:
    """همان گیتِ پلنِ «پرداخت مستقیم ارز» — عمداً همان Feature.TON_DIRECT.

    این یک قابلیتِ تازه نیست؛ همان قابلیتِ قبلی است که حالا سه ارز را
    پوشش می‌دهد. پس نه عضوِ enum تازه‌ای لازم دارد نه نگاشتِ پلنِ جدید.
    """
    try:
        from tenant.gateways.ton_direct import plan_allows as _pa
        return await _pa(bot_id)
    except Exception:
        logger.exception("tron_direct: بررسیِ پلن ناموفق بود")
        return False


async def is_available(bot_id: int, asset: str) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, _k(asset, "enabled"), "0") == "1"
            and bool(await wallet_address(bot_id, asset))
            and await plan_allows(bot_id))


async def available_assets(bot_id: int) -> list[str]:
    """ارزهایِ TRON که همین حالا برایِ کاربر قابلِ استفاده‌اند."""
    out = []
    for a in ASSETS:
        try:
            if await is_available(bot_id, a):
                out.append(a)
        except Exception:
            logger.exception("tron_direct: بررسیِ دسترسیِ %s", a)
    return out


# ═══════════════════════ نرخ ═══════════════════════

_rate_cache: dict[str, tuple[Decimal, float]] = {}
_RATE_TTL = 300   # ۵ دقیقه


async def _wallex_toman(symbol: str) -> Decimal | None:
    """قیمتِ یک بازارِ والکس به تومان — مثلِ TRXTMN یا USDTTMN.

    والکس صرافیِ داخلی است و از سرورِ ایران در دسترس؛ خروجی‌اش هم مستقیم
    تومان است، پس نه به نرخِ دلار نیاز داریم نه به سرویسِ خارجی.
    """
    if aiohttp is None:
        return None
    try:
        t = aiohttp.ClientTimeout(total=_RATE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get("https://api.wallex.ir/v1/markets") as r:
                if r.status != 200:
                    return None
                d = await r.json(content_type=None)
        sym = (d.get("result") or d).get("symbols") or {}
        st = (sym.get(symbol) or {}).get("stats") or {}
        val = st.get("lastPrice") or st.get("bidPrice")
        if not val:
            return None
        rate = Decimal(str(val).replace(",", ""))
        return rate if rate > 0 else None
    except Exception:
        logger.warning("tron_direct: نرخِ %s از والکس گرفته نشد", symbol, exc_info=True)
        return None


async def _trx_usd_price() -> Decimal | None:
    """قیمتِ هر TRX به دلار — منبعِ پشتیبان وقتی والکس در دسترس نیست."""
    if aiohttp is None:
        return None
    urls = (
        "https://api.coinbase.com/v2/prices/TRX-USD/spot",
        "https://api.binance.com/api/v3/ticker/price?symbol=TRXUSDT",
    )
    for url in urls:
        try:
            t = aiohttp.ClientTimeout(total=_RATE_TIMEOUT)
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        continue
                    d = await r.json(content_type=None)
            raw = (d.get("data") or {}).get("amount") if "data" in d else d.get("price")
            if not raw:
                continue
            val = Decimal(str(raw))
            if val > 0:
                return val
        except Exception:
            logger.warning("tron_direct: قیمتِ TRX از %s نیامد", url, exc_info=True)
    return None


async def toman_rate(bot_id: int, asset: str, cached_only: bool = False) -> Decimal | None:
    """نرخِ هر واحدِ ارز به تومان.

    ترتیب: کش → والکس (تومانِ مستقیم) → دلارِ جهانی × نرخِ دلار→تومان
           → نرخِ دستیِ مالک → None

    مثلِ ton_direct، `cached_only=True` یعنی «اگر کش نداری None بده» تا
    رندرِ منو هیچ‌وقت منتظرِ شبکه نماند (باگِ منویِ ۴۰ ثانیه‌ای).
    """
    import time
    from tenant import get_setting

    asset = _norm_asset(asset)

    async def _manual() -> Decimal | None:
        raw = await get_setting(bot_id, _k(asset, "manual_price"), "")
        try:
            val = Decimal(str(raw or "0"))
        except Exception:
            return None
        return val if val > 0 else None

    ck = f"{asset}_toman"
    cached = _rate_cache.get(ck)
    if cached and (time.time() - cached[1]) < _RATE_TTL:
        return cached[0]
    if cached_only:
        return await _manual()

    # ۱) والکس: هم TRXTMN دارد هم USDTTMN — خروجی مستقیم تومان
    val = await _wallex_toman("TRXTMN" if asset == "TRX" else "USDTTMN")

    # ۲) پشتیبان: قیمتِ جهانی × نرخِ دلار→تومان
    if val is None:
        try:
            from tenant.gateways.crypto_pay import get_rate_smart
            brs_key = await get_setting(bot_id, "brsapi_key", "")
            usd_toman = Decimal(str(await asyncio.wait_for(
                get_rate_smart(brs_key), timeout=10)))
        except asyncio.TimeoutError:
            logger.warning("tron_direct: نرخِ دلار→تومان تایم‌اوت شد")
            usd_toman = Decimal(0)
        except Exception:
            logger.warning("tron_direct: نرخِ دلار→تومان به دست نیامد", exc_info=True)
            usd_toman = Decimal(0)
        if usd_toman <= 0:
            try:
                usd_toman = Decimal(str(await get_setting(bot_id, "toman_per_usd", "0")))
            except Exception:
                usd_toman = Decimal(0)
        if usd_toman > 0:
            if asset == "USDT":
                val = usd_toman          # تتر عملاً همان دلار است
            else:
                usd = await _trx_usd_price()
                if usd:
                    val = usd * usd_toman

    if val is None or val <= 0:
        return await _manual()
    _rate_cache[ck] = (val, time.time())
    return val


# ═══════════════════════ کلاینتِ زنجیره ═══════════════════════

def _hex_to_text(raw: str | None) -> str:
    """فیلدِ `raw_data.data` تراکنشِ TRX هگز است؛ اگر متنِ خوانا بود برش می‌گردانیم."""
    if not raw:
        return ""
    try:
        return bytes.fromhex(raw).decode("utf-8", "ignore").strip()
    except Exception:
        return ""


async def _get_json(url: str, params: dict) -> dict | None:
    if aiohttp is None:
        return None
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(url, params=params) as r:
                return await r.json(content_type=None)
    except Exception:
        logger.warning("tron_direct: TronGrid پاسخ نداد (%s)", url, exc_info=True)
        return None


async def fetch_incoming(address: str, asset: str, limit: int = 50) -> list[dict]:
    """واریزهایِ اخیرِ یک آدرسِ TRON.

    خروجی: [{"hash":…, "value":Decimal, "comment":str, "source":str}, …]
    خطا → لیستِ خالی (هیچ‌وقت استثنا به حلقه نمی‌دهد).
    """
    asset = _norm_asset(asset)
    out: list[dict] = []

    if asset == "USDT":
        data = await _get_json(
            f"{TRONGRID_BASE}/v1/accounts/{address}/transactions/trc20",
            {"limit": str(limit), "only_to": "true", "only_confirmed": "true",
             "contract_address": USDT_TRC20_CONTRACT})
        if not isinstance(data, dict):
            return []
        for it in data.get("data") or []:
            try:
                if (it.get("to") or "") != address:
                    continue
                info = it.get("token_info") or {}
                if (info.get("address") or "") != USDT_TRC20_CONTRACT:
                    continue
                dec = int(info.get("decimals") or 6)
                value = Decimal(str(it.get("value") or 0)) / (Decimal(10) ** dec)
            except Exception:
                continue
            if value <= 0:
                continue
            out.append({
                "hash": (it.get("transaction_id") or "")[:80],
                "value": value,
                "comment": "",   # TRC20 اصلاً فیلدِ memo ندارد
                "source": it.get("from") or "",
            })
        return out

    # ── TRX بومی ──
    data = await _get_json(
        f"{TRONGRID_BASE}/v1/accounts/{address}/transactions",
        {"limit": str(limit), "only_to": "true", "only_confirmed": "true"})
    if not isinstance(data, dict):
        return []
    for tx in data.get("data") or []:
        try:
            rets = tx.get("ret") or []
            if rets and (rets[0].get("contractRet") or "SUCCESS") != "SUCCESS":
                continue
            raw = tx.get("raw_data") or {}
            contracts = raw.get("contract") or []
            if not contracts:
                continue
            c0 = contracts[0]
            if (c0.get("type") or "") != "TransferContract":
                continue   # TransferAssetContract = توکنِ TRC10، نه TRX
            pv = ((c0.get("parameter") or {}).get("value") or {})
            value = Decimal(str(pv.get("amount") or 0)) / Decimal(SUN)
        except Exception:
            continue
        if value <= 0:
            continue
        out.append({
            "hash": (tx.get("txID") or "")[:80],
            "value": value,
            "comment": _hex_to_text(raw.get("data")),
            "source": str(pv.get("owner_address") or ""),
        })
    return out


# ═══════════════════════ ساختِ فاکتور ═══════════════════════

def _new_memo() -> str:
    """کدِ پیگیریِ کوتاه و یکتا. پیشوندِ TR تا از کدهایِ TON قابلِ تفکیک باشد."""
    return "TR" + secrets.token_hex(3).upper()


def _uniquify(amount: Decimal) -> Decimal:
    """چهار رقمِ آخرِ اعشار را تصادفی می‌کند تا مبلغ عملاً یکتا شود.

    برایِ تتر این تنها راهِ تشخیصِ پرداخت است (TRC20 memo ندارد)، پس
    برخلافِ TON که سه رقم می‌گیرد اینجا چهار رقم — ۱۰٬۰۰۰ حالت — می‌گیریم.
    همیشه رو به بالا، تا مبلغِ دریافتی از انتظارمان کمتر نشود.
    """
    base = amount.quantize(Decimal("0.01"))
    return (base + Decimal(random.randint(1, 9999)) / Decimal(1_000_000)).quantize(
        Decimal("0.000001"))


# ═══════════════════ هستهٔ امن: تسویه و شارژ ═══════════════════

def _asset_filter(asset: str):
    return TonDirectCharge.asset == asset


async def _credit(bot_id: int, charge_id: int, tx_hash: str, asset: str) -> int:
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
                            kind="tron_direct",
                            note=f"شارژ با واریز مستقیم {asset}"))
        _kyc_uid = u.id if u else None
        await s.commit()
    # === PATCH_KYC_GATE_ALL_PATHS_V1 ===
    if _kyc_uid:
        from tenant.shared.kyc import after_charge as _ac
        await _ac(bot_id, _kyc_uid)
    return credit


async def _match_charge(bot_id: int, asset: str, tx: dict) -> int | None:
    """فاکتورِ متناظرِ یک تراکنش را پیدا می‌کند (اول memo، بعد مبلغِ یکتا)."""
    comment = (tx.get("comment") or "").strip().upper()
    value = Decimal(str(tx.get("value") or 0))

    async with Session() as s:
        rows = (await s.scalars(select(TonDirectCharge).where(
            TonDirectCharge.bot_id == bot_id,
            _asset_filter(asset),
            TonDirectCharge.status == "waiting"))).all()

        if comment:
            for ch in rows:
                if ch.memo and ch.memo.upper() in comment:
                    if value >= Decimal(str(ch.ton_amount)) * AMOUNT_TOLERANCE:
                        return ch.id
                    logger.warning(
                        "tron_direct: memo خورد ولی مبلغ کم بود (فاکتور %s: "
                        "انتظار %s، دریافت %s)", ch.id, ch.ton_amount, value)
                    return None

        # بدونِ memo (حالتِ همیشگیِ تتر و بیشترِ واریزهایِ TRX) → فقط اگر
        # دقیقاً یک فاکتورِ بازِ این ربات با این مبلغِ یکتا جور دربیاید.
        # اگر چند فاکتور هم‌زمان بخورند، به‌جایِ حدس‌زدن هیچ‌کدام خودکار
        # تسویه نمی‌شود — حدسِ اشتباه یعنی شارژِ رایگانِ کاربرِ اشتباه.
        candidates = [
            ch for ch in rows
            if Decimal(str(ch.ton_amount)) > 0
            and abs(value - Decimal(str(ch.ton_amount))) <= FALLBACK_ABS_TOLERANCE
        ]
        if len(candidates) == 1:
            return candidates[0].id
        if len(candidates) > 1:
            logger.warning(
                "tron_direct: تراکنشِ بدونِ memo (bot_id=%s, %s, مبلغ %s) با %d "
                "فاکتورِ بازِ هم‌مبلغ برخورد کرد؛ برایِ جلوگیری از شارژِ اشتباه "
                "هیچ‌کدام خودکار تسویه نشد. فاکتورها: %s",
                bot_id, asset, value, len(candidates), [c.id for c in candidates])
    return None


async def settle_bot(bot_id: int, asset: str, tbot: Bot | None = None) -> int:
    """یک دورِ بررسیِ زنجیره برایِ یک ربات و یک ارز. خروجی: تعدادِ تسویه‌ها."""
    asset = _norm_asset(asset)
    addr = await wallet_address(bot_id, asset)
    if not addr:
        return 0
    async with Session() as s:
        pending = await s.scalar(select(TonDirectCharge.id).where(
            TonDirectCharge.bot_id == bot_id,
            _asset_filter(asset),
            TonDirectCharge.status == "waiting").limit(1))
    if not pending:
        return 0   # فاکتورِ بازی نیست؛ بی‌خود به زنجیره دست نمی‌زنیم

    settled = 0
    for tx in await fetch_incoming(addr, asset):
        tx_hash = tx.get("hash") or ""
        if not tx_hash:
            continue
        async with Session() as s:
            seen = await s.scalar(select(TonDirectCharge.id).where(
                TonDirectCharge.tx_hash == tx_hash))
        if seen:
            continue   # این تراکنش قبلاً استفاده شده
        charge_id = await _match_charge(bot_id, asset, tx)
        if not charge_id:
            continue
        async with Session() as s:
            ch = await s.get(TonDirectCharge, charge_id)
            payer, memo = int(ch.user_telegram_id), ch.memo
        credit = await _credit(bot_id, charge_id, tx_hash, asset)
        if not credit:
            continue
        settled += 1
        logger.info("tron_direct: فاکتور %s (bot_id=%s, %s) با tx %s تسویه شد.",
                    memo, bot_id, asset, tx_hash[:16])
        if tbot is None:
            continue

        # پیامِ فاکتور دیگر لازم نیست در چت بماند؛ اگر حذف نشد بی‌سروصدا رد می‌شویم.
        async with Session() as s:
            ch2 = await s.get(TonDirectCharge, charge_id)
            inv_chat, inv_msg = ch2.invoice_chat_id, ch2.invoice_message_id
        if inv_chat and inv_msg:
            try:
                await tbot.delete_message(inv_chat, inv_msg)
            except Exception:
                logger.info("tron_direct: حذفِ پیامِ فاکتور %s ممکن نشد (بی‌اهمیت)", memo)
        try:
            await tbot.send_message(
                payer,
                "✅ <b>پرداخت شما تایید شد</b>\n\n"
                f"🧾 کد فاکتور: <code>{memo}</code>\n"
                f"💰 کیف پول شما <b>{credit:,} تومان</b> شارژ شد.")
        except Exception:
            logger.exception("tron_direct: اطلاعِ شارژ به کاربر نرسید")

        # اطلاعِ مالکِ ربات: کی پرداخت کرد و چقدر.
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
                    f"{ASSET_EMOJI[asset]} <b>پرداخت جدید — {ASSET_LABEL[asset]} مستقیم</b>\n\n"
                    "<blockquote>"
                    f"👤 نام: {fname}\n"
                    f"🔗 یوزرنیم: {uname}\n"
                    f"🆔 آیدی عددی: <code>{payer}</code>"
                    "</blockquote>\n\n"
                    f"🧾 کد پیگیری: <code>{memo}</code>\n"
                    f"🛒 خرید: شارژ کیف پول\n"
                    f"💰 مبلغ: <b>{credit:,} تومان</b>\n"
                    f"{ASSET_EMOJI[asset]} مبلغ {asset}: <code>{ch3.ton_amount if ch3 else ''}</code>\n"
                    f"🔗 tx: <code>{tx_hash[:24]}…</code>")
        except Exception:
            logger.exception("tron_direct: اطلاعِ مالکِ ربات ارسال نشد")
    return settled


async def tron_direct_loop(interval: int = 60):
    """هر دقیقه: زنجیرهٔ TRON را برایِ ربات‌هایی که فاکتورِ باز دارند چک می‌کند."""
    from tenant import get_setting
    await asyncio.sleep(45)   # عمداً با ton_direct_loop هم‌زمان نمی‌شود
    while True:
        try:
            now = _dt.datetime.utcnow()
            async with Session() as s:
                await s.execute(
                    sa_update(TonDirectCharge)
                    .where(TonDirectCharge.status == "waiting",
                           TonDirectCharge.asset.in_(ASSETS),
                           TonDirectCharge.expires_at.isnot(None),
                           TonDirectCharge.expires_at < now)
                    .values(status="expired"))
                await s.commit()
                pairs = list((await s.execute(select(
                    TonDirectCharge.bot_id, TonDirectCharge.asset).where(
                        TonDirectCharge.status == "waiting",
                        TonDirectCharge.asset.in_(ASSETS)).distinct())).all())
            for bot_id, asset in pairs:
                try:
                    if await get_setting(bot_id, _k(asset, "enabled"), "0") != "1":
                        continue
                    async with Session() as s:
                        brow = await s.get(BotRow, bot_id)
                        token = brow.token if (brow and brow.status == "active") else None
                    if not token:
                        continue
                    tbot = Bot(token=token, default=DEFAULTS)
                    try:
                        await settle_bot(bot_id, asset, tbot)
                    finally:
                        try:
                            await tbot.session.close()
                        except Exception:
                            logger.exception("tron_direct: بستنِ سشنِ ربات")
                except Exception:
                    logger.exception("tron_direct: خطا در ربات %s (%s)", bot_id, asset)
                # TronGrid بدونِ کلید نرخ‌محدود است؛ بینِ ربات‌ها مکث می‌کنیم
                await asyncio.sleep(1.2)
        except Exception:
            logger.exception("tron_direct_loop error")
        await asyncio.sleep(interval)


# ═══════════════════ هابِ «پرداخت مستقیم ارز» ═══════════════════

async def _hub(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """صفحهٔ اصلی: هر سه ارز با وضعیتِ خودشان، هرکدام منویِ جداگانه دارد."""
    from tenant import get_setting
    from tenant.gateways.ton_direct import wallet_address as _ton_addr

    ton_addr = await _ton_addr(bot_id)
    ton_on = await get_setting(bot_id, "ton_direct_enabled", "0") == "1"

    body = [
        "💎 <b>پرداخت مستقیم ارز</b>", "",
        "کاربر مبلغ را مستقیم به <b>ولت خودت</b> واریز می‌کند و ربات پرداخت را "
        "از روی بلاک‌چین تشخیص می‌دهد. هیچ درگاه و واسطه‌ای در کار نیست و "
        "کارمزد درگاه هم ندارد.", "",
        "برای هر ارز یک آدرس جداگانه ثبت می‌کنی و جداگانه فعال/غیرفعالش "
        "می‌کنی. کاربر موقع شارژ خودش انتخاب می‌کند با کدام ارز پرداخت کند.", "",
    ]

    def _line(emoji: str, label: str, addr: str, on: bool) -> str:
        if not addr:
            return f"{emoji} {label}: <i>آدرس ثبت نشده</i>"
        return f"{emoji} {label}: {'✅ فعال' if on else '❌ غیرفعال'} — <code>{addr}</code>"

    body.append(_line("💎", "تون (TON)", ton_addr, ton_on))
    for a in ASSETS:
        body.append(_line(ASSET_EMOJI[a], ASSET_LABEL[a],
                          await wallet_address(bot_id, a),
                          await get_setting(bot_id, _k(a, "enabled"), "0") == "1"))

    body += [
        "",
        "⚠️ مسئولیت نگهداری کلید/عبارت بازیابی ولت‌ها کاملاً با خودت است. "
        "بات‌ساز به ولت تو دسترسی ندارد و نمی‌تواند تراکنشی را برگرداند.",
    ]

    rows = [
        [InlineKeyboardButton(text="💎 تون (TON)", callback_data="tond:menu",
                              style=ButtonStyle.PRIMARY)],
    ]
    for a in ASSETS:
        rows.append([InlineKeyboardButton(
            text=f"{ASSET_EMOJI[a]} {ASSET_LABEL[a]}",
            callback_data=f"trxd:menu:{a}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:finance",
                                      style=ButtonStyle.DANGER)])
    return "\n".join(body), InlineKeyboardMarkup(inline_keyboard=rows)


async def set_coin_direct_start(cb: CallbackQuery, bot: Bot, state: FSMContext | None = None):
    """ورودیِ هاب — هم از دکمهٔ شیشه‌ای و هم از کیبوردِ پایین صدا زده می‌شود."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await plan_allows(row.id):
        await cb.answer()
        await cb.message.answer(
            "🔒 <b>پرداخت مستقیم ارز — ویژهٔ پلن حرفه‌ای</b>\n\n"
            "با این قابلیت، کاربرانت مستقیم به ولت TON / TRON خودت واریز "
            "می‌کنند و ربات پرداخت را از روی بلاک‌چین تشخیص می‌دهد — بدون "
            "درگاه، بدون واسطه و بدون کارمزد درگاه.\n\n"
            "برای فعال‌سازی، پلن ربات را به <b>حرفه‌ای</b> ارتقا بده.")
        return
    text, kb = await _hub(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@tron_direct_router.callback_query(F.data.in_({"set:coin_direct", "cdir:hub"}))
async def cdir_hub(cb: CallbackQuery, bot: Bot):
    await set_coin_direct_start(cb, bot)


# ═══════════════════════ پنلِ مالکِ ربات (هر ارز) ═══════════════════════

def _asset_of(cb: CallbackQuery) -> str:
    parts = (cb.data or "").split(":")
    return _norm_asset(parts[2] if len(parts) > 2 else "")


async def _menu(bot_id: int, asset: str) -> tuple[str, InlineKeyboardMarkup]:
    from tenant import get_setting
    asset = _norm_asset(asset)
    addr = await wallet_address(bot_id, asset)
    on = await get_setting(bot_id, _k(asset, "enabled"), "0") == "1"
    in_wallet = await get_setting(bot_id, _k(asset, "show_wallet"), "1") == "1"
    label, emoji = ASSET_LABEL[asset], ASSET_EMOJI[asset]

    body = [f"{emoji} <b>پرداخت مستقیم {label}</b>", ""]
    if asset == "USDT":
        body.append(
            "کاربر <b>تتر روی شبکهٔ TRON (TRC20)</b> را مستقیم به ولت خودت "
            "واریز می‌کند و ربات واریز را از روی بلاک‌چین تشخیص می‌دهد.")
        body.append(
            "⚠️ انتقال TRC20 اصلاً فیلد memo ندارد، پس تشخیص فقط از روی "
            "<b>مبلغ دقیق و یکتای</b> هر فاکتور انجام می‌شود. به همین دلیل "
            "کاربر باید دقیقاً همان عدد اعلام‌شده را بفرستد.")
    else:
        body.append(
            "کاربر <b>TRX</b> را مستقیم به ولت خودت واریز می‌کند و ربات "
            "واریز را از روی بلاک‌چین تشخیص می‌دهد.")
    body.append(
        "هر ولت TRON قبول است (Tonkeeper، TronLink، Trust، OKX Wallet و…) — "
        "فقط آدرس واریزِ <b>صرافی</b> نگذار.")
    body += [
        "",
        (f"👛 ولت مقصد: <code>{addr}</code>" if addr else "👛 ولت مقصد: <i>ثبت نشده</i>"),
        f"وضعیت: {'✅ فعال' if on else '❌ غیرفعال'}",
    ]

    fee = await fee_percent(bot_id, asset)
    body.append(f"➕ کارمزد پرداخت: <b>{fee:g}٪</b>" if fee > 0
                else "➕ کارمزد پرداخت: <i>ندارد</i>")
    rate = await toman_rate(bot_id, asset, cached_only=True)
    if rate:
        body.append(f"📈 نرخ لحظه‌ای: هر {asset} حدود <b>{int(rate):,} تومان</b>")
    else:
        body.append("📈 نرخ لحظه‌ای: <i>برای دیدنش دکمهٔ «بررسی نرخ» را بزن</i>")
    manual = await get_setting(bot_id, _k(asset, "manual_price"), "")
    if manual:
        body.append(f"💵 نرخ دستی: هر {asset} <b>{int(float(manual)):,} تومان</b> "
                    "(وقتی نرخ زنده در دسترس نباشد)")
    min_t = await min_toman_setting(bot_id, asset)
    max_t = await max_toman_setting(bot_id, asset)
    body.append(f"💵 حداقل شارژ: <b>{min_t:,} تومان</b>" if min_t > 0
                else "💵 حداقل شارژ: <i>پیش‌فرض (فقط طبقِ کارمزدِ شبکه)</i>")
    body.append(f"💵 حداکثر شارژ: <b>{max_t:,} تومان</b>" if max_t > 0
                else "💵 حداکثر شارژ: <i>نامحدود</i>")
    body += [
        "",
        "⚠️ مسئولیت نگهداری کلید/عبارت بازیابی ولت کاملاً با خودت است. "
        "بات‌ساز به ولت تو دسترسی ندارد و نمی‌تواند تراکنشی را برگرداند.",
        f"⏳ هر فاکتور <b>{INVOICE_TTL_MINUTES} دقیقه</b> اعتبار دارد (چون قیمت نوسان دارد).",
    ]

    rows = [[InlineKeyboardButton(
        text=("✏️ تغییر آدرس ولت" if addr else f"👛 ثبت آدرس ولت {asset}"),
        callback_data=f"trxd:setwallet:{asset}", style=ButtonStyle.PRIMARY)]]
    if addr:
        rows.append([InlineKeyboardButton(
            text=("🔴 غیرفعال کن" if on else "🟢 فعال کن"),
            callback_data=f"trxd:toggle:{asset}",
            style=(ButtonStyle.DANGER if on else ButtonStyle.SUCCESS))])
    rows.append([InlineKeyboardButton(
        text=f"➕ کارمزد پرداخت ({fee:g}٪)",
        callback_data=f"trxd:setfee:{asset}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text="📈 بررسی نرخ لحظه‌ای", callback_data=f"trxd:rate:{asset}",
        style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text=(f"✏️ تغییر نرخ دستی {asset}" if manual else f"💵 ثبت نرخ دستی {asset}"),
        callback_data=f"trxd:setrate:{asset}", style=ButtonStyle.PRIMARY)])
    rows.append([
        InlineKeyboardButton(text="💵 حداقل مبلغ شارژ",
                             callback_data=f"trxd:setmin:{asset}",
                             style=ButtonStyle.PRIMARY),
        InlineKeyboardButton(text="💵 حداکثر مبلغ شارژ",
                             callback_data=f"trxd:setmax:{asset}",
                             style=ButtonStyle.PRIMARY),
    ])
    rows.append([InlineKeyboardButton(
        text="🧾 وضعیت فاکتورها", callback_data=f"trxd:invoices:{asset}",
        style=ButtonStyle.PRIMARY)])
    if on:
        rows.append([InlineKeyboardButton(
            text="👛 نمایش در کیف پول: " + ("✅ فعال" if in_wallet else "❌ غیرفعال"),
            callback_data=f"trxd:vis:{asset}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="cdir:hub",
                                      style=ButtonStyle.DANGER)])
    return "\n".join(body), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_menu(cb: CallbackQuery, bot_id: int, asset: str, prefix: str = ""):
    text, kb = await _menu(bot_id, asset)
    text = (prefix + text) if prefix else text
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


@tron_direct_router.callback_query(F.data.startswith("trxd:menu:"))
async def trxd_menu(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await plan_allows(row.id):
        await cb.answer("این قابلیت ویژهٔ پلن حرفه‌ای است.", show_alert=True)
        return
    await _show_menu(cb, row.id, _asset_of(cb))
    await cb.answer()


@tron_direct_router.callback_query(F.data.startswith("trxd:rate:"))
async def trxd_rate(cb: CallbackQuery, bot: Bot):
    """گرفتنِ نرخِ زنده — عمداً جدا از منو، چون به شبکه وصل می‌شود."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    asset = _asset_of(cb)
    await cb.answer("⏳ در حال گرفتن نرخ…")
    rate = await toman_rate(row.id, asset)
    if not rate:
        await cb.message.answer(
            "❌ نرخ لحظه‌ای در دسترس نیست.\n\n"
            "سرور به سرویس‌های قیمت دسترسی ندارد.\n\n"
            f"💵 راه‌حل: با دکمهٔ <b>ثبت نرخ دستی {asset}</b> خودت قیمت هر "
            f"{asset} را به تومان وارد کن. تا وقتی نرخ زنده برنگردد، ربات از "
            "همان استفاده می‌کند و کاربران بدون مشکل فاکتور می‌سازند.\n\n"
            "⚠️ یادت باشد نرخ دستی را هر چند وقت به‌روز کنی، وگرنه با نوسان "
            "قیمت ضرر می‌کنی.")
        return
    await _show_menu(cb, row.id, asset)


# ───────────────── آدرسِ ولت ─────────────────

@tron_direct_router.callback_query(F.data.startswith("trxd:setwallet:"))
async def trxd_setwallet(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    asset = _asset_of(cb)
    await state.set_state(TronDirectFSM.wallet)
    await state.update_data(asset=asset)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("tron_direct: بستنِ کیبوردِ قبلی")
    extra = ("\n\n💡 آدرس TRON برای TRX و تتر TRC20 یکی است؛ می‌توانی همان "
             "آدرس ولت دیگر را هم اینجا بزنی، یا برای جدا کردن حساب‌ها "
             "آدرس متفاوتی بدهی.")
    await cb.message.answer(
        f"👛 <b>آدرس ولت {ASSET_LABEL[asset]}</b>\n\n"
        "آدرس ولتی که می‌خواهی پول‌ها به آن برود را بفرست. هر ولت TRON قبول "
        "است — Tonkeeper (تب TRON)، TronLink، Trust Wallet، OKX Wallet یا هر "
        "ولت دیگر. از بخش <b>Receive</b> کپی‌اش کن.\n\n"
        "آدرس TRON با <code>T</code> شروع می‌شود و ۳۴ کاراکتر است."
        + extra + "\n\n"
        "🚫 <b>آدرس واریز صرافی نگذار.</b> آدرس دپوزیت صرافی‌ها بین هزاران "
        "کاربر مشترک است و صرافی برای تشخیص صاحب واریز memo مخصوص خودش را "
        "می‌خواهد. نتیجه‌اش این است که پول به حسابت ننشیند و برگرداندنش هم "
        "دردسر بزرگی باشد. حتماً ولت شخصی خودت را بده.\n\n"
        "⚠️ آدرس را با دقت بررسی کن؛ اگر اشتباه باشد پول‌ها به ولتِ شخصِ "
        "دیگری می‌رود و هیچ راهی برای برگرداندنش نیست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@tron_direct_router.message(TronDirectFSM.wallet, F.text)
async def trxd_setwallet_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    data = await state.get_data()
    asset = _norm_asset(data.get("asset"))
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _menu(row.id, asset)
        await msg.answer(text, reply_markup=kb)
        return
    if not _TRON_ADDR_RE.match(raw):
        await msg.answer(
            "❌ این آدرس معتبر به نظر نمی‌رسد.\n"
            "آدرس TRON دقیقاً ۳۴ کاراکتر است و با <code>T</code> شروع "
            "می‌شود. دوباره بفرست:",
            reply_markup=await smart_cancel_kb(bot))
        return
    # آدرسِ دپوزیتِ صرافی از نظرِ فنی با ولتِ شخصی هیچ فرقی ندارد، پس کد
    # نمی‌تواند تشخیصش بدهد. تنها دفاع، یک تاییدِ آگاهانهٔ خودِ مالک است.
    await state.update_data(pending_wallet=raw)
    await msg.answer(
        "🔎 <b>یک بار دیگر آدرس را چک کن</b>\n\n"
        f"<code>{raw}</code>\n\n"
        f"این آدرس برای دریافت <b>{ASSET_LABEL[asset]}</b> ثبت می‌شود.\n"
        "کاراکترهای ابتدا و انتهای آن را با چیزی که در ولتت می‌بینی مقایسه کن.\n\n"
        "<blockquote>با زدن دکمهٔ تایید، تصدیق می‌کنی که این آدرس <b>ولت شخصی "
        "خودت</b> است و آدرس واریزِ صرافی نیست.\n\n"
        "پول‌های کاربران مستقیم به همین آدرس می‌رود. اگر اشتباه یا آدرس صرافی "
        "باشد، بات‌ساز هیچ راهی برای بازگرداندن آن ندارد.</blockquote>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ تایید می‌کنم؛ ولت شخصی خودم است",
                callback_data=f"trxd:wok:{asset}", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="❌ انصراف",
                                  callback_data=f"trxd:wcancel:{asset}",
                                  style=ButtonStyle.DANGER)],
        ]))


@tron_direct_router.callback_query(F.data.startswith("trxd:wcancel:"))
async def trxd_wallet_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await trxd_menu(cb, bot)


@tron_direct_router.callback_query(F.data.startswith("trxd:wok:"))
async def trxd_wallet_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """ذخیرهٔ آدرس فقط بعد از تاییدِ صریحِ مالک."""
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    asset = _asset_of(cb)
    addr = (await state.get_data()).get("pending_wallet") or ""
    if not _TRON_ADDR_RE.match(addr):
        await cb.answer("آدرس پیدا نشد؛ دوباره ثبتش کن.", show_alert=True)
        return
    await state.clear()
    await set_setting(row.id, _k(asset, "wallet"), addr)
    await cb.answer("✅ ذخیره شد")
    await _show_menu(cb, row.id, asset, "✅ آدرس ولت ذخیره شد.\n\n")


# ───────────────── نرخِ دستی ─────────────────

@tron_direct_router.callback_query(F.data.startswith("trxd:setrate:"))
async def trxd_setrate(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, get_setting, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    asset = _asset_of(cb)
    cur = await get_setting(row.id, _k(asset, "manual_price"), "")
    live = await toman_rate(row.id, asset, cached_only=True)
    await state.set_state(TronDirectFSM.manual_rate)
    await state.update_data(asset=asset)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("tron_direct: بستنِ کیبوردِ قبلی")
    sample = "3500" if asset == "TRX" else "115000"
    await cb.message.answer(
        f"💵 <b>نرخ دستی {asset}</b>\n\n"
        f"قیمت هر <b>۱ {asset}</b> را به <b>تومان</b> بفرست "
        f"(مثلاً <code>{sample}</code>).\n\n"
        "این نرخ فقط زمانی استفاده می‌شود که نرخ زنده در دسترس نباشد — "
        "مثلاً وقتی سرور به سرویس‌های قیمت دسترسی ندارد. تا وقتی نرخ زنده "
        "کار کند، همان اولویت دارد.\n\n"
        + (f"نرخ دستی فعلی: <b>{int(float(cur)):,} تومان</b>\n" if cur else "")
        + (f"نرخ زندهٔ فعلی: <b>{int(live):,} تومان</b>\n" if live else "")
        + "\n⚠️ اگر عدد را خیلی پایین بزنی، عملاً ارز را ارزان می‌فروشی. "
        "قیمت روز را از یک صرافی چک کن.\n"
        "برای حذف نرخ دستی <code>0</code> بفرست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@tron_direct_router.message(TronDirectFSM.manual_rate, F.text)
async def trxd_setrate_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, fa_to_int, set_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    asset = _norm_asset((await state.get_data()).get("asset"))
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _menu(row.id, asset)
        await msg.answer(text, reply_markup=kb)
        return
    val = fa_to_int(raw)
    if val is None or val < 0:
        await msg.answer("عدد نامعتبر است. قیمت هر واحد را به تومان بفرست:",
                         reply_markup=await smart_cancel_kb(bot))
        return
    await state.clear()
    await set_setting(row.id, _k(asset, "manual_price"), "" if val == 0 else str(val))
    text, kb = await _menu(row.id, asset)
    head = "✅ نرخ دستی حذف شد.\n\n" if val == 0 else \
        f"✅ نرخ دستی روی <b>{val:,} تومان</b> تنظیم شد.\n\n"
    await msg.answer(head + text, reply_markup=kb)


# ───────────────── کارمزد ─────────────────

@tron_direct_router.callback_query(F.data.startswith("trxd:setfee:"))
async def trxd_setfee(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    asset = _asset_of(cb)
    cur = await fee_percent(row.id, asset)
    await state.set_state(TronDirectFSM.fee)
    await state.update_data(asset=asset)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("tron_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        f"➕ <b>کارمزد پرداخت {asset}</b>\n\n"
        "درصدی که می‌خواهی روی هر پرداخت سوار شود را بفرست (مثلاً "
        "<code>3</code> یعنی ۳٪).\n\n"
        "کاربر همان مبلغی که خواسته شارژ می‌شود، ولی معادل ارزی که می‌پردازد "
        "به اندازهٔ این درصد بیشتر است. برای جبران کارمزد شبکه، نوسان قیمت "
        "بین ساخت فاکتور تا واریز، و اختلاف نرخ هنگام نقد کردن.\n\n"
        f"کارمزد فعلی: <b>{cur:g}٪</b>\n"
        f"حداکثر مجاز: <b>{MAX_FEE_PERCENT}٪</b> — برای حذف <code>0</code> بفرست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@tron_direct_router.message(TronDirectFSM.fee, F.text)
async def trxd_setfee_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, set_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    asset = _norm_asset((await state.get_data()).get("asset"))
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _menu(row.id, asset)
        await msg.answer(text, reply_markup=kb)
        return
    try:
        val = Decimal(raw.replace("٪", "").replace("%", "").strip())
    except Exception:
        val = None
    if val is None or val < 0 or val > MAX_FEE_PERCENT:
        await msg.answer(
            f"عدد نامعتبر است. یک عدد بین <code>0</code> تا "
            f"<code>{MAX_FEE_PERCENT}</code> بفرست:",
            reply_markup=await smart_cancel_kb(bot))
        return
    await state.clear()
    await set_setting(row.id, _k(asset, "fee_percent"), f"{val:g}")
    text, kb = await _menu(row.id, asset)
    head = "✅ کارمزد حذف شد.\n\n" if val == 0 else \
        f"✅ کارمزد روی <b>{val:g}٪</b> تنظیم شد.\n\n"
    await msg.answer(head + text, reply_markup=kb)


# ───────────────── حداقل/حداکثر ─────────────────

@tron_direct_router.callback_query(F.data.startswith("trxd:setmin:"))
async def trxd_setmin(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    asset = _asset_of(cb)
    cur = await min_toman_setting(row.id, asset)
    await state.set_state(TronDirectFSM.min_amount)
    await state.update_data(asset=asset)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("tron_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        f"💵 <b>حداقل مبلغ شارژ ({asset})</b>\n\n"
        "کمترین مبلغی که کاربر می‌تواند با این روش شارژ کند را به "
        "<b>تومان</b> بفرست.\n\n"
        f"مقدار فعلی: <b>{cur:,} تومان</b>\n"
        "برای برداشتن محدودیت <code>0</code> بفرست.\n\n"
        f"ℹ️ مستقل از این عدد، کف شبکه‌ای <b>{MIN_UNIT[asset]:g} {asset}</b> "
        "همیشه رعایت می‌شود.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@tron_direct_router.message(TronDirectFSM.min_amount, F.text)
async def trxd_setmin_save(msg: Message, bot: Bot, state: FSMContext):
    await _save_limit(msg, bot, state, "min_toman", "حداقل")


@tron_direct_router.callback_query(F.data.startswith("trxd:setmax:"))
async def trxd_setmax(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    asset = _asset_of(cb)
    cur = await max_toman_setting(row.id, asset)
    await state.set_state(TronDirectFSM.max_amount)
    await state.update_data(asset=asset)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("tron_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        f"💵 <b>حداکثر مبلغ شارژ ({asset})</b>\n\n"
        "بیشترین مبلغی که کاربر می‌تواند با این روش شارژ کند را به "
        "<b>تومان</b> بفرست.\n\n"
        f"مقدار فعلی: <b>{cur:,} تومان</b>\n"
        "برای نامحدود کردن <code>0</code> بفرست.",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@tron_direct_router.message(TronDirectFSM.max_amount, F.text)
async def trxd_setmax_save(msg: Message, bot: Bot, state: FSMContext):
    await _save_limit(msg, bot, state, "max_toman", "حداکثر")


async def _save_limit(msg: Message, bot: Bot, state: FSMContext,
                      suffix: str, label: str):
    """ذخیرهٔ مشترکِ حداقل و حداکثر — تنها تفاوتشان کلید و برچسب است."""
    from tenant import get_bot_row, fa_to_int, set_setting, smart_cancel_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    asset = _norm_asset((await state.get_data()).get("asset"))
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _menu(row.id, asset)
        await msg.answer(text, reply_markup=kb)
        return
    val = fa_to_int(raw)
    if val is None or val < 0:
        await msg.answer("عدد نامعتبر است. یک مبلغ به تومان بفرست:",
                         reply_markup=await smart_cancel_kb(bot))
        return
    if suffix == "max_toman" and val > 0:
        min_t = await min_toman_setting(row.id, asset)
        if min_t > 0 and val < min_t:
            await msg.answer(
                f"❌ حداکثر نمی‌تواند از حداقل ({min_t:,} تومان) کمتر باشد. "
                "عدد بزرگ‌تری بفرست:",
                reply_markup=await smart_cancel_kb(bot))
            return
    await state.clear()
    await set_setting(row.id, _k(asset, suffix), str(val))
    text, kb = await _menu(row.id, asset)
    head = f"✅ محدودیتِ {label} برداشته شد.\n\n" if val == 0 else \
        f"✅ {label} مبلغ شارژ روی <b>{val:,} تومان</b> تنظیم شد.\n\n"
    await msg.answer(head + text, reply_markup=kb)


# ───────────────── فعال/غیرفعال و نمایش ─────────────────

@tron_direct_router.callback_query(F.data.startswith("trxd:toggle:"))
async def trxd_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    asset = _asset_of(cb)
    if not await wallet_address(row.id, asset):
        await cb.answer("اول آدرس ولت را ثبت کن.", show_alert=True)
        return
    cur = await get_setting(row.id, _k(asset, "enabled"), "0") == "1"
    await set_setting(row.id, _k(asset, "enabled"), "0" if cur else "1")
    await cb.answer("✅ غیرفعال شد" if cur else "✅ فعال شد")
    await _show_menu(cb, row.id, asset)


@tron_direct_router.callback_query(F.data.startswith("trxd:vis:"))
async def trxd_vis(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    asset = _asset_of(cb)
    cur = await get_setting(row.id, _k(asset, "show_wallet"), "1") == "1"
    await set_setting(row.id, _k(asset, "show_wallet"), "0" if cur else "1")
    await cb.answer("✅ ذخیره شد")
    await _show_menu(cb, row.id, asset)


# ───────────────── فاکتورها (ادمین) ─────────────────

INVOICE_LIST_LIMIT = 8
_STATUS_LABEL = {"paid": "✅ پرداخت‌شده", "waiting": "⏳ در انتظار",
                 "expired": "⌛️ منقضی‌شده"}


def _invoice_row(ch: "TonDirectCharge") -> str:
    when = ch.created_at.strftime("%m/%d %H:%M") if ch.created_at else "—"
    return (f"• <code>{ch.memo}</code> — {int(ch.amount_toman or 0):,} تومان — "
            f"{_STATUS_LABEL.get(ch.status, ch.status)} — {when}")


def _invoice_detail_text(ch: "TonDirectCharge", asset: str) -> str:
    when = ch.created_at.strftime("%Y/%m/%d %H:%M") if ch.created_at else "—"
    out = [
        "🧾 <b>جزئیات فاکتور</b>", "",
        f"کد پیگیری: <code>{ch.memo}</code>",
        f"وضعیت: <b>{_STATUS_LABEL.get(ch.status, ch.status)}</b>",
        f"👤 کاربر: <code>{ch.user_telegram_id}</code>",
        f"💰 مبلغ: <b>{int(ch.amount_toman or 0):,} تومان</b>",
        f"{ASSET_EMOJI.get(asset, '💠')} مبلغ {asset}: <code>{ch.ton_amount}</code>",
        f"📈 نرخ لحظهٔ ساخت: <b>{int(ch.rate_toman or 0):,} تومان</b>",
        f"🕘 ساخته‌شده: {when}",
    ]
    if ch.tx_hash:
        out.append(f"🔗 tx: <code>{ch.tx_hash}</code>")
    return "\n".join(out)


@tron_direct_router.callback_query(F.data.startswith("trxd:invoices:"))
async def trxd_invoices(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    asset = _asset_of(cb)
    async with Session() as s:
        rows = (await s.scalars(select(TonDirectCharge).where(
            TonDirectCharge.bot_id == row.id,
            _asset_filter(asset))
            .order_by(TonDirectCharge.id.desc())
            .limit(INVOICE_LIST_LIMIT))).all()
        lines = [_invoice_row(ch) for ch in rows]

    body = [f"🧾 <b>آخرین فاکتورهای {ASSET_LABEL[asset]}</b>", ""]
    body += lines if lines else ["<i>هنوز فاکتوری ساخته نشده است.</i>"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 جستجو با کد پیگیری",
                              callback_data=f"trxd:searchinv:{asset}",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت",
                              callback_data=f"trxd:menu:{asset}",
                              style=ButtonStyle.DANGER)],
    ])
    try:
        await cb.message.edit_text("\n".join(body), reply_markup=kb)
    except Exception:
        await cb.message.answer("\n".join(body), reply_markup=kb)
    await cb.answer()


@tron_direct_router.callback_query(F.data.startswith("trxd:searchinv:"))
async def trxd_searchinv(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok, smart_cancel_kb
    row = await _owner_ok(cb, bot)
    if not row:
        return
    asset = _asset_of(cb)
    await state.set_state(TronDirectFSM.search)
    await state.update_data(asset=asset)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("tron_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        "🔍 <b>جستجوی فاکتور</b>\n\nکد پیگیری فاکتور را بفرست:",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@tron_direct_router.message(TronDirectFSM.search, F.text)
async def trxd_search_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    asset = _norm_asset((await state.get_data()).get("asset"))
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _menu(row.id, asset)
        await msg.answer(text, reply_markup=kb)
        return
    await state.clear()
    async with Session() as s:
        ch = await s.scalar(select(TonDirectCharge).where(
            TonDirectCharge.bot_id == row.id,
            TonDirectCharge.memo == raw.upper()))
        detail = _invoice_detail_text(ch, _norm_asset(ch.asset)) if ch else None
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="⬅️ بازگشت", callback_data=f"trxd:invoices:{asset}",
        style=ButtonStyle.DANGER)]])
    await msg.answer(detail or "❌ فاکتوری با این کد پیدا نشد.", reply_markup=kb)


# ═══════════════════════ جریانِ کاربر ═══════════════════════

async def _picker_text_kb(bot_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """صفحهٔ انتخابِ ارز — فقط ارزهایی که واقعاً فعال‌اند نشان داده می‌شوند."""
    from tenant import get_setting
    from tenant.gateways.ton_direct import is_available as _ton_ok

    rows = []
    try:
        if await _ton_ok(bot_id) and \
                await get_setting(bot_id, "ton_direct_show_wallet", "1") == "1":
            rows.append([InlineKeyboardButton(
                text="💎 تون (TON)", callback_data="w:ton_direct",
                style=ButtonStyle.SUCCESS)])
    except Exception:
        logger.exception("tron_direct: بررسیِ دسترسیِ TON")
    for a in ASSETS:
        try:
            if await is_available(bot_id, a) and \
                    await get_setting(bot_id, _k(a, "show_wallet"), "1") == "1":
                rows.append([InlineKeyboardButton(
                    text=f"{ASSET_EMOJI[a]} {ASSET_LABEL[a]}",
                    callback_data=f"trxd:ent:{a}", style=ButtonStyle.SUCCESS)])
        except Exception:
            logger.exception("tron_direct: بررسیِ دسترسیِ %s", a)
    if not rows:
        return None
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="w:back",
                                      style=ButtonStyle.DANGER)])
    text = ("💎 <b>شارژ کیف پول با واریز مستقیم ارز</b>\n\n"
            "با کدام ارز می‌خواهی پرداخت کنی؟\n\n"
            "مبلغ را به تومان می‌دهی، به ارز انتخابی تبدیل می‌شود و یک فاکتور "
            "با آدرس و مبلغِ دقیق برایت ساخته می‌شود. همین‌که واریز را روی "
            "زنجیره ببینیم، کیف پولت خودکار شارژ می‌شود.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def visible_assets(bot_id: int) -> list[str]:
    """ارزهایی که همین حالا باید در کیفِ پولِ کاربر دیده شوند (شاملِ TON).

    خروجی مثلاً ["TON", "USDT"]. tenant/__init__.py از همین استفاده می‌کند تا
    تصمیم بگیرد یک دکمهٔ مستقیم بگذارد یا صفحهٔ انتخابِ ارز.
    """
    from tenant import get_setting
    from tenant.gateways.ton_direct import is_available as _ton_ok

    out: list[str] = []
    try:
        if await _ton_ok(bot_id) and \
                await get_setting(bot_id, "ton_direct_show_wallet", "1") == "1":
            out.append("TON")
    except Exception:
        logger.exception("tron_direct: بررسیِ دسترسیِ TON")
    for a in ASSETS:
        try:
            if await is_available(bot_id, a) and \
                    await get_setting(bot_id, _k(a, "show_wallet"), "1") == "1":
                out.append(a)
        except Exception:
            logger.exception("tron_direct: بررسیِ دسترسیِ %s", a)
    return out


async def wallet_button(bot_id: int) -> tuple[str, str] | None:
    """(برچسب، callback_data) دکمهٔ کیفِ پول — یا None اگر هیچ ارزی فعال نیست.

    وقتی فقط یک ارز فعال است یک‌راست به همان می‌رود تا کاربرِ فعلیِ TON
    یک تپِ اضافه نخورد؛ از دو ارز به بالا صفحهٔ انتخاب باز می‌شود.
    """
    vis = await visible_assets(bot_id)
    if not vis:
        return None
    if len(vis) == 1:
        a = vis[0]
        if a == "TON":
            return ("💎 شارژ با TON (واریز مستقیم)", "w:ton_direct")
        return (f"{ASSET_EMOJI[a]} شارژ با {ASSET_SHORT[a]} (واریز مستقیم)",
                f"trxd:ent:{a}")
    return ("💎 شارژ با ارز دیجیتال (واریز مستقیم)", "w:cdirect")


async def wallet_coin_direct_text(msg: Message, bot: Bot, state: FSMContext):
    """شروعِ شارژ از حالتِ کیبوردی — انتخابِ ارز."""
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return
    res = await _picker_text_kb(row.id)
    if not res:
        await msg.answer("این روش پرداخت در حال حاضر در دسترس نیست.")
        return
    await state.clear()
    text, kb = res
    await msg.answer(text, reply_markup=kb)


@tron_direct_router.callback_query(F.data == "w:cdirect")
async def w_coin_direct(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await cb.answer()
        return
    res = await _picker_text_kb(row.id)
    if not res:
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    await state.clear()
    text, kb = res
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


async def _entry_text_kb(bot_id: int, asset: str) -> tuple[str, InlineKeyboardMarkup]:
    """متن+کیبوردِ صفحهٔ ورودیِ یک ارز — هم برایِ ورودِ اول، هم برایِ بازگشت."""
    rate = await toman_rate(bot_id, asset, cached_only=True)
    fee = await fee_percent(bot_id, asset)
    min_t = await min_toman_setting(bot_id, asset)
    max_t = await max_toman_setting(bot_id, asset)

    lines = [f"{ASSET_EMOJI[asset]} <b>شارژ کیف پول با واریز مستقیم "
             f"{ASSET_LABEL[asset]}</b>\n",
             "مبلغ را به تومان می‌دهی، به ارز تبدیل می‌شود و یک فاکتور با آدرس "
             "و مبلغِ دقیق برایت ساخته می‌شود. همین‌که واریز را روی زنجیره "
             "ببینیم، کیف پولت خودکار شارژ می‌شود."]
    if asset == "USDT":
        lines.append("\n⚠️ حتماً روی شبکهٔ <b>TRON (TRC20)</b> بفرست — شبکهٔ "
                     "دیگر یعنی از دست رفتن پول.")
    if rate:
        lines.append(f"\n📈 نرخ لحظه‌ای: هر {asset} حدود <b>{int(rate):,} تومان</b>")
    if fee > 0:
        lines.append(f"➕ کارمزد پرداخت: <b>{fee:g}٪</b>")
    if min_t > 0:
        lines.append(f"💵 حداقل شارژ: <b>{min_t:,} تومان</b>")
    if max_t > 0:
        lines.append(f"💵 حداکثر شارژ: <b>{max_t:,} تومان</b>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 پرداخت", callback_data=f"trxd:pay:{asset}")],
        [InlineKeyboardButton(text=f"🔍 پیگیری فاکتورِ {asset} با کد",
                              callback_data=f"trxd:track:{asset}")],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="w:cdirect")],
    ])
    return "\n".join(lines), kb


@tron_direct_router.callback_query(F.data.startswith("trxd:ent:"))
async def trxd_entry(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    asset = _asset_of(cb)
    if not row or not await is_available(row.id, asset):
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    await state.clear()
    text, kb = await _entry_text_kb(row.id, asset)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@tron_direct_router.callback_query(F.data.startswith("trxd:pay:"))
async def trxd_pay(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    asset = _asset_of(cb)
    if not row or not await is_available(row.id, asset):
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    rate = await toman_rate(row.id, asset)
    if not rate:
        await cb.answer(f"نرخ لحظه‌ای {asset} در دسترس نیست، کمی بعد امتحان کن.",
                        show_alert=True)
        return
    fee = await fee_percent(row.id, asset)
    floor_toman = int((MIN_UNIT[asset] * rate * Decimal(100) / (Decimal(100) + fee)
                       ).to_integral_value())
    min_t = await min_toman_setting(row.id, asset)
    max_t = await max_toman_setting(row.id, asset)
    effective_min = max(floor_toman, min_t) if min_t > 0 else floor_toman

    await state.set_state(TronDirectFSM.amount)
    await state.update_data(asset=asset)
    limits = f"حداقل حدود <b>{effective_min:,} تومان</b>"
    if max_t > 0:
        limits += f" و حداکثر <b>{max_t:,} تومان</b>"
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("tron_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        f"💳 <b>پرداخت با {ASSET_LABEL[asset]}</b>\n\nمبلغی که می‌خواهی شارژ "
        f"کنی را به <b>تومان</b> بفرست ({limits}):",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@tron_direct_router.message(TronDirectFSM.amount, F.text)
async def trxd_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, fa_to_int, inline_back_kb
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    asset = _norm_asset((await state.get_data()).get("asset"))
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=await inline_back_kb(row.id))
        return
    amount = fa_to_int(raw)
    if amount is None or amount <= 0:
        await msg.answer("مبلغ نامعتبر است. یک عدد به تومان بفرست:")
        return

    # === PATCH_KYC_GATE_ALL_PATHS_V1 === سقفِ احرازِ هویت
    from tenant.shared.kyc import gate as _kyc_gate
    _why = await _kyc_gate(row.id, msg.from_user.id, amount)
    if _why:
        await msg.answer(_why)
        return

    min_t = await min_toman_setting(row.id, asset)
    max_t = await max_toman_setting(row.id, asset)
    if min_t > 0 and amount < min_t:
        await msg.answer(f"❌ حداقل مبلغِ شارژ <b>{min_t:,} تومان</b> است. "
                         "عددِ بزرگ‌تری بفرست:")
        return
    if max_t > 0 and amount > max_t:
        await msg.answer(f"❌ حداکثر مبلغِ شارژ <b>{max_t:,} تومان</b> است. "
                         "عددِ کوچک‌تری بفرست:")
        return

    rate = await toman_rate(row.id, asset)
    if not rate:
        await state.clear()
        await msg.answer("❌ نرخ لحظه‌ای در دسترس نیست. کمی بعد دوباره امتحان کن.")
        return

    # کارمزد رویِ مبلغِ پرداختی سوار می‌شود، نه رویِ شارژ: کاربر همان
    # amount تومان شارژ می‌شود ولی معادلِ ارزیِ amount+کارمزد را می‌پردازد.
    fee = await fee_percent(row.id, asset)
    payable_toman = (Decimal(amount) * (Decimal(100) + fee) / Decimal(100))
    coins = payable_toman / rate
    if coins < MIN_UNIT[asset]:
        await msg.answer(
            f"❌ مبلغ خیلی کم است (حدود {coins:.4f} {asset} می‌شود).\n"
            f"حداقل <b>{MIN_UNIT[asset]:g} {asset}</b> لازم است، چون کارمزد "
            "شبکه مبلغ‌های کوچک را بی‌صرفه می‌کند. مبلغ بیشتری بفرست:")
        return

    addr = await wallet_address(row.id, asset)
    if not addr:
        await state.clear()
        await msg.answer("❌ این روش پرداخت فعلاً در دسترس نیست.")
        return
    final = _uniquify(coins)
    memo = _new_memo()
    now = _dt.datetime.utcnow()
    async with Session() as s:
        s.add(TonDirectCharge(
            bot_id=row.id, user_telegram_id=msg.from_user.id, memo=memo,
            asset=asset, amount_toman=int(amount), ton_amount=float(final),
            rate_toman=int(rate), wallet_address=addr, status="waiting",
            expires_at=now + _dt.timedelta(minutes=INVOICE_TTL_MINUTES)))
        await s.commit()
    await state.clear()

    kb_rows = [
        [InlineKeyboardButton(text="📋 کپی آدرس مقصد",
                              copy_text=CopyTextButton(text=addr))],
        [InlineKeyboardButton(text=f"📋 کپی مبلغ دقیق ({final} {asset})",
                              copy_text=CopyTextButton(text=str(final)))],
    ]
    if asset == "TRX":
        kb_rows.append([InlineKeyboardButton(
            text=f"📋 کپی کدِ Note/Memo ({memo})",
            copy_text=CopyTextButton(text=memo))])
    kb_rows.append([InlineKeyboardButton(
        text="🔄 بررسی وضعیت پرداخت", callback_data=f"trxd:check:{memo}",
        style=ButtonStyle.PRIMARY)])

    net = "TRON (TRC20)" if asset == "USDT" else "TRON (TRX)"
    note = (
        "<blockquote>مبلغ را <b>دقیقاً</b> همان عدد بالا بفرست — حتی رقم‌های "
        "آخر اعشار. تشخیص پرداخت تتر فقط از روی همین عدد یکتا انجام می‌شود و "
        "شبکهٔ TRC20 اصلاً فیلد memo ندارد.</blockquote>"
        if asset == "USDT" else
        "<blockquote>مبلغ را <b>دقیقاً</b> همان عدد بالا بفرست — حتی رقم‌های "
        f"آخر اعشار. اگر ولتت فیلد Note/Memo دارد، کد <code>{memo}</code> را "
        "هم بنویس تا تشخیص سریع‌تر شود.</blockquote>")

    sent = await msg.answer(
        f"{ASSET_EMOJI[asset]} <b>فاکتور پرداخت {ASSET_LABEL[asset]}</b>\n\n"
        # === PATCH_TON_DIRECT_MEMO_LABEL_CLARITY_V1 ===
        # همون گزارشِ ادمین که برایِ TON اصلاح شد: برچسبِ سادهٔ «کد فاکتور»
        # باعثِ قاطی‌شدنِ کاربر می‌شد. اینجا فقط برایِ TRX معنا داره (چون
        # TRC20/USDT اصلاً فیلدِ Memo نداره — براش همون «کد فاکتور»یِ ساده
        # درسته، صرفاً یه شمارهٔ پیگیریه).
        + (f"🧾 کدِ Note/Memo (حتماً موقعِ واریز بفرست): <code>{memo}</code>\n"
           if asset == "TRX" else
           f"🧾 کد فاکتور: <code>{memo}</code>\n")
        + f"💰 مبلغ شارژ: <b>{amount:,} تومان</b>\n"
        + (f"➕ کارمزد پرداخت ({fee:g}٪): <b>{int(payable_toman) - amount:,} تومان</b>\n"
           f"🧮 مجموع قابل پرداخت: <b>{int(payable_toman):,} تومان</b>\n" if fee > 0 else "")
        + f"{ASSET_EMOJI[asset]} مبلغ دقیق: <code>{final}</code> {asset}\n"
        f"🌐 شبکه: <b>{net}</b>\n"
        f"👛 آدرس مقصد:\n<code>{addr}</code>\n\n"
        + note + "\n\n"
        f"⏳ این فاکتور تا <b>{INVOICE_TTL_MINUTES} دقیقه</b> اعتبار دارد.\n"
        "بعد از واریز، تایید معمولاً کمتر از یک دقیقه طول می‌کشد و خودم خبرت می‌کنم.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

    async with Session() as s:
        await s.execute(
            sa_update(TonDirectCharge)
            .where(TonDirectCharge.memo == memo)
            .values(invoice_chat_id=sent.chat.id, invoice_message_id=sent.message_id))
        await s.commit()


@tron_direct_router.callback_query(F.data.startswith("trxd:check:"))
async def trxd_check(cb: CallbackQuery, bot: Bot):
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
        asset = _norm_asset(ch.asset) if ch else "TRX"
    if status == "paid":
        await cb.answer("✅ این فاکتور قبلاً تایید و شارژ شده است.", show_alert=True)
        return
    if status is None:
        await cb.answer("فاکتور پیدا نشد.", show_alert=True)
        return
    await cb.answer("⏳ در حال بررسی زنجیره…")
    await settle_bot(row.id, asset, bot)
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


@tron_direct_router.callback_query(F.data.startswith("trxd:track:"))
async def trxd_track(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """پیگیریِ کاربرِ عادی — فقط فاکتورهایِ خودش را می‌بیند."""
    from tenant import get_bot_row, smart_cancel_kb
    row = await get_bot_row(bot)
    asset = _asset_of(cb)
    if not row or not await is_available(row.id, asset):
        await cb.answer("این روش پرداخت در دسترس نیست.", show_alert=True)
        return
    await state.set_state(TronDirectFSM.track)
    await state.update_data(asset=asset)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("tron_direct: بستنِ کیبوردِ قبلی")
    await cb.message.answer(
        f"🔍 <b>پیگیریِ فاکتور {asset}</b>\n\n"
        "کدِ پیگیری‌ای که موقعِ ساختِ فاکتور به تو داده شد را بفرست:",
        reply_markup=await smart_cancel_kb(bot))
    await cb.answer()


@tron_direct_router.message(TronDirectFSM.track, F.text)
async def trxd_track_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        await state.clear()
        return
    asset = _norm_asset((await state.get_data()).get("asset"))
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "❌ لغو / بازگشت"):
        await state.clear()
        text, kb = await _entry_text_kb(row.id, asset)
        await msg.answer(text, reply_markup=kb)
        return
    await state.clear()
    async with Session() as s:
        ch = await s.scalar(select(TonDirectCharge).where(
            TonDirectCharge.bot_id == row.id, TonDirectCharge.memo == raw.upper(),
            TonDirectCharge.user_telegram_id == msg.from_user.id))
        found = None
        if ch:
            found = (ch.memo, ch.status, int(ch.amount_toman or 0),
                     ch.ton_amount, _norm_asset(ch.asset))
    if not found:
        await msg.answer("❌ فاکتوری با این کد برایِ حسابِ تو پیدا نشد.")
        return
    memo, status, toman, coins, a = found
    await msg.answer(
        "🧾 <b>وضعیتِ فاکتور</b>\n\n"
        f"کد پیگیری: <code>{memo}</code>\n"
        f"وضعیت: <b>{_STATUS_LABEL.get(status, status)}</b>\n"
        f"💰 مبلغ: <b>{toman:,} تومان</b>\n"
        f"{ASSET_EMOJI.get(a, '💠')} مبلغ {a}: <code>{coins}</code>")
