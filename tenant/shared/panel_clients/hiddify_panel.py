"""
hiddify_panel.py — کلاینت async پنل هیدیفای (HiddifyManager) برای بات‌ساز.

منبع رسمی این پیاده‌سازی: سورسِ واقعیِ پکیجِ پایتونیِ رسمیِ `hiddifypanel`
(pypi.org/project/hiddifypanel، نسخهٔ ۱۲.۳.۳) — یعنی خودِ کدِ سرورِ پنل، نه
حدس یا مستنداتِ ناقص. فایل‌های کلیدیِ بررسی‌شده:
  panel/commercial/restapi/v2/admin/{users_api,user_api,schema}.py
  auth.py (مکانیزمِ Hiddify-API-Key)

نکاتِ مهمِ تفاوت با خانوادهٔ مرزبان (چرا این پنل یک کلاینتِ کاملاً جداگانه
لازم دارد، نه زیرکلاسِ کوچک):

  ۱) احرازِ هویت: نه یوزرنیم/پسورد به‌معنایِ رایج، نه لاگین‌گرفتنِ توکن —
     یک هدرِ ثابت به نامِ «Hiddify-API-Key» که مقدارش UUID خودِ ادمین است
     (از پنل گرفته می‌شود). در فیلدِ «رمز عبورِ» ویزاردِ این پروژه همین
     UUID ذخیره می‌شود.
  ۲) آدرس: هر نصبِ هیدیفای یک بخشِ مخفیِ اضافه در مسیر دارد (proxy_path)،
     مثلاً https://domain/AbCd1234/api/v2/admin/... — این بخش را ادمینِ
     ربات باید از پنلِ خودش کپی کند و در فیلدِ «یوزرنیمِ» ویزارد بگذارد.
  ۳) شناسه‌ی کاربر UUID است، نه یوزرنیم/ایمیل.
  ۴) انقضا با «تعداد روز» (package_days) + «تاریخِ شروع» (start_date)
     تعریف می‌شود، نه یک timestamp مطلق. اگر start_date را صراحتاً امروز
     بفرستیم، شمارشِ روزها همان لحظه شروع می‌شود (رفتاری که بقیهٔ پنل‌های
     این پروژه هم دارند)؛ اگر start_date را خالی بگذاریم، هیدیفای شمارش را
     از اولین اتصالِ واقعیِ کاربر شروع می‌کند (که با رفتارِ سایرِ پنل‌ها
     همخوانی ندارد، پس اینجا همیشه صراحتاً پر می‌شود).
  ۵) حجم به‌صورتِ عددِ اعشاریِ گیگابایت (usage_limit_GB) است، نه بایت.
  ۶) مفهومِ inbound/service برایِ انتخاب توسطِ ادمین وجود ندارد — کاملاً
     خودکار است؛ برای همین active_inbound_ids/list_inbounds اینجا فقط
     برای سازگاریِ رابط، لیستِ خالی برمی‌گردانند.
  ۷) لینکِ ساب مستقیم در پاسخِ API برنمی‌گردد؛ طبق قراردادِ شناخته‌شده و
     پایدارِ هیدیفای، از الگویِ ثابتِ https://<base>/<proxy_path_client>/<uuid>/
     ساخته می‌شود. چون این آدرس کاربرمحور (نه ادمین‌محور) است و ممکن است
     proxy_path_client با proxy_path ادمین فرق داشته باشد، اگر ادمین موقعِ
     تنظیم پنل مسیرِ جدایی برای بخشِ کاربر نداده باشد، همان proxy_path
     ادمین استفاده می‌شود (که در اکثرِ نصب‌های پیش‌فرض یکی است).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import secrets
import string
import uuid as _uuid_mod

try:
    import aiohttp
except ImportError:
    aiohttp = None

from tenant.shared.panel_clients.vpn_panel import PanelError

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

GB = 1024 * 1024 * 1024


def _rand(n=8):
    a = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(a) for _ in range(n))


def gen_sub_id(n=16):
    return _rand(n)


def gen_uuid():
    return str(_uuid_mod.uuid4())


class HiddifyPanelClient:
    """کلاینتِ پنلِ هیدیفای.

    امضای سازنده عیناً هم‌شکلِ بقیهٔ کلاینت‌های پنلِ پروژه است تا
    vpn_panel_factory.py بدون شاخه‌زدنِ اضافه کار کند؛ فقط معنایِ فیلدها
    فرق دارد:
      username → «مسیرِ مخفیِ ادمین» (proxy_path) — بخشِ رمزآلودِ وسطِ آدرسِ
                 پنل، مثلاً در https://domain/AbCd1234/admin/ مقدارش AbCd1234 است.
      password → «Hiddify-API-Key» — UUID ادمین (از پنل، بخشِ Admin → Profile).
      inbound_ids → استفاده نمی‌شود (هیدیفای اصلاً این مفهوم را ندارد).
    """

    def __init__(self, base_url: str, username: str, password: str,
                 sub_base: str = "", inbound_ids=None, verify_ssl: bool = False,
                 timeout: int = 8):
        # === PATCH_PANEL_TIMEOUT_REDUCE_V1 ===
        # قبلاً پیش‌فرض ۲۵ ثانیه بود — چون این کلاینت مستقیم داخلِ یک
        # هندلرِ ربات (مثلاً «سرویس‌های من») صدا زده می‌شود، و بعضی مسیرها
        # (مثل get_usage) دو درخواستِ پی‌درپی می‌زنند (login + fetch)، در
        # بدترین حالت کاربر تا ۵۰ ثانیه معطل می‌ماند اگر پنل کند/معلق
        # باشد — دقیقاً همان الگویی که در لاگِ سرور به‌شکلِ «slow update»
        # دیده شد. ۸ ثانیه برایِ یک پنلِ سالم کاملاً کافی است؛ برایِ پنلِ
        # واقعاً معلق، خیلی سریع‌تر شکست می‌خورد و پیامِ روشن نشان می‌دهد.
        if aiohttp is None:
            raise PanelError("کتابخانهٔ aiohttp نصب نیست. `pip install aiohttp` را اجرا کن.")
        # === PATCH_STRIP_URL_FRAGMENT_V1 ===
        # نگاه کن به توضیحِ کامل در tenant/marzban_panel.py.
        base_url = (base_url or "").split("#", 1)[0]
        base_url = base_url.rstrip("/")
        self.base_url = base_url
        self.proxy_path = (username or "").strip().strip("/")
        self.api_key = (password or "").strip()
        # اگر ادمین یک proxy_path جداگانه برایِ لینکِ کاربرها داده باشد (در
        # sub_base)، همان استفاده می‌شود؛ وگرنه از همان مسیرِ ادمین.
        self.proxy_path_client = (sub_base or "").strip().strip("/") or self.proxy_path
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = None

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            t = aiohttp.ClientTimeout(total=self.timeout)
            conn = aiohttp.TCPConnector(ssl=self.verify_ssl)
            self._session = aiohttp.ClientSession(timeout=t, connector=conn)
        return self._session

    async def login(self, force=False):
        """هیدیفای لاگین/توکن‌گیریِ جدا ندارد (هدرِ Hiddify-API-Key در هر
        درخواست کافی است)؛ فقط برایِ سازگاریِ رابط با تستِ سبک از /me/
        استفاده می‌کنیم تا اعتبارِ کلید/proxy_path واقعاً بررسی شود."""
        if not self.proxy_path or not self.api_key:
            raise PanelError(
                "مسیرِ مخفیِ ادمین یا Hiddify-API-Key خالی است؛ هر دو را از پنلِ "
                "هیدیفای (بخشِ Admin → Profile) کپی کن.")
        res = await self._request("GET", "/me/")
        if res is None:
            raise PanelError("ورود ناموفق: proxy_path یا Hiddify-API-Key اشتباه است.")
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
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Hiddify-API-Key"] = self.api_key
        headers.setdefault("Accept", "application/json")
        kwargs["headers"] = headers
        url = f"{self.base_url}/{self.proxy_path}/api/v2/admin{path}"
        try:
            async with s.request(method, url, **kwargs) as r:
                text = await r.text()
                if r.status == 404:
                    return None
                if r.status in (401, 403):
                    raise PanelError(
                        f"{method} {path}: دسترسی رد شد (HTTP {r.status}) — احتمالاً "
                        f"Hiddify-API-Key یا proxy_path اشتباه است: {text[:200]}")
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
            # === PATCH_TIMEOUT_ERROR_MSG_V1 / PATCH_PANEL_POOL_SELFHEAL_V1 ===
            # نگاه کن به توضیحِ کاملِ همین دو پچ در tenant/marzban_panel.py.
            err_detail = str(e) or f"{type(e).__name__} (timeout={self.timeout}s)"
            try:
                if self._session and not self._session.closed:
                    await self._session.close()
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (بستنِ نشستِ خراب بعدِ شکستِ اتصال)")
            self._session = None
            raise PanelError(
                f"{method} {path}: اتصال به پنل برقرار نشد یا بیش از حد طول کشید: {err_detail}") from e

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self):
        await self.login()
        return self

    async def __aexit__(self, *a):
        await self.close()

    # ───────── inbound/service: هیدیفای این مفهوم را ندارد ─────────
    async def list_inbounds(self):
        return []

    async def active_inbound_ids(self):
        return []

    # ───────── ساخت ─────────
    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0,
                             on_hold=False):
        """ساخت کاربر در هیدیفای. email اینجا فقط به‌عنوانِ «نام نمایشی»
        استفاده می‌شود (هیدیفای نیازی به یکتا بودنِ آن ندارد چون شناسهٔ
        واقعی uuid است)."""
        uid = uuid_str or gen_uuid()
        try:
            _uuid_mod.UUID(uid)
        except ValueError:
            uid = gen_uuid()

        if mb is not None:
            usage_limit_gb = round(int(mb) / 1024, 4)
        else:
            usage_limit_gb = float(gb) if gb else 0

        body = {
            "uuid": uid,
            "name": str(email)[:64],
            "usage_limit_GB": usage_limit_gb or 999999,  # صفر یعنی نامحدود در بقیهٔ پنل‌ها؛
            # هیدیفای فیلدِ «نامحدود» ندارد، پس عددِ خیلی بزرگ می‌گذاریم.
            "package_days": int(days) if days else 3650,  # نبودِ روز یعنی «تقریباً نامحدود» (۱۰ سال)
            "start_date": _dt.date.today().isoformat(),
            "enable": True,
            "is_active": True,
            "comment": "",
        }
        res = await self._request("POST", "/user/", json=body)
        if res is None:
            raise PanelError("ساخت کانفیگ ناموفق: پاسخِ نامعتبر از پنل.")
        sub_link = self._build_sub_link(uid)
        expiry_ms = 0
        if days:
            expiry_dt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=int(days))
            expiry_ms = int(expiry_dt.timestamp() * 1000)
        return {
            "uuid": uid, "sub_id": sub_id or gen_sub_id(),
            "email": email, "gb": gb, "days": days,
            "inbounds": [], "sub_link": sub_link,
            "expiry_ms": expiry_ms,
        }

    def _build_sub_link(self, uid: str) -> str:
        base = self.base_url.rstrip("/")
        path = self.proxy_path_client or self.proxy_path
        return f"{base}/{path}/{uid}/"

    # ───────── لینک‌ها ─────────
    def sub_link(self, sub_id):
        return ""  # در هیدیفای لینکِ ساب از رویِ uuid ساخته می‌شود، نه sub_id

    async def get_sub_links(self, sub_id):
        return []

    async def get_client_links(self, email):
        return []

    # ───────── خواندن ─────────
    async def get_client(self, email):
        """email اینجا در واقع همان uuid ذخیره‌شده در دیتابیسِ خودِ ماست."""
        uid = email
        res = await self._request("GET", f"/user/{uid}/")
        if not res:
            return None
        res = dict(res)
        res["sub_link"] = self._build_sub_link(uid)
        res["subId"] = uid
        res["id"] = uid
        return res

    async def get_usage(self, email):
        cur = await self.get_client(email)
        if not cur:
            return None
        used_gb = float(cur.get("current_usage_GB") or 0)
        cap_gb = float(cur.get("usage_limit_GB") or 0)
        start_date = cur.get("start_date")
        package_days = cur.get("package_days")
        expiry_ms = 0
        if start_date and package_days:
            try:
                sd = _dt.date.fromisoformat(start_date)
                exp = sd + _dt.timedelta(days=int(package_days))
                expiry_ms = int(_dt.datetime.combine(
                    exp, _dt.time.min, tzinfo=_dt.timezone.utc).timestamp() * 1000)
            except Exception:
                expiry_ms = 0
        result = {
            "up": 0, "down": int(used_gb * GB),
            "total": int(cap_gb * GB), "expiryTime": expiry_ms,
            "enable": bool(cur.get("enable", True)),
        }
        # last_online فیلدِ واقعیِ خودِ هیدیفایه (schema رسمی)؛ اما هیدیفای
        # بر خلافِ مرزبان، نوعِ برنامه‌ی متصل‌شده رو گزارش نمی‌ده — پس اینجا
        # client_app عمداً ست نمی‌شه (برای جلوگیری از نمایشِ نادرست/حدسی).
        last_online = cur.get("last_online")
        if last_online:
            try:
                dt = _dt.datetime.fromisoformat(str(last_online).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_dt.timezone.utc)
                result["last_online_ms"] = int(dt.timestamp() * 1000)
            except Exception:
                logger.exception("تبدیلِ زمانِ آخرین اتصال ناموفق بود")
        return result

    # ───────── تمدید/ترافیک ─────────
    async def add_traffic(self, email, gb):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر روی پنل هیدیفای پیدا نشد.")
        new_limit = float(cur.get("usage_limit_GB") or 0) + float(gb)
        res = await self._request("PATCH", f"/user/{email}/", json={"usage_limit_GB": new_limit})
        if res is None:
            raise PanelError("افزایش حجم ناموفق: کاربر پیدا نشد.")
        return res

    async def renew(self, email, days, reset_usage=False):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر روی پنل هیدیفای پیدا نشد.")
        # === PATCH_RENEW_UNLIMITED_TIME_GUARD_V1 ===
        # باگِ گزارش‌شده: هیدیفای برایِ «زمانِ نامحدود» عددِ ۰ ندارد؛
        # create_account اینجا (بالاتر) به‌جایَش سنتینلِ ۳۶۵۰ روز (۱۰ سال)
        # می‌فرستد. این تابع همیشه بی‌قیدوشرط package_days را با روزهایِ
        # پلنِ تمدید (مثلاً ۳۰) رونویسی می‌کرد — یعنی یک حسابِ قبلاً
        # «تقریباً نامحدودِ» ۱۰ساله، با یک «تمدید مجدد» به فقط ۳۰ روزِ از
        # امروز کوچک می‌شد. حالا اگه package_days فعلی از این سنتینل
        # (۳۰۰۰+ روز) بزرگ‌تر باشد، یعنی از قبل «نامحدود» بوده، همون سنتینل
        # رو نگه می‌داریم به‌جایِ رونویسی‌کردنش با روزهایِ پلن.
        cur_days = int(cur.get("package_days") or 0)
        is_unlimited_time = cur_days >= 3000
        body = {
            "start_date": _dt.date.today().isoformat(),
            "package_days": cur_days if is_unlimited_time else int(days),
            "enable": True, "is_active": True,
        }
        if reset_usage:
            body["usage_limit_GB"] = cur.get("usage_limit_GB")
        res = await self._request("PATCH", f"/user/{email}/", json=body)
        if res is None:
            raise PanelError("تمدید ناموفق: کاربر پیدا نشد.")
        return res

    async def reset_usage(self, email):
        # هیدیفای endpoint اختصاصیِ «ریست مصرف» ندارد؛ نزدیک‌ترین معادل،
        # به‌روزرسانیِ start_date به امروز است (شمارشِ مصرفِ دوره‌ای از نو).
        res = await self._request("PATCH", f"/user/{email}/",
                                   json={"start_date": _dt.date.today().isoformat()})
        return res is not None

    async def reset_sub(self, email, new_sub_id=None):
        # لینکِ ساب در هیدیفای فقط به uuid وابسته است، نه یک شناسهٔ جدا؛
        # پس «ریست‌کردنِ» آن معادل ندارد و همان لینکِ فعلی برگردانده می‌شود.
        return self._build_sub_link(email)

    async def rename_account(self, old_email: str, new_email: str):
        res = await self._request("PATCH", f"/user/{old_email}/",
                                   json={"name": str(new_email)[:64]})
        if res is None:
            raise PanelError("تغییر نام ناموفق: کاربر پیدا نشد.")
        return True

    async def set_enabled(self, email, enabled: bool):
        res = await self._request("PATCH", f"/user/{email}/",
                                   json={"enable": bool(enabled), "is_active": bool(enabled)})
        if res is None:
            raise PanelError("تغییر وضعیت فعال/غیرفعال ناموفق: کاربر پیدا نشد.")
        return True

    async def delete_account(self, email, keep_traffic=False):
        res = await self._request("DELETE", f"/user/{email}/")
        return res is not None

    # ───────── آمار ─────────
    async def list_clients(self):
        res = await self._request("GET", "/user/")
        if not res:
            return []
        out = []
        for u in res:
            used_gb = float(u.get("current_usage_GB") or 0)
            cap_gb = float(u.get("usage_limit_GB") or 0)
            expiry_ms = 0
            start_date, package_days = u.get("start_date"), u.get("package_days")
            if start_date and package_days:
                try:
                    sd = _dt.date.fromisoformat(start_date)
                    exp = sd + _dt.timedelta(days=int(package_days))
                    expiry_ms = int(_dt.datetime.combine(
                        exp, _dt.time.min, tzinfo=_dt.timezone.utc).timestamp() * 1000)
                except Exception:
                    expiry_ms = 0
            out.append({
                "email": u.get("uuid"),
                "traffic": {"up": 0, "down": int(used_gb * GB), "total": int(cap_gb * GB)},
                "totalGB": int(cap_gb * GB),
                "expiryTime": expiry_ms,
            })
        return out

    async def test_connection(self):
        # === PATCH_LOGIN_THROTTLE_V1 ===
        # همون فیکسِ marzban_panel.py — قبلاً force=True بود و چک‌کردنِ
        # دوره‌ایِ panel_watchdog.py (هر ۵ دقیقه) هر بار یک لاگینِ واقعی
        # می‌زد و باعثِ فلپینگِ «قطع/وصل» می‌شد. حالا توکنِ کش‌شده استفاده
        # می‌شه؛ اگه باطل باشه، درخواستِ بعدی خودش با 401/403 دوباره
        # لاگین می‌کنه.
        await self.login()
        return {"ok": True, "inbounds": []}


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
        vol = f"{used_gb} از {cap_gb} گیگ مصرف شده (باقی‌مانده {left_gb} گیگ)"
    else:
        vol = f"{used_gb} گیگ مصرف شده (حجم نامحدود)"
    return vol
