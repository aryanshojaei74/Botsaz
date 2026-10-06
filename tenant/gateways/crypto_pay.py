"""
crypto_pay.py — کلاینت درگاه ارز دیجیتال NOWPayments برای بات‌ساز (چند‌مستأجری).

هر فروشنده کلید API و IPN secret خودش را دارد (در تنظیمات ربات وارد می‌کند).
همهٔ توابع، اعتبارنامه را به‌صورت پارامتر می‌گیرند (نه از config سراسری).

امنیت: تأیید امضای IPN با HMAC-SHA512 طبق مستندات رسمی NOWPayments.
نرخ ارز: دریافت زنده از بایننس P2P (USDT/IRR) با fallback به navasan.tech؛
        هیچ نرخ رسمی/جهانی استفاده نمی‌شود چون با بازار آزاد ایران فاصلهٔ زیادی دارد.
"""
import json
import hmac
import hashlib
import time
import asyncio

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

try:
    import aiohttp
except ImportError:
    aiohttp = None

API_BASE = "https://api.nowpayments.io/v1"
_TIMEOUT = 20

# کش نرخ دلار به تومان (برای جلوگیری از فراخوانی مکرر)
_rate_cache: dict[str, tuple[float, float]] = {}  # source -> (rate, timestamp)
_CACHE_TTL = 3600  # ۱ ساعت — نرخ هر یک ساعت یک‌بار به‌روز می‌شود
# === PATCH_RATE_FAIL_CACHE_V1 ===
# گزارشِ واقعی (با لاگِ سرور): تویِ سرورِ این ادمین، هیچ‌کدوم از منابع
# جواب نمی‌دن (navasan با ۲۰۰ ولی بدنه‌ی خالی/نامعتبر، بقیه هم ظاهراً
# timeout) — یعنی هر درخواستِ مشتری، کلِ زنجیرهٔ ۵تایی رو (~۱۲ ثانیه) از
# نو امتحان می‌کنه، چون فقط *موفقیت* کش می‌شد، نه شکست. الان شکست هم
# کش می‌شه، ولی با TTLِ خیلی کوتاه‌تر (۵ دقیقه به‌جایِ ۱ ساعت) — چون
# ممکنه شبکه موقتاً قطع بوده باشه و بخوایم زودتر دوباره امتحان کنیم؛ ولی
# دیگه هر مشتری مجبور نیست خودش ۱۲ ثانیه صبر کنه تا این رو بفهمه.
_FAIL_CACHE_TTL = 300


async def api_status(api_key: str) -> bool:
    if aiohttp is None:
        return False
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(f"{API_BASE}/status") as r:
                data = await r.json()
                return data.get("message") == "OK"
    except Exception:
        return False


# === PATCH_SMM_RATE_CROSS_PROCESS_CACHE_V1 ===
async def _persist_rate_to_db(cache_key: str, rate: float) -> None:
    """هر منبعی که موفق شد، نرخ رو تویِ PlatformSetting هم می‌نویسه (نه فقط
    دیکشنریِ محلی) تا پروسه‌های دیگه (مثلاً پروسه‌ای که واقعاً webhookِ
    مشتری‌ها رو سرو می‌کنه) هم بتونن بخوننش، بدونِ اینکه خودشون مجبور
    باشن دوباره این زنجیرهٔ کند رو طی کنن."""
    try:
        from config import Session
        from models import PlatformSetting
        from sqlalchemy import select
        db_key = f"usd_toman_rate:{cache_key}"
        payload = json.dumps({"rate": rate, "ts": time.time()})
        async with Session() as s:
            row = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == db_key))
            if row:
                row.value = payload
            else:
                s.add(PlatformSetting(key=db_key, value=payload))
            await s.commit()
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (نوشتنِ کشِ دیتابیسیِ نرخ)")


# === PATCH_WALLEX_RATE_V1 ===
async def _rate_from_wallex() -> float:
    """USDT/TMN از والکس — صرافیِ داخلی، خروجی مستقیماً تومان است."""
    try:
        t = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get("https://api.wallex.ir/v1/markets") as r:
                if r.status != 200:
                    return 0
                d = await r.json(content_type=None)
        sym = (d.get("result") or d).get("symbols") or {}
        st = (sym.get("USDTTMN") or {}).get("stats") or {}
        val = st.get("lastPrice") or st.get("bidPrice")
        if not val:
            return 0
        rate = float(str(val).replace(",", ""))
        # عددِ والکس تومان است. بازهٔ عقلانی تا غلطِ فاحش وارد نشود.
        return rate if 50000 < rate < 5000000 else 0
    except Exception:
        logger.exception("wallex rate failed")
        return 0


# === PATCH_RATE_FAST_PATH_V1 ===
# گزارش (Omid، رباتِ خدماتِ مجازی): «ثبتِ سفارش خیلی دیر می‌آورد». هر مرحلهٔ ثبتِ سفارش چند بار قیمت
# حساب می‌کند (پیش‌نمایش، تأیید، هزینه) و هر بار نرخِ دلار را می‌خواند. زنجیرهٔ نرخ قبلاً:
#   ۱) کشِ حافظه → ۲) *والکس (تا ۶ ثانیه)* → ۳) کشِ دیتابیس → ۴) بایننس/نوسان/... (هرکدام تا ۵ ثانیه)
# یعنی هر بار که کشِ حافظهٔ یک پروسه منقضی می‌شد (یا بعد از هر ری‌استارت/دیپلوی) مشتری اول منتظرِ والکسِ کند
# می‌ماند، حتی وقتی نرخِ تازه در دیتابیس بود. حالا:
#   حافظه → دیتابیس (مشترکِ پروسه‌ها) → اگر فقط نرخِ «کهنه» (تا ۴۸ ساعت) هست همان را فوراً بده و در پس‌زمینه
#   تازه کن → اگر هیچ نرخی نیست، زنجیره با سقفِ ۴ ثانیه (بعدش به نرخِ دستیِ ادمین سقوط می‌کند و زنجیره
#   در پس‌زمینه تمام می‌شود). یک پروازِ واحد (single-flight) هم جلوی صدها فراخوانیِ هم‌زمان را می‌گیرد.
_RATE_INFLIGHT: dict[str, "asyncio.Future"] = {}
_RATE_STALE_MAX = 48 * 3600
_RATE_COLD_CAP = 4.0


async def _read_rate_db(cache_key: str):
    try:
        from config import Session
        from models import PlatformSetting
        from sqlalchemy import select
        async with Session() as s:
            row = await s.scalar(select(PlatformSetting).where(
                PlatformSetting.key == f"usd_toman_rate:{cache_key}"))
        if row:
            p = json.loads(row.value)
            return float(p["rate"]), float(p["ts"])
    except Exception:
        logger.debug("خواندنِ کشِ دیتابیسیِ نرخ ممکن نشد (بی‌اهمیت)")
    return None


def _start_rate_refresh(cache_key: str, brsapi_key: str):
    fut = _RATE_INFLIGHT.get(cache_key)
    if fut is not None and not fut.done():
        return fut

    async def _run():
        try:
            return await _fetch_rate_chain(brsapi_key)
        except Exception:
            logger.exception("تازه‌سازیِ نرخِ دلار ناموفق بود")
            return 0
        finally:
            _RATE_INFLIGHT.pop(cache_key, None)

    fut = asyncio.ensure_future(_run())
    _RATE_INFLIGHT[cache_key] = fut
    return fut


async def get_usd_to_toman_rate(brsapi_key: str = "") -> float:
    """نرخِ دلار به تومان — سریع‌ترین مسیرِ ممکن؛ زنجیرهٔ کندِ منابع فقط در پس‌زمینه (یا با سقفِ ۴ثانیه) اجرا می‌شود."""
    if aiohttp is None:
        return 0
    cache_key = f"brsapi:{brsapi_key[:8]}" if brsapi_key else "main"
    now = time.time()
    best = _rate_cache.get(cache_key)
    if best is None or best[0] <= 0:
        db = await _read_rate_db(cache_key)
        if db and (best is None or db[1] > best[1]):
            best = db
            if db[0] > 0:
                _rate_cache[cache_key] = (db[0], db[1])
    if best:
        rate, ts = best
        ttl = _CACHE_TTL if rate > 0 else _FAIL_CACHE_TTL
        age = now - ts
        if age < ttl:
            return rate
        if rate > 0 and age < _RATE_STALE_MAX:
            _start_rate_refresh(cache_key, brsapi_key)   # کهنه ولی قابلِ‌استفاده: فوری بده، پس‌زمینه تازه کن
            return rate
    fut = _start_rate_refresh(cache_key, brsapi_key)
    try:
        return float(await asyncio.wait_for(asyncio.shield(fut), timeout=_RATE_COLD_CAP) or 0)
    except asyncio.TimeoutError:
        return 0   # فراخوان به نرخِ دستیِ ادمین سقوط می‌کند؛ زنجیره در پس‌زمینه ادامه دارد
    except Exception:
        return 0


async def _fetch_rate_chain(brsapi_key: str = "") -> float:
    """
    دریافت نرخ زنده دلار به تومان (نرخ بازار آزاد ایران، نه نرخ رسمی/جهانی).
    منابع (به ترتیب اولویت):
      0. BrsApi (brsapi.ir) — اگر ادمین API Key ثبت کرده باشد؛ بازار آزاد ایران.
      1. بایننس P2P ایران (USDT/IRR) — رایگان، بدون نیاز به API Key، بازتاب‌دهندهٔ
         واقعیِ بازار آزاد چون از معاملات واقعی مردم به‌دست می‌آید.
      2. navasan.tech (نرخ بازار آزاد ایران) — تلاش پشتیبان.
    عمداً از هیچ نرخ رسمی/جهانی (مثل exchangerate-api) استفاده نمی‌شود، چون آن نرخ
    با نرخ واقعی بازار آزاد ایران فاصلهٔ بسیار زیادی دارد و نتیجه را گمراه‌کننده
    می‌کند (مثلاً در نرخ رسمی ~۱۴۰هزار در برابر نرخ بازار آزاد ~۱۷۰هزار+).
    اگر همه ناموفق بودند، صفر برمی‌گرداند (و کد فراخواننده به نرخ دستیِ تنظیم‌شده
    توسط ادمین ربات (toman_per_usd) سقوط می‌کند).
    """
    if aiohttp is None:
        return 0

    # بررسی کش (کش جداگانه برای brsapi تا تداخل با منابع دیگر نداشته باشد)
    cache_key = f"brsapi:{brsapi_key[:8]}" if brsapi_key else "main"
    cached = _rate_cache.get(cache_key)
    if cached:
        cached_rate, cached_ts = cached
        ttl = _CACHE_TTL if cached_rate > 0 else _FAIL_CACHE_TTL
        if (time.time() - cached_ts) < ttl:
            return cached_rate

    # ── منبع ۰: والکس (USDT/TMN) — سریع‌ترین و در دسترس از سرورِ ایران ──
    _w = await _rate_from_wallex()
    if _w > 0:
        _rate_cache[cache_key] = (_w, time.time())
        await _persist_rate_to_db(cache_key, _w)
        return _w

    # === PATCH_SMM_RATE_CROSS_PROCESS_CACHE_V1 ===
    # باگِ واقعی: کشِ بالا (_rate_cache) فقط یه دیکشنریِ تویِ حافظه‌ست —
    # یعنی مالِ همون یه پروسه‌ست. worker_tenant.py (که حلقهٔ پیش‌گرم‌کننده
    # توشه) طبقِ خودِ کامنتِ بالایِ اون فایل یه پروسه‌ی کاملاً جدا از
    # botsaz-tenant.service (پروسه‌ای که واقعاً webhookِ مشتری‌ها رو سرو
    # می‌کنه) اجرا می‌شه. پس گرم‌کردنِ کش تو پروسه‌ی worker هیچ اثری رویِ
    # پروسه‌ای که مشتری واقعاً باهاش حرف می‌زنه نداشت — این همون چیزیه که
    # ادمین بعدِ دیپلویِ فیکسِ قبلی هنوز کند بودنش رو گزارش داد. الان یه
    # لایهٔ کشِ دومِ مبتنی‌بر دیتابیس (PlatformSetting) اضافه شده که بینِ
    # همه‌ی پروسه‌ها مشترکه — worker می‌نویسه، پروسه‌ی سرویس‌دهنده می‌خونه.
    db_key = f"usd_toman_rate:{cache_key}"
    try:
        from config import Session
        from models import PlatformSetting
        from sqlalchemy import select
        async with Session() as s:
            row = await s.scalar(select(PlatformSetting).where(PlatformSetting.key == db_key))
        if row:
            payload = json.loads(row.value)
            ttl = _CACHE_TTL if payload["rate"] > 0 else _FAIL_CACHE_TTL
            if (time.time() - payload["ts"]) < ttl:
                _rate_cache[cache_key] = (payload["rate"], time.time())  # کشِ محلی هم گرم شه
                return payload["rate"]
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (خواندنِ کشِ دیتابیسیِ نرخ)")

    # ── منبع ۰: BrsApi (بازار آزاد ایران) — اگر API Key دارد ──
    if brsapi_key:
        try:
            # === PATCH_SMM_RATE_PREWARM_V1 ===
            t = aiohttp.ClientTimeout(total=5)
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            }
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.get(
                    f"https://brsapi.ir/Api/Market/Gold_Currency.php?key={brsapi_key}",
                    headers=headers
                ) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        usd_toman = None
                        # فرمت پاسخ: {"currency": [{"name": "دلار", "price": 900000, ...}]}
                        currency_list = []
                        if isinstance(data, dict):
                            currency_list = data.get("currency") or data.get("currencies") or []
                            if not currency_list and isinstance(data, dict):
                                currency_list = [data]
                        elif isinstance(data, list):
                            currency_list = data
                        for item in currency_list:
                            if not isinstance(item, dict):
                                continue
                            name = str(item.get("name", "") or item.get("title", "")).lower()
                            if "دلار" in name or "dollar" in name or "usd" in name:
                                for key in ("price", "sell", "buy", "value", "USD",
                                            "usd", "dollar", "price_sell", "price_buy"):
                                    val = item.get(key)
                                    if val is not None:
                                        try:
                                            rate = float(
                                                str(val).replace(",", "").replace("٬", "").replace(" ", "")
                                            )
                                            if rate > 1_000_000:
                                                rate = rate / 10  # ریال به تومان
                                            if rate > 10_000:
                                                usd_toman = rate
                                                break
                                        except (ValueError, TypeError):
                                            continue
                            if usd_toman:
                                break
                        # اگه با فیلتر نام پیدا نشد، هر item با مقدار معقول رو امتحان کن
                        if not usd_toman:
                            for item in currency_list:
                                if not isinstance(item, dict):
                                    continue
                                for key in ("price", "sell", "buy", "value"):
                                    val = item.get(key)
                                    if val is not None:
                                        try:
                                            rate = float(
                                                str(val).replace(",", "").replace("٬", "").replace(" ", "")
                                            )
                                            if rate > 1_000_000:
                                                rate = rate / 10
                                            if 50_000 < rate < 10_000_000:
                                                usd_toman = rate
                                                break
                                        except (ValueError, TypeError):
                                            continue
                                if usd_toman:
                                    break
                        if usd_toman:
                            _rate_cache[cache_key] = (usd_toman, time.time())
                            await _persist_rate_to_db(cache_key, usd_toman)
                            return usd_toman
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # ── منبع ۱: بایننس P2P ایران (USDT/IRR) — معتبرترین منبع رایگان بازار آزاد ──
    # === PATCH_SMM_RATE_PREWARM_V1 ===
    # گزارشِ واقعی: مشتری رویِ کشِ سرد ~۱۵ ثانیه معطل موند. حالا این
    # مسیر با یه حلقهٔ پس‌زمینه (main_tenant.smm_rate_prewarm_loop) از قبل
    # گرم نگه داشته می‌شه، ولی برایِ اولین چند دقیقهٔ بعدِ دیپلوی (قبل از
    # اولین اجرایِ اون حلقه) این تایم‌اوت هم کوتاه‌تر شد (۱۰→۵ ثانیه) که
    # حتی تویِ بدترین حالت هم مشتری زیاد معطل نمونه.
    try:
        t = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(
                "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
                json={
                    "asset": "USDT", "fiat": "IRR", "tradeType": "SELL",
                    "page": 1, "rows": 5, "payTypes": []
                },
                headers={"Content-Type": "application/json"}
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    prices = [
                        float(ad["adv"]["price"])
                        for ad in (data.get("data") or [])
                        if ad.get("adv", {}).get("price")
                    ]
                    if prices:
                        prices.sort()
                        # میانه به‌جای میانگین، تا اگر یک آگهی نامتعارف (خیلی گران/ارزان)
                        # بین چند آگهی معتبر باشد، نرخ نهایی را منحرف نکند.
                        mid = len(prices) // 2
                        irr = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
                        toman = irr / 10  # ریال به تومان
                        if toman > 10000:  # حداقل معقول برای بازار آزاد امروز
                            _rate_cache[cache_key] = (toman, time.time())
                            await _persist_rate_to_db(cache_key, toman)
                            return toman
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # ── منبع ۲: navasan.tech (قیمت بازار آزاد ایران) — تلاش پشتیبان ──
    # === PATCH_SMM_RATE_PREWARM_V1 ===
    try:
        t = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(
                "https://navasan.tech/api/?api_key=freekey&item=usd_buy",
                headers={"User-Agent": "Mozilla/5.0"}
            ) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    # فرمت پاسخ: [{"value": "70000", ...}] یا {"value": "70000"}
                    if isinstance(data, list) and data:
                        val = data[0].get("value") or data[0].get("price")
                    elif isinstance(data, dict):
                        val = data.get("value") or data.get("price")
                    else:
                        val = None
                    if val:
                        rate = float(str(val).replace(",", ""))
                        if rate > 10000:  # حداقل معقول برای بازار آزاد امروز
                            _rate_cache[cache_key] = (rate, time.time())
                            await _persist_rate_to_db(cache_key, rate)
                            return rate
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # ── منبع ۳: AlanChand (بازار آزاد ایران) — اگر alanchand_token داشت ──
    # (این منبع از طریق پارامتر alanchand_token قابل فعال‌سازی است)
    # فعلاً در get_usd_to_toman_rate پشتیبانی نمی‌شود — از get_usd_to_toman_rate_alanchand استفاده کنید

    # ── منبع ۴: BrsApi رایگان (بدون API Key) — آخرین تلاش ──
    try:
        # === PATCH_SMM_RATE_PREWARM_V1 ===
        t = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=t) as s:
            for free_url in (
                "https://brsapi.ir/FreeTsetmcBourseApi/Api_Free_Gold_Currency_v2.json",
                "https://brsapi.ir/FreeTsetmcBourseApi/Api_Free_Gold_Currency.json",
            ):
                try:
                    async with s.get(free_url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            # فرمت احتمالی: {"currency": [{"name": "dollar", "price": 900000, ...}]}
                            # یا آرایه مستقیم یا dict تخت
                            items = []
                            if isinstance(data, dict):
                                for v in data.values():
                                    if isinstance(v, list):
                                        items.extend(v)
                                    elif isinstance(v, dict):
                                        items.append(v)
                                if not items:
                                    items = [data]
                            elif isinstance(data, list):
                                items = data
                            for item in items:
                                if not isinstance(item, dict):
                                    continue
                                # جستجو بر اساس نام یا کلید مستقیم
                                name = str(item.get("name", "") or item.get("title", "")).lower()
                                is_dollar = any(x in name for x in ("dollar", "usd", "دلار"))
                                for key in ("price", "value", "USD", "usd", "sell", "buy"):
                                    val = item.get(key)
                                    if val is not None and (is_dollar or key in ("USD", "usd")):
                                        try:
                                            rate = float(str(val).replace(",", "").replace("٬", ""))
                                            if rate > 1_000_000:
                                                rate = rate / 10  # ریال به تومان
                                            if rate > 10_000:
                                                _rate_cache[cache_key] = (rate, time.time())
                                                await _persist_rate_to_db(cache_key, rate)
                                                return rate
                                        except (ValueError, TypeError):
                                            continue
                except Exception:
                    continue
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # ── منبع ۵: priceto.day (IRR/USD) — آخرین تلاش پشتیبان ──
    try:
        # === PATCH_SMM_RATE_PREWARM_V1 ===
        t = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(
                "https://api.priceto.day/v1/latest/irr/usd",
                headers={"User-Agent": "Mozilla/5.0"}
            ) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    val = None
                    if isinstance(data, dict):
                        val = (data.get("price")
                               or (data.get("data") or {}).get("price")
                               or data.get("value"))
                    if val:
                        rate = float(str(val).replace(",", ""))
                        # priceto.day نرخ رو به ریال می‌ده → تبدیل به تومان
                        if rate > 1_000_000:
                            rate = rate / 10
                        if 10_000 < rate < 10_000_000:
                            _rate_cache[cache_key] = (rate, time.time())
                            await _persist_rate_to_db(cache_key, rate)
                            return rate
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # === PATCH_RATE_FAIL_CACHE_V1 ===
    # همه‌ی منابع ناموفق بودن — این شکست رو هم کش می‌کنیم (با TTLِ کوتاه)
    # تا مشتریِ بعدی مجبور نباشه دوباره کلِ زنجیره رو تحمل کنه.
    _rate_cache[cache_key] = (0, time.time())
    await _persist_rate_to_db(cache_key, 0)
    return 0


async def get_rate_brs_priceto(brsapi_key: str = "") -> float:
    """
    نرخ دلار به تومان — فقط از BrsApi و priceto.day (همان دو منبعی که اول کار
    خوب جواب می‌دادند). ترتیب: BrsApi(Pro) → BrsApi رایگان → priceto.day.
    اگر هیچ‌کدام نشد، 0 برمی‌گرداند.
    """
    if aiohttp is None:
        return 0

    cache_key = f"brspri:{brsapi_key[:8]}" if brsapi_key else "brspri"
    cached = _rate_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        return cached[0]

    ua = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120.0.0.0 Safari/537.36")}

    def _norm(val) -> float:
        try:
            r = float(str(val).replace(",", "").replace("٬", "").replace(" ", ""))
        except (ValueError, TypeError):
            return 0
        if r > 1_000_000:      # ریال → تومان
            r = r / 10
        return r if 10_000 < r < 10_000_000 else 0

    # ── BrsApi: Pro (با کلید) یا رایگان ──
    urls = []
    if brsapi_key:
        urls.append(f"https://brsapi.ir/Api/Market/Gold_Currency.php?key={brsapi_key}")
    urls += [
        "https://brsapi.ir/FreeTsetmcBourseApi/Api_Free_Gold_Currency_v2.json",
        "https://brsapi.ir/FreeTsetmcBourseApi/Api_Free_Gold_Currency.json",
    ]
    for url in urls:
        try:
            t = aiohttp.ClientTimeout(total=4)
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.get(url, headers=ua) as r:
                    if r.status != 200:
                        continue
                    data = await r.json(content_type=None)
                    items = []
                    if isinstance(data, dict):
                        for v in data.values():
                            if isinstance(v, list):
                                items.extend(v)
                            elif isinstance(v, dict):
                                items.append(v)
                        if not items:
                            items = [data]
                    elif isinstance(data, list):
                        items = data
                    # اول دنبال «دلار» بگرد
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name", "") or item.get("title", "")).lower()
                        if any(x in name for x in ("dollar", "usd", "دلار")):
                            for k in ("price", "sell", "buy", "value"):
                                v = _norm(item.get(k))
                                if v:
                                    _rate_cache[cache_key] = (v, time.time())
                                    return v
        except Exception:
            continue

    # ── priceto.day ──
    try:
        t = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get("https://api.priceto.day/v1/latest/irr/usd",
                             headers=ua) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    val = None
                    if isinstance(data, dict):
                        val = (data.get("price")
                               or (data.get("data") or {}).get("price")
                               or data.get("value"))
                    v = _norm(val)
                    if v:
                        _rate_cache[cache_key] = (v, time.time())
                        return v
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # ── tgju (مرجع اصلی بازار آزاد ایران) ──
    try:
        t = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get("https://call.tgju.org/ajax.json", headers=ua) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    # ساختار: {"current": {"price_dollar_rl": {"p": "1,730,000", ...}}}
                    cur = data.get("current", {}) if isinstance(data, dict) else {}
                    node = cur.get("price_dollar_rl") or cur.get("dollar") or {}
                    v = _norm(node.get("p") if isinstance(node, dict) else node)
                    if v:
                        _rate_cache[cache_key] = (v, time.time())
                        return v
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # ── alanchand (بازار آزاد) ──
    try:
        t = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get("https://alanchand.com/api/arz", headers=ua) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    items = data if isinstance(data, list) else data.get("arz", [])
                    for item in (items or []):
                        if not isinstance(item, dict):
                            continue
                        slug = str(item.get("slug", "") or item.get("name", "")).lower()
                        if "usd" in slug or "dollar" in slug or "دلار" in slug:
                            v = _norm(item.get("price") or item.get("sell")
                                      or item.get("value"))
                            if v:
                                _rate_cache[cache_key] = (v, time.time())
                                return v
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    return 0


async def get_rate_smart(brsapi_key: str = "") -> float:
    """
    نرخ نهایی با سقف زمانی کلی تا ربات هنگ نکند:
    اول BrsApi+priceto.day (اولویت)، اگر نشد بایننس/navasan.
    کل عملیات حداکثر ~۸ ثانیه؛ اگر بیشتر شد یا همه ناموفق بودند، از آخرین
    نرخ کش‌شده (حتی منقضی) استفاده می‌کند تا کاربر معطل نماند.
    """
    # ۱) اگر نرخ تازه در کش هست، فوری برگردان (بدون هیچ درخواست شبکه)
    for k in ("wallex", "brspri", f"brspri:{brsapi_key[:8]}" if brsapi_key else "brspri"):
        c = _rate_cache.get(k)
        if c and (time.time() - c[1]) < _CACHE_TTL and c[0] > 10_000:
            return c[0]

    async def _fetch() -> float:
        # والکس اول (آدرس خارجی، روی سرورهای فیلترشده هم کار می‌کند)
        r = await get_rate_wallex()
        if r and r > 10_000:
            return r
        # سپس منابع دیگر (برای سرورهایی که به سایت‌های ایرانی دسترسی دارند)
        r = await get_rate_brs_priceto(brsapi_key)
        if r and r > 10_000:
            return r
        return await get_usd_to_toman_rate(brsapi_key)

    try:
        rate = await asyncio.wait_for(_fetch(), timeout=8.0)
        if rate and rate > 10_000:
            return rate
    except (asyncio.TimeoutError, Exception):
        logger.exception("گرفتنِ نرخِ ارز ناموفق بود")

    # ۲) اضطراری: هر نرخ کش‌شده‌ای (حتی منقضی) بهتر از هنگ کردن است
    best = 0
    for k, v in _rate_cache.items():
        if isinstance(v, tuple) and v[0] and v[0] > 10_000:
            best = v[0]
    return best  # اگر هیچی نبود، 0 برمی‌گردد و صداکننده پیام مناسب می‌دهد



async def get_rate_wallex() -> float:
    """
    نرخ زندهٔ USDT به تومان از والکس (api.wallex.ir).
    این منبع آدرس خارجی دارد و روی سرورهای فیلترشده هم کار می‌کند.
    مسیر: result.symbols.USDTTMN.stats.lastPrice
    """
    if aiohttp is None:
        return 0
    cached = _rate_cache.get("wallex")
    if cached and (time.time() - cached[1]) < _CACHE_TTL and cached[0] > 10_000:
        return cached[0]

    ua = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120.0.0.0 Safari/537.36")}
    try:
        t = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get("https://api.wallex.ir/v1/markets", headers=ua) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    sym = (data.get("result", {})
                               .get("symbols", {})
                               .get("USDTTMN", {}))
                    stats = sym.get("stats", {})
                    price = (stats.get("lastPrice")
                             or stats.get("bidPrice")
                             or stats.get("askPrice"))
                    if price:
                        rate = float(str(price).replace(",", ""))
                        # والکس مستقیم تومان می‌دهد
                        if 10_000 < rate < 10_000_000:
                            _rate_cache["wallex"] = (rate, time.time())
                            return rate
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return 0


async def get_usd_to_toman_rate_alanchand(alanchand_token: str) -> float:
    """دریافت نرخ دلار از AlanChand با Bearer Token."""
    if not alanchand_token or aiohttp is None:
        return 0
    cache_key = f"alanchand:{alanchand_token[:8]}"
    cached = _rate_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        return cached[0]
    try:
        t = aiohttp.ClientTimeout(total=10)
        headers = {
            "Authorization": f"Bearer {alanchand_token}",
            "User-Agent": "Mozilla/5.0"
        }
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(
                "https://api.alanchand.com?type=currency&symbols=usd",
                headers=headers
            ) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    # فرمت: [{"symbol": "usd", "price": 95000, ...}] یا {"usd": {"price": 95000}}
                    items = data if isinstance(data, list) else (
                        list(data.values()) if isinstance(data, dict) else []
                    )
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        for key in ("price", "value", "sell", "buy", "last"):
                            val = item.get(key)
                            if val is not None:
                                try:
                                    rate = float(str(val).replace(",", ""))
                                    if rate > 1_000_000:
                                        rate = rate / 10
                                    if rate > 10_000:
                                        _rate_cache[cache_key] = (rate, time.time())
                                        return rate
                                except (ValueError, TypeError):
                                    continue
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return 0


async def validate_api_key(api_key: str) -> tuple[bool, str]:
    """
    اعتبارسنجی API Key از NOWPayments.
    خروجی: (موفق، پیام)
    """
    if not api_key:
        return False, "API Key خالی است."
    if aiohttp is None:
        return False, "کتابخانه aiohttp نصب نیست."
    try:
        t = aiohttp.ClientTimeout(total=15)
        headers = {"x-api-key": api_key}
        async with aiohttp.ClientSession(timeout=t) as s:
            # تست ۱: وضعیت API
            async with s.get(f"{API_BASE}/status") as r:
                if r.status != 200:
                    return False, f"سرور NOWPayments پاسخ نداد (کد {r.status})."
                d = await r.json()
                if d.get("message") != "OK":
                    return False, "سرور NOWPayments در دسترس نیست."

            # تست ۲: اعتبار API Key با /merchant/coins
            async with s.get(f"{API_BASE}/merchant/coins", headers=headers) as r:
                if r.status == 401:
                    return False, "❌ API Key نامعتبر است (خطای ۴۰۱)."
                if r.status == 403:
                    return False, "❌ دسترسی API Key محدود شده (خطای ۴۰۳)."
                if r.status == 200:
                    return True, "✅ API Key معتبر است."
                return False, f"❌ پاسخ غیرمنتظره: {r.status}"
    except aiohttp.ClientConnectorError:
        return False, "❌ اتصال به NOWPayments برقرار نشد."
    except Exception as e:
        return False, f"❌ خطا: {type(e).__name__}: {str(e)[:80]}"


async def create_invoice(api_key: str, price_amount: float, order_id: str,
                         order_description: str, ipn_callback_url: str,
                         price_currency: str = "usd",
                         success_url: str | None = None) -> dict | None:
    """ساخت لینک پرداخت. خروجی موفق شامل invoice_url و id است."""
    if aiohttp is None:
        return None
    payload = {
        "price_amount": round(float(price_amount), 2),
        "price_currency": price_currency.lower(),
        "order_id": order_id,
        "order_description": order_description,
        "ipn_callback_url": ipn_callback_url,
    }
    if success_url:
        payload["success_url"] = success_url
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(f"{API_BASE}/invoice", headers=headers, json=payload) as r:
                data = await r.json()
                if r.status in (200, 201) and data.get("invoice_url"):
                    return data
                return None
    except Exception:
        return None


async def get_payment_status(api_key: str, payment_id) -> dict | None:
    if aiohttp is None:
        return None
    headers = {"x-api-key": api_key}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(f"{API_BASE}/payment/{payment_id}", headers=headers) as r:
                if r.status == 200:
                    return await r.json()
                return None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# پرداخت مستقیم (API) — برای سایت واسط که خودمان ارز را کنترل می‌کنیم
# (USDT/TON بالا بیاید نه بیت‌کوین). برخلاف invoice_url که کنترلی روی ارز نداریم.
# ══════════════════════════════════════════════════════════════════

# ارزهای پیشنهادی برای مبالغ خرد (کارمزد پایین). ترتیب = اولویت نمایش.
PREFERRED_CURRENCIES = ["usdttrc20", "ton", "usdtbsc", "trx", "ltc", "usdtsol"]

# نام قابل‌نمایش هر ارز
CURRENCY_LABELS = {
    "usdttrc20": "USDT (TRON/TRC20)",
    "usdtbsc": "USDT (BSC/BEP20)",
    "usdtsol": "USDT (Solana)",
    "ton": "TON",
    "trx": "TRON (TRX)",
    "ltc": "Litecoin (LTC)",
}


async def get_min_amount(api_key: str, currency_from: str,
                         currency_to: str = "usd") -> float:
    """حداقل مبلغ پرداخت برای یک ارز (به currency_to). اگر خطا، 0."""
    if aiohttp is None:
        return 0
    headers = {"x-api-key": api_key}
    params = {"currency_from": currency_from, "currency_to": currency_to}
    try:
        t = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(f"{API_BASE}/min-amount", headers=headers,
                             params=params) as r:
                if r.status == 200:
                    d = await r.json()
                    return float(d.get("min_amount", 0) or 0)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return 0


async def estimate_amount(api_key: str, amount_usd: float,
                          currency_to: str) -> float:
    """تخمین اینکه amount_usd دلار چند تا از ارز مقصد می‌شود."""
    if aiohttp is None:
        return 0
    headers = {"x-api-key": api_key}
    params = {"amount": amount_usd, "currency_from": "usd",
              "currency_to": currency_to}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.get(f"{API_BASE}/estimate", headers=headers,
                             params=params) as r:
                if r.status == 200:
                    d = await r.json()
                    return float(d.get("estimated_amount", 0) or 0)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
    return 0


async def create_payment(api_key: str, price_amount_usd: float,
                         pay_currency: str, order_id: str,
                         ipn_callback_url: str,
                         order_description: str = "") -> dict | None:
    """
    ساخت پرداخت مستقیم با ارز مشخص. خروجی موفق شامل pay_address و pay_amount است.
    این همان چیزی است که باعث می‌شود USDT/TON بیاید نه بیت‌کوین.
    """
    if aiohttp is None:
        return None
    payload = {
        "price_amount": round(float(price_amount_usd), 2),
        "price_currency": "usd",
        "pay_currency": pay_currency,
        "order_id": order_id,
        "order_description": order_description or f"Charge {order_id}",
        "ipn_callback_url": ipn_callback_url,
        # نکته: is_fee_paid_by_user را عمداً نگذاشتیم. اگر True باشد، کارمزد از
        # مبلغ کم می‌شود و برای مبالغ کوچک باعث خطای «amountTo is too small»
        # می‌شود. بدون آن، کارمزد جدا حساب می‌شود و مبالغ کوچک هم کار می‌کنند.
    }
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    try:
        t = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=t) as s:
            async with s.post(f"{API_BASE}/payment", headers=headers,
                              json=payload) as r:
                data = await r.json()
                if r.status in (200, 201) and data.get("pay_address"):
                    return data
                # خطا را برمی‌گردانیم تا صداکننده پیام مناسب بدهد
                return {"_error": True,
                        "message": data.get("message", ""),
                        "status": r.status}
    except Exception as e:
        return {"_error": True, "message": str(e), "status": 0}


def verify_ipn_signature(raw_body: bytes, received_sig: str, ipn_secret: str) -> bool:
    """تأیید امضای IPN: JSON با کلیدهای مرتب‌شده → HMAC-SHA512 → مقایسه با هدر."""
    if not ipn_secret or not received_sig:
        return False
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
        sorted_str = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        digest = hmac.new(ipn_secret.encode("utf-8"),
                          sorted_str.encode("utf-8"), hashlib.sha512).hexdigest()
        return hmac.compare_digest(digest, received_sig.strip())
    except Exception:
        return False
