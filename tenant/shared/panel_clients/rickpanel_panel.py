"""rickpanel_panel.py — کلاینتِ پنل RickPanel (rickpanel.io) — PATCH_RICKPANEL_PANEL_V1.

منبع: سندِ OpenAPI و صفحهٔ «API» خودِ پنل (۷۳ endpoint). قرارداد به مرزبان خیلی نزدیک است:

  لاگین:            POST /api/admin/token   (form: username, password) → access_token
  ساخت کاربر:        POST /api/user          {username, expire(epoch ثانیه، ۰=بی‌انقضا), data_limit(بایت، ۰=نامحدود), ...}
  خواندن/ویرایش/حذف: GET/PUT/DELETE /api/user/{username}
  ریست مصرف:         POST /api/user/{username}/reset
  ریست لینک ساب:      POST /api/user/{username}/revoke_sub    (لینک + اعتبارِ پروکسی عوض می‌شود)
  فهرست:             GET  /api/users          → {users, total}
  ادمین/قوانین:       GET  /api/admin          → رکوردِ ادمین + بلوکِ service (قوانینِ فروش)

تفاوت‌هایِ مهم با مرزبان (طبقِ توضیحِ خودِ پنل):
  • «inbounds» و «proxies» پذیرفته می‌شوند ولی نادیده‌اند: هر کاربر همیشه همهٔ پروتکل‌ها و همهٔ inboundها را دارد.
    پس اینجا هیچ‌وقتِ GET /api/inbounds و انتخابِ اینباند لازم نیست (برخلافِ MarzbanPanelClient).
  • ادمین (ریسلر) قوانینِ «سرویس» دارد: حداقلِ طولِ ساخت/تمدید، حداقلِ حجم، الزامِ نامحدود بودنِ حجم،
    ممنوعیتِ on-hold، مهلتِ بینِ دو ریستِ مصرف، و فقط-حذفِ-منقضی. رد شدن با ۴۰۰/۴۰۳ و متنِ دلیل می‌آید
    (به‌شکلِ PanelError با همان متن به ادمین نشان داده می‌شود).
  • پاسخِ ساخت قبل از رسیدنِ کاربر به نودها برمی‌گردد (پس‌زمینه)؛ لینکِ ساب بلافاصله معتبر است.
  • ریستِ مصرف و دانلودِ فایل‌هایِ ساب برایِ هر کاربر سقفِ نرخ دارد (۴۲۹ + Retry-After) — پیامِ صادقانهٔ
    کلاسِ پایه نشان داده می‌شود.
  • limit_ip و تغییرِ نامِ کاربری پشتیبانی نمی‌شود (مثلِ مرزبان).
"""
from __future__ import annotations

import time

from tenant.shared.panel_clients.marzban_panel import (
    GB, MarzbanPanelClient, PanelError, _safe_username, gen_sub_id, gen_uuid,
)

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

__all__ = ["RickPanelClient"]

# چون پنل پروتکل‌ها را خودش همه‌جانبه می‌دهد، فقط یک بدنهٔ حداقلی می‌فرستیم (نادیده گرفته می‌شود)
_PROXIES_STUB = {"vmess": {}, "vless": {}, "trojan": {}, "shadowsocks": {}}


class RickPanelClient(MarzbanPanelClient):
    """مرزبان-سازگار؛ بدونِ inbound، با نمایشِ قوانینِ سرویسِ ریسلر در تستِ اتصال."""

    # ───────── inbound لازم نیست ─────────
    async def active_inbound_ids(self):
        return []

    async def _inbounds_by_protocol(self, tags: list[str]) -> dict:
        return {}

    # ───────── اطلاعاتِ ادمین / قوانینِ سرویس ─────────
    async def admin_info(self) -> dict:
        """GET /api/admin — رکوردِ ادمینِ خودِ این توکن (شاملِ بلوکِ service)."""
        res = await self._request("GET", "/api/admin")
        return res if isinstance(res, dict) else {}

    async def test_connection(self):
        """لاگین + خواندنِ رکوردِ ادمین (سبک و فقط با توکنِ درست موفق). قوانینِ سرویس هم برمی‌گردد تا
        ادمین پیشاپیش بداند چه چیزی رد می‌شود (مثلاً «حجمِ نامحدود الزامی است»)."""
        await self.login()
        info = await self.admin_info()
        return {"ok": True, "inbounds": [], "service": info.get("service") or None,
                "admin": info.get("username") or self.username}

    # ───────── ساخت ─────────
    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0,
                             on_hold=False):
        username = _safe_username(email)
        if mb is not None:
            data_limit = int(mb) * 1024 * 1024
        else:
            data_limit = int(gb) * GB if gb else 0
        total_seconds = int(days) * 86400 + int(hours) * 3600
        expire = int(time.time()) + total_seconds if total_seconds else 0

        body = {
            "username": username,
            "proxies": dict(_PROXIES_STUB),
            "expire": expire,
            "data_limit": data_limit,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": "",
        }
        # on_hold: شمارشِ مدت از اولین اتصال (سرویسِ تست). ممکن است قانونِ سرویسِ ریسلر رد کند (۴۰۰ با دلیل).
        if on_hold and total_seconds:
            body["status"] = "on_hold"
            body["on_hold_expire_duration"] = total_seconds
            body["expire"] = 0
        try:
            res = await self._request("POST", "/api/user", json=body)
        except PanelError as e:
            raise PanelError(self.friendly_error(e)) from e
        if res is None:
            raise PanelError("ساخت کانفیگ ناموفق: کاربری با این نام از قبل وجود دارد.")
        sub_url = res.get("subscription_url") or ""
        return {
            "uuid": uuid_str or gen_uuid(), "sub_id": sub_id or gen_sub_id(),
            "email": username, "gb": gb, "days": days,
            "inbounds": [], "sub_link": self._abs_sub_link(sub_url),
            "expiry_ms": expire * 1000 if expire else 0,
        }

    async def _modify(self, email, **fields):
        try:
            return await super()._modify(email, **fields)
        except PanelError as e:
            raise PanelError(self.friendly_error(e)) from e

    async def reset_usage(self, email):
        try:
            return await super().reset_usage(email)
        except PanelError as e:
            low = str(e).lower()
            if "http 403" in low:
                raise PanelError("ریستِ مصرف رد شد: طبقِ قوانینِ سرویسِ این ادمین باید چند روز از ریستِ قبلی بگذرد. "
                                 f"({str(e)[-120:]})") from e
            raise

    # ───────── پیام‌هایِ فارسیِ خطا (۴۰۰/۴۰۳ ناشی از قوانینِ سرویس) ─────────
    @staticmethod
    def friendly_error(err: Exception) -> str:
        """متنِ خامِ خطایِ پنل را برایِ ادمین قابلِ‌فهم‌تر می‌کند؛ اگر الگو شناخته نشد همان متن."""
        t = str(err)
        low = t.lower()
        if "http 409" in low:
            return "این نامِ کاربری روی پنل از قبل وجود دارد."
        if "user limit" in low or "limit reached" in low:
            return "سقفِ تعدادِ کاربرِ ادمینِ پنل پر شده است؛ از پنل سقف را زیاد کنید."
        if "on hold" in low or "on_hold" in low:
            return "سرویسِ این ادمین روی پنل اجازهٔ «on hold» (شمارش از اولین اتصال) را نمی‌دهد."
        if "unlimited" in low:
            return "سرویسِ این ادمین روی پنل فقط حجمِ نامحدود می‌پذیرد (یا حجمِ محدود را رد می‌کند)."
        if "too short" in low or "minimum" in low:
            return "مدت یا حجمِ درخواستی از حداقلِ مجازِ سرویسِ این ادمین کمتر است."
        return t
