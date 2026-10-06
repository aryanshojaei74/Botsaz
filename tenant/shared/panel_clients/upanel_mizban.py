"""
upanel_mizban.py — کلاینت async پنل یوپنل (مرزبان) برای بات‌ساز (چند‌مستأجری).

همان قرارداد متدی PanelClient (پنل سنایی / مرزبان) را پیاده‌سازی می‌کند تا
بقیهٔ کد (vpn_admin.py / vpn_user.py / main.py) بدون شاخه‌زدن روی panel_key،
فقط با dispatch ساده (tenant/vpn_panel_factory.py) از آن استفاده کند.

یوپنل (مرزبان) از همان ساختار REST مرزبان استفاده می‌کند اما endpoint های
اضافه‌ای دارد:
  لاگین:              POST /api/admin/token     (form-urlencoded: username, password)
  اینباندها:          GET  /api/inbounds
  ساخت کاربر:         POST /api/user
  گرفتن کاربر:        GET  /api/user/{username}
  ویرایش کاربر:       PUT  /api/user/{username}
  حذف کاربر:          DELETE /api/user/{username}
  ریست مصرف:          POST /api/user/{username}/reset
  ریست ساب:           POST /api/user/{username}/revoke_sub
  فعال‌سازی دوره بعد: POST /api/user/{username}/active-next  ← اختصاصی یوپنل
  آمار مصرف:          GET  /api/user/{username}/usage
  لیست کاربران:       GET  /api/users
  کاربران منقضی:      GET  /api/users/expired
  حذف منقضی‌ها:       DELETE /api/users/expired
  قالب‌های کاربر:     GET  /api/user_template / POST / PUT / DELETE /{id}
  گزارش اتصال ISP:    GET  /api/isp/user/{username}/connection-logs
  سشن‌های زنده ISP:   GET  /api/isp/sessions/live
  اطلاعات سیستم:      GET  /api/system
  پشتیبان‌گیری:       POST /api/backup/users/export
"""
from __future__ import annotations

import time
import re
import secrets
import string
import asyncio

try:
    import aiohttp
except ImportError:
    aiohttp = None

from tenant.shared.panel_clients.vpn_panel import PanelError, login_domain_root

# === PATCH_UPANEL_USAGE_FALLBACK_V1 ===
from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)
from tenant.shared.panel_clients.marzban_panel import _expire_to_epoch, _looks_like_iso_expire, _epoch_to_iso_z

GB = 1024 * 1024 * 1024

_DEFAULT_PROXIES = {"vless": {"flow": ""}}


def _rand(n=8):
    a = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(a) for _ in range(n))


def gen_sub_id(n=16):
    return _rand(n)


def gen_uuid():
    import uuid as _uuid
    return str(_uuid.uuid4())


def _safe_username(email: str) -> str:
    """یوپنل مرزبان نیز همان محدودیت مرزبان را دارد: [a-z0-9_] و ۳ تا ۳۲ کاراکتر.
    === PATCH_UPANEL_NO_CONSECUTIVE_UNDERSCORE_V1 ===
    قبلاً هر کاراکترِ غیرمجاز جدا با «_» جایگزین می‌شد؛ اگر چند کاراکترِ
    غیرمجاز پشتِ‌هم بودند (مثلاً «.» و «@» کنارِ هم در ایمیل)، چند «_»
    پشتِ‌هم تولید می‌شد که این پنل با خطای ۴۲۲ («Username cannot have
    consecutive special characters») ردش می‌کرد. حالا «_»های پشتِ‌هم یکی
    می‌شوند و از ابتدا/انتهای نام هم حذف می‌شوند."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", str(email)).lower()
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    cleaned = cleaned[:32]
    if len(cleaned) < 3:
        cleaned = (cleaned + "_usr")[:32]
    return cleaned


# === PATCH_UPANEL_TOKEN_CACHE_V1 ===
# گزارش #145 (Sajad): «تحویلِ کانفیگ یوپنل خیلی دیر است». برخلافِ مرزبان/پاسارگاد (که توکنِ لاگین را
# بینِ نمونه‌ها کش می‌کنند)، هر نمونهٔ تازهٔ کلاینتِ یوپنل از صفر لاگین می‌کرد (تا ۹ مسیرِ لاگین × تایم‌اوتِ ۸ثانیه).
# حالا توکنِ معتبر برایِ هر (base_url, username) چند دقیقه مشترک نگه داشته می‌شود؛ ۴۰۱/۴۰۳ همچنان
# لاگینِ اجباریِ تازه می‌زند، پس توکنِ باطل خودش ترمیم می‌شود.
_UP_TOKEN_CACHE: dict = {}
_UP_TOKEN_DEFAULT_TTL = 600


def _up_jwt_exp(token: str) -> int:
    try:
        import base64
        import json as _json
        b = token.split(".")[1]
        b += "=" * (-len(b) % 4)
        return int(_json.loads(base64.urlsafe_b64decode(b)).get("exp", 0))
    except Exception:
        return 0


class UPanelMizbanClient:
    """کلاینت async پنل یوپنل (مرزبان).

    امضای سازنده عیناً مثل PanelClient / MarzbanPanelClient است تا factory
    بدون تغییر بتواند نمونه‌سازی کند.
    """

    def __init__(self, base_url: str, username: str, password: str,
                 sub_base: str = "", inbound_ids=None, verify_ssl: bool = False,
                 timeout: int = 8):
        # === PATCH_PANEL_TIMEOUT_REDUCE_V1 ===
        # نگاه کن به توضیحِ کاملِ همین پچ در tenant/hiddify_panel.py.
        if aiohttp is None:
            raise PanelError("کتابخانهٔ aiohttp نصب نیست. `pip install aiohttp` را اجرا کن.")
        # === PATCH_UPANEL_STRIP_DASHBOARD_PATH_V1 ===
        # همون مشکلِ MarzbanPanelClient اینجا هم هست: فروشنده‌ها گاهی آدرسِ
        # «داشبورد» (مثل host/dashboard یا host/dashboard/xK9fQ2) رو به‌جایِ
        # ریشهٔ دامنه وارد می‌کنن، که باعثِ ۴۰۵ روی همهٔ مسیرهای لاگین می‌شه.
        # قبلاً این حذف فقط تو MarzbanPanelClient بود؛ چون UPanelMizbanClient
        # کلاسِ مستقلیه (نه زیرکلاسِ اون)، این فیکس رو نداشت.
        # === PATCH_STRIP_URL_FRAGMENT_V1 ===
        # نگاه کن به توضیحِ کامل در tenant/marzban_panel.py.
        base_url = (base_url or "").split("#", 1)[0]
        base_url = base_url.rstrip("/")
        base_url = re.sub(r"/dashboard(/[^/]*)?$", "", base_url, flags=re.IGNORECASE)
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        # === PATCH_SUB_BASE_SANITIZE_V1 ===
        _sb = (sub_base or "").strip().strip("ـ-—–_ ").rstrip("/")
        if _sb and not _sb.lower().startswith(("http://", "https://")):
            _sb = ""
        self.sub_base = _sb
        # تگ‌های inbound به‌صورت رشته (مثل «VLESS TCP REALITY»)
        self.inbound_tags = [str(i).strip() for i in inbound_ids] if inbound_ids else []
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = None
        self._token = None
        self._logged_in = False
        self._lock = asyncio.Lock()
        self._inbounds_cache = None  # {protocol: [tag, ...]}

    # ───────────────────────── نشست HTTP ─────────────────────────

    async def _ensure_session(self):
        if self._session and not self._session.closed:
            cur_timeout = self._session.timeout
            if cur_timeout is None or cur_timeout.total != self.timeout:
                await self._session.close()
                self._session = None
                self._logged_in = False
                self._token = None
        if self._session is None or self._session.closed:
            # === PATCH_UPANEL_CONNECT_TIMEOUT_V1 ===
            # بدونِ sock_connect، هاستِ خاموش تا کلِ total منتظر می‌ماند و
            # چون _request دو بار تلاش می‌کند و login هم چند مسیر را
            # امتحان می‌کند، یک آپدیت تا ده‌ها ثانیه طول می‌کشید
            # (هشدارهای «slow update» در لاگ).
            t = aiohttp.ClientTimeout(total=self.timeout, sock_connect=6)
            conn = aiohttp.TCPConnector(ssl=self.verify_ssl)
            self._session = aiohttp.ClientSession(timeout=t, connector=conn)
        return self._session

    # === PATCH_UPANEL_LOGIN_DISCOVERY_V1 ===
    # یوپنل مسیرِ لاگینش را بینِ نسخه‌ها عوض کرده و کاربران خطایِ
    # «HTTP 405 Method Not Allowed» می‌گرفتند — یعنی مسیر هست ولی POST
    # قبول نمی‌کند، پس آدرس عوض شده نه نام‌کاربری/رمز.
    #
    # به‌جایِ هاردکدکردنِ یک مسیر (که با آپدیتِ بعدی دوباره می‌شکند)،
    # مسیرهایِ شناخته‌شده به ترتیب امتحان می‌شوند و اولی که توکن بدهد
    # برایِ همان نشست کش می‌شود. اگر هیچ‌کدام جواب ندهند، گزارشِ دقیقِ
    # اینکه هر مسیر چه پاسخی داد به کاربر نشان داده می‌شود تا بتواند
    # مسیرِ درست را به پشتیبانی بگوید.
    _LOGIN_PATHS = (
        ("/api/admin/token", "form"),
        ("/api/admins/token", "form"),
        ("/api/admin/login", "form"),
        ("/api/admin/login", "json"),
        ("/api/login", "json"),
        ("/api/login", "form"),
        ("/api/v1/admin/token", "form"),
        ("/api/auth/login", "json"),
        ("/api/token", "form"),
    )

    # مسیری که در این پروسه جواب داده — تا دفعهٔ بعد دوباره همه امتحان نشوند
    _known_login_path: tuple[str, str] | None = None

    @staticmethod
    def _extract_token(payload: dict) -> str:
        """توکن را از پاسخ بیرون می‌کشد؛ نامِ فیلد بینِ نسخه‌ها فرق دارد."""
        for k in ("access_token", "token", "accessToken", "jwt"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                return v
        # بعضی نسخه‌ها توکن را داخلِ data می‌گذارند
        data = payload.get("data")
        if isinstance(data, dict):
            for k in ("access_token", "token", "accessToken", "jwt"):
                v = data.get(k)
                if isinstance(v, str) and v:
                    return v
        return ""

    async def _try_login_paths(self, s, base_url: str, form: dict, js: dict, paths: list) -> tuple[str | None, list[str]]:
        """یک دورِ کاملِ امتحانِ مسیرهایِ شناخته‌شدهٔ لاگین رویِ یک base_url
        مشخص. توکن (در صورتِ موفقیت) یا None، به‌همراه لیستِ نتایجِ هر
        مسیر (برایِ پیامِ خطا) برمی‌گرداند. ۴۰۱/۴۰۳ همچنان فوراً raise
        می‌شود (یعنی مسیر درست بوده، فقط اعتبارنامه غلطه)."""
        tried: list[str] = []
        import json as _json
        # === PATCH_UPANEL_LOGIN_BUDGET_V1 ===
        # سقفِ زمانیِ کلِ جستجویِ مسیرِ لاگین. بدونِ آن، پنلی که کند
        # جواب می‌دهد (ولی وصل می‌شود) ۹ مسیر × timeout طول می‌کشید.
        _deadline = time.monotonic() + max(self.timeout * 2, 20)
        for path, kind in paths:
            if time.monotonic() > _deadline:
                tried.append("… جستجو به‌خاطرِ کندیِ پنل متوقف شد")
                break
            url = f"{base_url}{path}"
            try:
                kw = {"data": form} if kind == "form" else {"json": js}
                async with s.post(url, **kw) as r:
                    text = await r.text()
                    if r.status in (404, 405):
                        tried.append(f"{path} [{kind}] → {r.status}")
                        continue
                    if r.status != 200:
                        tried.append(f"{path} [{kind}] → {r.status}")
                        # ۴۰۱/۴۰۳ یعنی مسیر درست است ولی نام‌کاربری یا
                        # رمز غلط — ادامهٔ جستجو بی‌فایده است.
                        if r.status in (401, 403):
                            raise PanelError(
                                "نام کاربری یا رمزِ پنل یوپنل درست نیست "
                                f"(HTTP {r.status}).")
                        continue
                    try:
                        payload = _json.loads(text)
                    except ValueError:
                        tried.append(f"{path} [{kind}] → پاسخِ نامعتبر")
                        continue
                    token = self._extract_token(payload if isinstance(payload, dict) else {})
                    if not token:
                        tried.append(f"{path} [{kind}] → بدونِ توکن")
                        continue
                    type(self)._known_login_path = (path, kind)
                    return token, tried
            except PanelError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as e:
                raise PanelError(
                    f"اتصال به پنل یوپنل برقرار نشد یا بیش از حد طول کشید. "
                    f"آدرس/پورت و دسترس‌پذیری سرور را بررسی کن: {e}") from e
        return None, tried

    async def login(self, force=False):
        async with self._lock:
            if self._logged_in and not force:
                return True
            _ck = (self.base_url, self.username)
            if not force:
                _c = _UP_TOKEN_CACHE.get(_ck)
                if _c and _c["exp"] - 30 > time.time():
                    self._token = _c["token"]
                    self._logged_in = True
                    return True
            s = await self._ensure_session()

            form = {
                "username": self.username,
                "password": self.password,
                "grant_type": "password",
                "scope": "",
                "client_id": "",
                "client_secret": "",
            }
            js = {"username": self.username, "password": self.password}

            # اگر قبلاً مسیرِ درست پیدا شده، اول همان
            paths = list(self._LOGIN_PATHS)
            if self._known_login_path and self._known_login_path in paths:
                paths.remove(self._known_login_path)
                paths.insert(0, self._known_login_path)

            token, tried = await self._try_login_paths(s, self.base_url, form, js, paths)
            # === PATCH_LOGIN_ROOT_FALLBACK_V1 ===
            # اگه هیچ مسیری روی base_urlِ داده‌شده جواب نداد، ممکنه خودِ
            # base_url اشتباه باشه (مثلاً آدرسِ صفحهٔ لاگینِ camouflage‌شده،
            # نه ریشهٔ واقعیِ API — نگاه کن به توضیحِ کاملِ در
            # tenant/marzban_panel.py). یه‌بار همون مسیرها را روی ریشهٔ
            # دامنه هم امتحان کن.
            if token is None:
                root = login_domain_root(self.base_url)
                if root:
                    logger.info(
                        "upanel_mizban: هیچ مسیرِ لاگینی روی %s جواب نداد — "
                        "یک‌بار با ریشهٔ دامنه (%s) امتحان می‌شود.",
                        self.base_url, root)
                    token2, tried2 = await self._try_login_paths(s, root, form, js, paths)
                    if token2 is not None:
                        logger.info(
                            "upanel_mizban: فال‌بکِ ریشهٔ دامنه موفق بود؛ "
                            "base_url از %s به %s تغییر کرد.", self.base_url, root)
                        self.base_url = root
                        token = token2
                    else:
                        tried += [f"(ریشهٔ دامنه) {t}" for t in tried2]

            if token is None:
                raise PanelError(
                    "ورود به پنل یوپنل ناموفق بود؛ هیچ‌کدام از مسیرهایِ شناخته‌شدهٔ "
                    "لاگین جواب ندادند.\n\n"
                    "• مطمئن شو آدرس پنل کامل و بدونِ / انتهایی باشد "
                    "(مثلاً <code>https://panel.example.com:8000</code>)\n"
                    "• اگر پنل زیرمسیر دارد، آن را هم در آدرس بیاور\n\n"
                    "نتیجهٔ تلاش‌ها:\n" + "\n".join(tried[:9]))
            self._token = token
            self._logged_in = True
            _exp = _up_jwt_exp(token) or int(time.time()) + _UP_TOKEN_DEFAULT_TTL
            _UP_TOKEN_CACHE[(self.base_url, self.username)] = {"token": token, "exp": _exp}
            return True

    # === PATCH_PANEL_TIMING_LOG_V1 === (همان زمان‌سنجیِ مرزبان؛ یوپنل تا حالا در لاگ «نامرئی» بود)
    async def _request(self, method, path, **kwargs):
        _t0 = time.monotonic()
        try:
            return await self._request_timed(method, path, **kwargs)
        finally:
            _d = time.monotonic() - _t0
            if _d >= 2.0:
                logger.warning('پنلِ کند: %s %s | %.1fs | %s', method, path, _d,
                               getattr(self, 'base_url', '?'))

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
        # گزارشِ واقعیِ ادمین: «یه‌وقتایی اتصال برقرار نمی‌شه». دو تا فیکس:
        # ۱) قبلاً رویِ خطایِ خالصِ اتصال (نه پاسخِ HTTP)، فقط ۱ بار دوباره
        #    امتحان می‌کرد؛ الان تا ۳ تلاش، با یه مکثِ کوتاه قبلِ آخری.
        # ۲) قبلاً حتی رویِ خطایِ خالصِ اتصال هم force-relogin می‌زد — که
        #    بی‌فایده‌ست (خطایِ اتصال ربطی به اعتبارِ توکن نداره) و فقط یه
        #    hitِ اضافه رویِ endpointِ لاگین اضافه می‌کنه؛ الان فقط رویِ
        #    ۴۰۱/۴۰۳ (که واقعاً یعنی توکن رد شده) relogin می‌زنیم.
        auth_retried = False
        conn_attempts_left = 2
        while True:
            try:
                async with s.request(method, url, **kwargs) as r:
                    text = await r.text()
                    if r.status in (401, 403) and not auth_retried:
                        auth_retried = True
                        await self.login(force=True)
                        kwargs["headers"]["Authorization"] = f"Bearer {self._token}"
                        continue
                    if r.status == 404:
                        return None
                    if r.status >= 400:
                        raise PanelError(f"{method} {path} HTTP {r.status}: {text[:300]}")
                    if not text:
                        return {}
                    try:
                        import json as _json
                        return _json.loads(text)
                    except Exception:
                        return {"raw": text}
            except PanelError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as e:
                if conn_attempts_left > 0:
                    conn_attempts_left -= 1
                    if conn_attempts_left == 0:
                        await asyncio.sleep(0.7)
                    continue
                # === PATCH_TIMEOUT_ERROR_MSG_V1 / PATCH_PANEL_POOL_SELFHEAL_V1 ===
                # نگاه کن به توضیحِ کاملِ همین دو پچ در tenant/marzban_panel.py.
                err_detail = str(e) or f"{type(e).__name__} (timeout={self.timeout}s)"
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

    async def __aenter__(self):
        await self.login()
        return self

    async def __aexit__(self, *a):
        await self.close()

    # ───────────────────────── اینباندها ─────────────────────────

    async def list_inbounds(self):
        """خروجی معمولِ مرزبان: {protocol: [{tag, protocol, network, tls, port}, ...]}.
        اما این نسخه/فورکِ یوپنل به‌جای آن، یک «لیستِ ساده از رشته» (فقط
        نامِ تگ‌ها، مثلِ ["tcp", "Reality", "TUUL", ...]) برمی‌گرداند — نه
        دیکشنری و نه لیستی از آبجکت. res.items() روی این حالت با
        AttributeError کرش می‌کرد. این‌جا هر سه شکلِ پاسخ پشتیبانی می‌شود؛
        در هر حالت self._inbounds_cache به‌صورتِ دیکشنریِ {protocol: [items]}
        نرمال‌سازی می‌شود. چون این فرمتِ رشته‌ای، پروتکلِ واقعیِ هر تگ را
        اعلام نمی‌کند، پروتکل روی "unknown" گذاشته می‌شود (فقط برای گروه‌بندیِ
        داخلی استفاده می‌شود، نه ارسال به پنل).
        """
        res = await self._request("GET", "/api/inbounds") or {}
        flat = []
        by_proto: dict[str, list[dict]] = {}

        if isinstance(res, dict):
            for proto, items in res.items():
                for ib in items or []:
                    by_proto.setdefault(proto, []).append(ib)
        elif isinstance(res, list):
            for ib in res:
                if isinstance(ib, str):
                    by_proto.setdefault("unknown", []).append({"tag": ib, "protocol": "unknown"})
                elif isinstance(ib, dict):
                    proto = ib.get("protocol") or "unknown"
                    by_proto.setdefault(proto, []).append(ib)

        for proto, items in by_proto.items():
            for ib in items:
                flat.append({
                    "id": ib.get("tag"), "tag": ib.get("tag"),
                    "protocol": ib.get("protocol", proto),
                    "remark": ib.get("tag"), "enable": True,
                })
        self._inbounds_cache = by_proto
        return flat

    async def active_inbound_ids(self):
        """تگ‌های inbound هدف؛ اگر ذخیره نشده، همهٔ تگ‌های موجود برگردانده می‌شود."""
        if self.inbound_tags:
            return list(self.inbound_tags)
        inbounds = await self.list_inbounds()
        tags = [ib["tag"] for ib in inbounds if ib.get("tag")]
        if not tags:
            raise PanelError("هیچ inbound فعالی روی پنل یوپنل پیدا نشد.")
        return tags

    async def _inbounds_by_protocol(self, tags: list[str]) -> dict:
        """تگ‌های هدف را بر اساس پروتکل دسته‌بندی می‌کند ({protocol: [tag, ...]})."""
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

    # ───────────────────────── ساخت کاربر ─────────────────────────

    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0,
                             on_hold=False):
        """ساخت کاربر در یوپنل مرزبان با همان امضای create_account پنل سنایی.

        gb: حجم به گیگ (0=نامحدود). mb: حجم به مگابایت (برای سرویس تست).
        days>0 → expire به‌صورت UTC timestamp مطلق (ثانیه).
        days=0 → expire=0 (بی‌انقضا).
        limit_ip: محدودیت همزمان اتصال (در صورت پشتیبانی پنل اعمال می‌شود).

        === PATCH_TRIAL_HOURLY_V1 ===
        hours: مدتِ اضافه به ساعت (روی days). expire ثانیه‌ست پس ساعتی هم
        پشتیبانی می‌شه.
        """
        username = _safe_username(email)
        tags = inbound_ids if inbound_ids is not None else await self.active_inbound_ids()
        inbounds_body = await self._inbounds_by_protocol(tags)
        # === PATCH_UPANEL_EMPTY_INBOUNDS_FIX_V1 ===
        # همون باگی که تویِ tenant/marzban_panel.py با
        # PATCH_MARZBAN_EMPTY_INBOUNDS_FIX_V1 فیکس شده بود، اینجا (یوپنلِ
        # مرزبان) هیچ‌وقت کپی نشده بود: اگه تگ‌هایِ اینباندِ تنظیم‌شده رو
        # پنل دیگه نداشته باشه، inbounds_body خالی می‌مونه ولی proxies
        # به‌خاطرِ همین «or dict(_DEFAULT_PROXIES)» بی‌سروصدا پر از
        # پروتکل‌هایِ پیش‌فرض می‌شد — یعنی یه درخواستِ ناهماهنگ (proxies پر،
        # inbounds خالی) می‌رفت که معمولاً باعثِ یه ۵۰۰ِ گنگ از پنل می‌شد،
        # نه یه پیامِ روشن. الان قبل از فرستادنِ درخواست همین‌جا هم متوقف
        # می‌شه و دقیقاً می‌گه چرا.
        if not inbounds_body:
            raise PanelError(
                "ساخت کانفیگ ناموفق: هیچ‌کدوم از اینباندهایِ تنظیم‌شده رویِ پنل پیدا نشد. "
                "احتمالاً اینباندِ موردنظر رویِ خودِ پنلِ یوپنل حذف یا تغییرِ اسم داده. "
                "از تنظیماتِ این پنل (تویِ ربات) دوباره اینباندها رو انتخاب/به‌روزرسانی کن، "
                "یا از پنلِ وبِ یوپنل مطمئن شو اون اینباند هنوز هست.")
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
        res = await self._request("POST", "/api/user", json=body)
        if res is None:
            raise PanelError("ساخت اکانت ناموفق: کاربری با این نام از قبل وجود دارد.")
        sub_url = res.get("subscription_url") or ""
        return {
            "uuid": uuid_str or gen_uuid(), "sub_id": sub_id or gen_sub_id(),
            "email": username, "gb": gb, "days": days,
            "inbounds": tags, "sub_link": self._abs_sub_link(sub_url),
            "expiry_ms": expire * 1000 if expire else 0,
        }

    def _abs_sub_link(self, sub_url: str) -> str:
        """مسیر نسبی ساب‌لینک را به آدرس کامل تبدیل می‌کند."""
        if not sub_url:
            return ""
        if sub_url.startswith("http://") or sub_url.startswith("https://"):
            return sub_url
        base = self.sub_base or self.base_url
        return f"{base.rstrip('/')}/{sub_url.lstrip('/')}"

    # ───────────────────────── لینک‌های اشتراک ─────────────────────────

    def sub_link(self, sub_id):
        """برای سازگاری امضا؛ از get_client استفاده کن تا لینک واقعی را بگیری."""
        return f"{self.sub_base}/{sub_id}" if self.sub_base else ""

    async def get_sub_links(self, sub_id):
        return []

    async def get_client_links(self, email):
        """لینک‌های مستقیم (تکی) هر inbound برای این کاربر.

        بعضی نصب‌های UPanel در `subscription_url` هاست اشتراک جداگانه‌ای
        می‌فرستند که از سرور بات‌ساز 403 می‌دهد؛ همان مسیر `/sub/...` روی
        ریشهٔ پنل API ممکن است سالم باشد. هر دو مقصد را فقط read-only و به
        ترتیب امتحان می‌کنیم و اولین خروجی معتبر را برمی‌گردانیم.
        """
        cur = await self.get_client(email)
        if cur and cur.get("links"):
            return cur["links"]
        sub_url = (cur or {}).get("sub_link") or self._abs_sub_link(
            (cur or {}).get("subscription_url", ""))
        if not sub_url:
            return []

        candidates = [sub_url]
        try:
            from urllib.parse import urlsplit, urlunsplit
            declared = urlsplit(sub_url)
            panel = urlsplit(str(self.base_url))
            if declared.path and panel.scheme and panel.netloc:
                panel_sub = urlunsplit((
                    panel.scheme, panel.netloc, declared.path,
                    declared.query, declared.fragment,
                ))
                if panel_sub not in candidates:
                    candidates.append(panel_sub)
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

        for index, candidate in enumerate(candidates):
            links = await self._fetch_links_from_sub(candidate)
            if links:
                if index > 0:
                    logger.info(
                        "ساب‌لینک هاست اعلامیِ یوپنل در دسترس نبود؛ "
                        "fallback ریشهٔ پنل برای %s موفق شد (%d لینک)",
                        email, len(links),
                    )
                return links
        return []

    async def _fetch_links_from_sub(self, sub_url: str) -> list[str]:
        """=== PATCH_SUB_UNIFIED_V1 === به sub_fetch.fetch_sub_links واگذار شد:
        نشستِ تازه (بدونِ توکنِ پنل)، چند UA روی ۴۰۳/۵xx، و تبدیلِ JSONِ Xray به لینک."""
        from tenant.shared.panel_clients.sub_fetch import fetch_sub_links
        return await fetch_sub_links(sub_url)

    async def get_client(self, email):
        username = _safe_username(email)
        res = await self._request("GET", f"/api/user/{username}")
        if not res:
            return None
        res = dict(res)
        res["sub_link"] = self._abs_sub_link(res.get("subscription_url", ""))
        res["subId"] = username
        res["id"] = username
        return res

    async def get_usage(self, email):
        """آمار مصرف با کلیدهای استاندارد: up, down, total, expiryTime (ms)."""
        # ابتدا از endpoint اختصاصی usage تلاش می‌کنیم
        username = _safe_username(email)
        # === PATCH_UPANEL_USAGE_FALLBACK_V1 ===
        # این endpoint در بعضی نسخه‌های یوپنل وجود ندارد یا برای بعضی
        # کاربران HTTP 500 می‌دهد. کدِ زیر از اول «fallback» داشت، ولی
        # _request روی خطای ۵۰۰ استثنا پرتاب می‌کرد و هرگز به fallback
        # نمی‌رسید — نتیجه: کاربر مصرفش را نمی‌دید. حالا خطا بلعیده
        # می‌شود و از اطلاعاتِ خودِ کاربر محاسبه می‌کنیم.
        try:
            usage_res = await self._request("GET", f"/api/user/{username}/usage")
        except PanelError:
            logger.warning("usage endpoint برایِ %s جواب نداد؛ fallback به get_client",
                           username)
            usage_res = None
        if usage_res and isinstance(usage_res, dict):
            used = int(usage_res.get("used_traffic", 0) or 0)
            cap = int(usage_res.get("data_limit", 0) or 0)
            expire = usage_res.get("expire")
            expiry_ms = _expire_to_epoch(expire) * 1000
            return {
                "up": 0, "down": used,
                "total": cap, "expiryTime": expiry_ms,
                "enable": usage_res.get("status") == "active",
            }
        # fallback: از اطلاعات کاربر
        cur = await self.get_client(email)
        if not cur:
            return None
        used = int(cur.get("used_traffic", 0) or 0)
        cap = int(cur.get("data_limit", 0) or 0)
        expire = cur.get("expire")
        expiry_ms = _expire_to_epoch(expire) * 1000
        return {
            "up": 0, "down": used,
            "total": cap, "expiryTime": expiry_ms,
            "enable": cur.get("status") == "active",
        }

    # ───────────────────────── تمدید / ترافیک ─────────────────────────

    async def _modify(self, email, **fields):
        username = _safe_username(email)
        res = await self._request("PUT", f"/api/user/{username}", json=fields)
        if res is None:
            raise PanelError("کاربر روی پنل یوپنل پیدا نشد.")
        return res

    async def add_traffic(self, email, gb):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر روی پنل یوپنل پیدا نشد.")
        cur_limit = int(cur.get("data_limit", 0) or 0)
        if cur_limit == 0:
            return cur  # نامحدود است
        new_limit = cur_limit + int(gb) * GB
        return await self._modify(email, data_limit=new_limit)

    async def renew(self, email, days, reset_usage=False):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر روی پنل یوپنل پیدا نشد.")
        raw_expire = cur.get("expire")
        cur_expire = _expire_to_epoch(raw_expire)
        # === PATCH_RENEW_UNLIMITED_TIME_GUARD_V1 ===
        # مثلِ گاردِ اضافه‌شده به renew مرزبان: اگه از قبل زمان نامحدود
        # بود (expire خالی → cur_expire==0)، تمدید را بی‌خطر رد می‌کنیم.
        if not raw_expire and cur_expire == 0:
            return cur
        now = int(time.time())
        base = cur_expire if cur_expire > now else now
        new_expire_epoch = base + int(days) * 86400
        new_expire = _epoch_to_iso_z(new_expire_epoch) if _looks_like_iso_expire(raw_expire) else new_expire_epoch
        obj = await self._modify(email, expire=new_expire, status="active")
        if reset_usage:
            await self.reset_usage(email)
        return obj

    async def activate_next_period(self, email):
        """فعال‌سازی دوره بعدی اشتراک — endpoint اختصاصی یوپنل مرزبان.
        پس از منقضی شدن، به‌جای تمدید دستی، این متد دوره جدید را فعال می‌کند."""
        username = _safe_username(email)
        res = await self._request("POST", f"/api/user/{username}/active-next", json={})
        return res is not None

    async def reset_usage(self, email):
        username = _safe_username(email)
        res = await self._request("POST", f"/api/user/{username}/reset", json={})
        return res is not None

    async def reset_sub(self, email, new_sub_id=None):
        """ریست لینک اشتراک — ساب‌لینک قدیمی باطل و لینک جدید صادر می‌شود."""
        username = _safe_username(email)
        res = await self._request("POST", f"/api/user/{username}/revoke_sub", json={})
        if res is None:
            raise PanelError("ریست لینک ناموفق: کاربر پیدا نشد.")
        return self._abs_sub_link(res.get("subscription_url", ""))

    async def rename_account(self, old_email: str, new_email: str):
        """یوزرنیم همان شناسهٔ اکانت است و قابل تغییر روی اکانت‌های موجود نیست."""
        raise PanelError(
            "در یوپنل مرزبان نام کاربری همان شناسهٔ اکانت است و قابل تغییر "
            "روی اکانت‌های موجود نیست؛ نام جدید فقط روی کانفیگ‌های تازه اعمال می‌شود.")

    async def set_enabled(self, email, enabled: bool):
        """فعال/غیرفعال کردن کاربر با فیلد status (active/disabled)."""
        await self._modify(email, status=("active" if enabled else "disabled"))
        return True

    async def delete_account(self, email, keep_traffic=False):
        username = _safe_username(email)
        res = await self._request("DELETE", f"/api/user/{username}")
        return res is not None or True

    # ───────────────────────── لیست / آمار کلی ─────────────────────────

    async def list_clients(self):
        """لیست کاربران با کلیدهای هم‌نام PanelClient (برای vpn_expiry_loop)."""
        # === PATCH_LIST_USERS_LONG_TIMEOUT_V1 ===
        # قبلاً فقط ۱۰۰۰ کاربرِ اول خوانده می‌شد (پنل‌هایِ بزرگ‌تر بی‌صدا ناقص
        # می‌ماندند و هشدارِ انقضا برایِ بقیه نمی‌رفت) و با تایم‌اوتِ ۸ثانیه‌ای
        # رویِ پنل‌هایِ کند شکست می‌خورد. مثلِ مرزبان صفحه‌بندی + مهلتِ بلندتر.
        _list_timeout = aiohttp.ClientTimeout(total=max(float(self.timeout or 0), 45.0))
        users: list = []
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
            out.append({
                "email": u.get("username"),
                "traffic": {"up": 0, "down": used, "total": cap},
                "totalGB": cap,
                "expiryTime": _expire_to_epoch(expire) * 1000,
            })
        return out

    async def get_expired_users(self):
        """لیست کاربران منقضی‌شده — endpoint اختصاصی یوپنل."""
        res = await self._request("GET", "/api/users/expired") or {}
        if isinstance(res, list):
            return res
        return res.get("users", [])

    async def delete_expired_users(self):
        """حذف همهٔ کاربران منقضی‌شده — endpoint اختصاصی یوپنل."""
        res = await self._request("DELETE", "/api/users/expired")
        return res is not None

    # ───────────────────────── امکانات ISP اختصاصی یوپنل ─────────────────────────

    async def get_connection_logs(self, email, limit: int = 50):
        """گزارش اتصالات یک کاربر (لاگ ISP) — اختصاصی یوپنل مرزبان."""
        username = _safe_username(email)
        res = await self._request(
            "GET", f"/api/isp/user/{username}/connection-logs",
            params={"limit": limit})
        return res or []

    async def get_live_sessions(self):
        """سشن‌های زنده همهٔ کاربران — اختصاصی یوپنل مرزبان."""
        res = await self._request("GET", "/api/isp/sessions/live")
        return res or []

    async def get_user_timeline(self, email):
        """تایم‌لاین فعالیت یک کاربر — اختصاصی یوپنل مرزبان."""
        username = _safe_username(email)
        res = await self._request("GET", f"/api/isp/user/{username}/timeline")
        return res or {}

    async def kick_user_sessions(self, email):
        """قطع همهٔ سشن‌های فعال یک کاربر — اختصاصی یوپنل مرزبان."""
        username = _safe_username(email)
        res = await self._request("POST", f"/api/isp/user/{username}/sessions/kick", json={})
        return res is not None

    # ───────────────────────── قالب‌های کاربر ─────────────────────────

    async def get_templates(self):
        """لیست قالب‌های کاربری از پنل."""
        res = await self._request("GET", "/api/user_template")
        if isinstance(res, list):
            return res
        return (res or {}).get("templates", [])

    async def get_template(self, template_id: int):
        """گرفتن یک قالب خاص."""
        return await self._request("GET", f"/api/user_template/{template_id}")

    # ───────────────────────── اطلاعات سیستم ─────────────────────────

    async def get_system_info(self):
        """اطلاعات کلی سیستم (مصرف CPU/RAM/دیسک)."""
        return await self._request("GET", "/api/system") or {}

    async def get_inbounds_info(self):
        """اطلاعات ورودی‌ها (inbounds) همراه با آمار مصرف."""
        return await self._request("GET", "/api/inbounds") or {}

    # ───────────────────────── پشتیبان‌گیری ─────────────────────────

    async def export_users_backup(self):
        """خروجی گرفتن از همهٔ کاربران (backup/export)."""
        res = await self._request("POST", "/api/backup/users/export", json={})
        return res or {}

    # ───────────────────────── تست اتصال ─────────────────────────

    async def test_connection(self):
        """تست اتصال — لاگین + گرفتن inboundها. True اگر موفق بود."""
        # === PATCH_LOGIN_THROTTLE_V1 ===
        # همون فیکسِ marzban_panel.py — قبلاً force=True بود و چک‌کردنِ
        # دوره‌ایِ panel_watchdog.py (هر ۵ دقیقه) هر بار یک لاگینِ واقعی
        # می‌زد و باعثِ فلپینگِ «قطع/وصل» می‌شد.
        await self.login()
        ids = await self.active_inbound_ids()
        sys_info = await self.get_system_info()
        return {
            "ok": True,
            "inbounds": ids,
            "version": sys_info.get("version", "نامشخص"),
        }


# ───────────────────────── کمک‌تابع نمایشی ─────────────────────────

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
