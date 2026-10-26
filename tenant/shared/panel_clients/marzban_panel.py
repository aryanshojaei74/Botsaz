"""
marzban_panel.py — کلاینت async پنل مرزبان (Marzban) برای بات‌ساز (چند‌مستأجری).

همان قرارداد متدی PanelClient (تنانت سنایی/3X-UI در vpn_panel.py) را پیاده‌سازی
می‌کند تا بقیهٔ کد (vpn_admin.py / vpn_user.py / main.py) بدون شاخه‌زدن روی
panel_key، فقط با dispatch ساده (tenant/vpn_panel_factory.py) از آن استفاده کند.

منبع رسمی API: https://github.com/Gozargah/Marzban
مستندات کامل OpenAPI (آینه‌شده روی Swagger/ReDoc پنل با DOCS=True):
  لاگین:            POST  /api/admin/token      (form-urlencoded: username, password)
  گرفتن inboundها:   GET   /api/inbounds          → {protocol: [{tag, protocol, network, tls, port}, ...]}
  ساخت کاربر:        POST  /api/user
  گرفتن کاربر:       GET   /api/user/{username}
  ویرایش کاربر:      PUT   /api/user/{username}
  حذف کاربر:         DELETE /api/user/{username}
  ریست مصرف:         POST  /api/user/{username}/reset
  ریست ساب:          POST  /api/user/{username}/revoke_sub
  لیست کاربران:      GET   /api/users

نکات مهم API مرزبان (طبق مستندات رسمی):
  - username باید ۳ تا ۳۲ کاراکتر، فقط حروف کوچک/عدد/زیرخط (بدون نقطه یا خط تیره).
  - expire باید UTC timestamp (ثانیه، نه میلی‌ثانیه — برخلاف 3X-UI) باشد؛ 0 یا
    null به‌معنی بی‌انقضا/بدون‌تغییر است.
  - data_limit به بایت است؛ 0 = نامحدود.
  - proxies دیکشنری از نوع پروتکل→تنظیمات (مثلاً {"vless": {}}) است.
  - inbounds دیکشنری از نوع پروتکل→لیست tag اینباند است (نه عدد!)؛ خالی یعنی
    «همهٔ اینباندها». برخلاف 3X-UI که inbound با id عددی شناخته می‌شود، در
    مرزبان شناسهٔ inbound رشتهٔ «tag» است — برای همین ستون inbound_ids در
    دیتابیس اینجا رشته‌های جداشده با کاما از تگ‌ها را نگه می‌دارد، نه اعداد.
  - پاسخ‌های خطا را FastAPI به‌صورت {"detail": "..."} برمی‌گرداند.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
import re

from tenant.shared.panel_clients.username_resolve import name_candidates, remember, resolve_username
import secrets
import string
import asyncio

try:
    import aiohttp
except ImportError:  # تا زمانی که نصب نشده، ایمپورت ماژول خطا ندهد
    aiohttp = None

from tenant.shared.panel_clients.vpn_panel import PanelError, frontend_404_hint, is_static_frontend_405, login_domain_root  # همان کلاس خطا در کل پروژه استفاده شود
from logging_setup import get_logger as _get_logger  # PATCH_PANEL_TIMING_LOG_V1
logger = _get_logger(__name__)
# تا except PanelError در vpn_admin.py/vpn_user.py/main.py برای هر دو نوع
# پنل (سنایی و مرزبان) به‌درستی کار کند.

GB = 1024 * 1024 * 1024

# === PATCH_TOKEN_CACHE_V1 ===
# علتِ اصلیِ 429 روی endpointِ لاگین این بود که هر عملیاتِ ادمین یک client
# جدید می‌ساخت و از صفر لاگین می‌زد؛ پس هر کلیک = یک hit روی /api/admin/token.
# محافظ‌های brute-force (مثلِ cPHulk رویِ WHM) دقیقاً همین تعددِ لاگین را
# می‌شمارند و IP را موقتاً بلاک می‌کنند. راهِ درست: توکن را بین instanceها
# با کلیدِ (base_url, username) کش کنیم و تا قبلِ انقضا (exp داخلِ خودِ JWT)
# دوباره لاگین نزنیم. این فشار روی endpointِ لاگین را عملاً حذف می‌کند.
# مقدارِ هر ورودی: {"token": str, "exp": int(epoch)}.
_TOKEN_CACHE: dict[tuple[str, str], dict] = {}
# چند ثانیه قبلِ exp واقعی توکن را «منقضی» فرض می‌کنیم تا وسطِ یک درخواست
# باطل نشود.
_TOKEN_SKEW = 30

# === PATCH_LOGIN_THROTTLE_V1 ===
# ریشهٔ 429 «خودزخم» بود: چند مسیر (دکمهٔ تستِ ادمین + حلقه‌های پس‌زمینه مثل
# vpn_first_connect_loop که هر ۹۰ ثانیه اجرا می‌شود) نزدیک به هم به
# /api/admin/token می‌زدند و به سقفِ نرخِ کوتاهِ پنل (Retry-After ~۱۰s) می‌خوردند.
# راهِ درست: خودِ کلاینت لاگین‌ها را برای هر (base_url, username) از هم فاصله
# بدهد و اگر سرور 429 با Retry-After داد، تا پایانِ آن پنجره اصلاً لاگینِ
# جدید نزند. این «محدودکردنِ خودمان» است، نه دور زدنِ محافظِ سرور.
_LAST_LOGIN_TS: dict[tuple[str, str], float] = {}   # آخرین زمانِ تلاشِ لاگینِ واقعی
_BLOCK_UNTIL: dict[tuple[str, str], float] = {}      # تا این epoch لاگینِ جدید نزن
_LAST_LOGIN_ERR: dict[tuple[str, str], tuple[float, str]] = {}   # PATCH_LOGIN_FAILFAST_V1: (epoch, پیامِ آخرین شکست)
_MIN_LOGIN_INTERVAL = 12.0   # حداقل فاصلهٔ دو لاگینِ واقعی (کمی بیش از ۱۰s)
_MAX_LOGIN_WAIT = 15.0       # سقفِ صبرِ مجاز داخلِ یک درخواست


def _now_epoch() -> int:
    import time
    return int(time.time())


def _login_wait_needed(base_url: str, username: str) -> float:
    """چند ثانیه باید صبر کرد تا یک لاگینِ واقعیِ تازه مجاز شود (۰ = همین حالا)؛ همان منطقِ محدودکنندهٔ login()."""
    ck = (base_url, username)
    now = _now_epoch()
    return max(0.0, _BLOCK_UNTIL.get(ck, 0) - now, (_LAST_LOGIN_TS.get(ck, 0) + _MIN_LOGIN_INTERVAL) - now)


def _note_wait(kind: str, base_url: str, secs: float, min_secs: float) -> None:
    """انتظارِ عمدیِ کد را در شکستنِ زمانِ هر آپدیت (perfctx) ثبت و در لاگ نشان می‌دهد؛ فقط اندازه‌گیری."""
    if secs <= min_secs:
        return
    host = re.sub(r"^https?://", "", base_url or "").split("/")[0][:30]
    try:
        import perfctx as _pf
        _pf.add("wait", secs, f"{kind} {host}")
    except Exception:
        logger.exception("ثبتِ انتظار در perfctx ناموفق بود")
    if secs >= 1.0:
        logger.warning("انتظارِ %s: %.1fs | %s", kind, secs, host)


def _jwt_exp(token: str) -> int:
    """exp (epoch) را از payloadِ JWT بیرون می‌کشد؛ اگر نشد 0 برمی‌گرداند
    (یعنی «نمی‌دانم»، که باعث می‌شود کش استفاده نشود و لاگینِ تازه بزنیم)."""
    try:
        import base64
        import json as _json
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # padding
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload.get("exp", 0))
    except Exception:
        return 0

# پروتکل‌هایی که مرزبان پشتیبانی می‌کند؛ پیش‌فرض همیشه vless با flow خالی
# است چون کم‌دردسرترین و پرکاربردترین پروتکل برای ساخت خودکار است.
_DEFAULT_PROXIES = {"vless": {"flow": ""}}


def _expire_to_epoch(expire) -> int:
    """expire برگشتی از پنل را به epoch-ثانیه تبدیل می‌کند.

    === PATCH_MARZBAN_EXPIRE_ISO_FALLBACK_V1 ===
    قبلاً اینجا فقط int(expire) بود و فرض می‌شد پنل همیشه epoch (ثانیه)
    برمی‌گرداند — طبقِ مستنداتِ رسمیِ مرزبان هم واقعاً همین‌طور بود. ولی توی
    عمل، بعضی از فورک‌های خانوادهٔ مرزبان (PasarGuard، یوپنلِ مرزبان، و حتی
    بعضی نصب‌های خودِ مرزبان که آپدیت شده‌اند) الان expire را به‌صورتِ رشتهٔ
    ISO-8601 (مثلاً "2026-08-14T18:56:19Z") برمی‌گردانند — دقیقاً همون
    مشکلی که برای پنلِ ربکا هم دیده شده بود. این تابع هر دو فرمت را قبول
    می‌کند تا با خطای `ValueError: invalid literal for int()` روی
    get_usage/list_clients کرش نکند.
    """
    if not expire:
        return 0
    if isinstance(expire, (int, float)):
        return int(expire)
    s = str(expire).strip()
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _looks_like_iso_expire(expire) -> bool:
    """آیا مقدارِ expireِ برگشتی از پنل رشتهٔ ISO بوده (نه عددِ خام)؟ برای
    اینکه renew() بدونه موقعِ نوشتنِ مقدارِ جدید هم باید همون فرمتی که پنل
    خودش برمی‌گردونه رو بفرسته، نه همیشه epoch را که خیلی از این فورک‌ها
    دیگه قبول نمی‌کنند."""
    if not expire or isinstance(expire, (int, float)):
        return False
    return not str(expire).strip().isdigit()


def _epoch_to_iso_z(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rand(n=8):
    a = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(a) for _ in range(n))


def gen_sub_id(n=16):
    # مرزبان subId جدا ندارد؛ لینک ساب از روی username ساخته می‌شود، اما
    # برای سازگاری با فراخوانی‌های موجود (که sub_id را ذخیره می‌کنند) یک
    # شناسهٔ کوتاه تصادفی برمی‌گردانیم که جایی در منطق مرزبان استفاده نمی‌شود.
    return _rand(n)


def gen_uuid():
    import uuid as _uuid
    return str(_uuid.uuid4())


def _safe_username(email: str) -> str:
    """مرزبان فقط [a-z0-9_] را در یوزرنیم می‌پذیرد (۳ تا ۳۲ کاراکتر).
    email ساخته‌شده در vpn_user.py از قبل تقریباً همین قالب را دارد، اما برای
    اطمینان حروف غیرمجاز را حذف/جایگزین می‌کنیم تا خطای 422 نگیریم."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", str(email)).lower()
    cleaned = cleaned[:32]
    if len(cleaned) < 3:
        cleaned = (cleaned + "_usr")[:32]
    return cleaned


_LIST_TTL = 120.0
_LIST_CACHE: dict = {}
_LIST_LOCKS: dict = {}


class MarzbanPanelClient:
    """یک نشست به پنل مرزبان یک مستأجر. اعتبارنامه در سازنده داده می‌شود.

    امضای سازنده عیناً مثل PanelClient (vpn_panel.py) است تا factory بتواند
    بدون تغییر در محل صدازنی، نوع کلاینت را بر اساس panel_key انتخاب کند.

    sub_base در مرزبان لازم نیست (ساب‌لینک را خود پنل برمی‌گرداند) اما برای
    یکسان‌ماندن امضا نگه داشته شده و در صورت خالی‌بودن، آدرس ساب از روی
    base_url خود پنل ساخته می‌شود.
    inbound_ids اینجا لیستی از رشته («تگ» اینباند مرزبان) است، نه عدد.
    """

    def __init__(self, base_url: str, username: str, password: str,
                 sub_base: str = "", inbound_ids=None, verify_ssl: bool = False,
                 timeout: int = 8, api_key: str | None = None):
        # === PATCH_PANEL_TIMEOUT_REDUCE_V1 ===
        # نگاه کن به توضیحِ کاملِ همین پچ در tenant/hiddify_panel.py.
        if aiohttp is None:
            raise PanelError("کتابخانهٔ aiohttp نصب نیست. `pip install aiohttp` را اجرا کن.")
        # === PATCH_STRIP_DASHBOARD_PATH_V1 ===
        # خطای رایجِ فروشنده‌ها: آدرسِ «داشبورد» را وارد می‌کنند (مثلِ
        # host/dashboard/lorna یا host/dashboard/xK9fQ2 — یک توکنِ تصادفی
        # بعدِ dashboard، که در همهٔ پنل‌های خانوادهٔ مرزبان — Marzban،
        # Rebecca، Pasargad، PasarGuard — رایج است، نه فقط «/dashboard»ی
        # ساده) در حالی که API همیشه روی ریشهٔ دامنه است. قبلاً این حذف فقط
        # توی pasargad_panel.py و فقط برای «/dashboard» یا «/dashboard/»ی
        # دقیق انجام می‌شد؛ توکنِ تصادفیِ بعدش (مثل «lorna») حذف نمی‌شد و
        # نتیجه‌اش خطای HTTP 405 در لاگین بود. حالا این‌جا (کلاسِ پایه، برای
        # هر چهار پنل یک‌جا) با یک regex عمومی‌تر انجام می‌شود.
        # === PATCH_STRIP_URL_FRAGMENT_V1 ===
        # گزارشِ ادمین: فروشنده آدرسِ لاگینِ پنل رو دقیقاً همون‌طور که تو
        # مرورگر می‌بینه کپی می‌کنه — و خیلی از پنل‌ها (SPAهای Vue/React با
        # hash-routing) آدرسشون به «#/login» ختم می‌شه. چون «#» به بعدش
        # (fragment) هیچ‌وقت واقعاً به سرور فرستاده نمی‌شه، وقتی رشته‌ی
        # base_url رو با «/api/admin/token» می‌چسبونیم، aiohttp فقط تا قبل
        # از «#» رو به سرور می‌فرسته و «/api/admin/token» بی‌صدا دور
        # ریخته می‌شه — نتیجه‌اش یه HTTP 405 گیج‌کننده از رویِ ریشه‌ی
        # استاتیکِ سایته، نه از API. حالا هر چیزی بعد از «#» قبل از هر کارِ
        # دیگه‌ای حذف می‌شه.
        base_url = (base_url or "").split("#", 1)[0]
        base_url = base_url.rstrip("/")
        base_url = re.sub(r"/dashboard(/[^/]*)?$", "", base_url, flags=re.IGNORECASE)
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        # === PATCH_PANEL_API_KEY_AUTH_V1 ===
        self.api_key = (api_key or "").strip() or None
        # === PATCH_SUB_BASE_SANITIZE_V1 ===
        _sb = (sub_base or "").strip().strip("ـ-—–_ ").rstrip("/")
        if _sb and not _sb.lower().startswith(("http://", "https://")):
            _sb = ""
        self.sub_base = _sb
        # تگ‌های اینباند به‌صورت رشته نگه داشته می‌شوند (مثل "VLESS TCP REALITY")
        self.inbound_tags = [str(i).strip() for i in inbound_ids] if inbound_ids else []
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = None
        self._token = None
        self._logged_in = False
        self._lock = asyncio.Lock()
        self._inbounds_cache = None  # {protocol: [tag, ...]}

    # ───────── نشست ─────────
    async def _ensure_session(self):
        # اگر timeout تغییر کرده باشد ولی session قدیمی هنوز باز باشد، آن را
        # می‌بندیم تا session جدید با timeout به‌روز ساخته شود.
        if self._session and not self._session.closed:
            cur_timeout = self._session.timeout
            if cur_timeout is None or cur_timeout.total != self.timeout:
                await self._session.close()
                self._session = None
                self._logged_in = False
                self._token = None
        if self._session is None or self._session.closed:
            t = aiohttp.ClientTimeout(total=self.timeout)
            conn = aiohttp.TCPConnector(ssl=self.verify_ssl)
            # === PATCH_BROWSER_USER_AGENT_V1 ===
            # قبلاً هدرِ User-Agent ست نمی‌شد، پس aiohttp خودش مقدارِ
            # پیش‌فرضِ «Python/3.x aiohttp/x.y» رو می‌فرستاد. خیلی از
            # WAFها/fail2banها روی endpointِ لاگینِ ادمین دقیقاً همین
            # الگو رو (User-Agent غیرِ مرورگری) سخت‌گیرانه‌تر بلاک/rate-limit
            # می‌کنن — دقیقاً همون چیزی که دیدیم: curl با UA پیش‌فرضِ خودش
            # موفق (۴۰۱ روی پس اشتباه) ولی همون‌لحظه، همون IP، از داخلِ
            # ربات ۴۲۹ می‌گرفت. با یه User-Agent شبیهِ مرورگر، این تمایز
            # از بین می‌ره.
            self._session = aiohttp.ClientSession(
                timeout=t, connector=conn,
                headers={"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                                         "Chrome/124.0.0.0 Safari/537.36")})
        return self._session

    async def login(self, force=False):
        """=== PATCH_LOGIN_FAILFAST_V1 ===
        اندازه‌گیریِ زنده: پنل‌هایی که لاگینشان خطا می‌دهد (رمز/آدرسِ اشتباه) هر مشتری را ~۱۲ثانیه پشتِ محدودکنندهٔ
        لاگین می‌خواباندند تا دوباره همان خطا را بگیرد. حالا اگر آخرین لاگینِ واقعی *شکست* خورده و هنوز در پنجرهٔ
        محدودکننده‌ایم، همان خطا فوراً داده می‌شود (بدونِ خواب و بدونِ فشارِ اضافه به پنل)."""
        ck = (self.base_url, self.username)
        try:
            ok = await self._login_impl(force)
        except PanelError as e:
            if not self.api_key and not str(e).startswith("برای جلوگیری از بلاک"):
                _LAST_LOGIN_ERR[ck] = (_now_epoch(), str(e))
            raise
        _LAST_LOGIN_ERR.pop(ck, None)
        return ok

    async def _login_impl(self, force=False):
        # === PATCH_PANEL_API_KEY_AUTH_V1 ===
        # اگه به‌جایِ یوزرنیم/پسورد، «API Key» ثابت وارد شده، نیازی به
        # درخواستِ لاگین/گرفتنِ توکن نیست — خودِ کلید مستقیم به‌عنوانِ
        # Bearer token استفاده می‌شه (طبقِ رایج‌ترین الگویِ اپ‌هایِ FastAPI،
        # که پنلِ خانوادهٔ مرزبان/پاسارگارد هم همین معماری رو داره). ⚠️ این
        # فرضیه بر اساسِ الگویِ رایج ساخته شده، نه تاییدِ مستقیم از سورسِ
        # پنل — اگه بعدِ اتصال با «تستِ اتصال» خطای ۴۰۱ گرفتی، یعنی این
        # پنلِ خاص هدرِ متفاوتی (مثلاً X-API-Key) می‌خواد؛ به من بگو کدوم
        # خطا اومد تا اصلاحش کنم.
        if self.api_key:
            self._token = self.api_key
            self._logged_in = True
            return True
        _lk_t0 = time.monotonic()
        async with self._lock:
            _note_wait("login-lock", self.base_url, time.monotonic() - _lk_t0, 0.5)
            if self._logged_in and not force:
                return True
            # === PATCH_TOKEN_CACHE_V1 ===
            # قبل از هر لاگینِ جدید، کشِ مشترک را بررسی کن: اگر توکنِ معتبرِ
            # هنوز-منقضی‌نشده‌ای برای همین (base_url, username) داریم، همان را
            # بردار و اصلاً به endpointِ لاگین دست نزن. (روی force=True هم کش
            # را می‌پذیریم، چون force معمولاً برای «نشستِ همین instance تازه
            # نیست» صدا زده می‌شود، نه لزوماً «توکن باطل شده»؛ اگر واقعاً توکن
            # ردّ شد، مسیرِ 401/403 در _request خودش کش را پاک و لاگینِ اجباری
            # می‌زند — نگاه کن به _force_new_token.)
            ck = (self.base_url, self.username)
            if not force:
                cached = _TOKEN_CACHE.get(ck)
                if cached and cached.get("exp", 0) - _TOKEN_SKEW > _now_epoch():
                    self._token = cached["token"]
                    self._logged_in = True
                    return True
            # === PATCH_LOGIN_THROTTLE_V1 ===
            # به اینجا رسیدیم یعنی باید یک لاگینِ واقعی بزنیم. قبلش دو چیز را
            # رعایت کن:
            # ۱) اگر پنل قبلاً 429 با Retry-After داده و پنجره‌اش هنوز باز است،
            #    اصلاً درخواست نزن؛ چون فقط بلاک را تمدید می‌کند.
            # ۲) لاگین‌های واقعی را برای این پنل دست‌کم _MIN_LOGIN_INTERVAL ثانیه
            #    از هم فاصله بده. اگر لازم شد کمی صبر می‌کنیم (تا سقفِ مجاز)،
            #    وگرنه با پیامِ شفاف بیرون می‌آییم.
            now = _now_epoch()
            blocked_until = _BLOCK_UNTIL.get(ck, 0)
            last = _LAST_LOGIN_TS.get(ck, 0)
            wait_needed = max(
                blocked_until - now,
                (last + _MIN_LOGIN_INTERVAL) - now,
            )
            if wait_needed > 0:
                # === PATCH_LOGIN_FAILFAST_V1 === آخرین تلاش شکست خورده بود ⇒ نخواب؛ همان خطا را فوراً بده
                _le = _LAST_LOGIN_ERR.get(ck)
                if _le and now - _le[0] < _MIN_LOGIN_INTERVAL + 1 and wait_needed > 0.5:
                    raise PanelError(_le[1])
                if wait_needed <= _MAX_LOGIN_WAIT:
                    _note_wait("login-throttle", self.base_url, wait_needed, 0.0)
                    await asyncio.sleep(wait_needed)
                    now = _now_epoch()
                else:
                    secs = int(wait_needed) + 1
                    raise PanelError(
                        f"برای جلوگیری از بلاک‌شدن، لاگینِ جدید فعلاً متوقف شده؛ "
                        f"حدوداً {secs} ثانیهٔ دیگر دوباره امتحان کن. "
                        f"(پنل قبلاً درخواست‌ها را محدود کرده بود.)")
            _LAST_LOGIN_TS[ck] = now
            s = await self._ensure_session()
            data = {"username": self.username, "password": self.password,
                    "grant_type": "password",
                    "scope": "",
                    "client_id": "",
                    "client_secret": ""}
            try:
                async with s.post(f"{self.base_url}/api/admin/token", data=data) as r:
                    status, text, headers = r.status, await r.text(), r.headers
            except (asyncio.TimeoutError, TimeoutError) as e:
                # TimeoutError (از جمله asyncio.TimeoutError) زیرکلاس
                # aiohttp.ClientError نیست؛ اگر جدا گرفته نشود، خطا بدون
                # پیام مشخص بالا می‌رود و کاربر فقط «گیر کردن» را می‌بیند.
                raise PanelError(
                    f"اتصال به پنل برقرار نشد یا بیش از حد طول کشید (Timeout). "
                    f"آدرس/پورت و دسترس‌پذیری سرور را بررسی کن: {e}") from e
            except (aiohttp.ClientError, OSError) as e:
                raise PanelError(f"اتصال به پنل ناموفق بود: {e}") from e

            # === PATCH_LOGIN_ROOT_FALLBACK_V1 ===
            # گزارشِ واقعی: فروشنده آدرسِ صفحهٔ لاگین رو داد، که فایل‌هایِ
            # فرانت‌اندش زیرِ یه مسیرِ camouflage (مثلاً یه مسیرِ فونت،
            # برایِ دورزدنِ فیلترینگ) سرو می‌شدن؛ API واقعی رویِ ریشهٔ
            # دامنه بود. POSTِ لاگین به مسیرِ استاتیک خورد و ۴۰۵ گرفت. حالا
            # دقیقاً همون امضایِ خاص رو تشخیص می‌دیم و یه‌بار با ریشهٔ دامنه
            # امتحان می‌کنیم؛ اگه جواب داد، از این به بعد کلِ این instance
            # (لیست/ساخت/حذفِ کاربر و...) از همون ریشه استفاده می‌کنه.
            if status == 405 and is_static_frontend_405(text):
                root = login_domain_root(self.base_url)
                if root:
                    logger.info(
                        "marzban_panel: لاگین با ۴۰۵ِ استاتیک‌فایل مواجه شد "
                        "(base_url=%s) — یک‌بار با ریشهٔ دامنه (%s) امتحان می‌شود.",
                        self.base_url, root)
                    try:
                        async with s.post(f"{root}/api/admin/token", data=data) as r2:
                            status2, text2, headers2 = r2.status, await r2.text(), r2.headers
                    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError):
                        status2 = None
                    if status2 is not None:
                        status, text, headers = status2, text2, headers2
                        if status == 200:
                            logger.info(
                                "marzban_panel: فال‌بکِ ریشهٔ دامنه موفق بود؛ "
                                "base_url از %s به %s تغییر کرد.", self.base_url, root)
                            self.base_url = root
                            ck = (self.base_url, self.username)  # کلیدِ کش با base_urlِ جدید

            if status == 429:
                ra = (headers.get("Retry-After", "") or "").strip()
                # === PATCH_LOGIN_THROTTLE_V1 === پنجرهٔ بلاک را ثبت کن تا
                # لاگین‌های بعدی (از هر مسیری) تا پایانش صبر کنند.
                try:
                    ra_secs = float(ra) if ra else _MIN_LOGIN_INTERVAL
                except ValueError:
                    ra_secs = _MIN_LOGIN_INTERVAL
                _BLOCK_UNTIL[ck] = _now_epoch() + ra_secs
                wait = f" حدوداً {ra} ثانیه دیگر دوباره امتحان کن." if ra else \
                       " چند دقیقه صبر کن و دوباره امتحان کن."
                raise PanelError(
                    "پنل موقتاً تعدادِ تلاش‌های ورود را محدود کرده (HTTP 429). "
                    "این یعنی محافظِ brute-force سرور (مثلِ cPHulk رویِ WHM) "
                    "فعلاً IP را بلاک کرده — یوزر/پسورد مشکلی ندارد." + wait)
            # === PATCH_PASARGAD_GROUPS_CF403_V1 ===
            # ۴۰۳ِ کلادفلر: صفحهٔ خطایِ کلادفلر (نه پاسخِ API پنل). یوزر/پسورد
            # مشکلی ندارد؛ کلادفلر آی‌پیِ سرورِ ربات را بلاک کرده. به‌جایِ
            # نمایشِ HTML خام، پیامِ قابلِ‌اقدام بده.
            if status in (403, 503):
                _low = (text or "")[:4000].lower()
                _srv = (headers.get("Server", "") or "").lower()
                if ("cloudflare" in _srv or headers.get("CF-RAY") is not None
                        or "cf-ray" in _low or "attention required" in _low
                        or "just a moment" in _low or "cf-chl" in _low
                        or "no-js ie6 oldie" in _low):
                    raise PanelError(
                        f"کلادفلرِ جلوی پنل، درخواستِ ورودِ رباتِ ما را بلاک کرد "
                        f"(HTTP {status} از Cloudflare). مشکلی در آدرس/یوزر/پسورد "
                        "نیست.\n\n"
                        "راه‌حل (باید صاحبِ پنل انجام دهد — یکی از این‌ها):\n"
                        "۱) Cloudflare ← Security ← WAF ← Tools ← IP Access Rules: "
                        "آی‌پیِ سرورِ ربات را Allow کن\n"
                        "۲) Bot Fight Mode / Under Attack Mode / بلاکِ کشور را برایِ "
                        "مسیرِ /api/ خاموش کن\n"
                        "۳) دامنه‌ای بده که پروکسیِ کلادفلر (ابرِ نارنجی) ندارد، یا "
                        "آدرسِ مستقیمِ IP:PORT پنل")
            if status != 200:
                # === PATCH_FRONTEND_404_HINT_V1 ===
                raise PanelError(
                    f"ورود به پنل ناموفق بود (HTTP {status})؛ "
                    f"آدرس/نام‌کاربری/رمز را بررسی کن: {text[:200]}{frontend_404_hint(text)}")
            try:
                import json as _json
                p = _json.loads(text)
            except Exception:
                p = {}
            token = p.get("access_token")
            if not token:
                raise PanelError("ورود به پنل ناموفق بود؛ توکن دریافت نشد.")
            self._token = token
            self._logged_in = True
            # === PATCH_TOKEN_CACHE_V1 === توکنِ تازه را در کشِ مشترک بگذار
            exp = _jwt_exp(token)
            if exp:
                _TOKEN_CACHE[ck] = {"token": token, "exp": exp}
            return True

    # === PATCH_PANEL_TIMING_LOG_V1 ===
    # این کلاس _requestِ خودش را دارد و از PanelClient ارث نمی‌برد، پس
    # زمان‌سنجیِ vpn_panel.py اصلاً رویش اجرا نمی‌شد. برایِ همین در
    # اولین اندازه‌گیری «هیچ عملیاتِ پنلی بالای ۲ ثانیه» گزارش شد —
    # درحالی‌که پنل‌هایِ مرزبان/هیدیفای/مرزنشین/نودکس کلاً نامرئی بودند.
    async def _request(self, method, path, **kwargs):
        import time as _t
        _t0 = _t.monotonic()
        try:
            return await self._request_timed(method, path, **kwargs)
        finally:
            _d = _t.monotonic() - _t0
            if _d >= 2.0:
                logger.warning('پنلِ کند: %s %s | %.1fs | %s',
                               method, path, _d, getattr(self, 'base_url', '?'))

    async def _request_timed(self, method, path, **kwargs):
        s = await self._ensure_session()
        if not self._logged_in:
            await self.login()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json")
        headers["Authorization"] = f"Bearer {self._token}"
        kwargs["headers"] = headers
        url = f"{self.base_url}{path}"
        # === PATCH_CONN_RETRY_BACKOFF_V1 ===
        # گزارشِ واقعیِ ادمین: «یه‌وقتایی اتصال برقرار نمی‌شه». قبلاً رویِ
        # خطاهایِ خالصِ اتصال (نه پاسخِ HTTP) فقط ۱ بار دوباره امتحان
        # می‌کردیم (جمعاً ۲ تلاش) — یه بلیپِ گذرایِ شبکه (بینِ VPSها رایج)
        # می‌تونست همون تلاشِ دوم رو هم بگیره. الان تا ۳ تلاش، با یه مکثِ
        # کوتاه (۰.۷ ثانیه) قبلِ تلاشِ سوم تا شبکه فرصتِ برگشتن داشته باشه.
        # این فقط رویِ ClientError/Timeout اثر داره؛ ۴۰۱/۴۲۹/۴۰۴ (که پاسخِ
        # واقعیِ پنله، نه قطعیِ اتصال) دست‌نخورده می‌مونن.
        auth_retried = False
        conn_attempts_left = 2  # جمعاً تا ۳ تلاش برایِ خطایِ خالصِ اتصال
        while True:
            # کلاینت در panel_pool بینِ درخواست‌هایِ هم‌زمان مشترک است؛ اگر
            # درخواستِ دیگری بعد از شکستِ اتصال نشست را بسته باشد، نشستِ
            # محلیِ قبلی «Session is closed» می‌داد (error.log). هر تلاش
            # نشستِ زندهٔ فعلی را می‌گیرد.
            if s.closed:
                s = await self._ensure_session()
                if not self._logged_in:
                    await self.login()
                    kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
            try:
                async with s.request(method, url, **kwargs) as r:
                    text = await r.text()
                    if r.status in (401, 403) and not auth_retried:
                        # === PATCH_FORBIDDEN_NO_THROTTLE_WAIT_V1 ===
                        # اندازه‌گیریِ زنده: «wait login-throttle ۱۱ثانیه» در دکمه‌هایِ مشتری. ۴۰۳ معمولاً یعنی «اجازه
                        # نداری» (ادمینِ اپراتور/RBAC)، نه «توکن باطل» — لاگینِ دوباره چیزی را درست نمی‌کند و فقط
                        # پشتِ محدودکنندهٔ ۱۲ثانیه‌ای می‌خوابید و باز همان ۴۰۳ را می‌گرفت. اگر لاگینِ اجباری باید
                        # صبر کند، منتظر نمی‌مانیم؛ همان خطای ۴۰۳ را فوراً می‌دهیم (توکنِ کش هم دست‌نخورده می‌ماند).
                        # ۴۰۱ (توکنِ منقضی) مثلِ قبل لاگینِ تازه می‌زند.
                        if r.status == 403 and _login_wait_needed(self.base_url, self.username) > 1.0:
                            raise PanelError(f"{method} {path} HTTP 403: {text[:300]}")
                        # === PATCH_TOKEN_CACHE_V1 ===
                        # توکن ردّ شد؛ پس کشِ مشترک هم کهنه است — پاکش کن تا
                        # login(force=True) واقعاً توکنِ تازه بگیرد، نه دوباره
                        # همان توکنِ باطل را از کش بردارد.
                        auth_retried = True
                        _TOKEN_CACHE.pop((self.base_url, self.username), None)
                        self._logged_in = False
                        await self.login(force=True)
                        kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
                        continue
                    if r.status == 429:
                        # === PATCH_HONEST_429_V1 ===
                        # روی 429 هرگز force-relogin نزن؛ فقط بلاک را طولانی‌تر
                        # می‌کند. پیامِ صادق بده و بیرون بیا.
                        ra = r.headers.get("Retry-After", "").strip()
                        wait = f" حدوداً {ra} ثانیه دیگر امتحان کن." if ra else \
                               " کمی صبر کن و دوباره امتحان کن."
                        raise PanelError(
                            f"{method} {path}: پنل موقتاً درخواست‌ها را محدود کرده "
                            f"(HTTP 429 — rate limit)." + wait)
                    if r.status == 404:
                        return None
                    if r.status >= 400:
                        raise PanelError(f"{method} {path} HTTP {r.status}: {text[:300]}")
                    if not text:
                        return {}
                    try:
                        from tenant.shared.panel_clients.vpn_panel import loads_json_async
                        return await loads_json_async(text)   # PATCH_PANEL_JSON_OFFLOOP_V1
                    except Exception:
                        return {"raw": text}
            except PanelError:
                raise
            except RuntimeError as e:
                # نشست وسطِ کار توسطِ درخواستِ هم‌زمانِ دیگری بسته شد
                if "session is closed" not in str(e).lower() or conn_attempts_left <= 0:
                    raise
                conn_attempts_left -= 1
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as e:
                if conn_attempts_left > 0:
                    conn_attempts_left -= 1
                    # === PATCH_TOKEN_CACHE_V1 ===
                    # خطای اتصال ربطی به اعتبارِ توکن ندارد؛ توکن را دوباره
                    # نگیر (یک hitِ اضافه روی endpointِ لاگین را حذف می‌کند).
                    if conn_attempts_left == 0:
                        # === PATCH_CONN_RETRY_BACKOFF_V1 === آخرین تلاش، با مکث
                        await asyncio.sleep(0.7)
                    continue
                # === PATCH_TIMEOUT_ERROR_MSG_V1 ===
                # گزارشِ ادمین: پیامِ خطا با یه دونقطهٔ خالی تموم می‌شد
                # («...طول کشید: ») بدونِ هیچ جزئیاتی. علتش اینه که
                # asyncio.TimeoutError وقتی بدونِ آرگومان ساخته می‌شه،
                # str(e) خودش رشتهٔ خالیه — این یه رفتارِ شناخته‌شدهٔ خودِ
                # پایتونه، نه باگِ اینجا. حداقل نوعِ خطا و تایم‌اوتِ
                # پیکربندی‌شده رو نشون می‌دیم تا قابلِ‌پیگیری باشه.
                err_detail = str(e) or f"{type(e).__name__} (timeout={self.timeout}s)"
                # === PATCH_PANEL_POOL_SELFHEAL_V1 ===
                # گزارشِ ادمین: خریدِ اول با تایم‌اوت شکست خورد، ولی یه تستِ
                # دستیِ بلافاصله‌بعد (با یه کلاینتِ تازه) بی‌مشکل موفق شد —
                # یعنی خودِ پنل/شبکه سالم بود. علتِ واقعی: مسیرِ خرید از
                # panel_pool.py یه کلاینتِ زنده و لاگین‌شده رو بینِ
                # خریدهایِ مختلف به اشتراک می‌ذاره (برایِ سرعت)، ولی
                # هیچ‌جا این کلاینت رو بعدِ یه خطایِ اتصال از استخر بیرون
                # نمی‌نداخت (drop_sync تعریف شده بود ولی هیچ‌وقت صدا زده
                # نمی‌شد) — یعنی یه اتصالِ مرده/نیمه‌قطع تا ۳ دقیقه (TTL)
                # مدام به همون مشتری‌ها و بقیه هم برمی‌گشت. الان به‌جایِ
                # وابسته‌بودن به اینکه هرکدوم از آن ۱۴ نقطهٔ فراخوان یادش
                # باشه drop کنه، خودِ کلاینت بعدِ اتمامِ تلاش‌ها نشست و
                # وضعیتِ لاگینش رو ریست می‌کنه — دفعهٔ بعد (چه از پول، چه
                # مستقیم) مجبور به اتصال/لاگینِ کاملاً تازه می‌شه.
                try:
                    if self._session and not self._session.closed:
                        await self._session.close()
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (بستنِ نشستِ خراب بعدِ شکستِ اتصال)")
                self._session = None
                self._logged_in = False
                self._token = None
                raise PanelError(
                    f"{method} {path}: اتصال برقرار نشد یا بیش از حد طول کشید: {err_detail}") from e

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._logged_in = False
        self._token = None

    # === PATCH_WIREGUARD_NATIVE_DELIVERY_V1 ===
    # گزارشِ ادمین: رباتِ همکارش (پنلِ هم‌خانواده — آزادپنل/PasarGuard) فایلِ
    # وایرگارد را «دقیقاً همان چیزی که در ساب‌لینک است، بدونِ تغییر» می‌فرستد
    # و اسمِ فایل هم درست است؛ رباتِ خودش نه. علتِ ریشه‌ای: طبقِ مستندِ رسمیِ
    # پنل، فیلدِ عمومیِ «links» برایِ کاربرِ وایرگارد **همیشه خالی** است —
    # آدرسِ اشتراکی از جنسِ vless:// برایِ وایرگارد اصلاً معنی ندارد. کدِ قبلی
    # (get_client_links → استخراجِ heuristic) یا از رویِ ساب‌لینکِ عمومی حدس
    # می‌زد یا فقط اولین لوکیشن را می‌گرفت، و اسمِ فایل را خودش می‌ساخت —
    # درحالی‌که پنل مسیرِ اختصاصی دارد که هم فهرستِ *همهٔ* لوکیشن‌ها را با
    # نامِ فایلِ صحیح می‌دهد (GET /api/wg/user/{u}/configs)، هم متنِ خودِ
    # فایل را بدونِ دست‌کاری (GET .../config?host=N)، هم QR را (.../qr?host=N).
    #
    # این سه متد فقط پنل‌هایِ این خانواده (که واقعاً این endpointها را دارند)
    # را پوشش می‌دهند؛ برایِ بقیه (404/400) لیستِ خالی برمی‌گردانند تا صدازننده
    # بی‌خطر به مسیرِ قدیمیِ heuristic برگردد — هیچ رفتارِ موجودی خراب نمی‌شود.
    async def get_wireguard_endpoints(self, username: str) -> list[dict]:
        """GET /api/wg/user/{username}/configs → فهرستِ لوکیشن‌ها، هرکدام با
        نامِ فایلِ صحیح (endpoint["file"]) و مسیرهایِ config_url/qr_url.
        اگر این پنل/کاربر وایرگاردِ بومی نداشته باشد: [] (نه استثنا)."""
        import asyncio as _aio
        for attempt, delay in enumerate((0, 1, 2, 4)):
            if delay:
                await _aio.sleep(delay)
            try:
                res = await self._request("GET", f"/api/wg/user/{username}/configs")
            except PanelError as e:
                msg = str(e)
                if "HTTP 503" in msg:
                    continue  # هنوز peer/کلیدِ سرور آماده نشده — دوباره امتحان کن
                if "HTTP 404" in msg or "HTTP 400" in msg or "HTTP 405" in msg:
                    return []  # این پنل اصلاً این endpoint را ندارد (خانوادهٔ دیگر)
                raise
            if not res:
                return []
            eps = res.get("endpoints")
            return eps if isinstance(eps, list) else []
        return []

    async def fetch_wireguard_config_text(self, username: str, host_id=None, *, path: str | None = None) -> str:
        """GET .../config?host=<id> → متنِ خامِ .conf، بدونِ هیچ دست‌کاری.

        اگر «path» داده شود (همان config_url که خودِ پنل در /configs برگردانده)
        دقیقاً همان مسیر صدا زده می‌شود — طبقِ توصیهٔ مستندِ پنل («config_url
        را همان‌طور که آمده صدا بزن؛ خودت ?host= نساز»)، چون ممکن است در
        نسخه‌هایِ بعدیِ پنل شکلِ آدرس عوض شود. اگر path داده نشود (برایِ
        سازگاریِ عقب‌رو)، از روی host_id ساخته می‌شود."""
        p = path or f"/api/wg/user/{username}/config?host={host_id}"
        res = await self._request("GET", p)
        if isinstance(res, dict):
            return res.get("raw") or ""
        return str(res or "")

    async def fetch_wireguard_qr_bytes(self, username: str, host_id=None, *, path: str | None = None) -> bytes | None:
        """GET .../qr?host=<id> → بایت‌هایِ خامِ PNG؛ همان قاعدهٔ path بالا."""
        p = path or f"/api/wg/user/{username}/qr?host={host_id}"
        s = await self._ensure_session()
        if not self._logged_in:
            await self.login()
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self.base_url}{p}"
        try:
            async with s.get(url, headers=headers) as r:
                if r.status != 200:
                    return None
                return await r.read()
        except Exception:
            logger.exception("دریافتِ QRِ وایرگارد ناموفق بود")
            return None

    async def __aenter__(self):
        await self.login()
        return self

    async def __aexit__(self, *a):
        await self.close()

    # ───────── inboundها ─────────
    async def list_inbounds(self):
        """خروجی مرزبان: {protocol: [{tag, protocol, network, tls, port}, ...]}.
        برای هم‌شکلی با خروجی PanelClient (لیست تخت با id/remark/enable)، اینجا
        را به لیستی از دیکشنری با کلیدهای tag/protocol تبدیل می‌کنیم؛ شناسهٔ
        یکتای اینباند در مرزبان همان «tag» است (در 3X-UI عدد id بود)."""
        res = await self._request("GET", "/api/inbounds") or {}
        flat = []
        for proto, items in res.items():
            for ib in items or []:
                flat.append({
                    "id": ib.get("tag"), "tag": ib.get("tag"),
                    "protocol": ib.get("protocol", proto),
                    "remark": ib.get("tag"), "enable": True,
                })
        self._inbounds_cache = res
        return flat

    async def active_inbound_ids(self):
        """لیست تگ‌های اینباند هدف؛ اگر در تنظیمات پنل تگ مشخصی ذخیره نشده
        باشد، همهٔ تگ‌های موجود (همهٔ پروتکل‌ها) برگردانده می‌شود."""
        if self.inbound_tags:
            return list(self.inbound_tags)
        inbounds = await self.list_inbounds()
        tags = [ib["tag"] for ib in inbounds if ib.get("tag")]
        if not tags:
            raise PanelError("هیچ inbound فعالی روی پنل مرزبان پیدا نشد.")
        return tags

    # === PATCH_PANEL_YAR_MOVE_V1 ===
    # مدیریتِ ادمین‌هایِ خودِ پنل (نه کاربرانِ مشتری) — طبقِ APIِ رسمیِ
    # مرزبان (/api/admin[s]). مستقل از منطقِ فروش، فقط برایِ پنل‌یار.
    async def list_admins(self) -> list[dict]:
        """GET /api/admins → [{username, is_sudo, ...}, ...]"""
        res = await self._request("GET", "/api/admins")
        return res if isinstance(res, list) else []

    async def create_admin(self, username: str, password: str, is_sudo: bool = False) -> dict:
        """POST /api/admin — ساختِ ادمینِ جدیدِ پنل."""
        return await self._request("POST", "/api/admin", json={
            "username": username, "password": password, "is_sudo": is_sudo})

    async def modify_admin_password(self, username: str, new_password: str) -> dict:
        """PUT /api/admin/{username} — فقط پسورد رو عوض می‌کنه (is_sudo دست‌نخورده).
        مرزبان برایِ PUT کلِ بدنه رو می‌خواد؛ برایِ همین اول is_sudoِ فعلی رو
        از لیستِ ادمین‌ها می‌خونیم تا موقعِ تغییرِ پسورد اشتباهی صفر نشه."""
        admins = await self.list_admins()
        is_sudo = False
        for a in admins:
            if a.get("username") == username:
                is_sudo = bool(a.get("is_sudo"))
                break
        return await self._request("PUT", f"/api/admin/{username}", json={
            "password": new_password, "is_sudo": is_sudo})

    async def delete_admin(self, username: str) -> None:
        """DELETE /api/admin/{username}"""
        await self._request("DELETE", f"/api/admin/{username}")

    async def _inbounds_by_protocol(self, tags: list[str]) -> dict:
        """تگ‌های هدف را بر اساس پروتکل واقعی‌شان در پنل دسته‌بندی می‌کند تا
        بدنهٔ inbounds برای /api/user به‌درستی {protocol: [tag, ...]} ساخته شود."""
        if self._inbounds_cache is None:
            await self.list_inbounds()
        by_proto: dict[str, list[str]] = {}
        cache = self._inbounds_cache or {}
        wanted = set(tags) if tags else None
        for proto, items in cache.items():
            for ib in items or []:
                tag = ib.get("tag")
                if not tag:
                    continue
                if wanted is None or tag in wanted:
                    by_proto.setdefault(proto, []).append(tag)
        return by_proto

    # ───────── ساخت ─────────
    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0,
                             on_hold=False):
        """ساخت کاربر در مرزبان با همان امضای create_account پنل سنایی.

        gb: حجم به گیگ (0=نامحدود). mb: اگر داده شود حجم به مگابایت (سرویس تست).
        days>0 → expire به‌صورت UTC timestamp مطلق (ثانیه) از همین لحظه محاسبه
        می‌شود؛ days=0 → expire=0 (بی‌انقضا) — دقیقاً مثل رفتار create_account
        در پنل سنایی، تا منطق نمایش «N روز باقی‌مانده» در vpn_user.py یکسان بماند.

        limit_ip در مرزبان معادل مستقیم ندارد (محدودیت هم‌زمان اتصال جزو
        تنظیمات سراسری/per-inbound مرزبان است، نه per-user ساده)، پس نادیده
        گرفته می‌شود — این تنها تفاوت رفتاری مهم نسبت به پنل سنایی است.

        === PATCH_TRIAL_HOURLY_V1 ===
        hours: مدتِ اضافه به ساعت. expire به ثانیه‌ست، پس ساعتی هم پشتیبانی
        می‌شه. days=0, hours=2 → تستِ ۲ ساعته.
        """
        username = _safe_username(email)
        tags = inbound_ids if inbound_ids is not None else await self.active_inbound_ids()
        inbounds_body = await self._inbounds_by_protocol(tags)
        # === PATCH_MARZBAN_EMPTY_INBOUNDS_FIX_V1 ===
        # باگِ واقعی: اگه تگ‌هایِ اینباندِ تنظیم‌شده (self.inbound_tags) رو
        # پنل دیگه نداره (مثلاً ادمینِ پنل بعداً اون اینباند رو حذف/رنیم
        # کرده)، inbounds_body خالی می‌موند ولی proxies به‌خاطرِ همون
        # `or dict(_DEFAULT_PROXIES)` بی‌سر و صدا پر از پروتکل‌هایِ
        # پیش‌فرض می‌شد — یعنی به مرزبان یه درخواستی می‌رفت که توش
        # proxies چندتا پروتکل داشت ولی inbounds برایِ هیچ‌کدومشون تگ
        # نداشت (خالی). این ناهماهنگی همون چیزیه که خیلی وقتا باعثِ ۵۰۰
        # از سمتِ خودِ مرزبان می‌شه، نه یه ۴۰۰ی واضح. الان قبل از فرستادنِ
        # درخواست، این حالت رو تشخیص می‌دیم و یه خطایِ روشن می‌دیم.
        if not inbounds_body:
            raise PanelError(
                "ساخت کانفیگ ناموفق: هیچ‌کدوم از اینباندهایِ تنظیم‌شده رویِ پنل پیدا نشد. "
                "احتمالاً اینباندِ موردنظر رویِ خودِ پنلِ مرزبان حذف یا تغییرِ اسم داده. "
                "از تنظیماتِ این پنل (تویِ ربات) دوباره اینباندها رو انتخاب/به‌روزرسانی کن، "
                "یا از پنلِ وبِ مرزبان مطمئن شو اون اینباند هنوز هست.")
        proxies = {proto: dict(_DEFAULT_PROXIES.get(proto, {})) for proto in inbounds_body} \
            or dict(_DEFAULT_PROXIES)

        if mb is not None:
            data_limit = int(mb) * 1024 * 1024
        else:
            data_limit = int(gb) * GB if gb else 0
        total_seconds = int(days) * 86400 + int(hours) * 3600
        expire = int(time.time()) + total_seconds if total_seconds else 0

        body = {
            "username": username,
            "proxies": proxies,
            "inbounds": inbounds_body,
            "expire": expire,
            "data_limit": data_limit,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": "",
        }
        # === PATCH_TRIAL_ON_HOLD_V1 ===
        # «on_hold» یعنی شمارشِ مدتِ اشتراک از لحظهٔ *اولین اتصال* شروع شود،
        # نه از لحظهٔ ساخت. برایِ سرویسِ تست دقیقاً همین لازم است: کاربری که
        # تست می‌گیرد ولی چند روز بعد وصل می‌شود، نباید تستِ تمام‌شده تحویل
        # بگیرد. مرزبان در این حالت expire را نمی‌پذیرد و به‌جایش مدت را از
        # on_hold_expire_duration (ثانیه) می‌گیرد.
        if on_hold and total_seconds:
            body["status"] = "on_hold"
            body["on_hold_expire_duration"] = total_seconds
            body["expire"] = 0
        res = await self._request("POST", "/api/user", json=body)
        if res is None:
            raise PanelError("ساخت کانفیگ ناموفق: کاربری با این نام از قبل وجود دارد.")
        sub_url = res.get("subscription_url") or ""
        return {
            "uuid": uuid_str or gen_uuid(), "sub_id": sub_id or gen_sub_id(),
            "email": username, "gb": gb, "days": days,
            "inbounds": tags, "sub_link": self._abs_sub_link(sub_url),
            "expiry_ms": expire * 1000 if expire else 0,
        }

    def _abs_sub_link(self, sub_url: str) -> str:
        """subscription_url مرزبان معمولاً مسیر نسبی است (مثل /sub/xxxx)؛
        اینجا آن را به آدرس کامل تبدیل می‌کنیم."""
        if not sub_url:
            return ""
        if sub_url.startswith("http://") or sub_url.startswith("https://"):
            # === PATCH_SUB_BASE_OVERRIDES_PANEL_HOST_V1 ===
            # قبلاً لینکِ مطلقی که خودِ پنل برمی‌گردوند بدونِ چون‌وچرا همون‌طور
            # به مشتری می‌رفت. مشکل: پاسارگاد/PasarGuard آدرسِ ساب رو از
            # SUBSCRIPTION_URL_PREFIX (یا هاستِ خودِ درخواست) می‌سازه؛ اگه
            # فروشنده اون رو تنظیم نکرده باشه، لینک با آدرسِ داخلی/پورتِ پنل
            # (مثلاً http://127.0.0.1:8000/sub/... یا panel.x.com:8000) ساخته
            # می‌شه و رویِ گوشیِ مشتری باز نمی‌شه. حالا اگه فروشنده «آدرسِ ساب»
            # (sub_base) رو تویِ تنظیماتِ پنل پر کرده باشه، فقط هاست/اسکیم
            # جایگزین می‌شه و مسیرِ توکن (که تنها بخشِ مهمِ لینکه) دست‌نخورده
            # می‌مونه. اگه sub_base خالی باشه، رفتار دقیقاً مثل قبله.
            if not self.sub_base:
                return sub_url
            try:
                from urllib.parse import urlsplit
                parts = urlsplit(sub_url)
                path = parts.path or ""
                if parts.query:
                    path = f"{path}?{parts.query}"
                if not path.strip("/"):
                    return sub_url
                return f"{self.sub_base.rstrip('/')}/{path.lstrip('/')}"
            except Exception:
                return sub_url
        base = self.sub_base or self.base_url
        return f"{base.rstrip('/')}/{sub_url.lstrip('/')}"

    # ───────── لینک‌ها ─────────
    def sub_link(self, sub_id):
        """در مرزبان لینک ساب از روی username ساخته می‌شود نه sub_id؛ این متد
        فقط برای سازگاری امضا نگه داشته شده و معمولاً استفاده نمی‌شود — برای
        گرفتن ساب‌لینک واقعی از get_client(email) استفاده کن."""
        return f"{self.sub_base}/{sub_id}" if self.sub_base else ""

    async def get_sub_links(self, sub_id):
        return []

    async def get_client_links(self, email):
        """لینک‌های مستقیم (تکی) هر inbound برای این کاربر.

        ابتدا از فیلد links پاسخ کاربر می‌خوانیم؛ اگر پنل (مثل برخی نسخه‌های
        پاسارگاد) این فیلد را خالی برگرداند، محتوای ساب‌لینک را مستقیم می‌گیریم و
        خطوط کانفیگ (vless/vmess/trojan/ss) را از آن استخراج می‌کنیم.
        """
        cur = await self.get_client(email)
        api_links = list((cur or {}).get("links") or [])
        # === PATCH_WG_EXACT_FROM_SUB_V1 ===
        # گزارشِ ساپورت: فایل‌هایِ وایرگاردِ «ارسال تکی» با فایل‌هایِ خودِ
        # ساب‌لینک فرق داشت (فیلدها/سرورها). فیلدِ links گاهی فقط یه URIِ
        # wireguard:// می‌دهد که ما بازسازی‌اش می‌کنیم و ممکن است فیلد
        # جا بیفتد. اگر ساب خودش فایلِ کاملِ [Interface] بدهد، همان را
        # عیناً استفاده می‌کنیم. فقط وقتی جایگزین می‌شود که تعدادِ فایل‌هایِ
        # ساب ≥ تعدادِ وایرگاردهایِ API باشد (سروری گم نشود)؛ در هر خطا
        # یا ابهام رفتارِ قبلی حفظ می‌شود. لینک‌هایِ غیرِ وایرگارد دست‌نخورده‌اند.
        def _is_wg(x):
            x = (x or "").lstrip()
            return x.startswith("wireguard://") or "[Interface]" in x   # PATCH_WG_SPLIT_SUB_BLOB_V1
        sub_url = (cur or {}).get("sub_link") or self._abs_sub_link(
            (cur or {}).get("subscription_url", ""))
        if api_links:
            api_wg = [x for x in api_links if _is_wg(x)]
            if not api_wg or not sub_url:
                return api_links
            try:
                sub_links = await self._fetch_links_from_sub(sub_url)
                sub_wg = [x.strip() for x in sub_links
                          if "[Interface]" in (x or "")]    # PATCH_WG_SPLIT_SUB_BLOB_V1
                if sub_wg and len(sub_wg) >= len(api_wg):
                    others = [x for x in api_links if not _is_wg(x)]
                    return others + sub_wg
            except Exception:
                logger.exception("وایرگاردِ ساب گرفته نشد؛ از links پنل استفاده شد")
            return api_links
        # fallback: خواندن مستقیم محتوای ساب‌لینک
        if not sub_url:
            return []
        return await self._fetch_links_from_sub(sub_url)

    async def _fetch_links_from_sub(self, sub_url: str) -> list[str]:
        """=== PATCH_SUB_UNIFIED_V1 === به sub_fetch.fetch_sub_links واگذار شد:
        نشستِ تازه (بدونِ توکنِ پنل)، چند UA روی ۴۰۳/۵xx، و تبدیلِ JSONِ Xray به لینک."""
        from tenant.shared.panel_clients.sub_fetch import fetch_sub_links
        return await fetch_sub_links(sub_url)

    async def get_client(self, email):
        # PATCH_PANEL_RAW_USERNAME_V2 — اول نامِ تبدیل‌شده، بعد نامِ دقیقِ پنل (سرویسِ «افزودنِ دستی»)
        res = None
        for username in name_candidates(self, email, _safe_username):
            res = await self._request("GET", f"/api/user/{username}")
            if res:
                remember(self, email, username)
                break
        if not res:
            return None
        res = dict(res)
        res["sub_link"] = self._abs_sub_link(res.get("subscription_url", ""))
        res["subId"] = username
        res["id"] = username
        return res

    async def get_usage(self, email):
        """آمار مصرف با همان کلیدهای PanelClient: up, down, total, expiryTime
        (میلی‌ثانیه) — برای سازگاری با _usage_block در vpn_user.py.

        همچنین (اگه خودِ پنل داده باشه) last_online_ms و client_app را هم
        برمی‌گرداند — این دو مستقیماً از فیلدهای رسمیِ UserResponse مرزبان
        (online_at و sub_last_user_agent) میان، پس داده‌ی واقعی‌اند، نه حدسی."""
        cur = await self.get_client(email)
        if not cur:
            return None
        used = int(cur.get("used_traffic", 0) or 0)
        cap = int(cur.get("data_limit", 0) or 0)
        expire = cur.get("expire")
        expiry_ms = _expire_to_epoch(expire) * 1000
        result = {
            "up": 0, "down": used,  # مرزبان up/down جدا برنمی‌گرداند، همه در used_traffic است
            "total": cap, "expiryTime": expiry_ms,
            "enable": cur.get("status") in ("active", "on_hold"),   # PATCH_ONHOLD_DISPLAY_V1
        }
        # === PATCH_ONHOLD_DISPLAY_V1 === on_hold = مدت از اولین اتصال شروع می‌شود؛ expire خالی است
        # و بات اشتباهاً «بدون انقضا» نشان می‌داد. مدتِ واقعی (ثانیه) را هم برمی‌گردانیم.
        if cur.get("status") == "on_hold":
            try:
                result["on_hold_seconds"] = int(cur.get("on_hold_expire_duration") or 0)
            except (TypeError, ValueError):
                pass
        online_at = cur.get("online_at")
        if online_at:
            ms = _expire_to_epoch(online_at) * 1000
            if ms:
                result["last_online_ms"] = ms
        ua = cur.get("sub_last_user_agent")
        if ua and str(ua).strip():
            result["client_app"] = str(ua).strip()
        return result

    # ───────── تمدید/ترافیک ─────────
    async def _modify(self, email, **fields):
        username = await resolve_username(self, email, _safe_username, "/api/user/{}")  # PATCH_PANEL_RAW_USERNAME_V1
        res = await self._request("PUT", f"/api/user/{username}", json=fields)
        if res is None:
            raise PanelError("کاربر روی پنل مرزبان پیدا نشد.")
        return res

    async def add_traffic(self, email, gb):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر روی پنل مرزبان پیدا نشد.")
        cur_limit = int(cur.get("data_limit", 0) or 0)
        if cur_limit == 0:
            # کاربر نامحدود است؛ افزایش حجم بی‌معنی است، بدون خطا رد می‌شویم
            return cur
        new_limit = cur_limit + int(gb) * GB
        return await self._modify(email, data_limit=new_limit)

    async def renew(self, email, days, reset_usage=False):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر روی پنل مرزبان پیدا نشد.")
        raw_expire = cur.get("expire")
        cur_expire = _expire_to_epoch(raw_expire)
        # === PATCH_RENEW_UNLIMITED_TIME_GUARD_V1 ===
        # باگِ گزارش‌شده: کاربری که از قبل هم حجمِ نامحدود هم زمانِ نامحدود
        # داشت، بعدِ «تمدید مجدد سرویس (زمان + حجم)» چند روز بعد حذف شد.
        # علت: add_traffic() از قبل برایِ data_limit==0 (نامحدود) درست
        # نادیده می‌گیرد («افزایش حجم بی‌معنی است») — ولی renew() این‌کار
        # را برایِ expire نامحدود (None/0) نمی‌کرد: چون cur_expire=0 است و
        # 0 > now غلطه، base همیشه «الان» می‌شد و یک expireِ واقعی (الان +
        # روزهایِ پلن) رویِ یک حسابِ قبلاً-نامحدود می‌نوشت — یعنی نامحدودیِ
        # زمانی بی‌سروصدا از بین می‌رفت و حساب از آن پس مثلِ یک حسابِ
        # عادی به‌مرورِ زمان منقضی/حذف می‌شد. حالا مثلِ add_traffic، اگر از
        # قبل نامحدود بود (expire خالی/صفر)، تمدیدِ زمان را بی‌خطا رد
        # می‌کنیم و نامحدود می‌ماند.
        if not raw_expire and cur_expire == 0:
            return cur
        now = int(time.time())
        base = cur_expire if cur_expire > now else now
        new_expire_epoch = base + int(days) * 86400
        # اگه پنل خودش expire رو به‌صورتِ ISO برگردونده (نه عددِ خام)، یعنی
        # این نصب دیگه epoch رو برایِ نوشتن هم قبول نمی‌کنه؛ برای همین با
        # همون فرمتی که خودش داده می‌نویسیم (نگاه کن به PATCH_MARZBAN_EXPIRE_ISO_FALLBACK_V1).
        new_expire = _epoch_to_iso_z(new_expire_epoch) if _looks_like_iso_expire(raw_expire) else new_expire_epoch
        obj = await self._modify(email, expire=new_expire, status="active")
        if reset_usage:
            await self.reset_usage(email)
        return obj

    async def reset_usage(self, email):
        username = await resolve_username(self, email, _safe_username, "/api/user/{}")  # PATCH_PANEL_RAW_USERNAME_V1
        res = await self._request("POST", f"/api/user/{username}/reset", json={})
        return res is not None

    async def reset_sub(self, email, new_sub_id=None):
        """در مرزبان معادل «ریست لینک ساب» اندپوینت revoke_sub است که یک
        subscription_url جدید صادر می‌کند (لینک قبلی را باطل می‌کند)."""
        username = await resolve_username(self, email, _safe_username, "/api/user/{}")  # PATCH_PANEL_RAW_USERNAME_V1
        res = await self._request("POST", f"/api/user/{username}/revoke_sub", json={})
        if res is None:
            raise PanelError("ریست لینک ناموفق: کاربر پیدا نشد.")
        return self._abs_sub_link(res.get("subscription_url", ""))

    async def rename_account(self, old_email: str, new_email: str):
        """در مرزبان/پاسارگاد «نام کاربری» همان شناسهٔ یکتای اکانت است و طبق API
        این پنل‌ها قابل تغییر نیست؛ نام سرور فقط برای اکانت‌های جدید اعمال می‌شود.
        """
        raise PanelError(
            "در این پنل نام کاربری همان شناسهٔ اکانت است و قابل تغییر روی "
            "اکانت‌های موجود نیست؛ نام جدید فقط روی کانفیگ‌های تازه اعمال می‌شود.")

    # === PATCH_SVC_DISABLE_MARKER_MARZBAN ===
    async def set_enabled(self, email, enabled: bool):
        """فعال/غیرفعال کردن سریع کاربر (بدون حذف). در مرزبان/پاسارگاد این با
        فیلد status انجام می‌شود (active/disabled)؛ غیرفعال یعنی سرویس فوراً
        قطع می‌شود، فعال‌کردن دوباره بلافاصله وصل بودن را برمی‌گرداند."""
        await self._modify(email, status=("active" if enabled else "disabled"))
        return True

    async def delete_account(self, email, keep_traffic=False):
        username = await resolve_username(self, email, _safe_username, "/api/user/{}")  # PATCH_PANEL_RAW_USERNAME_V1
        res = await self._request("DELETE", f"/api/user/{username}")
        return res is not None or True  # حذف معمولاً body خالی برمی‌گرداند

    # ───────── آمار ─────────
    async def list_clients(self):
        """لیست کاربران مرزبان به‌شکل تخت و با کلیدهای هم‌نام PanelClient
        (email, traffic{up,down,total}, expiryTime, totalGB) تا
        main.py:vpn_expiry_loop بدون تغییر کار کند."""
        # کشِ کوتاه + ادغامِ درخواست‌های هم‌زمان: چند حلقهٔ پس‌زمینه (انقضا، اولین
        # اتصال، پاک‌سازی تست) هر کدام کلِ لیست را می‌کشیدند و روی پنل‌های بزرگ
        # ۳۰–۶۰ ثانیه فشار می‌آمد. حالا برای هر پنل حداکثر هر _LIST_TTL ثانیه
        # یک بار و فقط یک درخواست در لحظه.
        key = (self.base_url, self.username)
        now = time.monotonic()
        hit = _LIST_CACHE.get(key)
        if hit and now - hit[0] < _LIST_TTL:
            return list(hit[1])
        lock = _LIST_LOCKS.setdefault(key, asyncio.Lock())
        async with lock:
            hit = _LIST_CACHE.get(key)
            if hit and time.monotonic() - hit[0] < _LIST_TTL:
                return list(hit[1])
            out = await self._list_clients_fetch()
            _LIST_CACHE[key] = (time.monotonic(), out)
            return list(out)

    async def _list_clients_fetch(self):
        # طبق API رسمی (مرزبان و پاسارگارد) پاسخ {users, total} است. قبلاً فقط
        # ۱۰۰۰ کاربرِ اول خوانده می‌شد و پنل‌های بزرگ‌تر بی‌صدا ناقص می‌ماندند
        # (هشدار انقضا برای بقیه نمی‌رفت). حالا با offset صفحه‌بندی می‌کنیم.
        users: list = []
        # === PATCH_LIST_USERS_LONG_TIMEOUT_V1 ===
        # error.log: رویِ پنل‌هایِ بزرگ یک صفحهٔ ۱۰۰۰تایی گاهی بیش از ۸ثانیه
        # (تایم‌اوتِ پیش‌فرضِ کلاینت) طول می‌کشد؛ هر سه تلاش تایم‌اوت می‌شد و
        # هشدارِ انقضا، پاک‌سازیِ تست و همگام‌سازی برایِ کلِ پنل بی‌صدا رد
        # می‌شد. این خواندنِ حجیم مالِ حلقه‌هایِ پس‌زمینه است، پس مهلتِ
        # طولانی‌تری می‌گیرد (درخواست‌هایِ تکیِ کاربر همان ۸ثانیه می‌مانند).
        _list_timeout = aiohttp.ClientTimeout(total=max(float(self.timeout or 0), 45.0))
        for _page in range(30):
            res = await self._request("GET", "/api/users",
                                      params={"limit": 1000, "offset": len(users)},
                                      timeout=_list_timeout) or {}
            batch = res.get("users", []) if isinstance(res, dict) else []
            users.extend(batch)
            total = res.get("total") if isinstance(res, dict) else None
            if not batch or len(batch) < 1000 or not isinstance(total, int) or len(users) >= total:
                break
        out = []
        for u in users:
            used = int(u.get("used_traffic", 0) or 0)
            cap = int(u.get("data_limit", 0) or 0)
            expire = u.get("expire")
            _on, _cr = u.get("online_at"), u.get("created_at")
            _su = str(u.get("subscription_url") or "").rstrip("/")
            out.append({
                "email": u.get("username"),
                "traffic": {"up": 0, "down": used, "total": cap},
                "totalGB": cap,
                "expiryTime": _expire_to_epoch(expire) * 1000,
                # PATCH_PANEL_YAR_IDLE_V1: برایِ «کاربرانِ بدونِ اتصال» (کلیدهایِ افزوده؛ بقیهٔ مصرف‌کننده‌ها نادیده می‌گیرند)
                "last_online_ms": (_expire_to_epoch(_on) * 1000) if _on else 0,
                "created_ms": (_expire_to_epoch(_cr) * 1000) if _cr else 0,
                "status": u.get("status"),
                "sub_id": _su.rsplit("/", 1)[-1] if _su else None,
            })
        return out

    async def test_connection(self):
        """فقط برای تست تنظیمات پنل توسط فروشنده: True اگر لاگین و لیست
        inbound موفق بود."""
        # === PATCH_LOGIN_THROTTLE_V1 ===
        # قبلاً login(force=True) بود؛ یعنی هر بار زدنِ «تست اتصال» یک لاگینِ
        # واقعی می‌زد و با تکرارِ سریع، بلاکِ ۱۰ثانیه‌ای پنل را دائم تمدید
        # می‌کرد. حالا force نمی‌زنیم: اگر توکنِ معتبرِ کش‌شده داریم همان
        # استفاده می‌شود (لاگینِ اضافه نداریم)؛ و اگر توکن باطل باشد،
        # active_inbound_ids خودش با 401 لاگینِ تازه را trigger می‌کند.
        await self.login()
        ids = await self.active_inbound_ids()
        return {"ok": True, "inbounds": ids}


def usage_summary(usage: dict) -> str:
    """خلاصهٔ خوانا از مصرف برای نمایش به کاربر — عیناً مثل vpn_panel.py."""
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
