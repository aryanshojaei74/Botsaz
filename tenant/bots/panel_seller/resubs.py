"""resubs.py — PATCH_PSEL_RESUBS_V1 (ABOL / RickPanel «Sub-resellers API»).

مشکلِ واقعی: روی RickPanel حسابِ اتصالِ مالک (ریسلر، نه sudo) اجازهٔ `POST /api/admin` ندارد و ساختِ پنل برایِ مشتری با
«Permission denied: admins.create» رد می‌شد. خودِ پنل برایِ همین کار «API زیرنماینده» دارد (سندِ OpenAPI):

  GET  /accounting/api/resubs/enabled        ۲۰۰ اگر این حساب بخشِ زیرنماینده را دارد، ۴۰۴ برایِ بقیه
  GET  /accounting/api/resubs                فهرستِ زیرنماینده‌ها (+ سهمیه، قوانین)
  POST /accounting/api/resubs                {suffix, password, gb} ⇒ نامِ تازه = «<نامِ کاربریِ تو>_<suffix>»
  POST /accounting/api/resubs/{id}/limit     {gb}      تغییرِ سقف (بالا بردن: remaining ≥ min_remaining؛ پایین: نه کمتر از مصرف)
  POST /accounting/api/resubs/{id}/status    {status: active|disabled}
  POST /accounting/api/resubs/{id}/reset     آزادکردنِ سقفِ خودِ زیرنماینده (بایت‌هایِ مصرف‌شده برایِ تو شارژ می‌ماند)
  POST /accounting/api/resubs/{id}/password  {password}
  GET  /accounting/api/resubs/{id}/detail    سقف، مصرف، تعدادِ کاربران، …

قراردادهایِ مهم: حجم‌ها در پاسخ بایت و در بدنهٔ درخواست گیگ (۱۰۲۴³)؛ رمز ≥۱۲ نویسه (۲ بزرگ، ۲ کوچک، ۲ رقم، ۱ نماد) و بدونِ
نامِ کاربری؛ suffix ۲ تا ۱۶ حرفِ کوچک/رقم؛ ۳۰ نوشتن در دقیقه و ۱۰ ساخت در ساعت؛ ۴۰۹ = قانون رد کرد
(min_remaining، تکراری، سقفِ تعداد). حذفِ زیرنماینده در API نیست ⇒ «حذف» = غیرفعال‌کردن.

پاسخ‌هایِ OpenAPI شِما ندارند (فقط `{}`)، پس نامِ فیلدها را هرگز حدس نمی‌زنیم: چند نامِ محتمل خوانده می‌شود و اگر هیچ‌کدام
نبود PanelError با فهرستِ کلیدها داده می‌شود (و لاگ می‌شود) تا قابلِ اصلاح باشد.
"""
from __future__ import annotations

import re
import time

from tenant.shared.panel_clients.vpn_panel import PanelError

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

BASE = "/accounting/api/resubs"
GB = 1024 ** 3
MAX_GB = 100000                      # سقفِ مجازِ API؛ «نامحدود» (۰) را با همین می‌فرستیم
MIN_GB = 0.5
_ENABLED_TTL = 300.0
_ENABLED: dict[tuple[str, str], tuple[bool, str, float]] = {}     # (base_url, username) → (enabled, prefix, ts)

_ID_KEYS = ("id", "sub_id", "reseller_id", "admin_id")
_USER_KEYS = ("username", "name", "login")
_USED_KEYS = ("usage", "used", "used_bytes", "usage_bytes", "used_traffic", "consumed", "traffic_used")
_USERS_KEYS = ("user_count", "users_count", "users", "total_users", "num_users", "users_total")
_STATUS_KEYS = ("status", "state")
_SUFFIX_RX = re.compile(r"[^a-z0-9]")


def clear_cache() -> None:
    _ENABLED.clear()


def _first(d: dict, keys: tuple) -> object:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def find_rows(obj, _depth: int = 0) -> list[dict]:
    """همهٔ دیکشنری‌هایی (در هر عمقِ معقول) که هم نامِ کاربری و هم شناسه دارند — ساختارِ دقیقِ پاسخ را حدس نمی‌زنیم."""
    out: list[dict] = []
    if _depth > 5:
        return out
    if isinstance(obj, dict):
        if isinstance(_first(obj, _USER_KEYS), str) and _first(obj, _ID_KEYS) is not None:
            out.append(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                out.extend(find_rows(v, _depth + 1))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(find_rows(v, _depth + 1))
    return out


def make_suffix(username: str) -> str:
    """suffix مجاز: ۲ تا ۱۶ حرفِ کوچکِ انگلیسی/رقم."""
    s = _SUFFIX_RX.sub("", (username or "").lower())[:16]
    return s if len(s) >= 2 else (s + "ps")[:16]


def map_error(e: PanelError, what: str) -> PanelError:
    """خطایِ خامِ پنل (با HTTP code و متنِ پاسخ) ⇒ پیامِ فارسیِ قابلِ اقدام. متنِ اصلی هم کنارش می‌ماند."""
    t = str(e)
    low = t.lower()
    if "http 409" in low:
        if "min_remaining" in low:
            return PanelError(f"{what}: باقی‌ماندهٔ سهمیهٔ حسابِ اصلی کمتر از حداقلِ مجاز (۵۰۰ گیگ) است. سهمیهٔ پنل را زیاد کن. | {t[:200]}")
        if "exist" in low:
            return PanelError(f"{what}: نامِ کاربری تکراری است. | {t[:200]}")
        if "limit" in low or "maximum" in low:
            return PanelError(f"{what}: به سقفِ تعدادِ زیرنماینده‌هایِ مجاز رسیده‌ای. | {t[:200]}")
        return PanelError(f"{what}: پنل طبقِ قوانینش رد کرد. | {t[:200]}")
    if "http 429" in low or "rate limit" in low:
        return PanelError(f"{what}: سقفِ درخواست پر شده (۳۰ نوشتن در دقیقه، ۱۰ ساخت در ساعت)؛ کمی بعد دوباره امتحان کن. | {t[:200]}")
    if "http 403" in low:
        return PanelError(f"{what}: این حساب به بخشِ زیرنماینده‌ها دسترسی ندارد یا زیرنماینده مالِ تو نیست. | {t[:200]}")
    if "http 422" in low:
        return PanelError(f"{what}: ورودی نامعتبر (رمز باید ≥۱۲ نویسه با ۲ بزرگ، ۲ کوچک، ۲ رقم و ۱ نماد و بدونِ نامِ کاربری باشد). | {t[:200]}")
    return e


class ResubAPI:
    """نمای ساده بالای API زیرنماینده؛ client همان کلاینتِ مرزبان‌سازگارِ RickPanel است (_request با توکنِ ادمین)."""

    def __init__(self, client):
        self.c = client
        self.prefix: str = ""
        self._ids: dict[str, int] = {}

    # ───────── تشخیصِ فعال‌بودن ─────────
    async def enabled(self) -> bool:
        key = (str(getattr(self.c, "base_url", "")), str(getattr(self.c, "username", "")))
        hit = _ENABLED.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[2] < _ENABLED_TTL:
            self.prefix = hit[1]
            return hit[0]
        try:
            res = await self.c._request("GET", f"{BASE}/enabled")
        except PanelError as e:
            t = str(e)
            logger.info("resubs: بررسیِ فعال‌بودن ناموفق (%s)؛ مسیرِ قدیمیِ ادمین استفاده می‌شود", t[:120])
            if any(f"HTTP {c}" in t for c in (400, 403, 405, 410, 422)):
                _ENABLED[key] = (False, "", now)          # پاسخِ قطعیِ «این پنل ندارد/اجازه نیست» را کش کن؛ خطایِ موقتی/اتصال را نه
            return False
        # فقط یک JSON واقعیِ غیرخالی «فعال» است. ۴۰۴ ⇒ None؛ پنل‌هایی که برایِ مسیرِ ناشناخته صفحهٔ HTML (SPA) برمی‌گردانند
        # {"raw": ...} می‌دهند و هرگز نباید «فعال» حساب شوند.
        ok = isinstance(res, dict) and bool(res) and "raw" not in res
        prefix = ""
        if isinstance(res, dict) and ok:
            p = _first(res, ("prefix", "username_prefix", "username"))
            prefix = p if isinstance(p, str) else ""
        prefix = prefix or str(getattr(self.c, "username", "") or "")
        _ENABLED[key] = (ok, prefix, now)
        self.prefix = prefix
        return ok

    # ───────── فهرست/ردیف ─────────
    async def _rows(self) -> list[dict]:
        res = await self.c._request("GET", BASE)
        rows = find_rows(res)
        if not rows and res:
            logger.warning("resubs: فهرستِ زیرنماینده‌ها خوانده نشد؛ کلیدهایِ پاسخ: %s",
                           sorted(res.keys()) if isinstance(res, dict) else type(res).__name__)
        return rows

    async def row(self, username: str) -> dict | None:
        low = (username or "").lower()
        for r in await self._rows():
            nm = str(_first(r, _USER_KEYS) or "").lower()
            if nm == low:
                try:
                    self._ids[low] = int(_first(r, _ID_KEYS))
                except (TypeError, ValueError):
                    pass
                return r
        return None

    async def has(self, username: str) -> bool:
        return await self.row(username) is not None

    async def _id(self, username: str) -> int:
        low = (username or "").lower()
        if low in self._ids:
            return self._ids[low]
        r = await self.row(username)
        if r is None:
            raise PanelError(f"زیرنمایندهٔ «{username}» در پنل پیدا نشد.")
        return self._ids[low]

    async def _post(self, path: str, body: dict | None, what: str):
        try:
            res = await self.c._request("POST", path, json=body or {})
        except PanelError as e:
            raise map_error(e, what) from e
        if res is None:
            raise PanelError(f"{what}: بخشِ زیرنماینده‌ها برای این حساب فعال نیست یا زیرنماینده پیدا نشد (۴۰۴).")
        return res

    # ───────── عملیات ─────────
    async def create(self, username: str, password: str, gb: int) -> str:
        """نامِ واقعیِ ساخته‌شده را برمی‌گرداند («<پیشوند>_<suffix>»)."""
        suffix = make_suffix(username)
        cap = float(gb) if gb and float(gb) > 0 else float(MAX_GB)
        cap = max(MIN_GB, min(cap, float(MAX_GB)))
        if not self.prefix:
            await self.enabled()
        full = f"{self.prefix}_{suffix}" if self.prefix else suffix
        if suffix in password.lower() or full.lower() in password.lower():
            raise PanelError("رمزِ تولیدشده شاملِ نامِ کاربری است؛ دوباره امتحان کن.")
        res = await self._post(BASE, {"suffix": suffix, "password": password, "gb": cap}, "ساختِ زیرنماینده")
        rows = find_rows(res)
        real = None
        if rows:
            real = str(_first(rows[0], _USER_KEYS))
            try:
                self._ids[real.lower()] = int(_first(rows[0], _ID_KEYS))
            except (TypeError, ValueError):
                pass
        elif isinstance(res, dict) and isinstance(_first(res, _USER_KEYS), str):
            real = str(_first(res, _USER_KEYS))
        if not real:
            # پاسخ نامِ ساخته‌شده را نداد؛ از فهرست تأیید می‌کنیم (هرگز نامی را فقط حدس نمی‌زنیم)
            r = await self.row(full)
            if r is None:
                raise PanelError("زیرنماینده ساخته شد ولی در فهرستِ پنل پیدا نشد؛ از پنل بررسی کن "
                                 f"(انتظار: {full}).")
            real = str(_first(r, _USER_KEYS))
        return real

    async def set_password(self, username: str, password: str) -> None:
        await self._post(f"{BASE}/{await self._id(username)}/password", {"password": password}, "تغییرِ رمزِ زیرنماینده")

    async def set_disabled(self, username: str, disabled: bool) -> bool:
        sid = await self._id(username)
        await self._post(f"{BASE}/{sid}/status", {"status": "disabled" if disabled else "active"},
                         "غیرفعال‌کردنِ زیرنماینده" if disabled else "فعال‌کردنِ زیرنماینده")
        return True

    async def is_disabled(self, username: str) -> bool | None:
        r = await self.row(username)
        if r is None:
            return None
        v = _first(r, _STATUS_KEYS)
        if isinstance(v, str):
            return v.lower() == "disabled"
        for k in ("disabled", "is_disabled"):
            if isinstance(r.get(k), bool):
                return bool(r[k])
        if isinstance(r.get("enabled"), bool):
            return not r["enabled"]
        return None

    async def apply_data_limit(self, username: str, gb: int) -> bool:
        cap = float(gb) if gb and float(gb) > 0 else float(MAX_GB)
        cap = max(MIN_GB, min(cap, float(MAX_GB)))
        await self._post(f"{BASE}/{await self._id(username)}/limit", {"gb": cap}, "تغییرِ سقفِ زیرنماینده")
        return True

    async def reset_usage(self, username: str) -> None:
        await self._post(f"{BASE}/{await self._id(username)}/reset", None, "ریستِ مصرفِ زیرنماینده")

    async def _detail(self, username: str) -> dict:
        sid = await self._id(username)
        try:
            res = await self.c._request("GET", f"{BASE}/{sid}/detail")
        except PanelError as e:
            raise map_error(e, "خواندنِ جزئیاتِ زیرنماینده") from e
        if not isinstance(res, dict):
            raise PanelError("جزئیاتِ زیرنماینده خوانده نشد.")
        return res

    def _num(self, *srcs: dict, keys: tuple, what: str) -> int:
        for d in srcs:
            v = _first(d, keys) if isinstance(d, dict) else None
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                return int(v)
            if isinstance(v, str) and v.replace(".", "", 1).isdigit():
                return int(float(v))
        keys_seen = sorted({k for d in srcs if isinstance(d, dict) for k in d.keys()})
        logger.warning("resubs: %s در پاسخِ پنل پیدا نشد؛ کلیدها: %s", what, keys_seen)
        raise PanelError(f"{what} از پنل خوانده نشد (کلیدهایِ پاسخ: {', '.join(keys_seen)[:200]}).")

    async def usage_bytes(self, username: str) -> int:
        r = await self.row(username)
        try:
            d = await self._detail(username)
        except PanelError:
            d = {}
        return self._num(d, r or {}, keys=_USED_KEYS, what="مصرفِ زیرنماینده")

    async def user_count(self, username: str) -> int:
        r = await self.row(username)
        try:
            d = await self._detail(username)
        except PanelError:
            d = {}
        return self._num(d, r or {}, keys=_USERS_KEYS, what="تعدادِ کاربرانِ زیرنماینده")

    async def snapshot(self, username: str) -> dict:
        return {"used_bytes": await self.usage_bytes(username), "users": await self.user_count(username),
                "new_style": False}

    async def list_rows(self) -> list[dict]:
        return await self._rows()
