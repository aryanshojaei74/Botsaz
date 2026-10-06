"""
marzneshin_panel.py — کلاینت async پنل مرزنشین (Marzneshin) برای بات‌ساز (چند‌مستأجری).

همان قرارداد متدی PanelClient (تنانت سنایی/3X-UI در vpn_panel.py) را پیاده‌سازی
می‌کند تا بقیهٔ کد (vpn_admin.py / vpn_user.py / main.py) بدون شاخه‌زدن روی
panel_key، فقط با dispatch ساده (tenant/vpn_panel_factory.py) از آن استفاده کند.

منبع رسمی API: https://github.com/marzneshin/marzneshin
(تأییدشده از روی سورسِ واقعیِ بستهٔ پایتونیِ رسمی `marzneshin` روی PyPI —
pypi.org/project/marzneshin — که مستقیماً endpointها و مدل‌های Pydantic را
پیاده‌سازی کرده؛ نه حدسی/بر پایهٔ مستنداتِ ناقص.)

  لاگین:              POST   /api/admins/token        (form-urlencoded: username, password)
  ساخت کاربر:          POST   /api/users
  گرفتن کاربر:         GET    /api/users/{username}
  ویرایش کاربر:        PUT    /api/users/{username}
  حذف کاربر:           DELETE /api/users/{username}
  ریست مصرف:           POST   /api/users/{username}/reset
  فعال/غیرفعال:        POST   /api/users/{username}/enable | /disable
  ریست ساب:            POST   /api/users/{username}/revoke_sub
  لیست کاربران:        GET    /api/users
  لیست سرویس‌ها:       GET    /api/services
  لیست inboundها:      GET    /api/inbounds

نکات مهمِ تفاوت با مرزبان (مهم‌ترین دلیلِ فایلِ جداگانه به‌جای زیرکلاسِ نازک):
  - مرزنشین به‌جای «inbound tag»، مفهومِ «Service» دارد: هر Service یک گروهِ
    از پیش‌تعریف‌شده از چند inbound است (با شناسهٔ عددی) و کاربر به یک یا چند
    Service (با service_ids، لیستِ عدد) وصل می‌شود، نه مستقیم به inboundها.
    برای همین اینجا «شناسهٔ inbound» که در بقیهٔ کد ذخیره/گرفته می‌شود، در
    عمل «شناسهٔ Service» است — ولی چون هر دو عدد صحیح‌اند (بر خلاف تگِ رشته‌ایِ
    مرزبان)، در vpn_panel_factory.py به‌جای پنل‌های تگ‌محور، مسیرِ پیش‌فرضِ
    عددی (مثل سنایی) استفاده می‌شود.
  - از نسخهٔ v0.4.0 مرزنشین، فیلدِ منقضی‌شدن با یک «استراتژی» + یک تاریخ نمایش
    داده می‌شود: `expire_strategy` (اجباری، از enum `never` | `fixed_date` |
    `start_on_first_use`) و `expire_date` (اختیاری، رشتهٔ ISO-8601). فیلدِ قدیمیِ
    `expire` دیگر پذیرفته نمی‌شود و اگه بفرستی، HTTP 422 با پیام
    `{"detail":{"expire_strategy":"Field required"}}` می‌گیری.
  - فعال/غیرفعال‌کردن کاربر endpoint جداگانه دارد (/enable ، /disable)، نه
    فیلدی در بدنهٔ PUT (فیلدهای `enabled`/`status` توی UserCreate/UserModify
    نیستن و اگه بفرستی، توسط pydantic ignore می‌شن یا خطا می‌ده).
"""
from __future__ import annotations

import re
import secrets
import string
import asyncio
import datetime as _dt

try:
    import aiohttp
except ImportError:  # تا زمانی که نصب نشده، ایمپورت ماژول خطا ندهد
    aiohttp = None

from tenant.shared.panel_clients.vpn_panel import PanelError, frontend_404_hint, is_static_frontend_405, login_domain_root  # همان کلاس خطا در کل پروژه استفاده شود

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

GB = 1024 * 1024 * 1024


def _rand(n=8):
    a = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(a) for _ in range(n))


def gen_sub_id(n=16):
    # مرزنشین subId جدا ندارد؛ لینک ساب از روی username+key پنل ساخته می‌شود،
    # اما برای سازگاری با فراخوانی‌های موجود یک شناسهٔ کوتاه برمی‌گردانیم که
    # جایی در منطق مرزنشین استفاده نمی‌شود.
    return _rand(n)


def gen_uuid():
    import uuid as _uuid
    return str(_uuid.uuid4())


def _safe_username(email: str) -> str:
    """مرزنشین هم مثل مرزبان محدودیتِ [a-z0-9_] دارد (۳ تا ۳۲ کاراکتر)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", str(email)).lower()
    cleaned = cleaned[:32]
    if len(cleaned) < 3:
        cleaned = (cleaned + "_usr")[:32]
    return cleaned


def _iso(dt: "_dt.datetime | None") -> "str | None":
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat()


def _read_expire(u: dict):
    """تاریخِ انقضا رو از یک شیءِ کاربر برمی‌گردونه. مرزنشینِ v0.4+ از فیلدِ
    `expire_date` استفاده می‌کنه؛ نسخه‌های قدیمی‌تر `expire` داشتن. این هلپر
    برای سازگاری با هر دو، اول جدید رو چک می‌کنه بعد قدیمی رو."""
    return u.get("expire_date") or u.get("expire")


def _parse_iso_to_ms(val) -> int:
    """رشتهٔ ISO برگشتی از مرزنشین را به میلی‌ثانیه (برای سازگاری با بقیهٔ
    کد که expiryTime را میلی‌ثانیه فرض می‌کند) تبدیل می‌کند."""
    if not val:
        return 0
    try:
        s = str(val).replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


class MarzneshinPanelClient:
    """یک نشست به پنل مرزنشین یک مستأجر. اعتبارنامه در سازنده داده می‌شود.

    امضای سازنده عیناً مثل PanelClient (vpn_panel.py) است. inbound_ids اینجا
    در واقع «شناسهٔ Service»های عددی است (نگاه کن به توضیح بالای فایل).
    """

    def __init__(self, base_url: str, username: str, password: str,
                 sub_base: str = "", inbound_ids=None, verify_ssl: bool = False,
                 timeout: int = 8):
        # === PATCH_PANEL_TIMEOUT_REDUCE_V1 ===
        # نگاه کن به توضیحِ کاملِ همین پچ در tenant/hiddify_panel.py.
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
        # شناسهٔ سرویس‌ها (عددی)
        self.service_ids = [int(i) for i in inbound_ids] if inbound_ids else []
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = None
        self._token = None
        self._logged_in = False
        self._lock = asyncio.Lock()
        self._services_cache = None  # [{id, name, inbounds}, ...]

    # ───────── نشست ─────────
    async def _ensure_session(self):
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
            self._session = aiohttp.ClientSession(timeout=t, connector=conn)
        return self._session

    async def login(self, force=False):
        async with self._lock:
            if self._logged_in and not force:
                return True
            s = await self._ensure_session()
            data = {"username": self.username, "password": self.password}
            try:
                async with s.post(f"{self.base_url}/api/admins/token", data=data) as r:
                    status, text = r.status, await r.text()
            except PanelError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as e:
                raise PanelError(
                    f"اتصال به پنل برقرار نشد یا بیش از حد طول کشید (Timeout). "
                    f"آدرس/پورت و دسترس‌پذیری سرور را بررسی کن: {e}") from e

            # === PATCH_LOGIN_ROOT_FALLBACK_V1 ===
            # نگاه کن به توضیحِ کامل در tenant/marzban_panel.py.
            if status == 405 and is_static_frontend_405(text):
                root = login_domain_root(self.base_url)
                if root:
                    logger.info(
                        "marzneshin_panel: لاگین با ۴۰۵ِ استاتیک‌فایل مواجه شد "
                        "(base_url=%s) — یک‌بار با ریشهٔ دامنه (%s) امتحان می‌شود.",
                        self.base_url, root)
                    try:
                        async with s.post(f"{root}/api/admins/token", data=data) as r2:
                            status2, text2 = r2.status, await r2.text()
                    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError):
                        status2 = None
                    if status2 is not None:
                        status, text = status2, text2
                        if status == 200:
                            logger.info(
                                "marzneshin_panel: فال‌بکِ ریشهٔ دامنه موفق بود؛ "
                                "base_url از %s به %s تغییر کرد.", self.base_url, root)
                            self.base_url = root

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
                    # === PATCH_MARZNESHIN_ADMIN_DISABLED_HINT_V1 ===
                    # اگه اینجا رسیدیم یعنی attempt==1 (تلاشِ دوم، بعدِ
                    # login(force=True) که خودش موفق بود و توکنِ تازه گرفت)
                    # و بازم 401/403 گرفتیم. توی کدِ خودِ مرزنشین
                    # (app/dependencies.py get_current_admin) این دقیقاً یعنی
                    # لاگین (POST /api/admins/token) درست کار کرده، ولی
                    # get_admin() روی توکنِ تازه هم None برگردونده — که طبق
                    # همون فایل فقط ۲ تا دلیل داره: (۱) ادمین از رمزِ قبل از
                    # ساختِ همین توکن ریست‌شده (خیلی بعیده چون توکن همین الان
                    # ساخته شده)، یا (۲-محتمل‌تر) ادمین «sudo» نیست و
                    # enabled=False شده روی خودِ پنل. قبلاً این حالت با همون
                    # پیامِ عمومیِ «HTTP 401: Could not validate credentials»
                    # به کاربر نشون داده می‌شد که هیچ راهنماییِ عملی نداشت.
                    if r.status in (401, 403) and attempt == 1:
                        raise PanelError(
                            "ورود به پنل موفق بود ولی خودِ درخواست رد شد "
                            "(401/403 حتی بعد از ورودِ مجدد). طبق پیامِ پنل "
                            f"«{text[:200]}»، معمولاً یعنی ادمینِ متصل به این "
                            "بات روی پنل مرزنشین «غیرفعال» (Enabled=خیر) شده "
                            "یا دسترسیِ sudo ازش گرفته شده. برو توی پنل "
                            "مرزنشین → بخش Admins، وضعیتِ همین ادمین را بررسی "
                            "و در صورت نیاز دوباره فعالش کن.")
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
                    await self.login(force=True)
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

    async def _paginated_get(self, path: str, extra_params: dict | None = None) -> list:
        """صفحه‌بندیِ واقعی برایِ endpointهایی مثلِ /api/services و /api/users
        که هر صفحه حداکثر ۱۰۰ آیتم برمی‌گردونن (پنل با size>100 خطایِ ۴۲۲
        می‌ده: «Input should be less than or equal to 100»). قبلاً اینجا
        فقط یه درخواستِ تکی با size=200/1000 می‌رفت که هم خطا می‌داد، هم
        (اگه محدودیت رعایت می‌شد) داده‌هایِ بعدِ صفحه‌ی اول رو گم می‌کرد."""
        page = 1
        page_size = 100
        out: list = []
        while True:
            params = {"page": page, "size": page_size}
            if extra_params:
                params.update(extra_params)
            res = await self._request("GET", path, params=params) or {}
            items = res.get("items", res) if isinstance(res, dict) else (res or [])
            if not items:
                break
            out.extend(items)
            if len(items) < page_size:
                break  # صفحه‌ی آخر بود
            page += 1
            if page > 50:  # محافظتِ ایمنی در برابرِ حلقه‌ی بی‌نهایت
                break
        return out

    # ───────── سرویس‌ها (به‌جای inboundِ خام) ─────────
    async def list_inbounds(self):
        """در قراردادِ بقیهٔ کد، این متد باید «واحدهای قابل‌انتخاب» را با
        کلید id برگرداند — اینجا این واحد، Serviceهای مرزنشین است (نه
        inboundهای خام)، چون کاربر در مرزنشین به Service وصل می‌شود.
        خروجی: [{id, remark(=نام سرویس), enable: True}, ...]"""
        items = await self._paginated_get("/api/services")
        flat = []
        for sv in items or []:
            sid = sv.get("id")
            if sid is None:
                continue
            flat.append({"id": sid, "tag": sv.get("name"), "remark": sv.get("name"),
                        "enable": True})
        self._services_cache = items
        return flat

    async def active_inbound_ids(self):
        """لیست شناسهٔ Serviceهای هدف؛ اگر در تنظیمات پنل مشخص نشده، همهٔ
        Serviceهای موجود برگردانده می‌شود."""
        if self.service_ids:
            return list(self.service_ids)
        inbounds = await self.list_inbounds()
        ids = [ib["id"] for ib in inbounds if ib.get("id") is not None]
        if not ids:
            raise PanelError(
                "هیچ Service ای روی پنل مرزنشین پیدا نشد. اول از پنل مرزنشین "
                "(بخش Services) حداقل یک سرویس بساز.")
        return ids

    # ───────── ساخت ─────────
    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0,
                             on_hold=False):
        """ساخت کاربر در مرزنشین با همان امضای create_account پنل سنایی.

        gb: حجم به گیگ (0=نامحدود). mb: اگر داده شود حجم به مگابایت (سرویس تست).
        days>0 → expire به‌صورت ISO datetime (UTC) از همین لحظه محاسبه می‌شود؛
        days=0 → expire=None (بی‌انقضا).

        limit_ip در مرزنشین معادل مستقیم ندارد (مثل مرزبان) و نادیده گرفته
        می‌شود.

        === PATCH_TRIAL_HOURLY_V1 ===
        hours: مدتِ اضافه به ساعت. مرزنشین از expire_date (ISO datetime)
        استفاده می‌کنه پس دقتِ ساعتی/دقیقه‌ای هم کاملاً پشتیبانی می‌شه.
        """
        username = _safe_username(email)
        service_ids = inbound_ids if inbound_ids is not None else await self.active_inbound_ids()
        service_ids = [int(i) for i in service_ids]

        if mb is not None:
            data_limit = int(mb) * 1024 * 1024
        else:
            data_limit = int(gb) * GB if gb else 0
        total_seconds = int(days) * 86400 + int(hours) * 3600
        if total_seconds:
            expire_dt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=total_seconds)
        else:
            expire_dt = None

        # === PATCH_MARZNESHIN_EXPIRE_STRATEGY_V1 ===
        # از v0.4.0 مرزنشین، `expire_strategy` اجباری‌ست و فیلدِ قدیمیِ
        # `expire` دیگر پذیرفته نمی‌شود. فیلدهای `enabled`/`status` هم توی
        # UserCreate نیستن (فعال/غیرفعال با endpoint جداگانه انجام می‌شه).
        body: dict = {
            "username": username,
            "data_limit": data_limit,
            "data_limit_reset_strategy": "no_reset",
            "service_ids": service_ids,
            "note": "",
        }
        if expire_dt is not None:
            body["expire_strategy"] = "fixed_date"
            body["expire_date"] = _iso(expire_dt)
        else:
            body["expire_strategy"] = "never"
            body["expire_date"] = None
        res = await self._request("POST", "/api/users", json=body)
        if res is None:
            raise PanelError("ساخت کانفیگ ناموفق: کاربری با این نام از قبل وجود دارد.")
        sub_url = res.get("subscription_url") or ""
        return {
            "uuid": uuid_str or gen_uuid(), "sub_id": sub_id or gen_sub_id(),
            "email": username, "gb": gb, "days": days,
            "inbounds": service_ids, "sub_link": self._abs_sub_link(sub_url),
            "expiry_ms": _parse_iso_to_ms(_iso(expire_dt)) if expire_dt else 0,
        }

    def _abs_sub_link(self, sub_url: str) -> str:
        """subscription_url مرزنشین معمولاً مسیر نسبی است (مثل /sub/xxxx/yyyy)؛
        اینجا آن را به آدرس کامل تبدیل می‌کنیم."""
        if not sub_url:
            return ""
        if sub_url.startswith("http://") or sub_url.startswith("https://"):
            return sub_url
        base = self.sub_base or self.base_url
        return f"{base.rstrip('/')}/{sub_url.lstrip('/')}"

    # ───────── لینک‌ها ─────────
    def sub_link(self, sub_id):
        """در مرزنشین لینک ساب از روی username+key پنل ساخته می‌شود نه sub_id؛
        این متد فقط برای سازگاری امضا نگه داشته شده — برای گرفتن ساب‌لینک
        واقعی از get_client(email) استفاده کن."""
        return f"{self.sub_base}/{sub_id}" if self.sub_base else ""

    async def get_sub_links(self, sub_id):
        return []

    async def get_client_links(self, email):
        """مرزنشین لینک‌های تکیِ کانفیگ را در فیلد جداگانه‌ای در پاسخِ کاربر
        برنمی‌گرداند؛ اگر لازم شد، از محتوای ساب‌لینک استخراج می‌کنیم."""
        cur = await self.get_client(email)
        if cur and cur.get("links"):
            return cur["links"]
        sub_url = (cur or {}).get("sub_link", "")
        if not sub_url:
            return []
        return await self._fetch_links_from_sub(sub_url)

    async def _fetch_links_from_sub(self, sub_url: str) -> list[str]:
        """=== PATCH_SUB_UNIFIED_V1 === به sub_fetch.fetch_sub_links واگذار شد:
        نشستِ تازه (بدونِ توکنِ پنل)، چند UA روی ۴۰۳/۵xx، و تبدیلِ JSONِ Xray به لینک."""
        from tenant.shared.panel_clients.sub_fetch import fetch_sub_links
        return await fetch_sub_links(sub_url)

    # ───────── خواندن ─────────
    async def get_client(self, email):
        username = _safe_username(email)
        res = await self._request("GET", f"/api/users/{username}")
        if not res:
            return None
        res = dict(res)
        res["sub_link"] = self._abs_sub_link(res.get("subscription_url", ""))
        res["subId"] = username
        res["id"] = username
        return res

    async def get_usage(self, email):
        """آمار مصرف با همان کلیدهای PanelClient: up, down, total, expiryTime
        (میلی‌ثانیه) — برای سازگاری با _usage_block در vpn_user.py."""
        cur = await self.get_client(email)
        if not cur:
            return None
        used = int(cur.get("used_traffic", 0) or 0)
        cap = int(cur.get("data_limit", 0) or 0)
        return {
            "up": 0, "down": used,
            "total": cap, "expiryTime": _parse_iso_to_ms(_read_expire(cur)),
            "enable": cur.get("status") == "active" or bool(cur.get("enabled")),
        }

    # ───────── تمدید/ترافیک ─────────
    async def _modify(self, email, **fields):
        username = _safe_username(email)
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر روی پنل مرزنشین پیدا نشد.")
        # === PATCH_MARZNESHIN_EXPIRE_STRATEGY_V1 ===
        # UserModify از v0.4+ به‌جای `expire`، از `expire_strategy` +
        # `expire_date` استفاده می‌کنه. `enabled`/`status` توی UserModify
        # نیستن (با endpointهای /enable و /disable مدیریت می‌شن).
        cur_expire = _read_expire(cur)
        cur_strategy = cur.get("expire_strategy") or (
            "fixed_date" if cur_expire else "never")
        body: dict = {
            "username": username,
            "expire_strategy": cur_strategy,
            "expire_date": cur_expire,
            "usage_duration": cur.get("usage_duration"),
            "activation_deadline": cur.get("activation_deadline"),
            "data_limit": int(cur.get("data_limit", 0) or 0),
            "data_limit_reset_strategy": cur.get("data_limit_reset_strategy", "no_reset"),
            "service_ids": [s.get("id") if isinstance(s, dict) else s
                            for s in (cur.get("service_ids") or cur.get("services") or [])],
            "note": cur.get("note", ""),
        }
        body.update(fields)
        res = await self._request("PUT", f"/api/users/{username}", json=body)
        if res is None:
            raise PanelError("کاربر روی پنل مرزنشین پیدا نشد.")
        return res

    async def add_traffic(self, email, gb):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر روی پنل مرزنشین پیدا نشد.")
        cur_limit = int(cur.get("data_limit", 0) or 0)
        if cur_limit == 0:
            return cur  # کاربر نامحدود؛ افزایش حجم بی‌معنی است
        return await self._modify(email, data_limit=cur_limit + int(gb) * GB)

    async def renew(self, email, days, reset_usage=False):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر روی پنل مرزنشین پیدا نشد.")
        # === PATCH_RENEW_UNLIMITED_TIME_GUARD_V1 ===
        # همون باگِ مرزبان، اینجا هم بود: add_traffic از قبل برایِ
        # data_limit==0 (نامحدود) درست نادیده می‌گرفت، ولی renew() برایِ
        # expire_strategy=="never" (زمانِ نامحدود) همیشه بی‌قیدوشرط
        # expire_strategy رو به fixed_date تغییر می‌داد و یک تاریخِ واقعی
        # می‌نوشت — یعنی نامحدودیِ زمانی با هر «تمدید مجدد» بی‌سروصدا از
        # بین می‌رفت. حالا اگه از قبل «never» بود، تمدیدِ زمان را بی‌خطر
        # رد می‌کنیم.
        cur_strategy0 = cur.get("expire_strategy") or (
            "fixed_date" if _read_expire(cur) else "never")
        if cur_strategy0 == "never":
            return cur
        now = _dt.datetime.now(_dt.timezone.utc)
        cur_expire_ms = _parse_iso_to_ms(_read_expire(cur))
        base = (_dt.datetime.fromtimestamp(cur_expire_ms / 1000, tz=_dt.timezone.utc)
                if cur_expire_ms and cur_expire_ms > now.timestamp() * 1000 else now)
        new_expire = base + _dt.timedelta(days=int(days))
        # === PATCH_MARZNESHIN_EXPIRE_STRATEGY_V1 ===
        # فعال‌کردن دیگه در بدنه‌ی PUT ممکن نیست — با endpoint جداگانه.
        obj = await self._modify(
            email, expire_strategy="fixed_date", expire_date=_iso(new_expire),
            usage_duration=None, activation_deadline=None,
        )
        try:
            await self.set_enabled(email, True)
        except Exception:
            logger.exception("فعال‌سازیِ کاربر در پنل ناموفق بود")  # اگه از قبل فعال بود، مشکلی نیست
        if reset_usage:
            await self.reset_usage(email)
        return obj

    async def reset_usage(self, email):
        username = _safe_username(email)
        res = await self._request("POST", f"/api/users/{username}/reset", json={})
        return res is not None

    async def reset_sub(self, email, new_sub_id=None):
        """در مرزنشین معادل «ریست لینک ساب» اندپوینت revoke_sub است."""
        username = _safe_username(email)
        res = await self._request("POST", f"/api/users/{username}/revoke_sub", json={})
        if res is None:
            raise PanelError("ریست لینک ناموفق: کاربر پیدا نشد.")
        return self._abs_sub_link(res.get("subscription_url", ""))

    async def rename_account(self, old_email: str, new_email: str):
        """در مرزنشین «نام کاربری» همان شناسهٔ یکتای اکانت است و طبق API قابل
        تغییر روی اکانت‌های موجود نیست؛ نام جدید فقط روی کانفیگ‌های تازه اعمال
        می‌شود."""
        raise PanelError(
            "در این پنل نام کاربری همان شناسهٔ اکانت است و قابل تغییر روی "
            "اکانت‌های موجود نیست؛ نام جدید فقط روی کانفیگ‌های تازه اعمال می‌شود.")

    async def set_enabled(self, email, enabled: bool):
        """فعال/غیرفعال‌کردن سریعِ کاربر — در مرزنشین با endpoint جداگانه
        (نه فیلدِ بدنه)."""
        username = _safe_username(email)
        path = f"/api/users/{username}/{'enable' if enabled else 'disable'}"
        res = await self._request("POST", path, json={})
        if res is None:
            raise PanelError("تغییر وضعیت فعال/غیرفعال ناموفق: کاربر پیدا نشد.")
        return True

    async def delete_account(self, email, keep_traffic=False):
        username = _safe_username(email)
        res = await self._request("DELETE", f"/api/users/{username}")
        return res is not None or True  # حذف معمولاً body خالی برمی‌گرداند

    # ───────── آمار ─────────
    async def list_clients(self):
        """لیست کاربران مرزنشین به‌شکل تخت و با کلیدهای هم‌نام PanelClient
        (email, traffic{up,down,total}, expiryTime, totalGB) تا
        main.py:vpn_expiry_loop بدون تغییر کار کند."""
        users = await self._paginated_get("/api/users")
        out = []
        for u in users:
            used = int(u.get("used_traffic", 0) or 0)
            cap = int(u.get("data_limit", 0) or 0)
            out.append({
                "email": u.get("username"),
                "traffic": {"up": 0, "down": used, "total": cap},
                "totalGB": cap,
                "expiryTime": _parse_iso_to_ms(_read_expire(u)),
            })
        return out

    async def test_connection(self):
        """فقط برای تست تنظیمات پنل توسط فروشنده: True اگر لاگین و لیستِ
        Serviceها موفق بود."""
        # === PATCH_LOGIN_THROTTLE_V1 ===
        # همون فیکسِ marzban_panel.py — قبلاً force=True بود و چک‌کردنِ
        # دوره‌ایِ panel_watchdog.py (هر ۵ دقیقه) هر بار یک لاگینِ واقعی
        # می‌زد و باعثِ فلپینگِ «قطع/وصل» می‌شد.
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
