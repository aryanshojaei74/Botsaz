"""تنظیمات و اتصال دیتابیس (async)."""
import os
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from models import Base

# بارگذاری متغیرهای محیطی از فایل .env (در صورت وجود کتابخانه)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# پیکربندی logging مرکزی — چون config.py اولین چیزی است که تقریباً همهٔ
# ماژول‌های دیگر (main، platform_bot، tenant/*) import می‌کنند، این نقطه
# تضمین می‌کند logging قبل از هر print/logger دیگری آماده باشد.
from logging_setup import setup_logging, get_logger as _get_logger
setup_logging()


# === PATCH_SILENT_EXCEPT_V1 ===
logger = _get_logger(__name__)
# توکن خود ربات بات‌ساز (مادر)
PLATFORM_BOT_TOKEN = os.getenv("PLATFORM_BOT_TOKEN", "")


def platform_webhook_secret() -> str:
    """=== PATCH_PLATFORM_WEBHOOK_SECRET_V1 ===
    secret_tokenِ وبهوکِ ربات‌ساز. مسیرِ /webhook/platform ثابت و قابلِ حدس است و
    قبلاً هر کسی می‌توانست آپدیتِ جعلی (مثلاً از طرفِ PLATFORM_ADMIN_ID) به آن
    بفرستد. تلگرام این مقدار را در هدرِ X-Telegram-Bot-Api-Secret-Token برمی‌گرداند.
    از خودِ توکن مشتق می‌شود تا تنظیمِ تازه‌ای لازم نباشد (۶۴ کاراکترِ hex)."""
    import hashlib as _hl
    import hmac as _hm
    if not PLATFORM_BOT_TOKEN:
        return ""
    return _hm.new(PLATFORM_BOT_TOKEN.encode(), b"botsaz-platform-webhook",
                   _hl.sha256).hexdigest()


def platform_webhook_request_ok(request) -> bool:
    import hmac as _hm
    want = platform_webhook_secret()
    got = request.headers.get("x-telegram-bot-api-secret-token", "")
    return bool(want) and _hm.compare_digest(got.encode(), want.encode())

# آدرس پایهٔ وبهوک، مثلا https://example.com
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "")
# === PATCH_FRAGMENT_WALLET_V1 ===
# کلیدِ رمزنگاریِ mnemonicِ کیف‌پول‌هایِ TONِ ساخته‌شده برایِ «استارزِ خودکار».
# باید یک‌بار با: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# تولید و در .env ثبت شود. اگر خالی بماند، اتصال به فرگمنت غیرفعال می‌ماند.
WALLET_ENC_KEY = os.getenv("WALLET_ENC_KEY", "")

# === PATCH_FILE_LINK_CONVERTER_V1 ===
# مسیرِ دیسکِ سرور که فایل‌هایِ ربات‌هایِ نوعِ «تبدیل فایل↔لینک» توش
# ذخیره می‌شن. پیش‌فرض یه پوشهٔ نسبی به ریشهٔ پروژه (/opt/botsaz/storage/file_links).
FILE_LINK_STORAGE_DIR = os.getenv("FILE_LINK_STORAGE_DIR", "storage/file_links")

# پراکسیِ اختیاری برای همهٔ اتصال‌های ربات‌ها به سرورهای تلگرام — برای
# سرورهایی که اتصالِ مستقیم‌شان به تلگرام ناپایدار/کند است (باعثِ خطاهای
# «Request timeout error» و «query is too old» در لاگ می‌شود). اگر خالی
# بماند، هیچ تغییری در رفتارِ پیش‌فرض ایجاد نمی‌شود.
#   فرمت:  socks5://user:pass@host:port   یا   http://host:port
# برای SOCKS5 باید پکیجِ aiohttp-socks نصب باشد (در requirements.txt اضافه شده).
TELEGRAM_PROXY_URL = os.getenv("TELEGRAM_PROXY_URL", "").strip()

if TELEGRAM_PROXY_URL:
    # PATCH_TELEGRAM_PROXY — به‌جای تغییرِ تک‌تکِ ۱۷+ محلی که تو کل پروژه
    # Bot(token=...) ساخته می‌شود (main.py، platform_bot.py،
    # tenant/__init__.py و بقیهٔ ماژول‌های tenant/*.py برای ربات‌های موقتِ
    # اطلاع‌رسانی)، خودِ AiohttpSession.__init__ را پچ می‌کنیم تا اگر
    # صراحتاً proxyـی داده نشده بود، همین مقدار را پیش‌فرض بگیرد. این‌طوری
    # همهٔ Botها (تنانت‌ها، پلتفرم، Botهای موقت) بدونِ استثنا از همین
    # پراکسی رد می‌شوند، بدونِ ریسکِ جاماندنِ یکی از آن ۱۷+ محل.
    try:
        from aiogram.client.session.aiohttp import AiohttpSession as _AiohttpSession
        _orig_aiohttp_session_init = _AiohttpSession.__init__

        def _proxied_aiohttp_session_init(self, proxy=None, *args, **kwargs):
            if proxy is None:
                proxy = TELEGRAM_PROXY_URL
            _orig_aiohttp_session_init(self, proxy=proxy, *args, **kwargs)

        _AiohttpSession.__init__ = _proxied_aiohttp_session_init
        _proxy_host_only = TELEGRAM_PROXY_URL.split("@")[-1]  # پنهان‌کردنِ user:pass از لاگ
        print(f"[proxy] همهٔ اتصال‌های تلگرام از این پراکسی رد می‌شوند: {_proxy_host_only}")
    except Exception as _proxy_err:
        print(f"[proxy] تنظیمِ پراکسیِ تلگرام ناموفق بود (نادیده گرفته شد): {_proxy_err}")

# PATCH_TELEGRAM_RETRY — تلاشِ خودکارِ مجدد برای خطاهای شبکه‌ایِ گذرا (تایم‌اوت،
# قطعِ اتصال) هنگامِ صحبت با سرورهای تلگرام. این نوع خطاها معمولاً موقتی‌اند؛
# به‌جای اینکه بلافاصله شکست بخوریم و کاربر مجبور شود دستی دوباره دکمه را
# بزند، خودمان تا ۲ بار دیگر (جمعاً ۳ تلاش) با یک تأخیرِ کوتاه دوباره امتحان
# می‌کنیم. برخلافِ تنظیمِ پراکسی، این همیشه فعال است (نیازی به .env ندارد)
# === PATCH_FAST_TIMEOUT_V1 ===
# تایم‌اوتِ پیش‌فرضِ خودِ کتابخانهٔ aiogram برای هر تماسِ تلگرام ۶۰ ثانیه است
# (BaseSession.__init__(..., timeout: float = 60.0)). یعنی اگه یه ریکوئست
# (حتی نادر) گیر کنه، تا ۶۰ ثانیه صبر می‌کند قبل از fail‌شدن — و بعد پچِ
# retryِ زیر هم همون تماسِ کندِ ۶۰ثانیه‌ای را دوباره امتحان می‌کند (بدترین
# حالت: تا ۱۸۰ ثانیه برای یک تعاملِ ساده، که دقیقاً همان «یهو از کار
# افتادنِ» گاه‌به‌گاهیِ گزارش‌شده را توضیح می‌دهد). اینجا تایم‌اوتِ
# پیش‌فرض را به ۱۲ ثانیه کاهش می‌دهیم — برای یک تماسِ سالم به تلگرام که
# طبقِ تست‌های ما همیشه زیرِ نیم‌ثانیه جواب می‌دهد، این کاملاً کافی است؛
# ولی برای تماسِ گیرکرده، حالا خیلی سریع‌تر fail و retry می‌شود.
# === PATCH_SHARED_TG_SSL_CONTEXT_V1 ===
# لاگِ سرور: «event loop blocked ~1s» با پشته‌یِ _warm_one. هر Bot() در aiogram یک SSLContextِ تازه
# می‌سازد (ssl.create_default_context(cafile=certifi) — بارکردنِ کلِ بستهٔ گواهی‌ها، ~۱۵ms+ CPU)؛ گرم‌کردنِ
# ده‌ها ربات پشتِ‌سرِهم (هر ۳ دقیقه) رشتهٔ اصلی را تا یک ثانیه قفل می‌کرد. همهٔ نشست‌هایِ تلگرام
# می‌توانند یک SSLContextِ مشترک داشته باشند (فقط‌خواندنی است)؛ پس یک‌بار ساخته و دوباره استفاده می‌شود.
try:
    import ssl as _ssl_mod
    import aiogram.client.session.aiohttp as _ag_aiohttp_mod

    class _SharedSSL:
        _ctx_cache: dict = {}

        def __getattr__(self, name):
            return getattr(_ssl_mod, name)

        def create_default_context(self, *a, **kw):
            key = (a, tuple(sorted(kw.items())))
            ctx = self._ctx_cache.get(key)
            if ctx is None:
                ctx = _ssl_mod.create_default_context(*a, **kw)
                self._ctx_cache[key] = ctx
            return ctx

    if not isinstance(getattr(_ag_aiohttp_mod, "ssl", None), _SharedSSL):
        _ag_aiohttp_mod.ssl = _SharedSSL()
except Exception as _ssl_err:
    print(f"[ssl] SSLContextِ مشترکِ تلگرام نصب نشد (نادیده گرفته شد): {_ssl_err}")

try:
    from aiogram.client.session.aiohttp import AiohttpSession as _AiohttpSessionT
    _orig_aiohttp_init = _AiohttpSessionT.__init__

    def _fast_timeout_init(self, *args, **kwargs):
        kwargs.setdefault("timeout", 12.0)
        _orig_aiohttp_init(self, *args, **kwargs)
        # این سرور مسیرِ IPv6 ندارد (ENETUNREACH)؛ فقط IPv4 را امتحان کن تا نه جستجوی AAAA و نه تلاشِ بی‌نتیجهٔ v6 انجام شود.
        # با TG_FORCE_IPV4=0 خاموش می‌شود. اگر پراکسی تنظیم شده، connector دست‌نخورده می‌ماند.
        try:
            import socket as _socket
            if os.getenv("TG_FORCE_IPV4", "1") != "0" and getattr(self, "_connector_type", None) is not None \
                    and "ssl" in getattr(self, "_connector_init", {}) \
                    and self._connector_type.__name__ == "TCPConnector":
                self._connector_init["family"] = _socket.AF_INET
        except Exception as _v4_err:
            print(f"[ipv4] تنظیمِ فقط‌IPv4 ناموفق بود (نادیده گرفته شد): {_v4_err}")

    _AiohttpSessionT.__init__ = _fast_timeout_init
    print("[timeout] تایم‌اوتِ پیش‌فرضِ درخواست‌های تلگرام به ۱۲ ثانیه کاهش یافت.")
except Exception as _timeout_err:
    print(f"[timeout] کاهشِ تایم‌اوت ناموفق بود (نادیده گرفته شد): {_timeout_err}")


# === PATCH_NO_LINK_PREVIEW_GLOBAL_V1 ===
# گزارش (فدایی): پیام‌هایِ ربات‌هایِ مشتری که لینک دارند (مثلاً «سفارش انجام شد» با لینکِ پست) کارتِ بزرگِ
# پیش‌نمایشِ لینک می‌گرفتند. فقط تنظیمِ DEFAULTS در main_tenant کافی نبود چون ده‌ها جایِ دیگر هم Bot می‌سازند
# (حلقه‌هایِ پس‌زمینه/ورکر، درگاه‌ها، SMM sync و …). این‌جا مرکزی: هر Botی که توکنش «بات‌سازِ اصلی» نیست
# (یعنی ربات‌هایِ مشتری) پیش‌فرضِ «بدونِ پیش‌نمایش» می‌گیرد. جاهایی که پیش‌نمایش می‌خواهند
# (گیفتِ NFT، اسلایدشو، پست‌هایِ تبلیغاتی) link_preview_options را صریح می‌دهند و همین پیش‌فرض را override می‌کنند.
try:
    import dataclasses as _dc
    from aiogram import Bot as _AioBotCls
    from aiogram.client.default import DefaultBotProperties as _DBP

    _orig_bot_init = _AioBotCls.__init__

    def _bot_init_no_preview(self, token, session=None, default=None, **kwargs):
        try:
            if os.getenv("NO_LINK_PREVIEW", "1") != "0" and token and token != PLATFORM_BOT_TOKEN:
                if default is None:
                    default = _DBP(link_preview_is_disabled=True)
                elif default.link_preview is None and default.link_preview_is_disabled is None:
                    default = _dc.replace(default, link_preview_is_disabled=True)
        except Exception:
            logger.debug("تنظیمِ پیش‌فرضِ link_preview ممکن نشد", exc_info=True)
        return _orig_bot_init(self, token, session=session, default=default, **kwargs)

    _AioBotCls.__init__ = _bot_init_no_preview
    print("[preview] پیش‌نمایشِ لینک برایِ ربات‌هایِ مشتری پیش‌فرض خاموش شد.")
except Exception as _pv_err:
    print(f"[preview] تنظیمِ پیش‌فرضِ پیش‌نمایش ناموفق بود (نادیده گرفته شد): {_pv_err}")


# فقط خطاهای شبکه‌ای (TelegramNetworkError — یعنی asyncio.TimeoutError یا
# aiohttp.ClientError، از جمله قطع‌شدنِ اتصال) دوباره امتحان می‌شوند؛
# خطاهای واقعیِ تلگرام (مثل «query is too old» یا دسترسیِ غیرمجاز) دست‌نخورده
# می‌مانند چون تکرارشان یا بی‌فایده است یا اشتباه.
try:
    import asyncio as _asyncio
    from aiogram.client.session.aiohttp import AiohttpSession as _AiohttpSession2
    from aiogram.exceptions import TelegramNetworkError as _TelegramNetworkError

    _orig_make_request = _AiohttpSession2.make_request
    _RETRY_ATTEMPTS = 3          # جمعاً ۳ تلاش (۱ اصلی + ۲ تکرار)
    _RETRY_DELAY_SECONDS = 0.25  # فاصلهٔ بینِ تلاش‌ها — کوتاه نگه داشته شده
    # چون در عمل معلوم شد این سرور اکثرِ درخواست‌ها را (نه فقط گاه‌به‌گاه)
    # با یک بارِ retry موفق می‌کند؛ فاصلهٔ زیاد یعنی این تأخیر روی تقریباً
    # هر کلیک حس می‌شود.

    # === PATCH_TELEGRAM_RETRY_VISIBLE_V1 ===
    # این retry تا امروز کاملاً بی‌صدا بود: نه لاگی می‌گذاشت نه شمارنده‌ای.
    # نتیجه‌اش این شد که وقتی ادمین‌ها گفتند «ربات کند شده»، دو دور دنبالِ
    # گلوگاه در پنلِ VPN گشتیم — در حالی که اندازه‌گیری نشان داد هیچ
    # عملیاتِ پنلی از ۲ ثانیه رد نمی‌شود، ولی آپدیت‌ها ۱۳ تا ۱۸ ثانیه
    # طول می‌کشند. آن بازهٔ باریک دقیقاً یعنی: یک تماس با تلگرام سرِ ۱۲
    # ثانیه (تایم‌اوتِ همین فایل) می‌ترکد و تلاشِ دوم موفق می‌شود.
    #
    # حالا هر تلاشِ مجدد لاگ می‌شود تا بتوان اندازه گرفت: چند درصدِ
    # تماس‌ها retry می‌خورند، کدام متد، و آیا مشکل دائمی است یا رگباری.
    _retry_stats = {"total": 0, "retried": 0}

    async def _retrying_make_request(self, bot, method, timeout=None):
        import time as _time
        _retry_stats["total"] += 1
        _t0 = _time.monotonic()
        try:
            return await _retrying_inner(self, bot, method, timeout, _t0)
        finally:
            try:
                import perfctx as _pf
                _pf.add("tg", _time.monotonic() - _t0, type(method).__name__)
            except Exception as _pf_err:
                print(f"[perfctx] ثبتِ زمانِ تلگرام ناموفق بود (نادیده گرفته شد): {_pf_err}")

    async def _retrying_inner(self, bot, method, timeout, _t0):
        import time as _time
        last_err = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                _res = await _orig_make_request(self, bot, method, timeout)
                if attempt:
                    _retry_stats["retried"] += 1
                    _pct = _retry_stats["retried"] * 100 / max(1, _retry_stats["total"])
                    print(f"[retry] {type(method).__name__} بعد از {attempt} تلاشِ "
                          f"ناموفق در {_time.monotonic() - _t0:.1f}s موفق شد "
                          f"(نرخِ retry: {_pct:.1f}%)")
                return _res
            except _TelegramNetworkError as e:
                last_err = e
                if attempt < _RETRY_ATTEMPTS - 1:
                    await _asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
        _retry_stats["retried"] += 1
        print(f"[retry] {type(method).__name__} بعد از {_RETRY_ATTEMPTS} تلاش و "
              f"{_time.monotonic() - _t0:.1f}s شکست خورد: {last_err}")
        raise last_err

    _AiohttpSession2.make_request = _retrying_make_request
    try:
        import perfctx as _pfx
        _pfx.install_http()
    except Exception as _pfx_err:
        print(f"[perfctx] نصبِ شمارندهٔ http ناموفق بود (نادیده گرفته شد): {_pfx_err}")
    print(f"[retry] تلاشِ خودکارِ مجدد برای خطاهای شبکه‌ای فعال شد (حداکثر {_RETRY_ATTEMPTS} تلاش).")
except Exception as _retry_err:
    print(f"[retry] فعال‌سازیِ تلاشِ مجدد ناموفق بود (نادیده گرفته شد): {_retry_err}")

# === PATCH_SAFE_CALLBACK_ANSWER_V1 ===
# باگ: «خطایی پیش‌بینی‌نشده رخ داد ... ERR-XXXXXX» گاه هنگامِ بازکردنِ منوی
# «پلن‌های فروش» (یا موقعِ تغییرِ پلن) در بخشِ «ربات‌های VPN → پنل خودکار» ظاهر
# می‌شد (مثلاً ERR-220505 / ERR-220536). ریشه: cb.answer() آخرِ هندلر با خطای
# واقعیِ تلگرام «query is too old and response timeout expired or query ID is
# invalid» مواجه می‌شود — یعنی خودِ callback_query دیگر معتبر نیست (مثلاً کاربر
# چند بار پشتِ‌سرهم دکمه را زده، یا قبلاً پاسخ داده شده). نکتهٔ مهم: در همهٔ این
# موارد، کارِ اصلیِ هندلر (مثلاً edit_text لیستِ پلن‌ها) قبل از این خط قبلاً با
# موفقیت انجام شده — یعنی کاربر نتیجه را می‌بیند. این خطا فقط مربوط به بستنِ
# اسپینرِ روی دکمه است و اصلاً «رفع‌شدنی» نیست (تلاشِ مجدد هم بی‌فایده است، برای
# همین در پچِ retryِ بالا هم عمداً retry نمی‌شود). قبل از این پچ، همین خطای
# بی‌ضرر از دلِ cb.answer() بیرون می‌زد و به‌عنوانِ استثنایِ مدیریت‌نشده به
# _process_tenant/_process_platform می‌رسید؛ آن‌جا هم لاگِ ERR-XXXXXX ثبت
# می‌شد هم پیامِ ترسناکِ خطا برای کاربر فرستاده می‌شد — درحالی‌که عملاً هیچ
# مشکلی برایِ کاربر پیش نیامده بود.
# راه‌حل: به‌جایِ اصلاحِ تک‌تکِ ۱۹۰۰+ محلی که await cb.answer() صدا زده می‌شود،
# خودِ متدِ CallbackQuery.answer را پچ می‌کنیم تا این خطاهایِ خاص و بی‌ضرر را
# بی‌سروصدا (فقط لاگِ debug) نادیده بگیرد؛ خطاهای واقعیِ دیگر (مثلاً پیامِ
# «فقط مالک ربات دسترسی دارد» که با show_alert فرستاده می‌شود) دست‌نخورده و
# طبقِ همان رفتارِ قبلی raise می‌شوند.
try:
    from aiogram.types import CallbackQuery as _CallbackQuery
    from aiogram.exceptions import TelegramBadRequest as _TelegramBadRequest
    import logging as _cb_logging

    _orig_cb_answer = _CallbackQuery.answer
    _BENIGN_CALLBACK_ERRORS = (
        "query is too old",
        "query id is invalid",
        "query_id_invalid",
    )
    _cb_answer_logger = _cb_logging.getLogger("safe_callback_answer")

    async def _safe_cb_answer(self, *args, **kwargs):
        try:
            return await _orig_cb_answer(self, *args, **kwargs)
        except _TelegramBadRequest as e:
            msg = str(e).lower()
            if any(s in msg for s in _BENIGN_CALLBACK_ERRORS):
                _cb_answer_logger.debug(f"نادیده‌گرفتنِ callback_query منقضی/نامعتبر: {e}")
                return None
            raise

    _CallbackQuery.answer = _safe_cb_answer
    print("[callback] پاسخ به callback_queryهای منقضی/نامعتبر بی‌خطر شد.")
except Exception as _cb_patch_err:
    print(f"[callback] پچِ safe-answer ناموفق بود (نادیده گرفته شد): {_cb_patch_err}")

# آیدی عددی ادمین کل پلتفرم (برای تایید ارتقا پلن و...)
PLATFORM_ADMIN_ID = int(os.getenv("PLATFORM_ADMIN_ID", "0"))

# سرویس هوش مصنوعی آروان‌کلاد برای خواندن و تاییدِ خودکارِ رسیدها (پرداختِ
# ریالی، سازگار با OpenAI). این تنها موتورِ تشخیصِ رسید در پروژه است — کل کار
# (خواندن + مقایسه با سفارش + تصمیمِ تایید/ارجاع) یک‌جا به این مدل سپرده
# می‌شود. اگر خالی بماند، همهٔ رسیدها به تاییدِ دستیِ ادمین ارجاع داده
# می‌شوند (fail-safe).
# Base URL کاملِ endpoint که از پنل آروان می‌گیری و به /v1 ختم می‌شود، مثلاً:
#   ARVAN_AI_BASE_URL=https://arvancloudai.ir/gateway/models/GPT-4.1/XXXX/v1
ARVAN_AI_BASE_URL = os.getenv("ARVAN_AI_BASE_URL", "")
ARVAN_AI_MODEL = os.getenv("ARVAN_AI_MODEL", "Gemini-2.5-Flash-lite")
# بعضی endpointهای آروان کلید را داخلِ خودِ URL دارند و به هدرِ جدا نیاز ندارند؛
# اگر endpointت کلیدِ جدا می‌خواهد، اینجا مقدار بده (وگرنه خالی بگذار).
ARVAN_AI_API_KEY = os.getenv("ARVAN_AI_API_KEY", "")
# === PATCH_ARVAN_MULTI_KEY_ROTATION_V1 ===
# چند کلید (مثلاً چند اکانتِ رایگانِ Groq) با کاما جدا از هم — هر درخواست
# رندوم یکی از این‌ها رو انتخاب می‌کنه تا سقفِ ریت‌لیمیتِ هر اکانت جدا حساب
# بشه. اگه خالی باشه، به همون ARVAN_AI_API_KEY تکی برمی‌گرده (سازگار با قبل).
ARVAN_AI_API_KEYS = [k.strip() for k in os.getenv("ARVAN_AI_API_KEYS", "").split(",") if k.strip()] or (
    [ARVAN_AI_API_KEY] if ARVAN_AI_API_KEY else []
)

# === PATCH_GROUP_GUARD_SEPARATE_AI_PROVIDER_V1 ===
# «هوش مصنوعی محافظِ گروه» (platform_group_guard.py) یه پرامپتِ پایه‌ی خیلی
# بزرگ داره (~۴۴هزار کاراکتر / ~۱۵-۲۰هزار توکن) که از سقفِ رایگانِ
# ارائه‌دهنده‌هایی مثلِ Groq (۷۰۰۰-۱۲۰۰۰ توکن در دقیقه) رد می‌شه؛ درحالی‌که
# رسیدها (tenant/auto_receipt.py) پرامپتِ کوچیکی دارن و رو Groq مشکلی ندارن.
# برایِ همین این دو تا فیچر از دو تا متغیرِ جدا استفاده می‌کنن تا بشه رسیدها
# رو رو Groq (رایگان) نگه داشت ولی محافظِ گروه رو رو آروان (که پرامپتِ بزرگ
# رو راحت می‌خوره) گذاشت. اگه این‌ها خالی بمونن، به همون ARVAN_AI_* برمی‌گرده
# (سازگار با قبل، برایِ کسایی که فقط یه سرویس دارن).
GROUP_GUARD_AI_BASE_URL = os.getenv("GROUP_GUARD_AI_BASE_URL", "") or ARVAN_AI_BASE_URL
GROUP_GUARD_AI_MODEL = os.getenv("GROUP_GUARD_AI_MODEL", "") or ARVAN_AI_MODEL
GROUP_GUARD_AI_API_KEY = os.getenv("GROUP_GUARD_AI_API_KEY", "") or ARVAN_AI_API_KEY
GROUP_GUARD_AI_API_KEYS = [k.strip() for k in os.getenv("GROUP_GUARD_AI_API_KEYS", "").split(",") if k.strip()] or (
    [GROUP_GUARD_AI_API_KEY] if GROUP_GUARD_AI_API_KEY else ARVAN_AI_API_KEYS
)

# === MARKER_TONCENTER_RATELIMIT_V1 ===
# کلیدِ رایگانِ toncenter (از @tonapibot در تلگرام). اختیاری — اگر خالی
# بماند، بدونِ کلید کار می‌کند ولی سقفِ نرخِ پایین‌تری دارد.
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")

# برای شروع SQLite کافی است؛ برای مقیاس به Postgres سوییچ کن
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///botsaz.db")

CONFIG_CODE_START = 50   # کد اختصاصی کانفیگ‌ها از این عدد شروع می‌شود

if DATABASE_URL.startswith("sqlite"):
    engine = create_async_engine(DATABASE_URL, echo=False)
else:
    # PostgreSQL و مشابه: با ده‌ها ربات و ترافیکِ هم‌زمان، poolِ پیش‌فرض (۵)
    # کم است و کوئری‌ها صف می‌شوند (باعثِ کندی و خطا). pool را بزرگ‌تر و
    # مقاوم‌تر می‌کنیم؛ pool_pre_ping کانکشن‌های مرده را قبلِ استفاده تشخیص می‌دهد.
    engine = create_async_engine(
        DATABASE_URL, echo=False,
        pool_size=20, max_overflow=40,
        pool_timeout=30, pool_recycle=1800, pool_pre_ping=True,
    )
Session = async_sessionmaker(engine, expire_on_commit=False)
try:
    import perfctx as _pfx_db
    _pfx_db.install_db(engine)
except Exception as _pfx_db_err:
    print(f"[perfctx] نصبِ شمارندهٔ دیتابیس ناموفق بود (نادیده گرفته شد): {_pfx_db_err}")


# ── بهبود سرعت SQLite: WAL باعث می‌شود خواندن‌ها هنگام نوشتن قفل نشوند،
#    synchronous=NORMAL تعداد fsyncها را کم می‌کند و busy_timeout از خطای
#    «database is locked» تحت فشار جلوگیری می‌کند. (بدون تغییر رفتار منطقی) ──
if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA temp_store=MEMORY")
        finally:
            cur.close()


# === PATCH_INIT_DB_ADVISORY_LOCK_V1 ===
# کلیدِ قفلِ مشورتیِ Postgres برایِ هماهنگیِ init_db بینِ پروسه‌ها (عددِ دلخواهِ ثابت).
_INIT_DB_LOCK_KEY = 727351


async def init_db() -> None:
    async with engine.begin() as conn:
        # === PATCH_INIT_DB_ADVISORY_LOCK_V1 ===
        # همهٔ سرویس‌ها (tenant، worker، platform، سرویس‌هایِ جدایِ هر نوعِ ربات) موقعِ
        # بالا آمدن init_db را اجرا می‌کنند. وقتی چندتا هم‌زمان ری‌استارت می‌شوند (مثلاً
        # دیپلویی که مدلِ تازه دارد) همه هم‌زمان CREATE TABLE می‌زدند و همه‌شان به‌جز یکی
        # با «duplicate key ... pg_type_typname_nsp_index» می‌افتادند
        # («Application startup failed»). systemd دوباره بالایشان می‌آورد، ولی خطا بی‌جا بود.
        # قفلِ سطحِ تراکنش: تا پایانِ همین تراکنش (کلِ DDL و مهاجرت‌ها) نگه داشته و خودکار
        # آزاد می‌شود؛ پروسه‌هایِ بعدی صبر می‌کنند و create_all برایشان «از قبل هست» می‌شود.
        # سقفِ انتظار فقط برایِ گرفتنِ خودِ قفل است، تا DDLهایِ بعدی محدود نشوند.
        if conn.dialect.name == "postgresql":
            await conn.exec_driver_sql("SET LOCAL lock_timeout = '120s'")
            await conn.exec_driver_sql(f"SELECT pg_advisory_xact_lock({_INIT_DB_LOCK_KEY})")
            await conn.exec_driver_sql("SET LOCAL lock_timeout = 0")
        await conn.run_sync(Base.metadata.create_all)
        # مهاجرت سبک: افزودن ستون‌های جدید به دیتابیس‌های قدیمی (SQLite)
        await conn.run_sync(_migrate_new_columns)
        # حذفِ قابلیتِ «تغییر زبان با هوش مصنوعی»: دکمه‌هایش پاک و زبانِ همهٔ کاربران پیش‌فرض (fa) می‌شود.
        for _sql in ("DELETE FROM main_buttons WHERE action = 'toggle_lang'",
                     "DELETE FROM bot_menu_items WHERE action = 'toggle_lang'",
                     "UPDATE bot_users SET lang = 'fa' WHERE lang IS NOT NULL AND lang <> 'fa'"):
            try:
                async with conn.begin_nested():
                    await conn.exec_driver_sql(_sql)
            except Exception:  # noqa: BLE001
                # best-effort: ممکن است جدول/ستون در دیتابیسِ قدیمی هنوز نباشد؛ ادامه بده
                pass
        # مهاجرت ویژه: بازسازی قید یکتای gated_claims برای پشتیبانی از جایزهٔ پلکانی
        if DATABASE_URL.startswith("sqlite"):
            await conn.run_sync(_migrate_gated_claims_tier)
        # مهاجرت ویژه: بازسازی قید یکتای vpn_panels برای پشتیبانی از چند پنل هم‌زمان
        if DATABASE_URL.startswith("sqlite"):
            await conn.run_sync(_migrate_vpn_panels_unique)
        # === PATCH_SMM_SAME_ID_OTHER_PANEL_V1 ===
        # قیدِ یکتاییِ قدیمیِ smm_services روی (bot_id, service_id) بود؛ ولی چند ارائه‌دهنده شناسهٔ سرویسِ
        # یکسان دارند. حالا یکتایی فقط درونِ همان پنل است (panel_pk خالی = پنلِ پیش‌فرضِ قدیمی).
        if conn.dialect.name == "postgresql":
            for _sql in ("ALTER TABLE smm_services DROP CONSTRAINT IF EXISTS uq_smm_services_bot_service",
                         "DROP INDEX IF EXISTS uq_smm_services_bot_service",
                         "CREATE UNIQUE INDEX IF NOT EXISTS uq_smm_services_bot_panel_service "
                         "ON smm_services (bot_id, COALESCE(panel_pk, 0), service_id)"):
                try:
                    async with conn.begin_nested():
                        await conn.exec_driver_sql(_sql)
                except Exception as e:  # noqa: BLE001
                    print("smm uniq migration skip:", e)
        # ایندکس ترکیبی برای پرتکرارترین کوئری هر پیام (جستجوی کاربرِ یک ربات).
        # این کوئری (WHERE bot_id=... AND telegram_id=...) روی هر تک پیام/دکمه
        # اجرا می‌شود، پس نبودِ ایندکسِ ترکیبی مستقیماً روی سرعتِ کل ربات اثر
        # می‌گذارد؛ چه دیتابیس SQLite باشد چه Postgres.
        try:
            await conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_bot_users_bot_tg "
                "ON bot_users (bot_id, telegram_id)"
            )
        except Exception as e:
            print("index skip:", e)


def _migrate_new_columns(sync_conn) -> None:
    """مهاجرت خودکار: همهٔ جدول‌های موجود را با مدل‌ها مقایسه می‌کند و هر ستونی
    که در دیتابیس نباشد را با ALTER TABLE اضافه می‌کند (مخصوص دیتابیس‌های قدیمی)."""
    from sqlalchemy import inspect, text
    from sqlalchemy import Numeric, Float  # === PATCH_MIGRATE_NUMERIC_PRECISION_V1 ===

    insp = inspect(sync_conn)
    try:
        existing_tables = set(insp.get_table_names())
    except Exception:
        return

    def _literal(val, is_bool: bool = False) -> str:
        if isinstance(val, bool) or is_bool:
            if sync_conn.dialect.name == "postgresql":
                return "true" if val else "false"
            return "1" if val else "0"
        if isinstance(val, (int, float)):
            return str(val)
        return "'" + str(val).replace("'", "''") + "'"

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # جدول جدید را create_all ساخته است
        try:
            existing_cols = {c["name"] for c in insp.get_columns(table.name)}
        except Exception:
            continue
        for col in table.columns:
            if col.name in existing_cols:
                continue
            try:
                coltype = col.type.compile(dialect=sync_conn.dialect)
            except Exception:
                coltype = "TEXT"
            is_bool_col = "BOOL" in coltype.upper()
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            # برای ستون‌های NOT NULL باید مقدار پیش‌فرض بدهیم (محدودیت SQLite)
            if not col.nullable:
                default_val = None
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    default_val = _literal(col.default.arg, is_bool_col)
                if default_val is None:
                    t = coltype.upper()
                    if is_bool_col:
                        default_val = "false" if sync_conn.dialect.name == "postgresql" else "0"
                    elif any(k in t for k in ("INT", "NUMERIC", "REAL", "FLOAT", "DECIMAL")):
                        default_val = "0"
                    elif any(k in t for k in ("DATE", "TIME")):
                        default_val = "CURRENT_TIMESTAMP"
                    else:
                        default_val = "''"
                ddl += f" DEFAULT {default_val}"
            # هر ALTER در تراکنش/سیوپوینتِ جدا اجرا می‌شود تا اگر یکی شکست خورد،
            # مانع اجرای مهاجرت‌های بعدی در همان اتصال نشود (مخصوصاً روی Postgres که
            # یک خطا کل تراکنش را «aborted» می‌کند).
            try:
                if sync_conn.dialect.name == "postgresql":
                    with sync_conn.begin_nested():
                        sync_conn.execute(text(ddl))
                else:
                    sync_conn.execute(text(ddl))
                print("migrated:", ddl)
            except Exception as e:
                print(f"migration skip {table.name}.{col.name}:", e)

    # === PATCH_MIGRATE_NUMERIC_PRECISION_V1 ===
    # _migrate_new_columns فقط ستونِ *جدید* اضافه می‌کند و ستونِ موجود را
    # هیچ‌وقت عریض نمی‌کند. نتیجه: چند ستونِ پولی در Postgres هنوز
    # NUMERIC(12,0) مانده‌اند در حالی که مدل Numeric(14,0) می‌گوید — و هر
    # مبلغِ بالایِ ۱۰^۱۲ با «numeric field overflow» رد می‌شود (در error.log
    # دیده شده). عریض‌کردن بی‌خطر است (داده از دست نمی‌رود)؛ باریک‌کردن را
    # عمداً انجام نمی‌دهیم.
    if sync_conn.dialect.name != "postgresql":
        return
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        try:
            db_cols = {c["name"]: c for c in insp.get_columns(table.name)}
        except Exception:
            continue
        for col in table.columns:
            want = col.type
            if not isinstance(want, Numeric) or isinstance(want, Float):
                continue
            have = db_cols.get(col.name, {}).get("type")
            if have is None or want.precision is None:
                continue
            have_prec = getattr(have, "precision", None)
            if have_prec is None or have_prec >= want.precision:
                continue
            ddl = (f'ALTER TABLE "{table.name}" ALTER COLUMN "{col.name}" '
                   f'TYPE NUMERIC({want.precision}, {want.scale or 0})')
            try:
                with sync_conn.begin_nested():
                    sync_conn.execute(text(ddl))
                print(f"widened: {table.name}.{col.name} "
                      f"NUMERIC({have_prec}) → NUMERIC({want.precision})")
            except Exception as e:
                print(f"widen skip {table.name}.{col.name}:", e)


def _migrate_gated_claims_tier(sync_conn) -> None:
    """قید یکتای قدیمی gated_claims روی (button_id, user_id) را به
    (button_id, user_id, tier) ارتقا می‌دهد.

    در SQLite قید UNIQUE سطحِ‌جدول به‌صورت autoindex پخته می‌شود و با ALTER
    قابل‌حذف نیست؛ پس اگر ستون tier تازه اضافه شده باشد، جدول را با همان داده
    بازسازی می‌کنیم تا قید درست اعمال شود. این عملیات idempotent است.
    """
    from sqlalchemy import inspect, text

    insp = inspect(sync_conn)
    try:
        if "gated_claims" not in set(insp.get_table_names()):
            return
        cols = {c["name"] for c in insp.get_columns("gated_claims")}
    except Exception:
        return
    if "tier" not in cols:
        return  # ستون tier هنوز نیست (نسخهٔ خیلی قدیمی) — create_all بعداً می‌سازد

    # اگر قید درست از قبل وجود دارد، کاری نکن
    try:
        tbl_sql = sync_conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='gated_claims'"
        )).scalar() or ""
    except Exception:
        tbl_sql = ""
    if "uq_gated_claims_btn_user_tier" in tbl_sql or "user_id, tier" in tbl_sql:
        return

    try:
        sync_conn.execute(text("PRAGMA foreign_keys=OFF"))
        sync_conn.execute(text("""
            CREATE TABLE gated_claims__new (
                id INTEGER NOT NULL PRIMARY KEY,
                bot_id INTEGER NOT NULL,
                button_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                tier INTEGER NOT NULL DEFAULT 0,
                delivered TEXT,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                CONSTRAINT uq_gated_claims_btn_user_tier UNIQUE (button_id, user_id, tier),
                FOREIGN KEY(bot_id) REFERENCES bots (id),
                FOREIGN KEY(button_id) REFERENCES main_buttons (id),
                FOREIGN KEY(user_id) REFERENCES bot_users (id)
            )
        """))
        sync_conn.execute(text("""
            INSERT INTO gated_claims__new (id, bot_id, button_id, user_id, tier, delivered, created_at)
            SELECT id, bot_id, button_id, user_id, COALESCE(tier, 0), delivered, created_at
            FROM gated_claims
        """))
        sync_conn.execute(text("DROP TABLE gated_claims"))
        sync_conn.execute(text("ALTER TABLE gated_claims__new RENAME TO gated_claims"))
        # ایندکس‌های کمکی را دوباره بساز
        sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_gated_claims_bot_id ON gated_claims (bot_id)"))
        sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_gated_claims_button_id ON gated_claims (button_id)"))
        sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_gated_claims_user_id ON gated_claims (user_id)"))
        sync_conn.execute(text("PRAGMA foreign_keys=ON"))
        print("migrated: gated_claims -> (button_id,user_id,tier) unique")
    except Exception as e:
        print("migration skip gated_claims tier:", e)


def _migrate_vpn_panels_unique(sync_conn) -> None:
    """قید یکتای قدیمیِ vpn_panels روی (bot_id) به‌تنهایی را به (bot_id, panel_key)
    ارتقا می‌دهد.

    این جدول از قبل از افزوده‌شدنِ پشتیبانیِ «چند پنل هم‌زمان» ساخته شده بود؛
    چون قید UNIQUE سطحِ‌جدول در SQLite با ALTER TABLE قابل‌تغییر نیست، دیتابیس‌های
    قدیمی هنوز با همون قیدِ (bot_id) به‌تنهایی مونده‌اند — یعنی هر رباتی که از
    قبل یک پنل (مثلاً سنایی) وصل کرده بود، هیچ‌وقت نمی‌توانست پنل دومی (مثل
    پاسارگاد/مرزبان) ثبت کند، چون INSERTِ ردیفِ دوم با «UNIQUE constraint
    failed: vpn_panels.bot_id» رد می‌شد. این تابع جدول را با همان داده بازسازی
    می‌کند تا قیدِ درست اعمال شود. عملیات idempotent است."""
    from sqlalchemy import inspect, text

    insp = inspect(sync_conn)
    try:
        if "vpn_panels" not in set(insp.get_table_names()):
            return
        cols = {c["name"] for c in insp.get_columns("vpn_panels")}
    except Exception:
        return
    if "panel_key" not in cols:
        return  # ستون panel_key هنوز نیست (نسخهٔ خیلی قدیمی) — create_all بعداً می‌سازد

    try:
        tbl_sql = sync_conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vpn_panels'"
        )).scalar() or ""
    except Exception:
        tbl_sql = ""
    # اگر قیدِ ترکیبیِ درست از قبل روی جدول هست، کاری نکن (idempotent)
    if "bot_id, panel_key" in tbl_sql or "bot_id,panel_key" in tbl_sql:
        return

    try:
        sync_conn.execute(text("PRAGMA foreign_keys=OFF"))
        sync_conn.execute(text("""
            CREATE TABLE vpn_panels__new (
                id INTEGER NOT NULL PRIMARY KEY,
                bot_id INTEGER NOT NULL,
                panel_key VARCHAR(30) DEFAULT 'sanai',
                base_url VARCHAR(300) NOT NULL,
                username VARCHAR(120) NOT NULL,
                password VARCHAR(200) NOT NULL,
                sub_base VARCHAR(300),
                inbound_ids VARCHAR(120),
                enabled BOOLEAN DEFAULT 0,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                CONSTRAINT uq_vpn_panels_bot_panel UNIQUE (bot_id, panel_key),
                FOREIGN KEY(bot_id) REFERENCES bots (id)
            )
        """))
        sync_conn.execute(text("""
            INSERT INTO vpn_panels__new
                (id, bot_id, panel_key, base_url, username, password,
                 sub_base, inbound_ids, enabled, created_at)
            SELECT id, bot_id, COALESCE(panel_key, 'sanai'), base_url, username, password,
                   sub_base, inbound_ids, enabled, created_at
            FROM vpn_panels
        """))
        sync_conn.execute(text("DROP TABLE vpn_panels"))
        sync_conn.execute(text("ALTER TABLE vpn_panels__new RENAME TO vpn_panels"))
        sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_vpn_panels_bot_id ON vpn_panels (bot_id)"))
        sync_conn.execute(text("PRAGMA foreign_keys=ON"))
        print("migrated: vpn_panels -> (bot_id, panel_key) unique — چند پنل هم‌زمان الان پشتیبانی می‌شود")
    except Exception as e:
        print("migration skip vpn_panels unique:", e)


# تعداد روزهای ماندگاری ربات در بایگانی پیش از حذف نهایی
ARCHIVE_DAYS = 10                 # پلن رایگان (عدم تمدید/عدم فعالیت)
PAID_PLAN_ARCHIVE_DAYS = 60       # پلن رشد/حرفه‌ای (عدم تمدید/عدم فعالیت)
USER_DELETED_ARCHIVE_DAYS = 30    # ربات‌هایی که خودِ مالک حذف‌شان کرده (صرف‌نظر از پلن)
# === PATCH_TRIAL_ARCHIVE_V1 ===
TRIAL_ARCHIVE_DAYS = 7            # ربات‌هایِ تستِ منقضی‌شده (کوتاه، چون ۲ روزه بوده‌اند)

ARCHIVE_REASON_LABELS = {
    "nonpayment": "عدم تمدید پلن",
    "inactivity": "عدم فعالیت طولانی‌مدت (بدون هیچ پیام/کلیکی از کاربران یا مالک)",
    "user_deleted": "درخواستِ حذفِ ربات توسط خودِ مالک",
    # === PATCH_ARCHIVE_REASON_LABELS_GAP_V1 ===
    # این سه مقدار جاهایِ دیگرِ کد واقعاً ثبت می‌شدند (plan_trials.py،
    # scripts/expiry_report.py، platform_bot.py برایِ تعویضِ توکن، و
    # scripts/dupbots.py برایِ ادغامِ ربات‌هایِ تکراری) ولی اینجا لیبل
    # نداشتند — پس با اینکه دلیلِ واقعی ثبت شده بود، ادمین همیشه «نامشخص»
    # می‌دید. دقیقاً همین باعث می‌شد بایگانی‌شدنِ یک ربات (که خودش ممکن است
    # کاملاً درست باشد) «بی‌دلیل» به‌نظر برسد.
    "trial_expired": "پایانِ مهلتِ تست",
    "token_swapped": "توکن عوض شد (این ردیفِ قدیمی جایگزین شد)",
    "merged_duplicate": "ادغام با ردیفِ تکراریِ همین ربات",
}


def archive_retention_days(plan: str | None, archive_reason: str | None) -> int:
    """چند روز این ربات در بایگانی می‌ماند قبل از حذفِ نهایی — بسته به
    دلیلِ بایگانی‌شدن و پلن."""
    if archive_reason == "user_deleted":
        return USER_DELETED_ARCHIVE_DAYS
    # === PATCH_TRIAL_ARCHIVE_V1 ===
    # رباتِ تست plan="growth" دارد، پس بدونِ این شرط ۶۰ روز نگه داشته
    # می‌شد — برایِ رباتی که فقط ۲ روز عمر داشته بی‌معنی است.
    if archive_reason == "trial_expired":
        return TRIAL_ARCHIVE_DAYS
    if (plan or "free") != "free":
        return PAID_PLAN_ARCHIVE_DAYS
    return ARCHIVE_DAYS


def archive_reason_text(archive_reason: str | None) -> str:
    return ARCHIVE_REASON_LABELS.get(archive_reason, "نامشخص")


async def archive_fragment_wallets(s, bot_row) -> int:
    """=== PATCH_FRAGMENT_WALLET_ARCHIVE_V1 ===
    قبل از حذفِ ربات، از کیف‌پولِ TON حضانتیِ Fragment (اگر دارد) یک نسخهٔ بی‌ارجاع می‌گیرد.
    تعدادِ ردیف‌هایِ بایگانی‌شده را برمی‌گرداند. هر خطا را بالا می‌دهد — عمداً: اگر بایگانی
    نشد، حذفِ ربات نباید ادامه پیدا کند (وگرنه کلیدِ کیف‌پول برایِ همیشه از دست می‌رود)."""
    from sqlalchemy import select
    from models import FragmentWallet as _FW, FragmentWalletArchive as _FWA

    wallets = (await s.scalars(select(_FW).where(_FW.bot_id == bot_row.id))).all()
    for w in wallets:
        s.add(_FWA(
            old_bot_id=bot_row.id,
            owner_id=bot_row.owner_id,
            bot_username=bot_row.username,
            address=w.address,
            wallet_version=w.wallet_version,
            encrypted_mnemonic=w.encrypted_mnemonic,
            last_balance_ton=w.last_balance_ton or 0,
            last_balance_checked_at=w.last_balance_checked_at,
            wallet_created_at=w.created_at,
            reason="bot_deleted",
        ))
    if wallets:
        await s.flush()
        logger.warning("purge_bot[%s]: %d کیف‌پولِ Fragment بایگانی شد (fragment_wallet_archive)",
                       bot_row.id, len(wallets))
    return len(wallets)


async def purge_bot(bot_id: int) -> str | None:
    """حذف کامل یک ربات و تمام داده‌های متعلق به آن. توکن ربات را برمی‌گرداند
    (برای حذف وب‌هوک). سوابق مالی (PlanOrder) حفظ می‌شود؛ فقط ارجاعش به ربات NULL می‌شود.

    اگر حذف به‌خاطر یک جدول خاص با خطا مواجه شود، آن خطا با نام جدول در exception
    بازگردانده می‌شود تا در پنل ادمین قابل نمایش باشد (به‌جای شکست خاموش)."""
    from sqlalchemy import select
    from models import (
        Bot as _Bot, TENANT_OWNED_MODELS, TENANT_DUAL_REF_MODELS,
        TENANT_CHILD_VIA_MODELS, PlanOrder,
    )

    token = None
    async with Session() as s:
        r = await s.get(_Bot, bot_id)
        if not r:
            return None
        token = r.token

        # === PATCH_BOT_AUDIT_LOG_V1 ===
        # سابقهٔ حذف را قبل از پاک‌شدنِ رکورد نگه می‌داریم تا در پنلِ
        # کاربر معلوم بماند چه رباتی و کِی حذف شد.
        try:
            from models import BotAuditLog as _BAL
            s.add(_BAL(bot_id=bot_id, username=r.username, plan=r.plan,
                       action="delete", from_owner=r.owner_id,
                       note=(r.archive_reason or "")))
            await s.flush()
        except Exception:
            logger.exception("ثبتِ سابقهٔ حذفِ ربات ناموفق بود")

        # === PATCH_FRAGMENT_WALLET_ARCHIVE_V1 ===
        # کلیدِ کیف‌پولِ TONِ حضانتی نباید با حذفِ ربات از بین برود. برخلافِ سابقهٔ
        # حذف (که best-effort است)، این‌جا خطا حذف را متوقف می‌کند.
        try:
            await archive_fragment_wallets(s, r)
        except Exception as e:
            logger.exception("بایگانیِ کیف‌پولِ Fragment قبل از حذفِ ربات %s ناموفق بود", bot_id)
            raise RuntimeError(
                "بایگانیِ کیف‌پولِ Fragment ناموفق بود؛ برایِ جلوگیری از از دست رفتنِ کلید، "
                f"ربات حذف نشد: {e}") from e

        # === PATCH_FILE_LINK_CONVERTER_V1 ===
        # فایل‌هایِ رویِ دیسکِ این ربات (نوعِ «تبدیل فایل↔لینک») قبل از
        # حذفِ ردیف‌هایِ دیتابیس پاک می‌شن. این کار best-effort است — اگه
        # فایلی از قبل نبود یا دیسک خطا داد، حذفِ خودِ ربات نباید متوقف بشه.
        try:
            import os
            from models import FileLink
            file_rows = (await s.scalars(
                select(FileLink.disk_path).where(FileLink.bot_id == bot_id))).all()
            for _path in file_rows:
                try:
                    if _path:
                        os.remove(_path)
                except OSError:
                    pass
        except Exception:
            # === PATCH_SILENT_EXCEPT_V1 ===
            # پاک‌کردنِ فایل‌هایِ رویِ دیسک نباید جلویِ حذفِ ربات را بگیرد،
            # ولی بی‌سروصدا هم نباید باشد — وگرنه فایل‌هایِ یتیم روی دیسک
            # جمع می‌شوند و هیچ ردی از علتش نیست.
            logger.exception("پاک‌کردنِ فایل‌هایِ رباتِ حذف‌شده ناموفق بود")

        # علاقه‌مندی‌ها یک سطح غیرمستقیم به ربات وابسته‌اند:
        # slide_favorites.slide_id -> button_slides.id -> main_buttons.id -> bots.id.
        # قبل از حذف ButtonSlide باید این ردیف‌ها به‌صورت صریح پاک شوند؛
        # مدلِ تک‌سطحی TENANT_CHILD_VIA_MODELS به‌تنهایی برای این زنجیره کافی نیست.
        try:
            from sqlalchemy import delete as _sa_delete
            from models import ButtonSlide, SlideFavorite, MainButton
            _slide_ids = select(ButtonSlide.id).where(
                ButtonSlide.button_id.in_(
                    select(MainButton.id).where(MainButton.bot_id == bot_id)
                )
            )
            await s.execute(_sa_delete(SlideFavorite).where(
                SlideFavorite.slide_id.in_(_slide_ids)))
            await s.flush()
        except Exception as e:
            raise RuntimeError(
                f"خطا در پاک‌سازی علاقه‌مندی‌های اسلاید: {e}") from e
        # ۱) جدول‌هایی که «فرزند» یک جدول دیگرند (مثل ButtonSlide که button_id دارد
        #    نه bot_id) — باید قبل از والدشان حذف شوند تا FK گیر نکند.
        # === PATCH_PURGE_STAGE1_SAVEPOINT_V1 ===
        for Child, child_fk, Parent, parent_bot_col in TENANT_CHILD_VIA_MODELS:
            try:
                async with s.begin_nested():
                    parent_col = getattr(Parent, parent_bot_col)
                    parent_ids = (await s.scalars(
                        select(Parent.id).where(parent_col == bot_id))).all()
                    if parent_ids:
                        child_col = getattr(Child, child_fk)
                        children = (await s.scalars(
                            select(Child).where(child_col.in_(parent_ids)))).all()
                        for c in children:
                            await s.delete(c)
                        await s.flush()
            except Exception as e:
                raise RuntimeError(
                    f"خطا در حذف جدول {getattr(Child, '__name__', Child)}: {e}") from e

        # ۲) جدول‌هایی که مستقیم bot_id دارند و باید کامل حذف شوند
        # === PATCH_PURGE_RETRY_ORDER_V1 ===
        # قبلاً این حلقه فقط یک‌بار، به‌ترتیبِ ثابتِ TENANT_OWNED_MODELS،
        # تلاش می‌کرد. اون لیست چندتا وابستگیِ داخلی داره (مثلاً BotUser
        # باید آخر از همه حذف بشه چون تقریباً همه بهش وصلن؛ DiscountUsage
        # باید قبل از DiscountCode حذف بشه؛ و...) که دستی مرتب‌کردنِ کاملِ
        # همهٔ این‌ها پرریسک و مستعدِ خطاست. حالا به‌جاش چندبار تلاش می‌کنیم:
        # هر مدلی که fail بشه (به‌خاطرِ یه FK به مدلِ دیگه‌ای که هنوز پاک
        # نشده) توی SAVEPOINTِ خودش rollback و به دورِ بعد موکول می‌شه؛ با
        # تکرار، مستقل از ترتیبِ اولیه، در نهایت همه پاک می‌شن — مگر واقعاً
        # یه مشکلِ حل‌نشدنی باشه که در اون صورت همون خطای قبلی را می‌دهد.
        _pending = list(TENANT_OWNED_MODELS)
        _last_err: dict = {}
        for _pass in range(len(TENANT_OWNED_MODELS) + 1):
            if not _pending:
                break
            _still_pending = []
            _made_progress = False
            for M in _pending:
                try:
                    async with s.begin_nested():
                        objs = (await s.scalars(select(M).where(M.bot_id == bot_id))).all()
                        for o in objs:
                            await s.delete(o)
                        await s.flush()
                    _made_progress = True
                except Exception as e:
                    _still_pending.append(M)
                    _last_err[M] = e
            _pending = _still_pending
            if not _made_progress:
                break
        if _pending:
            M = _pending[0]
            raise RuntimeError(
                f"خطا در حذف جدول {getattr(M, '__name__', M)}: {_last_err[M]}") from _last_err[M]

        # ۳) جدول‌هایی که old_bot_id/new_bot_id دارند (تعویض توکن، انتقال مالکیت)
        for M, col_old, col_new in TENANT_DUAL_REF_MODELS:
            try:
                col_old_attr = getattr(M, col_old)
                col_new_attr = getattr(M, col_new)
                objs = (await s.scalars(
                    select(M).where(
                        (col_old_attr == bot_id) | (col_new_attr == bot_id)))).all()
                for o in objs:
                    await s.delete(o)
                await s.flush()
            except Exception as e:
                raise RuntimeError(
                    f"خطا در حذف جدول {getattr(M, '__name__', M)}: {e}") from e

        # ۴) سوابق مالی (PlanOrder): حذف نمی‌شود، فقط ارجاعش به ربات پاک می‌شود
        try:
            orders = (await s.scalars(
                select(PlanOrder).where(PlanOrder.target_bot_id == bot_id))).all()
            for o in orders:
                o.target_bot_id = None
            await s.flush()
        except Exception as e:
            raise RuntimeError(f"خطا در آزادسازی سوابق مالی (PlanOrder): {e}") from e

        # === PATCH_PURGE_PARTNER_LINKS_V1 ===
        # اگر این ربات همزمان یک «ربات همکار» هم بوده (یعنی ردیفی در
        # partner_bots داشته)، جدول‌های وابسته به آن (سوابق فروش/سفارش
        # انبار/تیکت‌های ارجاع/سفارش کارت‌به‌کارت) باید قبل از حذفِ خودِ
        # ردیفِ bots از partner_bots جدا شوند. این سوابق مالی/تاریخی
        # حذف نمی‌شوند — فقط ارجاعشان به partner_bot NULL می‌شود.
        try:
            from sqlalchemy import text as _text
            pb_row = await s.execute(
                _text("SELECT id FROM partner_bots WHERE bot_id = :bid"),
                {"bid": bot_id},
            )
            pb_id = pb_row.scalar()
            if pb_id is not None:
                for _t in ("partner_sales", "partner_stock_orders",
                           "partner_escalations", "partner_card_orders"):
                    await s.execute(
                        _text(f"UPDATE {_t} SET partner_bot_id = NULL "
                              f"WHERE partner_bot_id = :pid"),
                        {"pid": pb_id},
                    )
                await s.execute(
                    _text("DELETE FROM partner_bots WHERE id = :pid"),
                    {"pid": pb_id},
                )
                await s.flush()
        except Exception as e:
            raise RuntimeError(f"خطا در آزادسازیِ لینکِ رباتِ همکار: {e}") from e

        # === PATCH_PURGE_SCHEMA_SWEEP_V1 ===
        # تورِ ایمنی: تا اینجا همه‌چیز از رویِ سه لیستِ **دستیِ** بالا پاک
        # شده و تاریخچهٔ این پروژه نشان داده که آن لیست‌ها همیشه از اسکیما
        # عقب می‌مانند (هر جدولِ تازه‌ای که کسی یادش برود اضافه کند، حذفِ
        # ربات را می‌شکند — مثلِ gift_orders که ۸۴ بار در لاگ افتاده بود).
        # این حلقه مثلِ redirect_all_bot_id_references از رویِ اسکیمای واقعی
        # کار می‌کند: هر ستونی که FK به bots.id دارد و هنوز به این ربات
        # اشاره می‌کند را پاک/آزاد می‌کند. اگر لیست‌ها کامل باشند این حلقه
        # هیچ ردیفی پیدا نمی‌کند و عملاً بی‌هزینه است.
        try:
            from sqlalchemy import text as _text
            from models import Base as _Base

            _swept: list[str] = []
            for _mapper in _Base.registry.mappers:
                _tbl = _mapper.local_table
                if _tbl is None or _tbl.name == "bots":
                    continue
                for _col in _tbl.columns:
                    if not any(_fk.column.table.name == "bots"
                               for _fk in _col.foreign_keys):
                        continue
                    # ستونِ nullable را NULL می‌کنیم (سابقهٔ مالی حفظ شود)،
                    # ستونِ NOT NULL را چاره‌ای جز حذفِ ردیف نیست.
                    if _col.nullable:
                        _sql = (f'UPDATE "{_tbl.name}" SET "{_col.name}" = NULL '
                                f'WHERE "{_col.name}" = :bid')
                    else:
                        _sql = f'DELETE FROM "{_tbl.name}" WHERE "{_col.name}" = :bid'
                    try:
                        async with s.begin_nested():
                            _res = await s.execute(_text(_sql), {"bid": bot_id})
                        if _res.rowcount:
                            _swept.append(f"{_tbl.name}.{_col.name}={_res.rowcount}")
                    except Exception as _e:
                        print(f"purge sweep skip {_tbl.name}.{_col.name}:", _e)
            if _swept:
                # این لاگ یعنی یک جدول از لیست‌هایِ models.py جا افتاده —
                # اسمش را به TENANT_OWNED_MODELS اضافه کن.
                print(f"purge_bot[{bot_id}]: جدول‌های خارج از لیست پاک شدند: {_swept}")
            await s.flush()
        except Exception as e:
            raise RuntimeError(f"خطا در پاک‌سازیِ ارجاع‌هایِ باقی‌مانده: {e}") from e

        try:
            await s.delete(r)
            await s.commit()
        except Exception as e:
            await s.rollback()
            raise RuntimeError(f"خطا در حذف نهایی ردیف ربات: {e}") from e
    return token


async def migrate_bot_data(old_bot_id: int, new_bot_id: int) -> None:
    """انتقال تمام دادهٔ متعلق به یک ربات (TENANT_OWNED_MODELS) به ربات دیگر.
    برای تکمیل «تعویض توکن» — وقتی مالک ربات تلگرامی تازه‌ای ساخته و باید
    کل دادهٔ ربات قدیمش (مشتری‌ها، کیف پول، سفارش‌ها، انبار و ...) را داشته باشد.

    === PATCH_MIGRATE_BOT_DATA_ISOLATION_V1 ===
    قبلاً همهٔ جدول‌ها تو یه Session/تراکنشِ واحد آپدیت می‌شدن؛ تو Postgres،
    به‌محضِ خطاخوردنِ یه statement وسطِ تراکنش، کلِ تراکنش «aborted» می‌شه و
    هر دستورِ دیگه‌ای بعدش (حتی موفق) رد می‌شه — یعنی اگه یه جدول خطا
    می‌داد، همهٔ جدول‌های *بعدیِ* لیست هم بی‌سروصدا منتقل نمی‌شدن (فقط یه
    پرینت تو لاگِ سرور، بدونِ هیچ خطای قابل‌مشاهده). الان هر جدول Session
    جدا و مستقلِ خودش رو داره تا خطای یکی، بقیه رو خراب نکنه.
    """
    from sqlalchemy import update as _update
    from models import TENANT_OWNED_MODELS

    failed = []
    for M in TENANT_OWNED_MODELS:
        try:
            async with Session() as s:
                await s.execute(
                    _update(M).where(M.bot_id == old_bot_id).values(bot_id=new_bot_id)
                )
                await s.commit()
        except Exception as e:
            failed.append(getattr(M, "__name__", str(M)))
            print(f"migrate_bot_data: skip {getattr(M, '__name__', M)}:", e)
    if failed:
        print(f"migrate_bot_data: جدول‌هایی که منتقل نشدن: {failed}")


async def redirect_all_bot_id_references(old_bot_id: int, new_bot_id: int) -> None:
    """=== PATCH_REDIRECT_ALL_BOT_REFERENCES_V1 ===
    برای «تعویض توکن»: بعد از migrate_bot_data (که فقط جدول‌های
    TENANT_OWNED_MODELS با ستونِ دقیقاً به‌نامِ bot_id رو منتقل می‌کنه)،
    این تابع **همهٔ** ستون‌هایی که به bots.id اشاره می‌کنن رو — با هر
    اسمی (مثلاً target_bot_id تو PlanOrder، یا old_bot_id/new_bot_id تو
    TokenSwapRequest/OwnerTransferRequest) — پیدا و به رباتِ جدید ارجاع
    می‌ده. این باعث می‌شه دیگه هیچ‌وقت جدولی به‌خاطرِ ازقلم‌افتادن از
    یه لیستِ دستی، جلوی حذفِ رباتِ قدیم رو نگیره — چون این تابع خودش از
    رویِ اسکیمای واقعی (نه یه لیستِ ثابتِ دستی) کارش رو انجام می‌ده.
    """
    from sqlalchemy import update as _update, inspect as _inspect
    from models import Base, Bot as _BotModel

    failed = []
    for mapper in Base.registry.mappers:
        M = mapper.class_
        if M is _BotModel:
            continue
        for col in mapper.columns:
            for fk in col.foreign_keys:
                if fk.target_fullname == "bots.id":
                    attr_name = col.key
                    try:
                        async with Session() as s:
                            await s.execute(
                                _update(M)
                                .where(getattr(M, attr_name) == old_bot_id)
                                .values(**{attr_name: new_bot_id})
                            )
                            await s.commit()
                    except Exception as e:
                        failed.append(f"{getattr(M, '__name__', M)}.{attr_name}")
                        print(f"redirect_all_bot_id_references: skip "
                              f"{getattr(M, '__name__', M)}.{attr_name}:", e)
    if failed:
        print(f"redirect_all_bot_id_references: منتقل نشدن: {failed}")
