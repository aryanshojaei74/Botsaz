"""
wgres_panel.py — کلاینتِ async «پنلِ وایرگاردِ نماینده (Reseller API)» — PATCH_WGRES_PANEL_V1.

پنلی با داشبوردِ وایرگارد (peer-محور: peerName/peerIp/wg0.conf) و «API نماینده». احراز: فقط هدرِ «X-API-Key»؛ پنل
خودش اینترفیسِ شبکه و سقفِ حجمِ نماینده را اعمال می‌کند و نماینده فقط به peerهای خودش دسترسی دارد.
مثلِ ایلان: لاگین ندارد؛ در مراحلِ اتصال یوزرنیم «-» و کلیدِ API در فیلدِ «رمز» ذخیره می‌شود.

مسیرها (همان‌هایی که مستندِ خودِ پنل می‌گوید):
  GET  /api/developer/reseller-info          {success, username, interface, capacity_gb|"unlimited", used_gb, remaining_gb, active_peers_count}
  GET  /api/peers?page=&limit=               {peers:[{peer_name, peer_ip, config, limit:"50GiB", used, used_human, limit_human,
                                                       remaining_human, remaining_time(دقیقه), first_usage, short_link,
                                                       monitor_blocked, expiry_blocked, status}], total_pages}
  POST /api/create-peer   {peerName, dataLimit:"30GiB", expiryDays, firstUsage}
  POST /api/edit-peer     {peerName, dataLimit, expiryDays}     (expiryDays = مدتِ باقی‌مانده از «الان»؛ فرمِ ویرایشِ خودِ پنل هم با باقی‌مانده پر می‌شود)
  POST /api/reset-traffic {peerName}        POST /api/toggle-peer {peerName, blocked}
  POST /api/delete-peer   {peerName}        POST /api/delete-all  (پاکسازیِ منقضی‌ها — اینجا استفاده نمی‌شود)
  GET  /api/export-peer?peerName=&config=   متنِ کانفیگ (.conf) — همان که داشبورد برایِ QR می‌گیرد

نکته‌ها:
  • نامِ peer برایِ نامِ تونل در اپِ وایرگارد هم استفاده می‌شود (≤۱۵ نویسه)؛ پس مثلِ ایلان یک نامِ کوتاهِ قطعی (هش) می‌سازیم.
  • پاسخِ دقیقِ create-peer مستند نیست؛ بعد از ساخت، peer از لیست خوانده می‌شود (نه از بدنهٔ پاسخ).
  • «inbound» ندارد؛ مفهومِ انتخابِ اینباند اینجا بی‌معنی است.
"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import re
import string
import time

try:
    import aiohttp
except ImportError:      # pragma: no cover
    aiohttp = None

from tenant.shared.panel_clients.vpn_panel import PanelError

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

__all__ = ["WgResellerPanelClient"]

GB = 1024 ** 3
MB = 1024 ** 2
_MAX_PAGES = 40
_PAGE = 100
_UNITS = {"TIB": 1024 ** 4, "TB": 1024 ** 4, "GIB": GB, "GB": GB, "MIB": MB, "MB": MB, "KIB": 1024, "KB": 1024, "B": 1}


def _short_name(email: str) -> str:
    """نامِ کوتاهِ قطعی (≤۱۰ نویسه) از رویِ نامِ بلندِ ربات؛ قطعی بودن یعنی همهٔ عملیاتِ بعدی بدونِ ذخیرهٔ اضافه همان را پیدا می‌کنند."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(email or ""))
    if 3 <= len(cleaned) <= 10:
        return cleaned
    n = int.from_bytes(hashlib.sha1(str(email).encode("utf-8")).digest()[:6], "big")
    a = string.ascii_lowercase + string.digits
    out = ""
    for _ in range(9):
        n, r = divmod(n, len(a))
        out += a[r]
    return "w" + out


def parse_size_bytes(v) -> int:
    """«50GiB» / «500MiB» / «1.5 GB» / عددِ خام ⇒ بایت. «unlimited»/خالی/۰ ⇒ ۰ (نامحدود)."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return max(0, int(v))
    s = str(v).strip().replace(",", "")
    if not s or s.lower() in ("unlimited", "none", "null", "-", "∞", "نامحدود"):
        return 0
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([A-Za-z]*)$", s)
    if not m:
        return 0
    unit = m.group(2).upper()
    if not unit:
        return int(float(m.group(1)))
    return int(float(m.group(1)) * _UNITS.get(unit, 1))


def _fmt_limit(gb: float | None = None, mb: int | None = None) -> str:
    if mb:
        return f"{int(mb)}MiB"
    return f"{float(gb or 0):g}GiB"


class WgResellerPanelClient:
    """امضایِ سازنده مثلِ بقیهٔ کلاینت‌ها؛ password = کلیدِ API (X-API-Key)."""

    def __init__(self, base_url: str, username: str, password: str,
                 sub_base: str = "", inbound_ids=None, verify_ssl: bool = False,
                 timeout: int = 12, api_key: str | None = None):
        if aiohttp is None:
            raise PanelError("کتابخانهٔ aiohttp نصب نیست.")
        base_url = (base_url or "").split("#", 1)[0].strip().rstrip("/")
        base_url = re.sub(r"/(api|docs|dashboard)(/.*)?$", "", base_url, flags=re.IGNORECASE)
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or password or "").strip()
        self.username = username
        self.password = password
        self.sub_base = ""
        self.inbound_ids = []
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = None
        self._peers_cache: tuple[float, list] | None = None
        self._cfg_cache: dict[str, str] = {}

    # ───────────── HTTP ─────────────
    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            t = aiohttp.ClientTimeout(total=self.timeout, sock_connect=6)
            self._session = aiohttp.ClientSession(timeout=t, connector=aiohttp.TCPConnector(ssl=self.verify_ssl))
        return self._session

    async def login(self, force=False):
        if not self.api_key:
            raise PanelError("کلیدِ API پنلِ وایرگارد ثبت نشده است.")
        return True

    async def _request(self, method, path, *, raw=False, **kwargs):
        s = await self._ensure_session()
        await self.login()
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        if method.upper() != "GET":
            headers["Content-Type"] = "application/json"
        url = f"{self.base_url}{path}"
        for attempt in range(2):
            try:
                async with s.request(method, url, headers=headers, **kwargs) as r:
                    text = await r.text()
                    if r.status in (401, 403):
                        raise PanelError(
                            f"کلیدِ API پنلِ وایرگارد نامعتبر است یا اجازهٔ این کار را ندارد (HTTP {r.status}). "
                            "کلید را از پنل (بخش «API توسعه‌دهنده») دوباره کپی کن.")
                    if raw:
                        if r.status >= 400:
                            raise PanelError(f"پنلِ وایرگارد: {method} {path} ناموفق (HTTP {r.status}): {text[:200]}")
                        return text
                    data = None
                    if text:
                        try:
                            data = _json.loads(text)
                        except ValueError:
                            data = {"raw": text[:300]}
                    if r.status == 404:
                        return None
                    err = data.get("error") if isinstance(data, dict) else None
                    if r.status >= 400 or (isinstance(data, dict) and (data.get("success") is False or err)):
                        msg = (data or {}).get("error") or (data or {}).get("message") or text[:300] if isinstance(data, dict) else text[:300]
                        raise PanelError(f"پنلِ وایرگارد: {method} {path} ناموفق (HTTP {r.status}): {msg}")
                    return data if data is not None else {}
            except PanelError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as e:
                if attempt == 0:
                    continue
                try:
                    if self._session and not self._session.closed:
                        await self._session.close()
                except Exception:      # noqa: BLE001
                    logger.exception("wgres: بستنِ نشستِ خراب ناموفق بود")
                self._session = None
                raise PanelError(
                    f"اتصال به پنلِ وایرگارد برقرار نشد یا بیش از حد طول کشید: {str(e) or type(e).__name__} "
                    f"(آدرس/پورتِ پنل و فایروال را بررسی کن؛ timeout={self.timeout}s)") from e
        raise PanelError(f"{method} {path} پس از تلاشِ مجدد ناموفق ماند")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self):
        await self.login()
        return self

    async def __aexit__(self, *a):
        await self.close()

    # ───────────── اینباند (مفهومی ندارد) ─────────────
    async def list_inbounds(self):
        return []

    async def active_inbound_ids(self):
        return []

    # ───────────── peerها ─────────────
    async def _peers(self, force=False) -> list:
        now = time.monotonic()
        if not force and self._peers_cache and now - self._peers_cache[0] < 30:
            return self._peers_cache[1]
        out: list = []
        for page in range(1, _MAX_PAGES + 1):
            res = await self._request("GET", f"/api/peers?page={page}&limit={_PAGE}") or {}
            batch = res.get("peers") if isinstance(res, dict) else res
            batch = batch if isinstance(batch, list) else []
            out.extend(batch)
            total = int(res.get("total_pages") or 0) if isinstance(res, dict) else 0
            if not batch or (total and page >= total) or (not total and len(batch) < _PAGE):
                break
        self._peers_cache = (now, out)
        return out

    async def _find(self, email, force=False) -> dict | None:
        name = _short_name(email)
        for p in await self._peers(force=force):
            pn = str(p.get("peer_name") or p.get("peerName") or "")
            if pn == name or pn == str(email):
                return p
        return None

    @staticmethod
    def _expire_epoch(p: dict) -> int:
        rt = p.get("remaining_time")
        if rt is None or rt == "":
            return 0
        try:
            minutes = int(float(rt))
        except (TypeError, ValueError):
            return 0
        if minutes <= 0:
            return int(time.time()) - 1           # منقضی
        return int(time.time()) + minutes * 60

    @staticmethod
    def _used_bytes(p: dict) -> int:
        u = p.get("used")
        if isinstance(u, (int, float)):
            return int(u)
        if isinstance(u, str) and u.strip().isdigit():
            return int(u.strip())
        return parse_size_bytes(p.get("used_human") or u)

    @staticmethod
    def _blocked(p: dict) -> bool:
        return bool(p.get("monitor_blocked") in (1, True, "1", "true") or p.get("expiry_blocked") in (1, True, "1", "true"))

    def _norm(self, p: dict, email) -> dict:
        u = dict(p)
        name = str(p.get("peer_name") or _short_name(email))
        u["username"] = u["subId"] = u["id"] = name
        u["email"] = name
        u["data_limit"] = parse_size_bytes(p.get("limit") or p.get("limit_human"))
        u["expire"] = self._expire_epoch(p)
        u["used_traffic"] = self._used_bytes(p)
        u["sub_link"] = p.get("short_link") or ""
        u["status"] = "disabled" if self._blocked(p) or p.get("status") == "inactive" else "active"
        return u

    async def get_client(self, email):
        p = await self._find(email, force=True)
        return self._norm(p, email) if p else None

    async def get_usage(self, email):
        cur = await self.get_client(email)
        if not cur:
            return None
        return {"up": 0, "down": int(cur["used_traffic"]), "total": int(cur["data_limit"]),
                "expiryTime": int(cur["expire"]) * 1000, "enable": cur["status"] == "active"}

    async def list_clients(self):
        out = []
        for p in await self._peers(force=True):
            u = self._norm(p, p.get("peer_name"))
            out.append({"email": u["username"], "username": u["username"],
                        "traffic": {"up": 0, "down": u["used_traffic"], "total": u["data_limit"]},
                        "totalGB": u["data_limit"], "expiryTime": u["expire"] * 1000,
                        "enable": u["status"] == "active"})
        return out

    # ───────────── لینک‌ها / کانفیگ ─────────────
    def sub_link(self, sub_id):
        return ""

    async def get_sub_links(self, sub_id):
        return []

    async def get_client_links(self, email):
        return []

    async def _config_text(self, email) -> tuple[str, str]:
        """(نامِ فایل, متنِ .conf). از export-peer می‌خواند؛ اگر نشد خالی."""
        p = await self._find(email)
        if not p:
            return "", ""
        name = str(p.get("peer_name"))
        cfg = str(p.get("config") or "wg0.conf")
        key = f"{name}|{cfg}"
        if key not in self._cfg_cache:
            from urllib.parse import quote
            try:
                text = await self._request("GET", f"/api/export-peer?peerName={quote(name)}&config={quote(cfg)}", raw=True)
            except PanelError:
                logger.info("wgres: export-peer برایِ %s ناموفق بود", name)
                text = ""
            self._cfg_cache[key] = text if "[Interface]" in (text or "") else ""
        return f"{name}.conf", self._cfg_cache[key]

    async def get_wireguard_endpoints(self, username: str) -> list[dict]:
        fname, text = await self._config_text(username)
        return [{"id": 1, "name": "WireGuard", "file": fname}] if text else []

    async def fetch_wireguard_config_text(self, username: str, host_id=None, *, path: str | None = None) -> str:
        return (await self._config_text(username))[1]

    # ───────────── ساخت/ویرایش ─────────────
    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0, on_hold=False):
        name = _short_name(email)
        total_days = int(days or 0) + (1 if int(hours or 0) > 0 else 0)
        body = {"peerName": name, "dataLimit": _fmt_limit(gb, mb), "expiryDays": total_days,
                "firstUsage": bool(on_hold)}
        res = await self._request("POST", "/api/create-peer", json=body)
        if res is None:
            raise PanelError("ساختِ کاربر رویِ پنلِ وایرگارد ناموفق بود (مسیرِ /api/create-peer پیدا نشد).")
        self._peers_cache = None
        p = await self._find(email, force=True)
        sub = (p or {}).get("short_link") or ""
        if not sub and isinstance(res, dict):
            sub = res.get("short_link") or (res.get("peer") or {}).get("short_link") or ""
        exp = int(time.time()) + total_days * 86400 if total_days and not on_hold else 0
        return {"uuid": uuid_str or name, "sub_id": sub_id or name, "email": name, "gb": gb, "days": days,
                "inbounds": [], "sub_link": sub, "expiry_ms": exp * 1000}

    async def _edit(self, email, **fields):
        name = _short_name(email)
        res = await self._request("POST", "/api/edit-peer", json={"peerName": name, **fields})
        if res is None:
            raise PanelError("کاربر رویِ پنلِ وایرگارد پیدا نشد.")
        self._peers_cache = None
        return res

    async def add_traffic(self, email, gb):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر رویِ پنلِ وایرگارد پیدا نشد.")
        cap = int(cur["data_limit"])
        if cap == 0:                 # نامحدود
            return cur
        new_gb = (cap + float(gb) * GB) / GB
        fields = {"dataLimit": _fmt_limit(new_gb)}
        rem = int(cur["expire"]) - int(time.time())
        if rem > 0:                  # ویرایش بدونِ expiryDays ممکن است انقضا را عوض کند؛ باقی‌مانده را حفظ می‌کنیم
            fields["expiryDays"] = max(1, -(-rem // 86400))
        return await self._edit(email, **fields)

    async def renew(self, email, days, reset_usage=False):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر رویِ پنلِ وایرگارد پیدا نشد.")
        now = int(time.time())
        exp = int(cur["expire"])
        remaining_days = max(0, -(-(exp - now) // 86400)) if exp > now else 0
        fields = {"expiryDays": remaining_days + int(days)}
        if int(cur["data_limit"]) > 0:
            fields["dataLimit"] = _fmt_limit(cur["data_limit"] / GB)
        res = await self._edit(email, **fields)
        if reset_usage:
            await self.reset_usage(email)
        return res

    async def reset_usage(self, email):
        res = await self._request("POST", "/api/reset-traffic", json={"peerName": _short_name(email)})
        self._peers_cache = None
        return res is not None

    async def reset_sub(self, email, new_sub_id=None):
        raise PanelError("این پنل ریستِ لینک ندارد؛ کانفیگ را دوباره ارسال کن.")

    async def rename_account(self, old_email: str, new_email: str):
        return False

    async def set_enabled(self, email, enabled: bool):
        res = await self._request("POST", "/api/toggle-peer", json={"peerName": _short_name(email), "blocked": not enabled})
        if res is None:
            raise PanelError("کاربر رویِ پنلِ وایرگارد پیدا نشد.")
        self._peers_cache = None
        return True

    async def set_ip_limit(self, email, limit: int):
        return False

    async def delete_account(self, email, keep_traffic=False):
        res = await self._request("POST", "/api/delete-peer", json={"peerName": _short_name(email)})
        self._peers_cache = None
        return res is not None

    async def test_connection(self):
        res = await self._request("GET", "/api/developer/reseller-info")
        if res is None:
            raise PanelError("آدرسِ پنل درست نیست (/api/developer/reseller-info پیدا نشد).")
        if isinstance(res, dict) and res.get("success") is False:
            raise PanelError(str(res.get("error") or res.get("message") or "کلیدِ API پذیرفته نشد."))
        cap = res.get("capacity_gb")
        return {"ok": True, "inbounds": [], "username": res.get("username"), "interface": res.get("interface"),
                "capacity_gb": cap, "used_gb": res.get("used_gb"), "remaining_gb": res.get("remaining_gb"),
                "peers": res.get("active_peers_count")}
