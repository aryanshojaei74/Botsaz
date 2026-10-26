"""solidlayer_panel.py — کلاینتِ پنلِ «SolidLayer» («Management API»، Swagger 2.0؛ /api/subscriptions).

=== PATCH_SOLIDLAYER_PANEL_V1 ===
گزارشِ مشتری (ɪᴍ🍂mehdi): پنلی با مستنداتِ Swagger («Management API») روی app.solidlayerco.com. قراردادِ API از
openapi.json (doc.json) خوانده شد؛ هیچ درخواستِ واقعی‌ای به پنل زده نشده، پس نکاتِ زیر «حدس‌هایِ محافظه‌کارانه + تأییدِ
خودکار» هستند، نه واقعیتِ اثبات‌شده:

  • ورود: POST /api/admins/token {username,password} ⇒ {token,type,admin{api_key,…}}؛ یا مستقیم «API Key» ادمین (هدرِ X-API-Key).
    هر دو هدر (Authorization: Bearer … و X-API-Key) فرستاده می‌شود؛ اگر ۴۰۱ آمد یک‌بار دوباره لاگین می‌شود.
  • «کاربر» = subscription؛ شناسه‌اش همان username (= email در بقیهٔ کلاینت‌ها). ساخت/ویرایش/حذف/فعال/غیرفعال/ریست/ریووک همه
    «bulk by usernames» هستند (حداکثر ۱۰ نفر)؛ اینجا همیشه یک نفر می‌فرستیم.
  • limit_usage = بایت (مستندات: «Limits never drop below 1 byte»)؛ ۰/خالی = نامحدود.
  • limit_expire واحدش در مستندات نیامده (ثانیه/میلی‌ثانیه/روز؟). برای همین بعد از هر نوشتن، تاریخِ واقعی که پنل برمی‌گرداند
    (expired_at) با تاریخِ خواسته‌شده مقایسه می‌شود و در صورتِ ناهم‌خوانی فرمتِ بعدی امتحان می‌شود؛ اگر هیچ‌کدام درست نشد
    خطای صریح می‌دهد (به‌جایِ ساختِ اکانتِ نادرست) و اکانتِ ساخته‌شده حذف می‌شود. فرمتِ تأییدشده برایِ هر پنل در حافظه می‌ماند.
  • مصرفِ فعلی = total_usage − reset_usage (حدس: reset_usage مبنایِ آخرین ریست است).
  • سرویس‌ها (services): اگر مالک شناسه ندهد، همهٔ سرویس‌هایِ قابلِ‌دسترسِ همین ادمین (GET /api/services) به کاربر وصل می‌شود.
  • لینکِ ساب خودِ پنل می‌دهد (subscription_link)؛ ساخت از رویِ sub_id لازم نیست.
  • تغییرِ نامِ کاربری پشتیبانی نمی‌شود (username شناسهٔ یکتاست) و on-hold/limit_ip استفاده نمی‌شود.
"""
from __future__ import annotations

import asyncio
import json as _json
import math
import time
import datetime as _dt

try:
    import aiohttp
except ImportError:           # pragma: no cover — PanelClient خودش خطای واضح می‌دهد
    aiohttp = None

from tenant.shared.panel_clients.vpn_panel import PanelError, gen_sub_id, gen_uuid
from tenant.shared.panel_clients.inactive_users import parse_ms

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

__all__ = ["SolidLayerPanelClient"]

GB = 1024 ** 3
_PAGE = 100
_MAX_PAGES = 100
# فرمتِ تأییدشدهٔ limit_expire به‌ازایِ هر پنل (base_url): "s" | "ms" | "days"
_EXPIRE_FMT: dict[str, str] = {}
_FMT_ORDER = ("s", "ms", "days")
_TOLERANCE_MS = 36 * 3600 * 1000          # اختلافِ مجازِ تاریخِ خواسته‌شده با تاریخِ پنل


def _encode_expire(fmt: str, target_ms: int, now_ms: int) -> int:
    if fmt == "s":
        return int(target_ms // 1000)
    if fmt == "ms":
        return int(target_ms)
    return max(1, math.ceil((target_ms - now_ms) / 86_400_000))


def _used(sub: dict) -> int:
    try:
        return max(0, int(sub.get("total_usage") or 0) - int(sub.get("reset_usage") or 0))
    except (TypeError, ValueError):
        return 0


def _msg_of(res) -> str:
    if isinstance(res, dict):
        errs = res.get("errors")
        if isinstance(errs, list) and errs:
            return "; ".join(f"{e.get('field')}: {e.get('message')}" for e in errs[:4] if isinstance(e, dict))
        return str(res.get("message") or res)[:300]
    return str(res)[:300]


class SolidLayerPanelClient:
    """امضای سازنده هم‌شکلِ بقیهٔ کلاینت‌ها: username/password ⇒ ورودِ ادمین؛ یا api_key (بدونِ یوزرنیم/پسورد).
    inbound_ids ⇒ شناسهٔ سرویس‌ها (اختیاری؛ خالی = همهٔ سرویس‌هایِ ادمین)."""

    def __init__(self, base_url: str, username: str, password: str, sub_base: str = "",
                 inbound_ids=None, verify_ssl: bool = False, timeout: int = 8, api_key: str | None = None):
        if aiohttp is None:
            raise PanelError("کتابخانهٔ aiohttp نصب نیست. `pip install aiohttp` را اجرا کن.")
        base_url = (base_url or "").split("#", 1)[0].rstrip("/")
        if base_url.endswith("/docs") or "/docs/" in base_url:
            base_url = base_url.split("/docs", 1)[0].rstrip("/")
        self.base_url = base_url
        self.username = (username or "").strip()
        self.password = password or ""
        self.api_key = (api_key or "").strip()
        self.sub_base = (sub_base or "").strip().rstrip("/")
        self.service_ids = [int(x) for x in (inbound_ids or []) if str(x).strip().isdigit()]
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = None
        self._token = ""
        self._token_type = "Bearer"
        self._login_lock = asyncio.Lock()

    # ───────────────────────── اتصال ─────────────────────────
    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            t = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(timeout=t, connector=aiohttp.TCPConnector(ssl=self.verify_ssl))
        return self._session

    def _auth_headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"{self._token_type} {self._token}"
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    async def login(self, force: bool = False):
        """با یوزرنیم/پسورد توکن می‌گیرد (و api_keyِ ادمین را اگر برگردد نگه می‌دارد). با API Key فقط اعتبار را می‌سنجد."""
        async with self._login_lock:
            if self._token and not force:
                return True
            if self.username and self.password:
                s = await self._ensure_session()
                try:
                    async with s.post(f"{self.base_url}/api/admins/token",
                                      json={"username": self.username, "password": self.password},
                                      headers={"Accept": "application/json"}) as r:
                        text = await r.text()
                        if r.status in (400, 401, 403):
                            raise PanelError(f"ورود ناموفق (HTTP {r.status}): نام‌کاربری/رمز اشتباه است. {text[:160]}")
                        if r.status >= 400:
                            raise PanelError(f"ورود ناموفق: HTTP {r.status}: {text[:200]}")
                        data = _json.loads(text) if text else {}
                except PanelError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError, ValueError) as e:
                    await self._reset_session()
                    raise PanelError(f"اتصال به پنل برقرار نشد یا بیش از حد طول کشید: {str(e) or type(e).__name__}") from e
                self._token = str(data.get("token") or "")
                self._token_type = str(data.get("type") or "Bearer") or "Bearer"
                adm = data.get("admin") if isinstance(data.get("admin"), dict) else {}
                if not self.api_key and adm.get("api_key"):
                    self.api_key = str(adm["api_key"])
                if not self._token and not self.api_key:
                    raise PanelError("ورود موفق بود ولی توکنی برنگشت.")
                return True
            if not self.api_key:
                raise PanelError("نام‌کاربری/رمز یا API Key پنل خالی است.")
        res = await self._request("GET", "/api/admins/current", _retry=False)
        if res is None:
            raise PanelError("ورود ناموفق: API Key اشتباه است.")
        return True

    async def _reset_session(self):
        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (بستنِ نشستِ خراب)")
        self._session = None

    async def _request(self, method: str, path: str, *, _retry: bool = True, **kwargs):
        """JSON برمی‌گرداند؛ ۴۰۴ ⇒ None؛ بقیهٔ خطاها ⇒ PanelError."""
        if not self._token and not self.api_key:
            await self.login()
        t0 = time.monotonic()
        try:
            s = await self._ensure_session()
            headers = {**self._auth_headers(), **(kwargs.pop("headers", None) or {})}
            try:
                async with s.request(method, f"{self.base_url}{path}", headers=headers, **kwargs) as r:
                    text = await r.text()
                    status = r.status
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as e:
                await self._reset_session()
                raise PanelError(f"{method} {path}: اتصال به پنل برقرار نشد یا بیش از حد طول کشید: "
                                 f"{str(e) or type(e).__name__}") from e
            if status in (401, 403) and _retry and self.username and self.password:
                self._token = ""
                await self.login(force=True)
                return await self._request(method, path, _retry=False, **kwargs)
            if status == 404:
                return None
            try:
                data = _json.loads(text) if text else {}
            except ValueError:
                data = {"raw": text}
            if status in (401, 403):
                raise PanelError(f"{method} {path}: دسترسی رد شد (HTTP {status}): {_msg_of(data)}")
            if status == 429:
                raise PanelError(f"{method} {path} HTTP 429: درخواستِ زیاد؛ کمی بعد دوباره امتحان کن.")
            if status >= 400:
                raise PanelError(f"{method} {path} HTTP {status}: {_msg_of(data)}")
            return data
        finally:
            d = time.monotonic() - t0
            if d >= 2.0:
                logger.warning("پنلِ کند: %s %s | %.1fs | %s", method, path, d, self.base_url)

    async def close(self):
        await self._reset_session()

    async def __aenter__(self):
        await self.login()
        return self

    async def __aexit__(self, *a):
        await self.close()

    # ───────────────────────── سرویس‌ها (به‌جایِ inbound) ─────────────────────────
    async def list_inbounds(self):
        out, page = [], 1
        while page <= 20:
            res = await self._request("GET", "/api/services", params={"page": page, "limit": 100})
            items = (res or {}).get("items") or []
            for it in items:
                if isinstance(it, dict) and it.get("id") is not None:
                    out.append({"id": it["id"], "remark": it.get("name") or f"service {it['id']}", "protocol": "service"})
            if len(items) < 100:
                break
            page += 1
        return out

    async def active_inbound_ids(self):
        return [i["id"] for i in await self.list_inbounds()]

    async def _services_for_new(self) -> list[int]:
        return list(self.service_ids) or [int(i) for i in await self.active_inbound_ids()]

    # ───────────────────────── خواندنِ کاربر ─────────────────────────
    async def _find(self, username: str) -> dict | None:
        username = (username or "").strip()
        res = await self._request("GET", "/api/subscriptions", params={"usernames": username, "limit": 20})
        for it in (res or {}).get("items") or []:
            if isinstance(it, dict) and str(it.get("username") or "").lower() == username.lower():
                return it
        return None

    def _abs_link(self, link: str) -> str:
        link = (link or "").strip()
        if not link:
            return ""
        if link.startswith(("http://", "https://")):
            if not self.sub_base:
                return link
            from urllib.parse import urlsplit
            p = urlsplit(link)
            path = (p.path or "") + (f"?{p.query}" if p.query else "")
            return f"{self.sub_base}/{path.lstrip('/')}" if path.strip("/") else link
        return f"{(self.sub_base or self.base_url).rstrip('/')}/{link.lstrip('/')}"

    @staticmethod
    def _token_of(link: str) -> str:
        return (link or "").rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]

    async def get_client(self, email):
        sub = await self._find(email)
        if not sub:
            return None
        out = dict(sub)
        link = self._abs_link(str(sub.get("subscription_link") or ""))
        out["sub_link"] = link
        out["subId"] = self._token_of(link) or str(sub.get("access_key") or "")
        out["id"] = sub.get("id")
        return out

    async def get_usage(self, email):
        sub = await self._find(email)
        if not sub:
            return None
        res = {
            "up": 0, "down": _used(sub), "total": int(sub.get("limit_usage") or 0),
            "expiryTime": parse_ms(sub.get("expired_at")),
            "enable": bool(sub.get("enabled", True)),
        }
        lo = parse_ms(sub.get("last_online_at"))
        if lo:
            res["last_online_ms"] = lo
        return res

    async def get_client_links(self, email):
        res = await self._request("GET", f"/api/subscriptions/{(email or '').strip()}/links")
        return [str(i.get("link")) for i in (res or {}).get("links") or [] if isinstance(i, dict) and i.get("link")]

    async def get_sub_links(self, sub_id):
        return []

    def sub_link(self, sub_id):
        return ""        # لینکِ ساب را خودِ پنل می‌دهد (get_client → sub_link)

    # ───────────────────────── نوشتن ─────────────────────────
    async def _update(self, username: str, **fields) -> dict:
        res = await self._request("PUT", "/api/subscriptions", json={"usernames": [username], **fields})
        if res is None:
            raise PanelError("کاربر روی پنل پیدا نشد.")
        item = res[0] if isinstance(res, list) and res else (res if isinstance(res, dict) else {})
        return item if isinstance(item, dict) else {}

    def _fmt_order(self, first: str | None = None) -> list[str]:
        cached = _EXPIRE_FMT.get(self.base_url)
        head = first or cached
        return [head] + [f for f in _FMT_ORDER if f != head] if head else list(_FMT_ORDER)

    async def _confirm_expiry(self, username: str, target_ms: int, first: dict | None, first_fmt: str | None = None) -> dict:
        """تاریخِ انقضا را روی target_ms می‌نشاند و با expired_aِ پنل تأیید می‌کند؛ فرمتِ درستِ limit_expire را پیدا می‌کند.
        first = پاسخِ نوشتنِ قبلی با فرمتِ first_fmt (اگر داده شود همان اول سنجیده می‌شود). فرمتِ غلط ممکن است خطایِ
        ۴۰۰/۵۰۰ هم بدهد؛ آن هم فقط یعنی «فرمتِ بعدی»."""
        now_ms = int(time.time() * 1000)
        last_err: Exception | None = None
        for n, fmt in enumerate(self._fmt_order(first_fmt)):
            if n == 0 and first is not None:
                last = first
            else:
                try:
                    last = await self._update(username, limit_expire=_encode_expire(fmt, target_ms, now_ms))
                except PanelError as e:
                    last_err = e
                    continue
            got = parse_ms(last.get("expired_at"))
            if got:
                if abs(got - target_ms) <= _TOLERANCE_MS:
                    _EXPIRE_FMT[self.base_url] = fmt
                    return last
                continue
            # تاریخی برنگشت: اگر پنل همان عددِ ارسالی را نگه داشته، (بدونِ تأیید) بپذیر
            if int(last.get("limit_expire") or 0) == _encode_expire(fmt, target_ms, now_ms):
                logger.warning("solidlayer: انقضایِ %s بدونِ expired_at پذیرفته شد (فرمت %s، تأییدنشده)", username, fmt)
                return last
        raise PanelError("واحدِ زمانِ limit_expire در این پنل تشخیص داده نشد (ثانیه/میلی‌ثانیه/روز هیچ‌کدام تاریخِ درست نداد"
                         + (f"؛ آخرین خطا: {last_err}" if last_err else "") + "). "
                         "نمونهٔ پاسخِ GET /api/subscriptions را برایِ پشتیبانی بفرست.")

    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None, limit_ip=0,
                             inbound_ids=None, mb=None, hours=0, on_hold=False):
        username = str(email).strip()
        if mb is not None:
            limit = int(int(mb) * 1024 * 1024)
        else:
            limit = int(float(gb) * GB) if gb else 0
        now_ms = int(time.time() * 1000)
        total_secs = int(days or 0) * 86400 + int(hours or 0) * 3600
        target_ms = now_ms + total_secs * 1000 if total_secs else 0
        services = [int(i) for i in (inbound_ids or []) if str(i).isdigit()] or await self._services_for_new()
        body: dict = {"username": username, "note": "botsaz"}
        if limit > 0:
            body["limit_usage"] = limit
        if services:
            body["services"] = services
        sub, used_fmt, last_err = None, None, None
        for fmt in (self._fmt_order() if target_ms else [None]):
            b2 = dict(body)
            if fmt:
                b2["limit_expire"] = _encode_expire(fmt, target_ms, now_ms)
            try:
                res = await self._request("POST", "/api/subscriptions", json=[b2])
            except PanelError as e:
                last_err = e                       # فرمتِ غلط می‌تواند ۴۰۰/۵۰۰ بدهد؛ اگر اکانت ساخته نشده بود فرمتِ بعدی
                try:
                    existing = await self._find(username)
                except PanelError:
                    existing = None
                if existing:
                    sub, used_fmt = existing, fmt
                    break
                continue
            if res is None:
                raise PanelError("ساخت کانفیگ ناموفق بود.")
            sub = res[0] if isinstance(res, list) and res else (res if isinstance(res, dict) else {})
            if not isinstance(sub, dict) or not sub.get("username"):
                raise PanelError(f"ساخت کانفیگ ناموفق: پاسخِ نامعتبر از پنل: {_msg_of(res)}")
            used_fmt = fmt
            break
        if sub is None:
            raise last_err or PanelError("ساخت کانفیگ ناموفق بود.")
        if target_ms:
            try:
                sub = await self._confirm_expiry(username, target_ms, sub, used_fmt)
            except PanelError:
                try:                                           # اکانتِ نادرست نماند
                    await self.delete_account(username)
                except Exception:
                    logger.exception("solidlayer: حذفِ اکانتِ ناموفق هم شکست خورد (%s)", username)
                raise
        link = self._abs_link(str(sub.get("subscription_link") or ""))
        if not link:                                           # برخی پنل‌ها لینک را فقط در GET می‌دهند
            cur = await self._find(username)
            link = self._abs_link(str((cur or {}).get("subscription_link") or ""))
        return {
            "uuid": uuid_str or gen_uuid(), "sub_id": self._token_of(link) or sub_id or gen_sub_id(),
            "email": username, "gb": gb, "days": days, "inbounds": services, "sub_link": link,
            "expiry_ms": parse_ms(sub.get("expired_at")) or target_ms,
        }

    async def add_traffic(self, email, gb):
        sub = await self._find(email)
        if not sub:
            raise PanelError("کاربر روی پنل پیدا نشد.")
        cur = int(sub.get("limit_usage") or 0)
        if cur <= 0:                       # نامحدود؛ افزایشِ حجم بی‌معنی است (مثلِ مرزبان بی‌خطا رد می‌شود)
            return sub
        return await self._update(sub["username"], limit_usage=cur + int(gb) * GB)

    async def renew(self, email, days, reset_usage=False):
        sub = await self._find(email)
        if not sub:
            raise PanelError("کاربر روی پنل پیدا نشد.")
        cur_ms = parse_ms(sub.get("expired_at"))
        if not cur_ms and not int(sub.get("limit_expire") or 0):
            out = sub                                  # زمانِ نامحدود: دست نمی‌زنیم (مثلِ مرزبان)
        else:
            now_ms = int(time.time() * 1000)
            base = cur_ms if cur_ms > now_ms else now_ms
            out = await self._confirm_expiry(sub["username"], base + int(days) * 86_400_000, None)
        if not sub.get("enabled", True) or str(out.get("status") or "").lower() in ("expired", "limited"):
            await self.set_enabled(sub["username"], True)
        if reset_usage:
            await self.reset_usage(sub["username"])
        return out

    async def reset_usage(self, email):
        res = await self._request("POST", "/api/subscriptions/reset", json={"usernames": [str(email).strip()]})
        return res is not None

    async def reset_sub(self, email, new_sub_id=None):
        res = await self._request("POST", "/api/subscriptions/revoke", json={"usernames": [str(email).strip()]})
        item = res[0] if isinstance(res, list) and res else {}
        link = self._abs_link(str((item or {}).get("subscription_link") or ""))
        if not link:
            cur = await self._find(email)
            link = self._abs_link(str((cur or {}).get("subscription_link") or ""))
        return link

    async def rename_account(self, old_email: str, new_email: str):
        raise PanelError("در این پنل نام‌کاربری شناسهٔ یکتای اکانت است و قابل تغییر نیست.")

    async def set_enabled(self, email, enabled: bool):
        path = "/api/subscriptions/enable" if enabled else "/api/subscriptions/disable"
        res = await self._request("POST", path, json={"usernames": [str(email).strip()]})
        if res is None:
            raise PanelError("تغییر وضعیت فعال/غیرفعال ناموفق: کاربر پیدا نشد.")
        return True

    async def delete_account(self, email, keep_traffic=False):
        res = await self._request("DELETE", "/api/subscriptions", json={"usernames": [str(email).strip()]})
        return res is not None

    # ───────────────────────── آمار/فهرست ─────────────────────────
    async def list_clients(self):
        out, page = [], 1
        while page <= _MAX_PAGES:
            res = await self._request("GET", "/api/subscriptions", params={"page": page, "limit": _PAGE})
            items = (res or {}).get("items") or []
            for u in items:
                if not isinstance(u, dict):
                    continue
                lim = int(u.get("limit_usage") or 0)
                link = self._abs_link(str(u.get("subscription_link") or ""))
                out.append({
                    "email": u.get("username"),
                    "traffic": {"up": 0, "down": _used(u), "total": lim},
                    "totalGB": lim,
                    "expiryTime": parse_ms(u.get("expired_at")),
                    # PATCH_PANEL_YAR_IDLE_V1
                    "last_online_ms": parse_ms(u.get("last_online_at")),
                    "created_ms": parse_ms(u.get("created_at")),
                    "status": "active" if u.get("enabled", True) else "disabled",
                    "sub_id": self._token_of(link) or None,
                })
            if len(items) < _PAGE:
                break
            page += 1
        return out

    async def test_connection(self):
        await self.login()
        adm = await self._request("GET", "/api/admins/current") or {}
        return {"ok": True, "inbounds": await self.active_inbound_ids() if adm else [],
                "admin": adm.get("username")}


def usage_summary(usage: dict) -> str:
    if not usage:
        return "نامشخص"
    used = int(usage.get("up", 0) or 0) + int(usage.get("down", 0) or 0)
    cap = int(usage.get("total", 0) or 0)
    used_gb = round(used / GB, 2)
    if cap:
        return f"{used_gb} از {round(cap / GB, 2)} گیگ مصرف شده (باقی‌مانده {round(max(cap - used, 0) / GB, 2)} گیگ)"
    return f"{used_gb} گیگ مصرف شده (حجم نامحدود)"
