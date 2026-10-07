"""
vpn_panel.py — کلاینت async پنل سنایی/3X-UI برای بات‌ساز (چند‌مستأجری).

برگرفته از نسخهٔ تولیدیِ آزموده‌شده، اما به‌جای خواندن از config سراسری،
اعتبارنامهٔ پنلِ هر «مستأجر» را در سازنده می‌گیرد تا هر فروشندهٔ VPN
پنل اختصاصی خودش را داشته باشد.

مسیرهای API (مطابق OpenAPI رسمی 3X-UI):
  لاگین:        GET <base>/  → csrf-token از HTML  →  POST <base>/login
  لیست inbound: GET  /panel/api/inbounds/list
  ساخت:         POST /panel/api/clients/add
  گرفتن:        GET  /panel/api/clients/get/{email}
  ترافیک:       GET  /panel/api/clients/traffic/{email}
  تمدید/حجم:    POST /panel/api/clients/bulkAdjust
  ریست مصرف:    POST /panel/api/clients/resetTraffic/{email}
  حذف:          POST /panel/api/clients/del/{email}?keepTraffic=0
  لینک ساب:     GET  /panel/api/clients/subLinks/{subId}
  لینک کلاینت:  GET  /panel/api/clients/links/{email}

نکات:
  - کلاینت با «email» (شناسهٔ یکتا) شناخته می‌شود.
  - totalGB بایت؛ 0 = نامحدود.
  - expiryTime میلی‌ثانیه؛ 0 = بی‌انقضا. این کلاینت همیشه timestamp مطلق
    (از لحظهٔ ساخت/تمدید) می‌فرستد، نه مقدار منفی (که در 3X-UI به معنی
    «انقضا از اولین اتصال» است) — چون مدت نمایش‌داده‌شده به کاربر از زمان خرید
    حساب می‌شود.
"""
from __future__ import annotations

import json
import time
import uuid
import string
import secrets
import asyncio
import os
import re
from urllib.parse import urlsplit  # === PATCH_LOGIN_ROOT_FALLBACK_V1 ===

try:
    import aiohttp
except ImportError:  # تا زمانی که نصب نشده، ایمپورت ماژول خطا ندهد
    aiohttp = None

GB = 1024 * 1024 * 1024

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

# === PATCH_SLOW_PANEL_PROGRESS_V1 ===
# {base_url: [تعدادِ کند, مجموعِ ثانیه‌ها, بیشینه]}
_SLOW_PANELS: dict[str, list] = {}


def _note_slow(base_url: str, seconds: float) -> None:
    e = _SLOW_PANELS.setdefault(base_url or "?", [0, 0.0, 0.0])
    e[0] += 1
    e[1] += seconds
    e[2] = max(e[2], seconds)


def panel_health() -> list[dict]:
    """گزارشِ پنل‌هایِ کند از زمانِ بالا آمدنِ سرویس — بدترین اول."""
    out = [{"panel": k, "slow_ops": v[0],
            "avg_sec": round(v[1] / max(1, v[0]), 1), "max_sec": round(v[2], 1)}
           for k, v in _SLOW_PANELS.items()]
    return sorted(out, key=lambda d: -d["slow_ops"])


# === PATCH_PANEL_JSON_OFFLOOP_V1 ===
# لاگِ سرور: «event loop blocked ~1s» با پشته‌یِ _request_inner — پاسخِ لیستِ inbound/کاربرانِ پنل‌هایِ
# بزرگ چند مگابایت است و json.loads رویِ همان رشتهٔ اصلیِ asyncio اجرا می‌شد؛ در آن مدت هیچ رباتی به
# هیچ پیامی جواب نمی‌داد. پاسخِ بزرگ حالا در یک رشتهٔ جدا پارس می‌شود (پاسخِ کوچک مثلِ قبل، بی‌هزینه).
_JSON_OFFLOOP_BYTES = 200_000


async def loads_json_async(text: str):
    if len(text) >= _JSON_OFFLOOP_BYTES:
        return await asyncio.to_thread(json.loads, text)
    return json.loads(text)


class PanelError(Exception):
    pass


# === PATCH_FRONTEND_404_HINT_V1 ===
# گزارشِ واقعیِ یه مستأجر: لاگین رویِ پنلِ نودکس با HTTP 404 شکست خورد و
# بدنه‌ی پاسخ این بود: {"error":true,"url":...,"statusCode":404,
# "statusMessage":"Page not found: ..."} — این دقیقاً شکلِ صفحه‌ی ۴۰۴
# خودِ فریمورک‌هایِ Nuxt/Nitro (و مشابه‌هاشون مثلِ Next.js/Express با
# میان‌افزارِ JSON 404) است، نه پاسخِ واقعیِ API پنل. یعنی آدرسی که وارد
# شده احتمالاً آدرسِ داشبوردِ وبِ پنله (frontend)، نه آدرسِ واقعیِ API —
# خیلی از پنل‌های سفارشی این دو رو رویِ دو زیردامنه‌ی جدا می‌ذارن (مثلاً
# app.دامنه برایِ داشبورد در برابرِ api.دامنه برایِ API). این تابع رو همه‌ی
# کلاینت‌هایِ پنل (marzban_panel.py، nodex_panel.py و بقیه) می‌تونن رویِ
# بدنه‌ی هر پاسخِ ۴۰۴ صدا بزنن تا این حدسِ خیلی‌محتمل رو به‌جایِ پیامِ
# مبهمِ همیشگی نشون بدن.
def frontend_404_hint(body_text: str) -> str:
    """اگه body شکلِ ۴۰۴ی یه سایتِ معمولی (نه API پنل) باشه، یه توضیحِ
    قابلِ‌اقدام برمی‌گردونه؛ وگرنه رشتهٔ خالی."""
    try:
        p = json.loads(body_text)
    except Exception:
        return ""
    if isinstance(p, dict) and "statusCode" in p and ("statusMessage" in p or "message" in p):
        return (
            "\n\n💡 این پاسخ شکلِ صفحه‌ی خطایِ یک سایتِ معمولیه، نه پاسخِ API پنل. "
            "احتمالاً آدرسی که وارد کردی، آدرسِ داشبوردِ وبِ پنله (چیزی که تو "
            "مرورگر بازش می‌کنی)، نه آدرسِ واقعیِ API. خیلی از پنل‌ها این دو تا "
            "رو رویِ دو زیردامنه‌ی جدا می‌ذارن (مثلاً app.… برایِ داشبورد و "
            "api.… برایِ API) — با پشتیبانیِ پنل چک کن آدرسِ درستِ API چیه.")
    return ""


# === PATCH_LOGIN_ROOT_FALLBACK_V1 ===
def is_static_frontend_405(body_text: str) -> bool:
    """تشخیصِ امضایِ خاصِ ۴۰۵ی که Starlette's StaticFiles برمی‌گردونه وقتی
    یه متدِ غیر از GET/HEAD به یه مسیرِ فایلِ استاتیک بخوره — دقیقاً
    {"detail": "Method Not Allowed"}, بدونِ هیچ کلیدِ دیگه.

    گزارشِ واقعی: بعضی پنل‌ها برایِ دورزدنِ فیلترینگ، فایل‌هایِ فرانت‌اند
    (صفحهٔ لاگین) رو زیرِ یه مسیرِ ظاهراً بی‌ربط camouflage می‌کنن (مثلاً
    یه مسیرِ فونت)، ولی خودِ API همیشه رویِ ریشهٔ دامنه می‌مونه. وقتی
    فروشنده آدرسِ همون صفحهٔ camouflage‌شده رو به‌عنوانِ base_url می‌ده،
    POSTِ لاگین به جایِ اینکه به API بخوره، به همون مسیرِ فایلِ استاتیک
    می‌خوره — و چون StaticFiles فقط GET/HEAD قبول می‌کنه، ۴۰۵ برمی‌گردونه.
    این تابع همون امضایِ خاص رو تشخیص می‌ده تا بشه خودکار با ریشهٔ دامنه
    دوباره امتحان کرد، به‌جایِ اینکه فروشنده مجبور بشه دستی حدس بزنه."""
    try:
        p = json.loads(body_text)
    except Exception:
        return False
    return isinstance(p, dict) and set(p.keys()) == {"detail"} and p.get("detail") == "Method Not Allowed"


def login_domain_root(base_url: str) -> str:
    """ریشهٔ دامنه (scheme://netloc) رو از یه base_url برمی‌گردونه؛ اگه
    خودِ base_url از قبل ریشه بود (بدونِ مسیرِ اضافه)، رشتهٔ خالی
    برمی‌گردونه (یعنی چیزی برایِ فال‌بک نیست)."""
    try:
        p = urlsplit(base_url)
        if not p.scheme or not p.netloc:
            return ""
        root = f"{p.scheme}://{p.netloc}"
        return root if root != base_url.rstrip("/") else ""
    except Exception:
        return ""


def _rand(n=16):
    a = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(a) for _ in range(n))


def gen_sub_id(n=16):
    return _rand(n)


def gen_uuid():
    return str(uuid.uuid4())


class PanelClient:
    """یک نشست به پنل یک مستأجر. اعتبارنامه در سازنده داده می‌شود."""

    def __init__(self, base_url: str, username: str, password: str,
                 sub_base: str = "", inbound_ids=None, verify_ssl: bool = False,
                 timeout: int = 12):  # PATCH_PANEL_FAILFAST_V1 (بود ۲۵)
        if aiohttp is None:
            raise PanelError("کتابخانهٔ aiohttp نصب نیست. `pip install aiohttp` را اجرا کن.")
        # === PATCH_STRIP_URL_FRAGMENT_V1 ===
        # نگاه کن به توضیحِ کامل در tenant/marzban_panel.py.
        self.base_url = (base_url or "").split("#", 1)[0].rstrip("/")
        self.username = username
        self.password = password
        # === PATCH_SUB_BASE_SANITIZE_V1 ===
        _sb = (sub_base or "").strip().strip("ـ-—–_ ").rstrip("/")
        if _sb and not _sb.lower().startswith(("http://", "https://")):
            _sb = ""
        self.sub_base = _sb
        self.inbound_ids = [int(i) for i in inbound_ids] if inbound_ids else []
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = None
        self._logged_in = False
        self._csrf = None
        self._lock = asyncio.Lock()
        # === PATCH_PANEL_LEAK_TRACKER_V1 ===
        # ردیابیِ «Unclosed client session»: با BOTSAZ_TRACK_PANEL_LEAKS=1
        # محلِ ساختِ هر کلاینت ذخیره می‌شود تا اگر بدونِ close() جمع‌آوری شد،
        # دقیقاً بگوییم کدام تابع آن را ساخته بود. پیش‌فرض خاموش (بی‌هزینه).
        self._birth_stack = None
        if os.environ.get("BOTSAZ_TRACK_PANEL_LEAKS") == "1":
            import traceback as _tb
            self._birth_stack = "".join(_tb.format_stack()[-6:-1])

    def __del__(self):
        try:
            if self._session is not None and not self._session.closed:
                logger.warning(
                    "نشتیِ کلاینتِ پنل (close() صدا زده نشد) — %s\n%s",
                    self.base_url, self._birth_stack or
                    "برای دیدنِ محلِ ساخت: BOTSAZ_TRACK_PANEL_LEAKS=1")
        except Exception:
            # __del__ حینِ خاموش‌شدنِ مفسر هم صدا زده می‌شود که آن‌موقع حتی
            # logger هم ممکن است از بین رفته باشد؛ اینجا سکوت عمدی است.
            return

    # === PATCH_PANEL_FAILFAST_V1 ===
    # وقتی پنلِ مشتری خاموش/فیلتر است، هر تپِ کاربر ۵۰ تا ۶۵ ثانیه ربات را
    # روی همان آپدیت نگه می‌داشت (تایم‌اوتِ ۲۵ثانیه‌ای × لاگینِ اجباری ×
    # تلاشِ دوباره) و صفِ آپدیت‌ها را پر می‌کرد — علتِ اصلیِ «کندیِ ربات».
    # حالا: تایم‌اوتِ کوتاه‌تر، اتصالِ ۶ثانیه‌ای، بدونِ تلاشِ دوبارهٔ کورِ
    # روی تایم‌اوت، و یک «فیوز» که پس از یک شکست تا ۶۰ ثانیه بلافاصله خطا
    # می‌دهد به‌جای منتظرماندن.
    _fuse: dict[str, float] = {}
    FUSE_SEC = 60

    def _fuse_check(self):
        import time as _t
        until = PanelClient._fuse.get(self.base_url, 0)
        if until > _t.monotonic():
            raise PanelError(
                "پنل چند لحظه پیش پاسخ نداد؛ کمی بعد دوباره تلاش کن "
                "(اتصال/آدرسِ پنل را هم بررسی کن).")

    def _fuse_trip(self):
        import time as _t
        PanelClient._fuse[self.base_url] = _t.monotonic() + PanelClient.FUSE_SEC

    def _fuse_clear(self):
        PanelClient._fuse.pop(self.base_url, None)

    # ───────── نشست ─────────
    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            # PATCH_PANEL_FAILFAST_V1: اتصالِ ۶ثانیه‌ای تا سرورِ خاموش سریع رد شود
            t = aiohttp.ClientTimeout(total=self.timeout, sock_connect=6)
            # === PATCH_PANEL_POOL_KEEPALIVE_V1 ===
            # حالا که نشست بینِ خریدها زنده می‌ماند (panel_pool)، یک خطرِ
            # تازه پیدا می‌شود: سوکتی که چند دقیقه بی‌استفاده مانده ممکن
            # است از سمتِ پنل یا فایروال بسته شده باشد، ولی aiohttp هنوز
            # آن را «زنده» بداند. درخواستِ بعدی رویِ سوکتِ مرده می‌رود و تا
            # سقفِ تایم‌اوت صبر می‌کند — یعنی دقیقاً همان کندی‌ای که
            # می‌خواستیم حذف کنیم، بدتر.
            #
            # keepalive_timeout=60 یعنی aiohttp سوکتِ بی‌استفادهٔ بیش از یک
            # دقیقه را خودش دور می‌ریزد و درخواستِ بعدی اتصالِ تازه می‌گیرد.
            # این عدد از idle-timeoutِ معمولِ nginx/cloudflare کمتر است.
            conn = aiohttp.TCPConnector(ssl=self.verify_ssl,
                                        keepalive_timeout=60, limit=20)
            # unsafe=True لازم است تا cookie روی IP مستقیم (بدون دامنه) کار کند
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(timeout=t, connector=conn, cookie_jar=jar)
        return self._session

    async def _fetch_csrf(self):
        s = await self._ensure_session()
        try:
            async with s.get(f"{self.base_url}/") as r:
                html = await r.text()
            m = (re.search(r'name="csrf-token"\s*content="([^"]+)"', html) or
                 re.search(r'content="([^"]+)"\s*name="csrf-token"', html))
            if m:
                self._csrf = m.group(1)
            return self._csrf
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def login(self, force=False):
        async with self._lock:
            if self._logged_in and not force:
                return True
            self._fuse_check()  # PATCH_PANEL_FAILFAST_V1
            s = await self._ensure_session()
            await self._fetch_csrf()
            origin = self.base_url.rsplit("/", 1)[0] if "/" in self.base_url.split("://", 1)[-1] else self.base_url
            headers = {
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/json",
                "Origin": origin,
                "Referer": f"{self.base_url}/",
            }
            if self._csrf:
                headers["X-CSRF-Token"] = self._csrf
            data = {"username": self.username, "password": self.password}
            data.update(getattr(self, "_LOGIN_EXTRA", None) or {})   # PATCH_DEGERIS_PANEL_V1
            try:
                async with s.post(f"{self.base_url}/login", json=data, headers=headers) as r:
                    text = await r.text()
                    if r.status != 200:
                        raise PanelError(f"login HTTP {r.status}: {text[:200]}")
                    try:
                        p = json.loads(text)
                    except Exception:
                        p = {}
                    if not p.get("success"):
                        raise PanelError("ورود به پنل ناموفق بود؛ آدرس/نام‌کاربری/رمز را بررسی کن.")
                    self._logged_in = True
                    self._fuse_clear()  # PATCH_PANEL_FAILFAST_V1
                    return True
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self._fuse_trip()  # PATCH_PANEL_FAILFAST_V1
                raise PanelError(f"خطای اتصال به پنل (تایم‌اوت/قطعی): {e}") from e

    async def _request(self, method, path, **kwargs):
        # === PATCH_PANEL_TIMING_LOG_V1 ===
        # «کندیِ ساختِ ساب» را دو بار حدس زدیم و هر دو بار بدونِ اندازه‌گیری.
        # این‌جا تنها گلوگاهی است که همهٔ ترافیکِ پنل از آن رد می‌شود، پس
        # زمان را همین‌جا می‌شماریم و تفکیک می‌کنیم:
        #     login=…  → هزینهٔ ورودِ دوباره (چیزی که استخر باید حذفش کند)
        #     req=…    → خودِ درخواستِ مفید
        # اگر login همیشه ۰ باشد یعنی استخر کار می‌کند و کندی جایِ دیگری
        # است؛ اگر نه، یعنی نشست‌ها زنده نمی‌مانند.
        import time as _t
        _t0 = _t.monotonic()
        s = await self._ensure_session()
        _relogin = False
        if not self._logged_in:
            _relogin = True
            await self.login()
        _t_login = _t.monotonic() - _t0
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("X-Requested-With", "XMLHttpRequest")
        headers.setdefault("Accept", "application/json")
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        kwargs["headers"] = headers
        url = f"{self.base_url}{path}"
        _t1 = _t.monotonic()
        try:
            return await self._request_inner(s, method, path, url, kwargs)
        finally:
            _total = _t.monotonic() - _t0
            if _total >= 2.0:      # فقط کندها، تا لاگ شلوغ نشود
                logger.warning(
                    "پنلِ کند: %s %s | مجموع=%.1fs (ورود=%.1fs%s، درخواست=%.1fs) | %s",
                    method, path, _total, _t_login,
                    "، ورودِ تازه" if _relogin else "، نشستِ مشترک",
                    _t.monotonic() - _t1, self.base_url)
                _note_slow(self.base_url, _total)

    # === PATCH_SLOW_PANEL_PROGRESS_V1 ===
    # شمارندهٔ سبکِ درون‌حافظه‌ای برایِ سلامتِ هر پنل. هدفش این است که
    # به‌جایِ اینکه ما هر بار لاگ بخوانیم، خودِ سیستم بداند کدام پنل مزمناً
    # کند است. `panel_health()` را هر جایی (مثلاً پنلِ ادمین) می‌شود خواند.

    async def _request_inner(self, s, method, path, url, kwargs):
        for attempt in range(2):
            try:
                async with s.request(method, url, **kwargs) as r:
                    text = await r.text()
                    if r.status in (401, 403) and attempt == 0:
                        await self.login(force=True)
                        if self._csrf:
                            kwargs["headers"]["X-CSRF-Token"] = self._csrf
                        continue
                    if r.status != 200:
                        raise PanelError(f"{method} {path} HTTP {r.status}: {text[:200]}")
                    try:
                        return await loads_json_async(text)   # PATCH_PANEL_JSON_OFFLOOP_V1
                    except Exception:
                        # پاسخِ غیرِJSON با استاتوس 200 معمولاً یعنی نشستِ لاگین منقضی
                        # شده و پنل به‌جای JSON صفحهٔ HTMLِ ورود را برگردانده —
                        # این را دیگر «موفق» فرض نکن (باعث می‌شد لیستِ inbound
                        # خالی/گمراه‌کننده برگردد)؛ یک‌بار با لاگینِ اجباری دوباره
                        # امتحان کن، وگرنه خطای واضح بده.
                        if attempt == 0:
                            await self.login(force=True)
                            if self._csrf:
                                kwargs["headers"]["X-CSRF-Token"] = self._csrf
                            continue
                        raise PanelError(
                            f"{method} {path}: پاسخ پنل معتبر (JSON) نبود؛ احتمالاً نشستِ "
                            f"ورود منقضی شده یا آدرس/تنظیمات پنل اشتباه است. "
                            f"نمونهٔ پاسخ: {text[:200]}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # PATCH_PANEL_FAILFAST_V1: تلاشِ دوبارهٔ کور روی تایم‌اوت فقط
                # زمانِ انتظار را دوبرابر می‌کرد؛ پنلِ قطع، با یک بار هم قطع است.
                self._fuse_trip()
                raise PanelError(f"{method} {path} خطای اتصال (تایم‌اوت/قطعی): {e}") from e
        raise PanelError(f"{method} {path} پس از تلاش مجدد ناموفق ماند")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._logged_in = False
        self._csrf = None

    async def __aenter__(self):
        await self.login()
        return self

    async def __aexit__(self, *a):
        await self.close()

    # ───────── inboundها ─────────
    async def list_inbounds(self):
        res = await self._request("GET", "/panel/api/inbounds/list")
        if not res.get("success"):
            raise PanelError(f"خطا در گرفتن لیست inbound: {res}")
        return res.get("obj", []) or []

    async def active_inbound_ids(self):
        inbounds = await self.list_inbounds()
        if self.inbound_ids:
            wanted = set(self.inbound_ids)
            ids = [int(ib["id"]) for ib in inbounds if int(ib.get("id")) in wanted]
        else:
            ids = [int(ib["id"]) for ib in inbounds if ib.get("enable", True)]
        if not ids:
            raise PanelError("هیچ inbound فعالی برای ساخت کانفیگ پیدا نشد.")
        return ids

    # ───────── ساخت ─────────
    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0,
                             on_hold=False):
        """ساخت کلاینت روی همهٔ inboundهای هدف با subId مشترک.
        gb: حجم به گیگ (0=نامحدود). mb: اگر داده شود حجم به مگابایت (سرویس تست).
        days>0 → انقضا از همین لحظه (زمان خرید) محاسبه و به‌صورت timestamp مطلق
        ثبت می‌شود؛ days=0 → بی‌انقضا.

        نکته: پنل از مقدار منفی برای «انقضا از اولین اتصال» پشتیبانی می‌کند،
        اما چون متن نمایش‌داده‌شده به کاربر («N روز») همیشه از زمان خرید
        حساب می‌شود، اینجا هم باید همان‌طور باشد — وگرنه تا وقتی کاربر برای
        اولین‌بار وصل نشود expiryTime صفر می‌ماند و در «کانفیگ‌های من» به‌اشتباه
        «بدون انقضا» نشان داده می‌شود، و واقعاً هم شمارش انقضا دیر شروع می‌شود."""
        uuid_str = uuid_str or gen_uuid()
        sub_id = sub_id or gen_sub_id()
        if inbound_ids is None:
            inbound_ids = await self.active_inbound_ids()
        if mb is not None:
            total_bytes = int(mb) * 1024 * 1024
        else:
            total_bytes = int(gb) * GB if gb else 0
        # === PATCH_TRIAL_HOURLY_V1 ===
        # 3X-UI expiryTime رو به میلی‌ثانیه ذخیره می‌کنه، پس دقتِ ساعتی/دقیقه‌ای
        # هم می‌تونیم بذاریم. total_seconds = days*86400 + hours*3600
        total_seconds = int(days) * 86400 + int(hours) * 3600
        if total_seconds:
            now_ms = int(time.time() * 1000)
            expiry = now_ms + total_seconds * 1000
        else:
            expiry = 0
        client = {
            "email": email, "id": uuid_str, "subId": sub_id, "security": "auto",
            "totalGB": total_bytes, "expiryTime": expiry, "tgId": 0,
            "limitIp": int(limit_ip), "enable": True, "reset": 0, "comment": "",
        }
        body = {"client": client, "inboundIds": inbound_ids}
        res = await self._request("POST", "/panel/api/clients/add", json=body)
        if not res.get("success"):
            raise PanelError(f"ساخت کانفیگ ناموفق: {res.get('msg', res)}")
        await self.ensure_sub_base()   # PATCH_SUB_BASE_DISCOVER_V1
        return {
            "uuid": uuid_str, "sub_id": sub_id, "email": email, "gb": gb, "days": days,
            "inbounds": inbound_ids, "sub_link": self.sub_link(sub_id),
            "expiry_ms": expiry,
        }

    # ───────── لینک‌ها ─────────
    def sub_link(self, sub_id):
        return f"{self.sub_base}/{sub_id}" if self.sub_base else ""

    # === PATCH_SUB_BASE_DISCOVER_V1 ===
    # گزارش (Mahdi): بعد از خرید «ارسال ساب لینک» می‌گفت «این سرویس ساب لینک ندارد»؛ چون آدرسِ
    # ساب در تنظیماتِ پنل خالی/نامعتبر بود. 3X-UI خودش آدرسِ ساب را در تنظیماتش دارد؛ پس اگر
    # sub_base خالی بود، از همان‌جا (POST /panel/setting/all) پیدا می‌کنیم.
    async def discover_sub_base(self) -> str:
        cached = getattr(self, "_disc_sub_base", None)
        if cached is not None:
            return cached
        out = ""
        # ۱) مرجعِ رسمیِ 3X-UI v3: POST /panel/api/setting/defaultSettings → subURI (آدرسِ کاملِ ساب با
        #    مسیر، دقیقاً همان که خودِ پنل در «اطلاعات کلاینت» نشان می‌دهد؛ لینک = subURI + subId).
        try:
            res = await self._request("POST", "/panel/api/setting/defaultSettings")
            o = (res or {}).get("obj") or {}
            uri = (o.get("subURI") or "").strip()
            if o.get("subEnable", True) and uri.lower().startswith(("http://", "https://")):
                out = uri.rstrip("/")
        except Exception:
            logger.info("defaultSettings پنل در دسترس نبود؛ روشِ بعدی (%s)", self.base_url)
        # ۲) fallback: تنظیماتِ کامل و ساختنِ آدرس مثلِ BuildSubURIBase پنل
        if not out:
            for path in ("/panel/api/setting/all", "/panel/setting/all"):
                try:
                    res = await self._request("POST", path)
                except Exception:
                    continue
                o = (res or {}).get("obj") or {}
                if not o:
                    continue
                if not o.get("subEnable", True):
                    break
                uri = (o.get("subURI") or "").strip()
                if uri.lower().startswith(("http://", "https://")):
                    out = uri.rstrip("/")
                    break
                from urllib.parse import urlparse
                host = (o.get("subDomain") or "").strip() or (urlparse(self.base_url).hostname or "")
                port = str(o.get("subPort") or "").strip()
                path_ = (o.get("subPath") or "/sub/").strip().strip("/")
                tls = bool((o.get("subCertFile") or "").strip()) and bool((o.get("subKeyFile") or "").strip())
                if host:
                    default = "443" if tls else "80"
                    hp = host if (not port or port == default) else f"{host}:{port}"
                    out = f"{'https' if tls else 'http'}://{hp}/{path_}".rstrip("/")
                break
        self._disc_sub_base = out
        return out

    async def ensure_sub_base(self) -> None:
        if not self.sub_base:
            found = await self.discover_sub_base()
            if found:
                self.sub_base = found

    async def get_sub_links(self, sub_id):
        res = await self._request("GET", f"/panel/api/clients/subLinks/{sub_id}")
        return res.get("obj", []) or []

    async def get_client_links(self, email):
        res = await self._request("GET", f"/panel/api/clients/links/{email}")
        return res.get("obj", []) or []

    async def _fetch_links_from_sub(self, sub_url: str) -> list[str]:
        """=== PATCH_SUB_UNIFIED_V1 === به sub_fetch.fetch_sub_links واگذار شد:
        نشستِ تازه (بدونِ توکنِ پنل)، چند UA روی ۴۰۳/۵xx، و تبدیلِ JSONِ Xray به لینک."""
        from tenant.shared.panel_clients.sub_fetch import fetch_sub_links
        return await fetch_sub_links(sub_url)

    # ───────── خواندن ─────────
    # === PATCH_SANAEI_CLIENT_ID_FIX_MARKER ===
    async def get_client(self, email):
        try:
            res = await self._request("GET", f"/panel/api/clients/get/{email}")
        except PanelError:
            return None
        if not (res.get("success") and res.get("obj")):
            return None
        obj = res["obj"]

        # شکل قدیمی: {"client": {...}}  — id همیشه رشته (UUID) است.
        if isinstance(obj, dict) and "client" in obj and isinstance(obj["client"], dict):
            inner = dict(obj["client"])
            inner["inboundIds"] = obj.get("inboundIds") or obj.get("inbounds") or []
            return inner

        # شکل جدید: رکورد خام inbound با settings به‌صورت رشتهٔ JSON.
        # اینجا obj["id"] شناسهٔ عددیِ inbound است، نه UUID کلاینت —
        # نباید مستقیم return شود وگرنه در آپدیت بعدی به‌جای رشته عدد
        # فرستاده می‌شود و پنل با خطای unmarshal رد می‌کند.
        if isinstance(obj, dict):
            raw_settings = obj.get("settings")
            clients = None
            if isinstance(raw_settings, str):
                try:
                    parsed = json.loads(raw_settings)
                    clients = parsed.get("clients")
                except (ValueError, AttributeError):
                    clients = None
            elif isinstance(obj.get("clients"), list):
                clients = obj.get("clients")

            if isinstance(clients, list):
                for c in clients:
                    if isinstance(c, dict) and c.get("email") == email:
                        inner = dict(c)
                        inner["id"] = str(inner.get("id") or inner.get("uuid") or "")
                        inner["inboundIds"] = [obj.get("id")] if obj.get("id") is not None else []
                        return inner
                return None  # اینباند پیدا شد ولی این email داخلش نبود

            # آخرین راه‌حل: اگر واقعاً چیزی برای تطبیق نبود، id مشکوک
            # (عددی) را حذف می‌کنیم تا لااقل باعث خطای unmarshal نشود؛
            # _merge_current از uuid ترافیک به‌عنوان جایگزین استفاده می‌کند.
            safe = dict(obj)
            if not isinstance(safe.get("id"), str):
                safe.pop("id", None)
            return safe

        return obj

    async def get_usage(self, email):
        """آمار مصرف: up, down, total(سقف), expiryTime, enable, ..."""
        res = await self._request("GET", f"/panel/api/clients/traffic/{email}")
        if res.get("success") and res.get("obj"):
            return res["obj"]
        return None

    # ───────── تمدید/ترافیک ─────────
    async def _bulk_adjust(self, email, add_days=0, add_bytes=0):
        body = {"emails": [email], "addDays": int(add_days), "addBytes": int(add_bytes)}
        res = await self._request("POST", "/panel/api/clients/bulkAdjust", json=body)
        if not res.get("success"):
            raise PanelError(f"تمدید/افزایش حجم ناموفق: {res.get('msg', res)}")
        return res.get("obj", {})

    async def add_traffic(self, email, gb):
        return await self._bulk_adjust(email, add_bytes=int(gb) * GB)

    async def renew(self, email, days, reset_usage=False):
        obj = await self._bulk_adjust(email, add_days=int(days))
        if reset_usage:
            await self.reset_usage(email)
        return obj

    async def reset_usage(self, email):
        res = await self._request("POST", f"/panel/api/clients/resetTraffic/{email}", json={})
        return bool(res.get("success"))

    async def reset_sub(self, email, new_sub_id=None, rotate_uuid: bool = True):
        """
        === PATCH_RESET_SUB_ROTATE_UUID_V1 ===
        عوض‌کردنِ فقط subId کافی *نیست*: کلاینتِ کاربر (v2rayNG و…) بعدِ
        اولین آپدیت، خودِ کانفیگ‌ها را ذخیره کرده و با UUID وصل می‌شود، نه
        با لینکِ ساب. پس با تعویضِ صرفِ subId، لینکِ قدیمی از کار می‌افتد
        ولی اتصالِ قبلی همچنان برقرار می‌ماند — دقیقاً همان چیزی که
        کاربران گزارش می‌کردند. برای اینکه «تغییر لینک» واقعاً دسترسیِ
        قبلی را قطع کند، UUID هم چرخانده می‌شود.

        خروجی: (لینکِ جدید، uuid جدید یا None اگر نچرخیده باشد)
        """
        new_sub_id = new_sub_id or gen_sub_id()
        body = await self._merge_current(email)
        body["subId"] = new_sub_id
        new_uuid = None
        if rotate_uuid:
            new_uuid = gen_uuid()
            body["id"] = new_uuid
        res = await self._request("POST", f"/panel/api/clients/update/{email}", json=body)
        if not res.get("success") and rotate_uuid:
            # بعضی نسخه‌های 3X-UI اجازهٔ تعویضِ id را نمی‌دهند؛ در آن صورت
            # دستِ‌کم لینک را عوض می‌کنیم تا کاربر کاملاً بی‌نتیجه نماند.
            logger.warning("چرخاندنِ UUID ناموفق بود (%s)؛ فقط subId عوض می‌شود.",
                           res.get("msg", res))
            body = await self._merge_current(email)
            body["subId"] = new_sub_id
            new_uuid = None
            res = await self._request("POST", f"/panel/api/clients/update/{email}", json=body)
        if not res.get("success"):
            raise PanelError(f"ریست لینک ناموفق: {res.get('msg', res)}")
        return self.sub_link(new_sub_id), new_uuid, new_sub_id

    async def rename_account(self, old_email: str, new_email: str):
        """تغییر «نام سرور» (email/شناسهٔ نمایشی) یک کلاینت در 3X-UI.

        در 3X-UI همان فیلد email است که به‌عنوان نام سرور به مشتری نمایش داده
        می‌شود. مسیر درخواست با email قدیمی است و email جدید در بدنه قرار می‌گیرد.
        subId و uuid حفظ می‌شوند تا لینک ساب کاربر تغییری نکند.
        """
        if not new_email or new_email == old_email:
            return False
        body = await self._merge_current(old_email)
        body["email"] = new_email
        res = await self._request(
            "POST", f"/panel/api/clients/update/{old_email}", json=body)
        if not res.get("success"):
            raise PanelError(f"تغییر نام ناموفق: {res.get('msg', res)}")
        return True

    # === PATCH_SVC_DISABLE_MARKER_PANEL ===
    async def set_enabled(self, email, enabled: bool):
        """فعال/غیرفعال کردن سریع کلاینت (بدون حذف). غیرفعال یعنی سرویس فوراً
        روی پنل قطع می‌شود و کاربر دیگر نمی‌تواند وصل شود؛ فعال‌کردن دوباره
        بلافاصله وصل بودن را برمی‌گرداند. بقیهٔ اطلاعات (حجم/انقضا/UUID/subId)
        دست‌نخورده می‌ماند."""
        body = await self._merge_current(email)
        body["enable"] = bool(enabled)
        res = await self._request(
            "POST", f"/panel/api/clients/update/{email}", json=body)
        if not res.get("success"):
            raise PanelError(f"تغییر وضعیت فعال/غیرفعال ناموفق: {res.get('msg', res)}")
        return True

    async def _merge_current(self, email):
        cur = await self.get_client(email) or {}
        tr = await self.get_usage(email) or {}
        # اولویت با uuid صریح (همیشه رشته)؛ سپس id فقط اگر خودش رشته
        # باشد؛ در غیر این صورت هیچ‌وقت عدد را مستقیم نمی‌فرستیم — پنل
        # فیلد Client.id را string می‌خواهد و عدد باعث خطای unmarshal
        # می‌شود («json: cannot unmarshal number into ... Client.id»).
        raw_id = cur.get("uuid") or tr.get("uuid") or cur.get("id")
        client_id = str(raw_id) if raw_id not in (None, "") else None
        return {
            "email": email,
            "id": client_id,
            "subId": cur.get("subId") or tr.get("subId"),
            "security": cur.get("security", "auto"),
            "totalGB": int(cur.get("totalGB", tr.get("total", 0)) or 0),
            "expiryTime": int(cur.get("expiryTime", tr.get("expiryTime", 0)) or 0),
            "limitIp": int(cur.get("limitIp", 0) or 0),
            "enable": bool(cur.get("enable", tr.get("enable", True))),
            "tgId": 0, "reset": 0, "comment": cur.get("comment", ""),
        }

    async def delete_account(self, email, keep_traffic=False):
        kt = 1 if keep_traffic else 0
        res = await self._request(
            "POST", f"/panel/api/clients/del/{email}?keepTraffic={kt}", json={})
        return bool(res.get("success"))

    # ───────── آمار ─────────
    async def list_clients(self):
        res = await self._request("GET", "/panel/api/clients/list")
        if not res.get("success"):
            raise PanelError(f"خطا در لیست کلاینت‌ها: {res}")
        return res.get("obj", []) or []

    async def test_connection(self):
        """فقط برای تست تنظیمات پنل توسط فروشنده: True اگر لاگین و لیست inbound موفق بود."""
        # === PATCH_LOGIN_THROTTLE_V1 ===
        # عیناً همون فیکسِ marzban_panel.py: قبلاً force=True بود، یعنی
        # panel_watchdog.py (هر ۵ دقیقه، برایِ همیشه) هر بار یک لاگینِ
        # واقعی می‌زد. رویِ 3x-ui یک قفلِ ضدِ بروت‌فورس هست که با هر
        # لاگینِ پشتِ‌سرِهم تمدید می‌شه؛ نتیجه این بود که خودِ چک‌کردنِ
        # سلامتِ پنل، قفل رو تمدید نگه می‌داشت و باعثِ «قطع می‌شه، وصل
        # می‌شه، دوباره قطع می‌شه» (فلپینگِ) گزارش‌شده توسطِ ادمین‌ها
        # می‌شد. حالا اگه توکنِ کش‌شده معتبره همون استفاده می‌شه؛ اگه
        # واقعاً باطل باشه، active_inbound_ids خودش با 401/403 لاگینِ
        # تازه رو trigger می‌کنه (دقیقاً مثلِ بقیه‌ی درخواست‌ها).
        await self.login()
        ids = await self.active_inbound_ids()
        return {"ok": True, "inbounds": ids}


def usage_summary(usage: dict) -> str:
    """خلاصهٔ خوانا از مصرف برای نمایش به کاربر."""
    if not usage:
        return "نامشخص"
    up = int(usage.get("up", 0) or 0)
    down = int(usage.get("down", 0) or 0)
    cap = int(usage.get("total", 0) or 0)
    used = up + down
    used_gb = round(used / GB, 2)
    if cap:
        cap_gb = round(cap / GB, 2)
        left_gb = round(max(cap - used, 0) / GB, 2)
        vol = f"{used_gb} از {cap_gb} گیگ مصرف شده (باقی‌مانده {left_gb} گیگ)"
    else:
        vol = f"{used_gb} گیگ مصرف شده (حجم نامحدود)"
    return vol
