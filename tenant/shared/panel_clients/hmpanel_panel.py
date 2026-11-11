"""
hmpanel_panel.py — کلاینتِ async پنلِ HMPanel (github.com/neoauroraproject/hmpanel) — PATCH_HMPANEL_PANEL_V1.

HMPanel یک مدیریتِ چندپنلهٔ ۳X-UI است با «کلیدِ API ادمین». احراز: هدرِ «Authorization: Bearer hmp_…»
(کلید در فیلدِ «رمز» ذخیره می‌شود؛ یوزرنیم «-»). کلید دقیقاً جایِ یک ادمین کار می‌کند: نماینده فقط
کاربرانِ خودش را می‌بیند. سقفِ پیش‌فرض ۶۰ درخواست در دقیقه برایِ هر کلید است (۴۲۹).

مسیرها (همه زیرِ /api؛ مستندِ رسمی docs/admin-api.md و docs/bot-api.md):
  GET    /api/v1/account                    نقش/سقف/موجودی/clientCount
  GET    /api/v1/inbounds                   {data:[{id, tag, remark, protocol, port, panel:{id,name,panelType}}]}
  GET    /api/v1/clients/lookup?key=        یک کاربر با ایمیل/توکن/subId/uuid → «شیءِ Client» (۴۰۴ اگر نبود)
  POST   /api/v1/clients                    {email, inboundIds[], total(بایت، ۰=نامحدود), expiryTime(میلی‌ثانیه، ۰=نامحدود), remark, limitIp}
  PATCH  /api/v1/clients/:id                {enable, total, expiryTime, remark, limitIp}
  DELETE /api/v1/clients/:id
  POST   /api/v1/clients/:id/renew          {days, totalGb?, resetUsage?} — days به «دیرتر از الان و انقضایِ فعلی» اضافه می‌شود
  POST   /api/v1/clients/:id/reset-usage

شیءِ Client: {id, email, remark, subToken, subscriptionUrl, enable, status(active|disabled|expired|limited),
upBytes, downBytes, usedBytes, totalBytes, remainingBytes, expiryTime(ms), limitIp, …}

نکته‌ها:
  • ساختِ کاربر حداقل یک inbound می‌خواهد. اگر ادمین inbound نداده باشد، همهٔ inboundهایی که کلید اجازهٔ ساختِ کاربر رویشان را
    دارد (GET /v1/inbounds) استفاده می‌شود؛ اگر inbound_ids ثبت شده باشد (شناسه یا tag) همان‌ها.
  • پاسخِ دقیقِ POST /clients در مستند نیست؛ بعد از ساخت، کاربر با lookup خوانده می‌شود (نه از بدنهٔ پاسخ).
"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import re
import time
from urllib.parse import quote

try:
    import aiohttp
except ImportError:      # pragma: no cover
    aiohttp = None

from tenant.shared.panel_clients.vpn_panel import PanelError

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

__all__ = ["HMPanelClient"]

GB = 1024 ** 3


def _hm_name(email: str) -> str:
    """نامِ قطعیِ کاربر در پنل (فقط A-Za-z0-9_.-، ≥۳ نویسه) از رویِ نامِ ربات؛ بدونِ ذخیرهٔ اضافه همیشه همان را پیدا می‌کنیم."""
    raw = str(email or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "", raw)
    if cleaned and cleaned == raw and len(cleaned) >= 3:
        return cleaned[:64]
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return ((cleaned[:20] + "-") if len(cleaned) >= 2 else "u-") + h


class HMPanelClient:
    """امضایِ سازنده مثلِ بقیهٔ کلاینت‌ها؛ password = کلیدِ API (hmp_…)."""

    def __init__(self, base_url: str, username: str, password: str,
                 sub_base: str = "", inbound_ids=None, verify_ssl: bool = False,
                 timeout: int = 15, api_key: str | None = None):
        if aiohttp is None:
            raise PanelError("کتابخانهٔ aiohttp نصب نیست.")
        base_url = (base_url or "").split("#", 1)[0].strip().rstrip("/")
        base_url = re.sub(r"/(api|docs|dashboard)(/.*)?$", "", base_url, flags=re.IGNORECASE)
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or password or "").strip()
        self.username = username
        self.password = password
        self.sub_base = ""
        self.inbound_ids = [str(i).strip() for i in (inbound_ids or []) if str(i).strip()]
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = None
        self._inb_cache: tuple[float, list] | None = None

    # ───────────── HTTP ─────────────
    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            t = aiohttp.ClientTimeout(total=self.timeout, sock_connect=7)
            self._session = aiohttp.ClientSession(timeout=t, connector=aiohttp.TCPConnector(ssl=self.verify_ssl))
        return self._session

    async def login(self, force=False):
        if not self.api_key:
            raise PanelError("کلیدِ API پنلِ HMPanel ثبت نشده است.")
        return True

    async def _request(self, method, path, *, ok404=False, **kwargs):
        s = await self._ensure_session()
        await self.login()
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if method.upper() != "GET":
            headers["Content-Type"] = "application/json"
        url = f"{self.base_url}/api{path}"
        for attempt in range(2):
            try:
                async with s.request(method, url, headers=headers, **kwargs) as r:
                    text = await r.text()
                    if r.status == 401:
                        raise PanelError(
                            "کلیدِ API پنلِ HMPanel نامعتبر است، ادمینِ آن غیرفعال/منقضی شده، یا کلید ابطال شده (HTTP 401). "
                            "از پنل (تنظیمات ← API) کلیدِ تازه بساز و دوباره وارد کن.")
                    if r.status == 403:
                        msg = _msg(text)
                        raise PanelError(
                            f"این کلیدِ HMPanel اجازهٔ این کار را ندارد (HTTP 403): {msg}. "
                            "هنگامِ ساختِ کلید دسترسی «همه (*)» را انتخاب کن.")
                    if r.status == 429:
                        raise PanelError("سقفِ درخواستِ کلیدِ HMPanel (۶۰ در دقیقه) پر شده؛ چند ثانیه بعد دوباره امتحان کن.")
                    if r.status == 404:
                        if ok404:
                            return None
                        raise PanelError(f"HMPanel: {method} {path} پیدا نشد (HTTP 404): {_msg(text)}")
                    data = None
                    if text:
                        try:
                            data = _json.loads(text)
                        except ValueError:
                            data = {"raw": text[:300]}
                    if r.status >= 400:
                        raise PanelError(f"HMPanel: {method} {path} ناموفق (HTTP {r.status}): {_msg(text)}")
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
                    logger.exception("hmpanel: بستنِ نشستِ خراب ناموفق بود")
                self._session = None
                raise PanelError(
                    f"اتصال به HMPanel برقرار نشد یا بیش از حد طول کشید: {str(e) or type(e).__name__} "
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

    # ───────────── inbound ─────────────
    async def list_inbounds(self):
        now = time.monotonic()
        if self._inb_cache and now - self._inb_cache[0] < 60:
            return self._inb_cache[1]
        res = await self._request("GET", "/v1/inbounds")
        rows = res.get("data") if isinstance(res, dict) else res
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        self._inb_cache = (now, rows)
        return rows

    async def active_inbound_ids(self):
        return [str(r.get("id")) for r in await self.list_inbounds() if r.get("id")]

    async def _pick_inbounds(self, inbound_ids=None) -> list[str]:
        rows = await self.list_inbounds()
        by_id = {str(r.get("id")): str(r.get("id")) for r in rows if r.get("id")}
        by_tag = {str(r.get("tag")): str(r.get("id")) for r in rows if r.get("tag") and r.get("id")}
        want = [str(i).strip() for i in (inbound_ids or self.inbound_ids or []) if str(i).strip()]
        if not want:
            return list(by_id.values())
        out = []
        for w in want:
            v = by_id.get(w) or by_tag.get(w)
            if v and v not in out:
                out.append(v)
        if not out:
            raise PanelError("هیچ‌کدام از inboundهای ثبت‌شده برایِ HMPanel در پنل پیدا نشد؛ تنظیمِ inbound را «همه» بگذار.")
        return out

    # ───────────── کاربرها ─────────────
    @staticmethod
    def _expire_epoch(c: dict) -> int:
        try:
            ms = int(float(c.get("expiryTime") or 0))
        except (TypeError, ValueError):
            return 0
        return ms // 1000 if ms > 0 else 0

    @staticmethod
    def _num(v) -> int:
        try:
            return int(float(v or 0))
        except (TypeError, ValueError):
            return 0

    def _norm(self, c: dict) -> dict:
        u = dict(c)
        name = str(c.get("email") or "")
        used = self._num(c.get("usedBytes")) or (self._num(c.get("upBytes")) + self._num(c.get("downBytes")))
        u["username"] = u["subId"] = name
        u["email"] = name
        u["id"] = str(c.get("id") or "")
        u["data_limit"] = self._num(c.get("totalBytes"))
        u["expire"] = self._expire_epoch(c)
        u["used_traffic"] = used
        u["sub_link"] = str(c.get("subscriptionUrl") or "")
        enabled = c.get("enable") not in (False, 0, "0", "false")
        u["status"] = "active" if enabled and c.get("status") not in ("disabled",) else "disabled"
        return u

    async def _lookup(self, email) -> dict | None:
        res = await self._request("GET", f"/v1/clients/lookup?key={quote(_hm_name(email), safe='')}", ok404=True)
        if isinstance(res, dict) and res.get("id"):
            return res
        if isinstance(res, dict) and isinstance(res.get("data"), dict) and res["data"].get("id"):
            return res["data"]
        return None

    async def get_client(self, email):
        c = await self._lookup(email)
        return self._norm(c) if c else None

    async def get_usage(self, email):
        cur = await self.get_client(email)
        if not cur:
            return None
        return {"up": self._num(cur.get("upBytes")), "down": self._num(cur.get("downBytes")),
                "total": int(cur["data_limit"]), "expiryTime": int(cur["expire"]) * 1000,
                "enable": cur["status"] == "active"}

    async def list_clients(self):
        out: list = []
        page = 1
        while page <= 40:
            res = await self._request("GET", f"/v1/clients/search?page={page}&limit=200") or {}
            batch = res.get("data") if isinstance(res, dict) else []
            batch = [b for b in (batch or []) if isinstance(b, dict)]
            for c in batch:
                u = self._norm(c)
                out.append({"email": u["username"], "username": u["username"],
                            "traffic": {"up": self._num(c.get("upBytes")), "down": self._num(c.get("downBytes")),
                                        "total": u["data_limit"]},
                            "totalGB": u["data_limit"], "expiryTime": u["expire"] * 1000,
                            "enable": u["status"] == "active"})
            total = self._num(res.get("total")) if isinstance(res, dict) else 0
            if not batch or len(out) >= total:
                break
            page += 1
        return out

    # ───────────── لینک‌ها ─────────────
    def sub_link(self, sub_id):
        return ""

    async def get_sub_links(self, sub_id):
        return []

    async def get_client_links(self, email):
        return []

    # ───────────── ساخت/ویرایش ─────────────
    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0, on_hold=False):
        name = _hm_name(email)
        inbounds = await self._pick_inbounds(inbound_ids)
        total = int(float(gb or 0) * GB) if gb else 0
        if mb:
            total = int(mb) * 1024 * 1024
        secs = int(days or 0) * 86400 + int(hours or 0) * 3600
        exp_ms = (int(time.time()) + secs) * 1000 if secs > 0 else 0
        body = {"email": name, "inboundIds": inbounds, "total": total, "expiryTime": exp_ms,
                "remark": str(email)[:64]}
        if limit_ip:
            body["limitIp"] = int(limit_ip)
        res = await self._request("POST", "/v1/clients", json=body)
        c = res if isinstance(res, dict) and res.get("id") and res.get("subscriptionUrl") else None
        for attempt in range(3):
            if c:
                break
            c = await self._lookup(email)
            if not c and attempt < 2:
                await asyncio.sleep(1.0)
        if not c:
            raise PanelError("کاربر در HMPanel ساخته شد ولی بلافاصله پیدا نشد؛ چند ثانیه بعد دوباره از پنل بررسی کن.")
        sub = str(c.get("subscriptionUrl") or "")
        return {"uuid": uuid_str or str(c.get("id")), "sub_id": sub_id or name, "email": name, "gb": gb, "days": days,
                "inbounds": inbounds, "sub_link": sub, "expiry_ms": exp_ms}

    async def _id_of(self, email) -> str:
        c = await self._lookup(email)
        if not c:
            raise PanelError("کاربر روی HMPanel پیدا نشد.")
        return str(c["id"])

    async def _patch(self, email, **fields):
        cid = await self._id_of(email)
        return await self._request("PATCH", f"/v1/clients/{quote(cid, safe='')}", json=fields)

    async def add_traffic(self, email, gb):
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر روی HMPanel پیدا نشد.")
        cap = int(cur["data_limit"])
        if cap == 0:                 # نامحدود
            return cur
        return await self._patch(email, total=cap + int(float(gb) * GB))

    async def renew(self, email, days, reset_usage=False):
        cid = await self._id_of(email)
        body = {"days": int(days)}
        if reset_usage:
            body["resetUsage"] = True
        return await self._request("POST", f"/v1/clients/{quote(cid, safe='')}/renew", json=body)

    async def reset_usage(self, email):
        cid = await self._id_of(email)
        await self._request("POST", f"/v1/clients/{quote(cid, safe='')}/reset-usage", json={})
        return True

    async def reset_sub(self, email, new_sub_id=None):
        raise PanelError("این پنل ریستِ لینک ندارد؛ لینک اشتراک را دوباره ارسال کن.")

    async def rename_account(self, old_email: str, new_email: str):
        return False

    async def set_enabled(self, email, enabled: bool):
        await self._patch(email, enable=bool(enabled))
        return True

    async def set_ip_limit(self, email, limit: int):
        await self._patch(email, limitIp=int(limit))
        return True

    async def delete_account(self, email, keep_traffic=False):
        c = await self._lookup(email)
        if not c:
            return False
        await self._request("DELETE", f"/v1/clients/{quote(str(c['id']), safe='')}")
        return True

    async def _diagnose_404(self) -> str:
        """وقتی /v1/account ۴۰۴ می‌دهد معلوم کن آدرس غلط است یا نسخه/نوعِ کلیدِ پنل."""
        health = await self._request("GET", "/v1/health", ok404=True)
        if health is None:
            return ("آدرس درست نیست: مسیرِ /api/v1/health هم در این آدرس پیدا نشد؛ مطمئن شو آدرس همان دامنه‌ای است که پنل با آن باز می‌شود "
                    "(مثلاً https://panel.example.com، بدونِ /api و بدونِ مسیرِ اضافه) و nginxِ پنل مسیرِ /api را به بک‌اند می‌دهد.")
        me = await self._request("GET", "/v1/me", ok404=True)
        if isinstance(me, dict) and me.get("id"):
            return ("پنل در دسترس است ولی این کلید از نوعِ «API کلاینتِ بات» (کلیدِ قدیمیِ سوپرادمین) است و /v1/account را ندارد. "
                    "کلیدِ ادمین بساز: پنل ← تنظیمات ← API (یا «دسترسی API»)، نه بخشِ API clients.")
        return ("پنل در دسترس است (health درست جواب می‌دهد) ولی /v1/account در این نسخه نیست؛ احتمالاً نسخهٔ پنل قدیمی است. "
                "پنل HMPanel را به آخرین نسخه آپدیت کن (حداقل نسخه‌ای که «تنظیمات ← API» و /api/v1/docs دارد) و دوباره امتحان کن.")

    async def test_connection(self):
        try:
            acc = await self._request("GET", "/v1/account")
        except PanelError as e:
            if "(HTTP 404)" in str(e):
                raise PanelError(f"{e}\n\n🔎 {await self._diagnose_404()}") from e
            raise
        if not isinstance(acc, dict) or not acc.get("id"):
            raise PanelError("پاسخِ HMPanel شبیهِ API نبود؛ آدرسِ پنل را (بدونِ /api) بررسی کن.")
        inbs = await self.list_inbounds()
        return {"ok": True, "inbounds": [str(r.get("tag") or r.get("id")) for r in inbs],
                "username": acc.get("username"), "role": acc.get("role"), "balance": acc.get("balance"),
                "clients": acc.get("clientCount"), "max_clients": acc.get("maxClients")}


def _msg(text: str) -> str:
    try:
        d = _json.loads(text)
        if isinstance(d, dict):
            m = d.get("message") or d.get("error")
            if isinstance(m, list):
                m = "، ".join(str(x) for x in m)
            return str(m)[:200] if m else text[:200]
    except ValueError:
        pass
    return (text or "")[:200]
