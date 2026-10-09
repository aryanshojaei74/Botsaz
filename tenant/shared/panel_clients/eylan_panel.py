"""
eylan_panel.py — کلاینتِ async پنلِ «ایلان» (EylanPanel — OpenVPN/L2TP/Cisco/WireGuard/sing-box)
=== PATCH_EYLAN_PANEL_V1 ===

همان قراردادِ متدیِ PanelClient (vpn_panel.py) را پیاده می‌کند تا بقیهٔ کد فقط با
vpn_panel_factory از آن استفاده کند. منبع: swagger.json ِ EylanPanel.top (EylanPanel API 1.0.0).

احرازِ هویت: فقط هدرِ «X-API-KEY» (کلیدِ ادمینِ اصلی یا نماینده). لاگین ندارد؛ در مراحلِ
اتصالِ پنل یوزرنیم «-» است و کلیدِ API در فیلدِ «رمز» ذخیره می‌شود (مثلِ پنلِ شاهان).

مسیرهایِ استفاده‌شده:
  GET    /api/v1/status                          سلامت (بدونِ کلید)
  GET    /api/v1/users/stats                     تستِ کلید + allowed_resources (نود/وایرگارد/سینگ‌باکس)
  GET    /api/v1/users/list_all                  همهٔ کاربران (مصرف/انقضا)
  POST   /api/v1/users                           ساختِ کاربر → created_users[0] نامِ نهایی
  GET    /api/v1/users/{u}                       جزئیات/مصرف
  PUT    /api/v1/users/{u}                       ویرایش (حجم/انقضا/is_disabled/...)
  DELETE /api/v1/users/{u}                       حذف
  POST   /api/v1/users/{u}/reset_traffic         صفرکردنِ مصرف
  POST   /api/v1/users/{u}/revoke                ساب‌لینکِ تازه (new_subscription_url)
  GET    /api/v1/users/{u}/sub                   ساب‌لینک (sub_url)
  GET    /api/v1/users/{u}/all_ovpn_links        لینک‌هایِ share ِ سینگ‌باکس/کانفیگ‌ها

نکته‌هایِ اسپک:
  • یوزرنیم: حروفِ انگلیسی، عدد، «_» و «-»، حداکثر ۸۰. با کلیدِ نماینده اگر نام تکراری باشد
    پنل خودش عددی به آخرش اضافه می‌کند و نامِ نهایی را در created_users برمی‌گرداند؛ همان را
    به‌عنوانِ email برمی‌گردانیم تا بقیهٔ عملیات رویِ نامِ درست انجام شود.
  • data_limit عددِ اعشاری با data_limit_unit (GB/MB)؛ ۰/خالی = نامحدود.
  • انقضا: expiry_days (ساخت) یا expiry_date_str «YYYY-MM-DD» (ویرایش)؛ «شروع از اولین اتصال»
    = activation_type=flexible_days + pending_activation_days.
  • نمونهٔ پاسخِ GET /users/{u} در اسپک فیلدِ حجم/انقضا ندارد؛ برایِ همین چند نامِ رایج خوانده
    می‌شود و اگر نبود، از list_all تکمیل می‌شود.
  • «اینباند»ها در این پنل: پروتکل‌ها. inbound_ids (با کاما) می‌تواند شامل این‌ها باشد:
    openvpn, l2tp, cisco, wg1, نامِ وایرگاردهایِ اضافه (wg2…)، تگِ اینباندهایِ سینگ‌باکس،
    و node:<id> برایِ محدودکردنِ نود. خالی = OpenVPN + همهٔ وایرگارد/سینگ‌باکس‌هایِ مجازِ کلید.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import re
import secrets
import string
import time

try:
    import aiohttp
except ImportError:      # pragma: no cover
    aiohttp = None

from tenant.shared.panel_clients.vpn_panel import PanelError

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

GB = 1024 * 1024 * 1024
MB = 1024 * 1024
_BASIC = ("openvpn", "l2tp", "cisco", "wg1")


def _rand(n=8):
    a = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(a) for _ in range(n))


def _safe_username(email: str) -> str:
    """حروفِ انگلیسی/عدد/«_»/«-»، ۳ تا ۸۰ کاراکتر."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", str(email or ""))
    cleaned = re.sub(r"_+", "_", cleaned).strip("_-")[:80]
    if len(cleaned) < 3:
        cleaned = (cleaned + "_usr")[:80]
    return cleaned


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(v) -> int:
    """تاریخ/زمانِ انقضا → epoch ثانیه (۰ = ندارد)."""
    if v in (None, "", 0, "0"):
        return 0
    if isinstance(v, (int, float)):
        x = int(v)
        return x // 1000 if x > 10**11 else x
    s = str(v).strip()
    if s.isdigit():
        return _parse_date(int(s))
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = _dt.datetime.strptime(s.replace("Z", "")[:26], fmt)
            if fmt == "%Y-%m-%d":   # پایانِ همان روز
                d = d.replace(hour=23, minute=59, second=59)
            return int(d.replace(tzinfo=_dt.timezone.utc).timestamp())
        except ValueError:
            continue
    return 0


class EylanPanelClient:
    """امضایِ سازنده مثلِ بقیهٔ کلاینت‌ها؛ password = کلیدِ API (X-API-KEY)."""

    def __init__(self, base_url: str, username: str, password: str,
                 sub_base: str = "", inbound_ids=None, verify_ssl: bool = False,
                 timeout: int = 10, api_key: str | None = None):
        if aiohttp is None:
            raise PanelError("کتابخانهٔ aiohttp نصب نیست.")
        base_url = (base_url or "").split("#", 1)[0].strip().rstrip("/")
        base_url = re.sub(r"/(api|docs|swagger)(/.*)?$", "", base_url, flags=re.IGNORECASE)
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or password or username or "").strip()
        self.username = username
        self.password = password
        self.sub_base = ""
        self.inbound_ids = [str(i).strip() for i in (inbound_ids or []) if str(i).strip()]
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = None
        self._list_cache: tuple[float, list] | None = None
        self._resources: dict | None = None

    # ───────────── HTTP ─────────────
    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            t = aiohttp.ClientTimeout(total=self.timeout, sock_connect=6)
            self._session = aiohttp.ClientSession(
                timeout=t, connector=aiohttp.TCPConnector(ssl=self.verify_ssl))
        return self._session

    async def login(self, force=False):
        """لاگین ندارد؛ فقط وجودِ کلید."""
        if not self.api_key:
            raise PanelError("کلیدِ API پنلِ ایلان ثبت نشده است.")
        return True

    async def _request(self, method, path, **kwargs):
        _t0 = time.monotonic()
        try:
            return await self._request_timed(method, path, **kwargs)
        finally:
            _d = time.monotonic() - _t0
            if _d >= 2.0:
                logger.warning("پنلِ کند: %s %s | %.1fs | %s", method, path, _d, self.base_url)

    async def _request_timed(self, method, path, *, auth=True, **kwargs):
        s = await self._ensure_session()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json")
        if auth:
            await self.login()
            headers["X-API-KEY"] = self.api_key
        url = f"{self.base_url}{path}"
        for attempt in range(2):
            try:
                async with s.request(method, url, headers=headers, **kwargs) as r:
                    text = await r.text()
                    if r.status == 404:
                        return None
                    if r.status in (401, 403):
                        raise PanelError(
                            f"کلیدِ API پنلِ ایلان نامعتبر است یا اجازهٔ این کار را ندارد (HTTP {r.status}). "
                            "کلید را از پنل (ادمینِ اصلی یا نماینده) دوباره کپی کن.")
                    data = None
                    if text:
                        try:
                            import json as _json
                            data = _json.loads(text)
                        except ValueError:
                            data = {"raw": text}
                    if r.status >= 400 or (isinstance(data, dict) and data.get("success") is False):
                        msg = (data or {}).get("message") or (data or {}).get("error") or text[:300] \
                            if isinstance(data, dict) else text[:300]
                        raise PanelError(f"پنلِ ایلان: {method} {path} ناموفق (HTTP {r.status}): {msg}")
                    return data if data is not None else {}
            except PanelError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as e:
                if attempt == 0:
                    continue
                try:
                    if self._session and not self._session.closed:
                        await self._session.close()
                except Exception:
                    logger.exception("eylan: بستنِ نشستِ خراب ناموفق بود")
                self._session = None
                raise PanelError(
                    f"اتصال به پنلِ ایلان برقرار نشد یا بیش از حد طول کشید: "
                    f"{str(e) or type(e).__name__} (timeout={self.timeout}s)") from e
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

    # ───────────── منابع/پروتکل‌ها ─────────────
    async def _allowed(self) -> dict:
        if self._resources is None:
            res = await self._request("GET", "/api/v1/users/stats") or {}
            self._resources = res.get("allowed_resources") or {}
        return self._resources

    async def list_inbounds(self):
        """«اینباندها» برایِ انتخاب در ربات: پروتکل‌هایِ پایه + وایرگارد/سینگ‌باکس/نودهایِ مجاز."""
        ar = await self._allowed()
        out = [{"id": "openvpn", "remark": "OpenVPN", "enable": True},
               {"id": "l2tp", "remark": "L2TP", "enable": True},
               {"id": "cisco", "remark": "Cisco (AnyConnect)", "enable": True}]
        for w in ar.get("wg_instances") or []:
            name = w.get("name") or ""
            if name:
                out.append({"id": name, "remark": f"WireGuard {w.get('display_name') or name}", "enable": True})
        for sb in ar.get("singbox_inbounds") or []:
            tag = sb.get("tag") or ""
            if tag:
                out.append({"id": tag, "remark": f"sing-box {sb.get('type', '')} {sb.get('display_name') or tag}",
                            "enable": True})
        for n in ar.get("nodes") or []:
            if n.get("id") is not None:
                out.append({"id": f"node:{n['id']}", "remark": f"نود {n.get('name') or n['id']}",
                            "enable": n.get("status", "online") == "online"})
        return out

    async def active_inbound_ids(self):
        if self.inbound_ids:
            return list(self.inbound_ids)
        # پیش‌فرض: OpenVPN + همهٔ WG/sing-box مجاز + همهٔ نودهایِ آنلاین («Node Access → Select All»
        # در پنل). بدونِ نود، کاربرِ ساخته‌شده به هیچ نودی دسترسی نداشت.
        return [i["id"] for i in await self.list_inbounds()
                if i["id"] not in ("l2tp", "cisco")
                and (not str(i["id"]).startswith("node:") or i.get("enable", True))]

    async def _protocol_fields(self, inbound_ids) -> dict:
        ids = [str(x).strip() for x in (inbound_ids or [])
               if str(x).strip() and str(x).strip() not in ("-", "همه", "all", "*")]
        if not ids:
            ids = await self.active_inbound_ids()
        ar = await self._allowed()
        wg_names = {w.get("name") for w in ar.get("wg_instances") or [] if w.get("name")}
        sb_tags = {s.get("tag") for s in ar.get("singbox_inbounds") or [] if s.get("tag")}
        nodes = [int(x.split(":", 1)[1]) for x in ids if x.startswith("node:") and x.split(":", 1)[1].isdigit()]
        extra_wg = [x for x in ids if x in wg_names and x != "wg1"]
        sb = [x for x in ids if x in sb_tags or (x not in _BASIC and x not in wg_names
                                                  and not x.startswith("node:"))]
        f = {"enable_openvpn": "openvpn" in ids, "enable_l2tp": "l2tp" in ids,
             "enable_cisco": "cisco" in ids, "enable_wg1": "wg1" in ids,
             "wg_instances": extra_wg, "enable_singbox": bool(sb), "singbox_inbounds": sb}
        if nodes:
            f["nodes"] = nodes
        return f

    # ───────────── خواندن ─────────────
    async def _list_all(self, force=False) -> list:
        now = time.monotonic()
        if not force and self._list_cache and now - self._list_cache[0] < 60:
            return self._list_cache[1]
        res = await self._request("GET", "/api/v1/users/list_all") or {}
        users = res.get("users") if isinstance(res, dict) else res
        users = users if isinstance(users, list) else []
        self._list_cache = (now, users)
        return users

    @staticmethod
    def _limit_bytes(u: dict) -> int:
        for k in ("data_limit_bytes", "total_limit_bytes"):
            if u.get(k) is not None:
                return int(_num(u[k]))
        v = _num(u.get("data_limit"))
        unit = str(u.get("data_limit_unit") or "GB").upper()
        return int(v * (MB if unit == "MB" else GB)) if v > 0 else 0

    @staticmethod
    def _used_bytes(u: dict) -> int:
        for k in ("total_traffic_bytes", "used_traffic_bytes", "traffic_bytes"):
            if u.get(k) is not None:
                return int(_num(u[k]))
        return int(_num(u.get("download_bytes")) + _num(u.get("upload_bytes")))

    @staticmethod
    def _expire_epoch(u: dict) -> int:
        for k in ("expiry_date", "expiry_date_str", "expire_date", "expires_at", "expiry", "expire_at",
                  "expiration_date"):
            if u.get(k):
                return _parse_date(u[k])
        return 0

    async def get_client(self, email):
        username = _safe_username(email)
        u = await self._request("GET", f"/api/v1/users/{username}")
        if not u:
            return None
        u = dict(u)
        has_limits = any(k in u for k in ("data_limit", "data_limit_bytes")) and \
            any(k in u for k in ("expiry_date", "expiry_date_str", "expire_date", "expires_at", "expiry"))
        if not has_limits:
            try:
                row = next((x for x in await self._list_all()
                            if str(x.get("username", "")).lower() == username.lower()), None)
                if row:
                    u = {**row, **u}
            except PanelError:
                logger.info("eylan: تکمیلِ اطلاعاتِ %s از list_all ناموفق بود", username)
        try:
            sub = await self._request("GET", f"/api/v1/users/{username}/sub") or {}
            u["sub_link"] = sub.get("sub_url") or ""
        except PanelError:
            u["sub_link"] = ""
        u["username"] = u.get("username") or username
        u["subId"] = u["id"] = u["username"]
        u["data_limit"] = self._limit_bytes(u)          # بایت، مثلِ خانوادهٔ مرزبان
        u["expire"] = self._expire_epoch(u)
        u["used_traffic"] = self._used_bytes(u)
        u["status"] = "active" if u.get("is_active", True) else "disabled"
        return u

    async def get_usage(self, email):
        cur = await self.get_client(email)
        if not cur:
            return None
        return {"up": 0, "down": int(cur["used_traffic"]), "total": int(cur["data_limit"]),
                "expiryTime": int(cur["expire"]) * 1000, "enable": cur["status"] == "active"}

    async def list_clients(self):
        out = []
        for u in await self._list_all(force=True):
            cap, used, exp = self._limit_bytes(u), self._used_bytes(u), self._expire_epoch(u)
            out.append({"email": u.get("username"), "username": u.get("username"),
                        "traffic": {"up": 0, "down": used, "total": cap},
                        "totalGB": cap, "expiryTime": exp * 1000,
                        "enable": bool(u.get("is_active", True))})
        return out

    # ───────────── لینک‌ها ─────────────
    def sub_link(self, sub_id):
        return ""

    async def get_sub_links(self, sub_id):
        return []

    async def get_client_links(self, email):
        username = _safe_username(email)
        res = await self._request("GET", f"/api/v1/users/{username}/all_ovpn_links") or {}
        links = []
        for ib in res.get("singbox_inbounds") or []:
            for srv in ib.get("servers") or []:
                if srv.get("share_link"):
                    links.append(str(srv["share_link"]))
        return links

    async def _wg_files(self, email) -> list[dict]:
        username = _safe_username(email)
        res = await self._request("GET", f"/api/v1/users/{username}/all_ovpn_links") or {}
        out = []
        for b in [res.get("wireguard_config") or {}] + list(res.get("wireguard_instances") or []):
            if not b or not b.get("enabled", True):
                continue
            for srv in b.get("servers") or []:
                if srv.get("full_config_text"):
                    out.append({"id": len(out) + 1, "name": srv.get("name") or "WireGuard",
                                "file": srv.get("filename") or f"{username}-{len(out) + 1}.conf",
                                "text": srv["full_config_text"]})
        return out

    # همان قراردادِ marzban_panel.get_wireguard_endpoints / fetch_wireguard_config_text که
    # vpn_user._send_native_wg_files صدا می‌زند: فایلِ دقیقِ پنل با نامِ خودش.
    async def get_wireguard_endpoints(self, username: str) -> list[dict]:
        files = await self._wg_files(username)
        self._wg_cache = {f["id"]: f["text"] for f in files}
        return [{"id": f["id"], "name": f["name"], "file": f["file"]} for f in files]

    async def fetch_wireguard_config_text(self, username: str, host_id=None, *, path: str | None = None) -> str:
        cache = getattr(self, "_wg_cache", None) or {}
        if host_id not in cache:
            cache = {f["id"]: f["text"] for f in await self._wg_files(username)}
            self._wg_cache = cache
        return cache.get(host_id, "")

    # ───────────── ساخت/ویرایش ─────────────
    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0, on_hold=False):
        username = _safe_username(email)
        body: dict = {"username": username, **await self._protocol_fields(inbound_ids)}
        if mb:
            body["data_limit"], body["data_limit_unit"] = int(mb), "MB"
        elif gb:
            body["data_limit"], body["data_limit_unit"] = round(float(gb), 2), "GB"
        total_days = int(days or 0) + (1 if int(hours or 0) > 0 else 0)
        if total_days > 0:
            if on_hold:
                body["activation_type"] = "flexible_days"
                body["pending_activation_days"] = total_days
            else:
                body["activation_type"] = "fixed_date"
                body["expiry_days"] = total_days
        if int(limit_ip or 0) > 0:
            body["max_clients"] = int(limit_ip)
        res = await self._request("POST", "/api/v1/users", json=body) or {}
        created = res.get("created_users") or []
        final = str(created[0]) if created else username
        sub = ""
        try:
            sub = ((await self._request("GET", f"/api/v1/users/{final}/sub")) or {}).get("sub_url") or ""
        except PanelError:
            logger.info("eylan: گرفتنِ ساب‌لینکِ %s بعدِ ساخت ناموفق بود", final)
        exp = int(time.time()) + total_days * 86400 if total_days and not on_hold else 0
        return {"uuid": uuid_str or final, "sub_id": sub_id or final, "email": final,
                "gb": gb, "days": days, "inbounds": body.get("singbox_inbounds") or [],
                "sub_link": sub, "expiry_ms": exp * 1000}

    async def _put(self, email, **fields):
        res = await self._request("PUT", f"/api/v1/users/{_safe_username(email)}", json=fields)
        if res is None:
            raise PanelError("کاربر رویِ پنلِ ایلان پیدا نشد.")
        self._list_cache = None
        return res

    async def add_traffic(self, email, gb):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر رویِ پنلِ ایلان پیدا نشد.")
        cap = int(cur["data_limit"])
        if cap == 0:            # نامحدود
            return cur
        return await self._put(email, data_limit=round((cap + float(gb) * GB) / GB, 2), data_limit_unit="GB")

    async def renew(self, email, days, reset_usage=False):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر رویِ پنلِ ایلان پیدا نشد.")
        exp = int(cur["expire"])
        if exp == 0:            # بی‌انقضا یا هنوز فعال‌نشده (flexible)؛ تاریخ نمی‌نویسیم
            res = cur
        else:
            base = max(exp, int(time.time()))
            new = _dt.datetime.fromtimestamp(base + int(days) * 86400, tz=_dt.timezone.utc)
            res = await self._put(email, expiry_date_str=new.strftime("%Y-%m-%d"))
        if reset_usage:
            await self.reset_usage(email)
        return res

    async def reset_usage(self, email):
        res = await self._request("POST", f"/api/v1/users/{_safe_username(email)}/reset_traffic")
        return res is not None

    async def reset_sub(self, email, new_sub_id=None):
        res = await self._request("POST", f"/api/v1/users/{_safe_username(email)}/revoke") or {}
        link = res.get("new_subscription_url") or ""
        if not link:
            raise PanelError("ریستِ لینک رویِ پنلِ ایلان ناموفق بود.")
        return link

    async def rename_account(self, old_email: str, new_email: str):
        return False            # اسپک تغییرِ یوزرنیم ندارد

    async def set_enabled(self, email, enabled: bool):
        await self._put(email, is_disabled=not enabled)
        return True

    async def set_ip_limit(self, email, limit: int):
        await self._put(email, max_clients=max(int(limit or 0), 1))
        return True

    async def delete_account(self, email, keep_traffic=False):
        res = await self._request("DELETE", f"/api/v1/users/{_safe_username(email)}")
        self._list_cache = None
        return res is not None

    async def test_connection(self):
        st = await self._request("GET", "/api/v1/status", auth=False)
        if st is None:
            raise PanelError("آدرسِ پنلِ ایلان درست نیست (/api/v1/status پیدا نشد).")
        self._resources = None
        ar = await self._allowed()      # کلید را هم تست می‌کند
        return {"ok": True, "inbounds": await self.active_inbound_ids(),
                "nodes": len(ar.get("nodes") or [])}
