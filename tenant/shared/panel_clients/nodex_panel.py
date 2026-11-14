"""
nodex_panel.py — کلاینت async پنل نودکس (GuardCore API) برای بات‌ساز (چند‌مستأجری).
=== PATCH_NODEX_PANEL_V1 ===

همان قرارداد متدی PanelClient (vpn_panel.py) را پیاده‌سازی می‌کند تا بقیهٔ کد
(vpn_admin.py / vpn_user.py / main.py) بدون شاخه‌زدن روی panel_key، فقط با
dispatch ساده (tenant/vpn_panel_factory.py) از آن استفاده کند.

منبع: openapi.json گرفته‌شده از https://api.cyberpower.space/docs
(عنوانِ اسپک: «GuardCore API»، نسخهٔ 0.13.0). این پنل با خانوادهٔ
مرزبان/پاسارگارد فرق دارد: به‌جایِ «user» از «subscription» و به‌جایِ
«inbound» از «service» استفاده می‌کند.

مسیرهای API:
  لاگین:                POST   /api/admins/token           (form-urlencoded: username, password, grant_type=password)
  لیستِ سرویس‌ها:        GET    /api/services               → [{id, remark, node_ids, users_count}]
  لیستِ سابسکریپشن‌ها:   GET    /api/subscriptions
  ساختِ سابسکریپشن:      POST   /api/subscriptions           (آرایه — bulk create)
  گرفتنِ یک سابسکریپشن:  GET    /api/subscriptions/{username}
  ویرایش:                PUT    /api/subscriptions/{username}
  حذف (bulk):            DELETE /api/subscriptions           (body: {usernames: [...]})
  فعال/غیرفعال (bulk):   POST   /api/subscriptions/enable | /disable   (body: {usernames: [...]})
  ریست مصرف (bulk):      POST   /api/subscriptions/reset            (body: {usernames: [...]})
  ریست لینک/کلید (bulk): POST   /api/subscriptions/revoke           (body: {usernames: [...]})

نکات مهمِ اسپک (طبقِ SubscriptionCreate/Response در openapi.json):
  - username رشته؛ محدودیتِ دقیقِ کاراکتری در اسپک نیامده — برای امنیت همان
    قاعدهٔ [a-z0-9_] (۳ تا ۳۲ کاراکتر) خانوادهٔ مرزبان را اعمال می‌کنیم.
  - limit_usage به بایت است؛ 0 = نامحدود.
  - service_ids آرایه‌ای از عددِ صحیح است (نه تگِ رشته‌ای مثلِ مرزبان) — پس
    این پنل در vpn_panel_factory.py به‌عنوانِ «عددمحور» ثبت شده (مثلِ سنایی)،
    نه در _TAG_BASED_PANELS.
  - ⚠️ فرضِ اصلاح‌نشده: اسپک برایِ limit_expire فقط «integer» گفته و توضیح
    نداده. با توجه به الگویِ رایجِ این خانواده از پنل‌ها (و اینکه response
    هیچ فیلدِ دیگری برایِ زمانِ انقضا ندارد، فقط بولینِ «expired»)، اینجا
    limit_expire را «epoch ثانیه» (مثلِ expire در مرزبان) فرض کرده‌ایم؛
    0 = بی‌انقضا. اگر بعدِ اولین «تستِ اتصال» ساختِ اکانت با خطای واضح از
    پنل ردّ شد یا expiredِ برگشتی منطقی نبود، همین‌جا باید اصلاح شود.
  - عملیاتِ enable/disable/reset/revoke/delete همه bulk هستند (حداکثر ۱۰
    یوزرنیم در هر درخواست طبقِ اسپک)؛ اینجا همیشه با یک‌آیتم صدا زده می‌شوند.
  - رنیم: بر خلافِ مرزبان، SubscriptionUpdate فیلدِ username دارد — یعنی
    این پنل تغییرِ نامِ کاربری را (با PUT) پشتیبانی می‌کند.
"""
# === PATCH_NODEX_GOGUARD_V2 ===
# نسخهٔ جدیدِ پنل («GoGuard API 1.0»، Swagger 2) با «GuardCore 0.13» فرق دارد:
#   • لاگین: بدنهٔ JSON {username,password} و پاسخ {"token": ...} (نه فرم/access_token)
#   • سرویس‌ها: صفحه‌بندی‌شده {items:[{id,name}]} (نه آرایهٔ ساده با remark)
#   • ساخت: فیلدِ «services» (نه service_ids)، لینک در «subscription_link»
#   • دریافتِ یک اشتراک: GET /api/subscriptions?usernames=<u> (مسیرِ تکیِ {username} حذف شده)
#   • ویرایش: PUT /api/subscriptions با {usernames:[u], ...فیلدها}
#   • مصرفِ فعلی: total_usage - reset_usage (فیلدِ current_usage دیگر نیست)
# کلاینت هر دو نسخه را پشتیبانی می‌کند؛ نسخه موقعِ لاگین تشخیص داده می‌شود (self._v2).
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

from tenant.shared.panel_clients.vpn_panel import PanelError, frontend_404_hint, is_static_frontend_405, login_domain_root

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

GB = 1024 * 1024 * 1024


def _rand(n=8):
    a = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(a) for _ in range(n))


def gen_sub_id(n=16):
    return _rand(n)


def gen_uuid():
    import uuid as _uuid
    return str(_uuid.uuid4())


def _safe_username(email: str) -> str:
    """[a-z0-9_]، ۳ تا ۳۲ کاراکتر — نگاه کن به یادداشتِ بالایِ فایل."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", str(email)).lower()
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    cleaned = cleaned[:32]
    if len(cleaned) < 3:
        cleaned = (cleaned + "_usr")[:32]
    return cleaned


class NodexPanelClient:
    """یک نشست به پنل نودکس (GuardCore) یک مستأجر. اعتبارنامه در سازنده داده می‌شود.

    امضای سازنده عیناً مثل PanelClient/MarzbanPanelClient است تا factory بدون
    تغییر بتواند نمونه‌سازی کند. inbound_ids اینجا service_id های عددی است.
    """

    def __init__(self, base_url: str, username: str, password: str,
                 sub_base: str = "", inbound_ids=None, verify_ssl: bool = False,
                 timeout: int = 8):
        if aiohttp is None:
            raise PanelError("کتابخانهٔ aiohttp نصب نیست. `pip install aiohttp` را اجرا کن.")
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
        self.inbound_ids = [int(i) for i in inbound_ids] if inbound_ids else []
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = None
        self._token = None
        self._logged_in = False
        self._v2 = False
        self._login_mode = "form"
        self._lock = asyncio.Lock()

    async def _ensure_session(self):
        if self._session and not self._session.closed:
            cur_timeout = self._session.timeout
            if cur_timeout is None or cur_timeout.total != self.timeout:
                await self._session.close()
                self._session = None
                self._logged_in = False
                self._token = None
        if self._session is None or self._session.closed:
            t = aiohttp.ClientTimeout(total=self.timeout, sock_connect=6)
            conn = aiohttp.TCPConnector(ssl=self.verify_ssl)
            self._session = aiohttp.ClientSession(timeout=t, connector=conn)
        return self._session

    async def login(self, force=False):
        async with self._lock:
            if self._logged_in and not force:
                return True
            s = await self._ensure_session()
            import json as _json
            data = {
                "username": self.username, "password": self.password,
                "grant_type": "password", "scope": "",
                "client_id": "", "client_secret": "",
            }
            async def _post(base, mode):
                kw = {"json": {"username": self.username, "password": self.password}} \
                    if mode == "json" else {"data": data}
                async with s.post(f"{base}/api/admins/token", **kw) as r:
                    return r.status, await r.text()

            modes = ["json", "form"] if self._login_mode == "json" else ["form", "json"]
            status, text = 0, ""
            for _mode in modes:
                try:
                    status, text = await _post(self.base_url, _mode)
                except PanelError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as e:
                    raise PanelError(
                        f"اتصال به پنل نودکس برقرار نشد یا بیش از حد طول کشید. "
                        f"آدرس/پورت و دسترس‌پذیری سرور را بررسی کن: {e}") from e

                # === PATCH_LOGIN_ROOT_FALLBACK_V1 ===
                # نگاه کن به توضیحِ کاملِ آن در tenant/marzban_panel.py.
                if status == 405 and is_static_frontend_405(text):
                    root = login_domain_root(self.base_url)
                    if root:
                        try:
                            status2, text2 = await _post(root, _mode)
                        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError):
                            status2 = None
                        if status2 is not None:
                            status, text = status2, text2
                            if status == 200:
                                logger.info("nodex_panel: فال‌بکِ ریشهٔ دامنه موفق بود؛ "
                                            "base_url از %s به %s تغییر کرد.", self.base_url, root)
                                self.base_url = root
                if status == 200:
                    self._login_mode = _mode
                    break
                if status in (401, 403):
                    break   # اعتبارنامهٔ غلط؛ حالتِ دیگر فرقی نمی‌کند

            if status != 200:
                # === PATCH_NODEX_404_DIAGNOSTIC_V1 ===
                # یه گزارشِ واقعی از یه مستأجر: 404 با بدنه‌ی
                # {"error":true,"url":...,"statusCode":404,...} —
                # شکلِ صفحه‌ی ۴۰۴ خودِ Nuxt/Nitro، نه پاسخِ API نودکس.
                # جزئیاتِ کامل: frontend_404_hint در tenant/vpn_panel.py.
                raise PanelError(
                    f"ورود به پنل نودکس ناموفق بود (HTTP {status})؛ "
                    f"آدرس/نام‌کاربری/رمز را بررسی کن: {text[:200]}"
                    f"{frontend_404_hint(text)}")
            try:
                p = _json.loads(text)
            except Exception:
                p = {}
            token = p.get("access_token") or p.get("token")
            self._v2 = bool(p.get("token")) and not p.get("access_token")
            if not token:
                raise PanelError("ورود به پنل نودکس ناموفق بود؛ توکن دریافت نشد.")
            self._token = token
            self._logged_in = True
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
        for attempt in range(2):
            try:
                async with s.request(method, url, **kwargs) as r:
                    text = await r.text()
                    if r.status in (401, 403) and attempt == 0:
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
                if attempt == 0:
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
                raise PanelError(f"{method} {path}: اتصال برقرار نشد یا بیش از حد طول کشید: {err_detail}") from e
        raise PanelError(f"{method} {path} پس از تلاش مجدد ناموفق ماند")

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

    @staticmethod
    def _items(res):
        """نسخهٔ جدید صفحه‌بندی‌شده است ({items:[...]})، نسخهٔ قدیم آرایهٔ ساده."""
        if isinstance(res, dict):
            res = res.get("items")
        return res if isinstance(res, list) else []

    @staticmethod
    def _link_of(obj) -> str:
        obj = obj or {}
        return obj.get("subscription_link") or obj.get("link") or ""

    def _norm(self, u: dict) -> dict:
        """شکلِ یکسان برایِ بقیهٔ کد: current_usage/link/enabled همیشه هست."""
        u = dict(u)
        if "current_usage" not in u:
            u["current_usage"] = max(int(u.get("total_usage", 0) or 0)
                                     - int(u.get("reset_usage", 0) or 0), 0)
        u["link"] = self._link_of(u)
        return u

    async def list_inbounds(self):
        res = await self._request("GET", "/api/services", params={"limit": 1000})
        return [{"id": s.get("id"), "remark": s.get("remark") or s.get("name"), "enable": True}
                for s in self._items(res)]

    async def active_inbound_ids(self):
        if self.inbound_ids:
            return list(self.inbound_ids)
        services = await self.list_inbounds()
        ids = [s["id"] for s in services if s.get("id") is not None]
        if not ids:
            raise PanelError("هیچ سرویسی روی پنل نودکس پیدا نشد.")
        return ids

    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0,
                             on_hold=False):
        """limit_ip اینجا معادلِ مستقیم در اسپک ندارد و نادیده گرفته می‌شود."""
        username = _safe_username(email)
        service_ids = inbound_ids if inbound_ids is not None else await self.active_inbound_ids()
        if mb is not None:
            limit_usage = int(mb) * 1024 * 1024
        else:
            limit_usage = int(gb) * GB if gb else 0
        total_seconds = int(days) * 86400 + int(hours) * 3600
        limit_expire = int(time.time()) + total_seconds if total_seconds else 0

        if not self._logged_in:
            await self.login()
        body = [{
            "username": username,
            "limit_usage": limit_usage,
            "limit_expire": limit_expire,
            ("services" if self._v2 else "service_ids"): service_ids,
        }]
        res = await self._request("POST", "/api/subscriptions", json=body)
        if not isinstance(res, list) or not res:
            raise PanelError(f"ساختِ کانفیگ روی پنل نودکس ناموفق بود: {res}")
        obj = res[0]
        return {
            "uuid": uuid_str or gen_uuid(), "sub_id": sub_id or gen_sub_id(),
            "email": username, "gb": gb, "days": days,
            "inbounds": service_ids, "sub_link": self._abs_link(self._link_of(obj)),
            "expiry_ms": limit_expire * 1000 if limit_expire else 0,
        }

    def _abs_link(self, link: str) -> str:
        if not link:
            return ""
        if link.startswith("http://") or link.startswith("https://"):
            return link
        base = self.sub_base or self.base_url
        return f"{base.rstrip('/')}/{link.lstrip('/')}"

    def sub_link(self, sub_id):
        """فقط برای سازگاریِ امضا؛ لینکِ واقعی از get_client(email) بیا."""
        return f"{self.sub_base}/{sub_id}" if self.sub_base else ""

    async def get_sub_links(self, sub_id):
        return []

    async def get_client_links(self, email):
        cur = await self.get_client(email)
        sub_url = (cur or {}).get("sub_link", "")
        if self._v2:
            try:
                res = await self._request(
                    "GET", f"/api/subscriptions/{_safe_username(email)}/links")
                vals = [str(i.get("link") or "") for i in (res or {}).get("links", [])]
                direct = [v for v in vals if v and not v.lower().startswith(("http://", "https://"))]
                if direct:
                    return direct
            except PanelError:
                logger.exception("nodex_panel: گرفتنِ /links ناموفق بود؛ از لینکِ ساب ادامه می‌دهیم")
        if not sub_url:
            return []
        return await self._fetch_links_from_sub(sub_url)

    async def _fetch_links_from_sub(self, sub_url: str) -> list[str]:
        """=== PATCH_SUB_UNIFIED_V1 === به sub_fetch.fetch_sub_links واگذار شد:
        نشستِ تازه (بدونِ توکنِ پنل)، چند UA روی ۴۰۳/۵xx، و تبدیلِ JSONِ Xray به لینک."""
        from tenant.shared.panel_clients.sub_fetch import fetch_sub_links
        return await fetch_sub_links(sub_url)

    async def get_client(self, email):
        username = _safe_username(email)
        if not self._logged_in:
            await self.login()
        if self._v2:
            lst = self._items(await self._request(
                "GET", "/api/subscriptions", params={"usernames": username, "limit": 10}))
            res = next((x for x in lst if (x.get("username") or "").lower() == username), None)
            if not res:   # اگر فیلترِ usernames نادیده گرفته شد، با search دوباره
                lst = self._items(await self._request(
                    "GET", "/api/subscriptions", params={"search": username, "limit": 50}))
                res = next((x for x in lst if (x.get("username") or "").lower() == username), None)
            if res:
                res = self._norm(res)
        else:
            res = await self._request("GET", f"/api/subscriptions/{username}")
        if not res:
            return None
        res = dict(res)
        res["sub_link"] = self._abs_link(self._link_of(res))
        res["subId"] = username
        res["id"] = username
        return res

    async def get_usage(self, email):
        cur = await self.get_client(email)
        if not cur:
            return None
        used = int(cur.get("current_usage", 0) or 0)
        cap = int(cur.get("limit_usage", 0) or 0)
        expiry_ms = int(cur.get("limit_expire", 0) or 0) * 1000
        result = {
            "up": 0, "down": used, "total": cap, "expiryTime": expiry_ms,
            "enable": bool(cur.get("enabled", True)),
        }
        ua = cur.get("last_client_agent")
        if ua and str(ua).strip():
            result["client_app"] = str(ua).strip()
        return result

    async def _modify(self, email, **fields):
        username = _safe_username(email)
        if not self._logged_in:
            await self.login()
        if self._v2:
            res = await self._request("PUT", "/api/subscriptions",
                                      json={"usernames": [username], **fields})
            if isinstance(res, list):
                res = res[0] if res else None
        else:
            res = await self._request("PUT", f"/api/subscriptions/{username}", json=fields)
        if res is None:
            raise PanelError("سابسکریپشن روی پنل نودکس پیدا نشد.")
        return res

    async def add_traffic(self, email, gb):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("سابسکریپشن روی پنل نودکس پیدا نشد.")
        cur_limit = int(cur.get("limit_usage", 0) or 0)
        if cur_limit == 0:
            return cur
        return await self._modify(email, limit_usage=cur_limit + int(gb) * GB)

    async def renew(self, email, days, reset_usage=False):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("سابسکریپشن روی پنل نودکس پیدا نشد.")
        cur_expire = int(cur.get("limit_expire", 0) or 0)
        # === PATCH_RENEW_UNLIMITED_TIME_GUARD_V1 ===
        # مثلِ add_traffic بالا که برایِ limit_usage==0 (نامحدود) نادیده
        # می‌گیرد، اینجا هم اگه از قبل limit_expire==0 (زمانِ نامحدود) بود،
        # تمدید را بی‌خطر رد می‌کنیم؛ وگرنه چون ۰ از «الان» کمتره، یک
        # تاریخِ واقعی رویِ یک حسابِ نامحدود می‌نوشت.
        if cur_expire == 0:
            return cur
        now = int(time.time())
        base = cur_expire if cur_expire > now else now
        new_expire = base + int(days) * 86400
        obj = await self._modify(email, limit_expire=new_expire)
        if reset_usage:
            await self.reset_usage(email)
        return obj

    async def _bulk(self, path, email):
        username = _safe_username(email)
        return await self._request("POST", path, json={"usernames": [username]})

    async def reset_usage(self, email):
        res = await self._bulk("/api/subscriptions/reset", email)
        return bool(res)

    async def reset_sub(self, email, new_sub_id=None):
        """معادلِ «ریست لینک ساب»: /api/subscriptions/revoke کلید/لینکِ دسترسی
        را می‌چرخاند (لینکِ قبلی باطل می‌شود)."""
        res = await self._bulk("/api/subscriptions/revoke", email)
        if not isinstance(res, list) or not res:
            raise PanelError("ریست لینک روی پنل نودکس ناموفق بود.")
        return self._abs_link(self._link_of(res[0]))

    async def rename_account(self, old_email: str, new_email: str):
        if not new_email or new_email == old_email:
            return False
        new_username = _safe_username(new_email)
        await self._modify(old_email, username=new_username)
        return True

    async def set_enabled(self, email, enabled: bool):
        path = "/api/subscriptions/enable" if enabled else "/api/subscriptions/disable"
        res = await self._bulk(path, email)
        if not res:
            raise PanelError("تغییر وضعیت فعال/غیرفعال روی پنل نودکس ناموفق بود.")
        return True

    async def delete_account(self, email, keep_traffic=False):
        username = _safe_username(email)
        res = await self._request("DELETE", "/api/subscriptions", json={"usernames": [username]})
        return res is not None

    async def list_clients(self):
        if not self._logged_in:
            await self.login()
        if self._v2:
            res = []
            for page in range(1, 101):
                r = await self._request("GET", "/api/subscriptions",
                                        params={"page": page, "limit": 100})
                got = self._items(r)
                res.extend(self._norm(x) for x in got)
                total = int((r or {}).get("total", 0) or 0) if isinstance(r, dict) else 0
                if not got or len(res) >= total:
                    break
        else:
            res = await self._request("GET", "/api/subscriptions", params={"size": 1000}) or []
        if not isinstance(res, list):
            return []
        out = []
        for u in res:
            used = int(u.get("current_usage", 0) or 0)
            cap = int(u.get("limit_usage", 0) or 0)
            expire = int(u.get("limit_expire", 0) or 0)
            out.append({
                "email": u.get("username"),
                "traffic": {"up": 0, "down": used, "total": cap},
                "totalGB": cap,
                "expiryTime": expire * 1000,
            })
        return out

    async def test_connection(self):
        # === PATCH_LOGIN_THROTTLE_V1 ===
        # همون فیکسِ marzban_panel.py — قبلاً force=True بود و چک‌کردنِ
        # دوره‌ایِ panel_watchdog.py (هر ۵ دقیقه) هر بار یک لاگینِ واقعی
        # می‌زد و باعثِ فلپینگِ «قطع/وصل» می‌شد.
        await self.login()
        ids = await self.active_inbound_ids()
        return {"ok": True, "inbounds": ids}


def usage_summary(usage: dict) -> str:
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
        return f"{used_gb} از {cap_gb} گیگ مصرف شده (باقی‌مانده {left_gb} گیگ)"
    return f"{used_gb} گیگ مصرف شده (حجم نامحدود)"
