"""
shahan_panel.py — کلاینت async پنل شاهان (Shahan Panel) برای بات‌ساز.

منبعِ مستندات: https://shahanpanel.com/api.html (تیر ۱۴۰۵ / جولای ۲۰۲۶)

معماریِ این پنل کاملاً متفاوت از خانوادهٔ مرزبان است:
  • یک endpoint واحد:  POST /apiV1/api.php  با فیلدِ method برای انتخابِ عملیات
    (adduser / edituser / deleteuser / suspenduser / unsuspenduser / userinfo /
    getusertraffic / alluser / changepassword / online ...)
  • احرازهویت با یک token ثابت (در body یا query) — نه لاگین/رمزِ کاربری و نه
    توکنِ موقتِ JWT.
  • هیچ مفهومِ inbound/گروه ندارد؛ کاربر مستقیم روی خودِ پنل ساخته می‌شود.
  • ترافیک به «مگابایت» است (نه بایت مثلِ 3X-UI، نه گیگ مستقیم).
  • برای «اولین اتصال» یه قراردادِ خاص دارد (finishdate=2100-01-01 + days تا
    شمارشِ روز از اولین وصل‌شدن شروع شود)، ولی چون UX این ربات همیشه انقضا را
    مطلق و از لحظهٔ خرید نشان می‌دهد (نه از اولین اتصال)، اینجا finishdate را
    مستقیم به‌صورت تاریخِ مطلق (امروز + days) می‌فرستیم — دقیقاً هم‌رفتار با
    بقیهٔ پنل‌های این پروژه.

⚠️ نکاتِ مهم (این کلاینت فقط بر پایهٔ مستنداتِ رسمی نوشته شده، بدون دسترسیِ
زنده به یک پنلِ واقعیِ شاهان برای تست — حتماً قبل از فروشِ واقعی با یک
حساب/کانفیگِ آزمایشی امتحانش کن):
  • مستنداتِ رسمی برای فیلدِ «traffic» بینِ اندپوینت‌های مختلف ناسازگارند
    (در adduser صراحتاً می‌گوید MB، ولی نمونهٔ پاسخِ alluser عددی می‌دهد که
    با MB جور در نمی‌آید). این کلاینت طبقِ قاعدهٔ کلیِ بالای مستندات («Traffic
    values are in MB») عمل می‌کند. اگر رفتارِ پنلِ واقعی فرق داشت، فقط باید
    _mb_to_input/_bytes_from_traffic را عوض کرد؛ بقیهٔ کلاینت دست‌نخورده می‌ماند.
  • این پنل «ساب‌لینکِ» یکپارچه (مثلِ مرزبان/3X-UI) ندارد؛ به‌جایش چند لینکِ
    مستقیمِ پروتکلی (tuiclink/shadowsockslink/...) می‌دهد که کدام‌ها موجودند
    به نسخهٔ نصب‌شدهٔ پنل بستگی دارد (مستندات این‌ها را با علامتِ ⚡ «نسخه‌های
    جدیدتر» مشخص کرده). اینجا اولین لینکِ غیرخالیِ شناخته‌شده را به‌عنوانِ
    sub_link برمی‌گردانیم؛ اگر پنل هیچ‌کدام را نداد، رشتهٔ خالی برمی‌گردد
    (دقیقاً مثلِ رفتارِ فعلیِ سایرِ پنل‌ها وقتی ساب‌لینک در دسترس نیست).
"""
from __future__ import annotations

import time
import re
import secrets
import string
import asyncio
from datetime import datetime, timedelta, timezone

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

try:
    import aiohttp
except ImportError:
    aiohttp = None

from tenant.shared.panel_clients.vpn_panel import PanelError

MB = 1024 * 1024
GB = 1024 * 1024 * 1024

__all__ = ["ShahanPanelClient"]


def _rand(n=8):
    a = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(a) for _ in range(n))


def gen_sub_id(n=16):
    return _rand(n)


def gen_uuid():
    import uuid as _uuid
    return str(_uuid.uuid4())


def _safe_username(email: str) -> str:
    """شاهان محدودیتِ رسمیِ مستندشده‌ای برای فرمتِ یوزرنیم اعلام نکرده، ولی
    برای اطمینان همان قاعدهٔ محافظه‌کارانهٔ مرزبان (فقط حروف/عدد/زیرخط) را
    اعمال می‌کنیم تا با احتمالِ محدودیتِ داخلیِ پنل خطا نگیریم."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", str(email)).lower()
    cleaned = cleaned[:32]
    if len(cleaned) < 3:
        cleaned = (cleaned + "_usr")[:32]
    return cleaned


# لینک‌های پروتکلیِ محتمل در پاسخِ userinfo (بسته به نسخهٔ پنل) — به ترتیبِ
# اولویت امتحان می‌شوند تا اولین موردِ غیرخالی به‌عنوانِ sub_link استفاده شود.
_LINK_FIELDS = (
    "sublink", "subscriptionlink", "sub_link", "subscription_link",
    "vlesslink", "vmesslink", "trojanlink", "shadowsockslink",
    "tuiclink", "wireguardlink", "hysterialink",
)


class ShahanPanelClient:
    """یک نشست به پنل شاهانِ یک مستأجر.

    امضای سازنده عیناً مثل PanelClient/MarzbanPanelClient است تا factory
    بدون تغییر کار کند — با این تفاوت که این پنل «یوزرنیم/رمزِ ورود» ندارد،
    فقط یک token ثابت. طبقِ قراردادِ ویزاردِ تنظیمِ پنل در vpn_admin.py،
    توکن در پارامترِ password می‌آید (پارامترِ username برای این پنل استفاده
    نمی‌شود، ویزارد از ادمین می‌خواهد آنجا «-» بفرستد).
    inbound_ids/sub_base برای این پنل معنایی ندارند و کاملاً نادیده گرفته
    می‌شوند (شاهان مفهومِ inbound/گروه ندارد).
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
        self.base_url = (base_url or "").split("#", 1)[0].rstrip("/")
        self.token = (password or "").strip()  # توکنِ ثابتِ API
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = None
        self._lock = asyncio.Lock()

    # ───────── نشست ─────────
    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            t = aiohttp.ClientTimeout(total=self.timeout)
            conn = aiohttp.TCPConnector(ssl=self.verify_ssl)
            self._session = aiohttp.ClientSession(timeout=t, connector=conn)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        await self.close()

    async def login(self, force=False):
        """شاهان لاگین/توکنِ موقت ندارد (توکن ثابت است)؛ این متد فقط برای
        سازگاریِ رابط با بقیهٔ کلاینت‌ها نگه داشته شده و همیشه True برمی‌گرداند."""
        if not self.token:
            raise PanelError("توکنِ پنل شاهان تنظیم نشده.")
        return True

    async def _call(self, method: str, **fields):
        """یک فراخوانیِ خام به /apiV1/api.php با فیلدِ method + بقیهٔ فیلدها،
        به‌شکلِ JSON body. توکن هم در body و هم در query فرستاده می‌شود
        (طبقِ مستندات هر دو پشتیبانی می‌شوند؛ فرستادنِ هردو بی‌ضرر و
        محافظه‌کارانه‌تر است)."""
        if not self.token:
            raise PanelError("توکنِ پنل شاهان تنظیم نشده.")
        s = await self._ensure_session()
        url = f"{self.base_url}/apiV1/api.php?token={self.token}"
        payload = {"method": method, "token": self.token}
        payload.update({k: v for k, v in fields.items() if v is not None})
        try:
            async with s.post(url, json=payload) as r:
                text = await r.text()
                if r.status >= 400:
                    raise PanelError(f"{method} HTTP {r.status}: {text[:300]}")
                try:
                    import json as _json
                    data = _json.loads(text)
                except Exception:
                    raise PanelError(f"{method}: پاسخِ پنل JSON معتبر نبود: {text[:200]}")
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
                f"اتصال به پنل شاهان برقرار نشد یا بیش از حد طول کشید: {err_detail}") from e

        # پاسخ‌های شاهان دو قالبِ رایج دارند: {response_code, response_desc, data}
        # یا {status, message, data} — هر دو را پشتیبانی می‌کنیم.
        code = data.get("response_code", data.get("status"))
        if code is not None and int(code) not in (200,):
            desc = data.get("response_desc") or data.get("message") or "خطای نامشخص"
            raise PanelError(f"شاهان خطا داد ({method}): {desc}")
        return data.get("data")

    # ───────── inbound/گروه (این پنل ندارد — no-op برای سازگاریِ رابط) ─────────
    async def list_inbounds(self):
        return []

    async def active_inbound_ids(self):
        return []

    # ───────── ساخت ─────────
    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0,
                             on_hold=False):
        """ساخت کاربر در شاهان با همان امضای create_account سایر پنل‌ها.

        gb: حجم به گیگ (0=نامحدود، اگر پنل واقعاً از مقدارِ ۰/خالی برای
        نامحدود پشتیبانی نکند، این را باید بعداً با یک عددِ خیلی بزرگ جایگزین
        کرد). mb: اگر داده شود حجم مستقیم به مگابایت (سرویسِ تست).
        finishdate مطلق (امروز + days) فرستاده می‌شود تا با UX «انقضا از
        لحظهٔ خرید» بقیهٔ پنل‌های این پروژه یکسان باشد.
        limit_ip → multiuser (تعداد اتصالِ هم‌زمان).
        """
        username = _safe_username(email)
        if mb is not None:
            traffic_mb = int(mb)
        else:
            traffic_mb = int(gb) * 1024 if gb else 0

        password = _rand(12)
        fields = {
            "username": username,
            "password": password,
            "traffic": traffic_mb,
        }
        if limit_ip:
            fields["multiuser"] = int(limit_ip)
        if days:
            finishdate = (datetime.now(timezone.utc) + timedelta(days=int(days))).strftime("%Y-%m-%d")
            fields["finishdate"] = finishdate
            fields["days"] = int(days)
        else:
            # بدونِ انقضا — طبقِ قراردادِ مستنداتِ شاهان برای «بدونِ محدودیتِ
            # زمانی»، تاریخِ خیلی دور در آینده می‌فرستیم.
            fields["finishdate"] = "2100-01-01"

        await self._call("adduser", **fields)

        # اطلاعاتِ کامل (لینک‌ها و ...) را جداگانه می‌گیریم چون adduser فقط
        # true/false برمی‌گرداند، نه جزئیاتِ کاربرِ ساخته‌شده.
        info = await self._userinfo_raw(username)
        sub_link = self._extract_link(info)
        expire = int(time.mktime(datetime.strptime(fields["finishdate"], "%Y-%m-%d").timetuple())) \
            if fields.get("finishdate") and fields["finishdate"] != "2100-01-01" else 0

        return {
            "uuid": uuid_str or gen_uuid(), "sub_id": sub_id or gen_sub_id(),
            "email": username, "gb": gb, "days": days,
            "inbounds": [], "sub_link": sub_link,
            "expiry_ms": expire * 1000 if expire else 0,
            "_shahan_password": password,  # برای مواردی که پنل نیازمندِ نمایشِ رمز به کاربر است
        }

    async def _userinfo_raw(self, username: str) -> dict:
        try:
            data = await self._call("userinfo", username=username)
            return data if isinstance(data, dict) else {}
        except PanelError:
            return {}

    def _extract_link(self, info: dict) -> str:
        if not info:
            return ""
        for key in _LINK_FIELDS:
            v = info.get(key)
            if v:
                return v
        return ""

    # ───────── خواندن ─────────
    async def get_client(self, email):
        username = _safe_username(email)
        info = await self._userinfo_raw(username)
        if not info:
            return None
        info = dict(info)
        info["sub_link"] = self._extract_link(info)
        info["id"] = username
        info["subId"] = username
        return info

    # === PATCH_SHAHAN_WG_SINGLE_CONFIG_V1 ===
    # «کانفیگ‌های تکی» (vget:single) قبلاً روی این پنل کلاً کار نمی‌کرد،
    # چون get_client_links اصلاً تعریف نشده بود (نه ارث‌بری، نه پیاده‌سازیِ
    # خودش) و صدازدنش AttributeError می‌داد. اینجا هر فیلدِ غیرخالیِ
    # _LINK_FIELDS (شاملِ wireguardlink) را به‌عنوانِ یک آیتمِ جدا برمی‌گردانیم؛
    # اگر محتوایِ wireguardlink خودِ کانفیگِ خامِ INI باشد (شروع با
    # «[Interface]»)، منطقِ عمومیِ vget_single در tenant/vpn_user.py آن را
    # به‌جایِ QR، به‌صورتِ فایلِ .conf می‌فرستد — بدونِ نیاز به هیچ تغییرِ
    # دیگری. اگر لینکِ URI-مانند باشد، مثلِ بقیهٔ پروتکل‌ها با QR می‌رود.
    async def get_client_links(self, email):
        username = _safe_username(email)
        info = await self._userinfo_raw(username)
        if not info:
            return []
        links = []
        for key in _LINK_FIELDS:
            v = info.get(key)
            if v:
                links.append(str(v).strip())
        return links

    async def get_usage(self, email):
        """آمار مصرف با همان کلیدهای PanelClient (up/down/total/expiryTime)
        برای سازگاری با _usage_block در vpn_user.py.

        ⚠️ نکته: مستنداتِ getusertraffic واحدِ اعداد را مشخص نکرده‌اند؛ اینجا
        به‌صورتِ محافظه‌کارانه بایت فرض شده (چون بیشترِ پنل‌های مشابه همین‌طورند).
        اگر روی پنلِ واقعی عددها خیلی کوچک/بزرگ به‌نظر رسید، این تابع اولین
        جایی است که باید تنظیم شود."""
        username = _safe_username(email)
        try:
            traffic_data = await self._call("getusertraffic", username=username)
        except PanelError:
            traffic_data = None
        up = down = 0
        if isinstance(traffic_data, list) and traffic_data:
            row = traffic_data[0]
            up = int(row.get("upload", 0) or 0)
            down = int(row.get("download", 0) or 0)
        info = await self._userinfo_raw(username)
        if not info:
            return None
        cap_raw = info.get("traffic", 0)
        try:
            cap = int(float(cap_raw)) * MB
        except Exception:
            cap = 0
        finishdate = info.get("finishdate", "")
        expiry_ms = 0
        if finishdate and finishdate != "2100-01-01":
            try:
                dt = datetime.strptime(finishdate, "%Y-%m-%d")
                expiry_ms = int(dt.timestamp() * 1000)
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        return {
            "up": up, "down": down, "total": cap, "expiryTime": expiry_ms,
            "enable": str(info.get("enable", "true")).lower() != "false",
        }

    # ───────── تمدید/ترافیک ─────────
    async def add_traffic(self, email, gb):
        username = _safe_username(email)
        info = await self._userinfo_raw(username)
        if not info:
            raise PanelError("کاربر روی پنل شاهان پیدا نشد.")
        try:
            cur_mb = int(float(info.get("traffic", 0) or 0))
        except Exception:
            cur_mb = 0
        if cur_mb == 0:
            return info  # نامحدود — افزایش بی‌معنی است
        new_mb = cur_mb + int(gb) * 1024
        await self._call("edituser", username=username, traffic=new_mb)
        return await self._userinfo_raw(username)

    async def renew(self, email, days, reset_usage=False):
        username = _safe_username(email)
        info = await self._userinfo_raw(username)
        if not info:
            raise PanelError("کاربر روی پنل شاهان پیدا نشد.")
        cur_finish = info.get("finishdate", "")
        # === PATCH_RENEW_UNLIMITED_TIME_GUARD_V1 ===
        # باگِ گزارش‌شده: این تابع سنتینلِ «نامحدود» (۲۱۰۰-۰۱-۰۱) را
        # تشخیص می‌داد ولی به‌جایِ نگه‌داشتنش، بازم base را «الان» می‌گذاشت
        # و finishdate را با یک تاریخِ نزدیکِ واقعی (الان + روزهایِ پلن)
        # رونویسی می‌کرد — یعنی هر «تمدید» رویِ یک حسابِ زمانِ‌نامحدود،
        # نامحدودی‌اش را از بین می‌برد. حالا اگه از قبل نامحدود بود، تمدیدِ
        # زمان را بی‌خطر رد می‌کنیم.
        if cur_finish == "2100-01-01":
            if reset_usage:
                await self.reset_usage(email)
            return info
        now = datetime.now(timezone.utc)
        base = now
        if cur_finish:
            try:
                cur_dt = datetime.strptime(cur_finish, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if cur_dt > now:
                    base = cur_dt
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        new_finish = (base + timedelta(days=int(days))).strftime("%Y-%m-%d")
        await self._call("edituser", username=username, finishdate=new_finish)
        if reset_usage:
            await self.reset_usage(email)
        return await self._userinfo_raw(username)

    async def reset_usage(self, email):
        username = _safe_username(email)
        try:
            await self._call("resettraffic", username=username)
            return True
        except PanelError:
            # resettraffic نشانه‌گذاری‌شده با ⚡ در مستندات (فقط نسخه‌های
            # جدیدتر) — اگر پنل پشتیبانی نکند، خطای واضح‌تری بده تا با یک
            # خطای مبهمِ HTTP اشتباه گرفته نشود.
            raise PanelError("ریست مصرف پشتیبانی نشد (این متد فقط در نسخه‌های جدیدترِ پنل شاهان موجود است).")

    async def reset_sub(self, email, new_sub_id=None):
        """شاهان ساب‌لینکِ جداگانه‌ای ندارد که «ریست» شود؛ ساده‌ترین معادل،
        تغییرِ رمزِ کاربر است (که معمولاً باعثِ تغییرِ لینک‌های پروتکلی هم
        می‌شود، چون لینک‌ها معمولاً بر پایهٔ همان اطلاعاتِ ورود ساخته می‌شوند)."""
        username = _safe_username(email)
        new_password = _rand(12)
        await self._call("changepassword", username=username, password=new_password)
        info = await self._userinfo_raw(username)
        return self._extract_link(info)

    async def rename_account(self, old_email: str, new_email: str):
        raise PanelError(
            "در پنل شاهان نام کاربری قابل تغییر نیست؛ نام جدید فقط روی کانفیگ‌های تازه اعمال می‌شود.")

    async def set_enabled(self, email, enabled: bool):
        username = _safe_username(email)
        method = "unsuspenduser" if enabled else "suspenduser"
        await self._call(method, username=username)
        return True

    async def delete_account(self, email, keep_traffic=False):
        username = _safe_username(email)
        try:
            await self._call("deleteuser", username=username)
            return True
        except PanelError:
            return False

    # ───────── آمار ─────────
    async def list_clients(self):
        """لیستِ کاربرانِ شاهان به‌شکلِ تخت و با کلیدهای هم‌نامِ PanelClient
        (email, traffic{up,down,total}, expiryTime, totalGB)."""
        try:
            data = await self._call("alluser")
        except PanelError:
            return []
        users = data if isinstance(data, list) else []
        out = []
        for u in users:
            try:
                cap_mb = int(float(u.get("traffic", 0) or 0))
            except Exception:
                cap_mb = 0
            finishdate = u.get("finishdate", "")
            expiry_ms = 0
            if finishdate and finishdate != "2100-01-01":
                try:
                    dt = datetime.strptime(finishdate, "%Y-%m-%d")
                    expiry_ms = int(dt.timestamp() * 1000)
                except Exception:
                    logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
            out.append({
                "email": u.get("username"),
                "traffic": {"up": 0, "down": 0, "total": cap_mb * MB},
                "totalGB": cap_mb * MB,
                "expiryTime": expiry_ms,
            })
        return out

    async def test_connection(self):
        """تستِ اتصالِ ادمین: یک فراخوانیِ سبک (alluser) که فقط با توکنِ
        درست موفق می‌شود."""
        await self._call("alluser")
        return {"ok": True, "inbounds": []}
