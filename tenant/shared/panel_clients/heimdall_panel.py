"""heimdall_panel.py — کلاینتِ پنلِ HEIMDALL (فورکِ نسلِ جدیدِ ۳X-UI؛ «OpenAPI: HEIMDALL Panel API 3.x»).

=== PATCH_HEIMDALL_PANEL_V1 ===
گزارشِ مشتری (ɪᴍ🍂mehdi): دو پنلِ HEIMDALL — یکی وایرگارد، یکی V2Ray («شبیه ثنایی»). مستندات (openapi.json) نشان می‌دهد
قراردادِ «clients» دقیقاً همان است که PanelClient/DEGERIS پیاده کرده: ساخت (POST /panel/api/clients/add)،
گرفتن/ترافیک/bulkAdjust/resetTraffic/update/del، لینکِ ساب و لینکِ کلاینت. تفاوت‌ها:
  • فرمِ ورود فیلدِ twoFactorCode دارد (در OpenAPI «required»؛ بدونِ 2FA خالی فرستاده می‌شود).
  • کوکیِ نشست «heimdall» است و درخواست‌هایِ ناامن (POST) هدرِ X-CSRF-Token می‌خواهند. توکن از GET /csrf-token
    (بعد از ورود) گرفته می‌شود؛ اگر صفحهٔ اصلی متاتگ نداشت باز هم کار می‌کند.
  • RBAC: ادمینِ بدونِ مجوزِ inbounds.view از /inbounds/list خطای ۴۰۳ می‌گیرد؛ /options و /list/slim هم امتحان می‌شود.
  • آدرسِ پنل «مسیرِ مخفی» دارد: https://host:port/<secret> — همان‌طور وارد شود (مسیرِ /panel/admins را نگذار).
  • وایرگارد: لینکِ کلاینت/ساب برایِ پروتکلِ wireguard چیزی برنمی‌گرداند («Protocols without a URL form ... wireguard»).
    اگر کلاینت به یک inboundِ وایرگارد وصل است و لینکی نیامد، فایلِ .conf از رویِ کلیدهایِ کلاینت و inbound ساخته می‌شود
    (متنِ خامِ INI؛ ربات همین را به‌صورتِ فایل/QR تحویل می‌دهد).
"""
from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import urlparse

try:
    import aiohttp
except ImportError:           # pragma: no cover — PanelClient خودش خطای واضح می‌دهد
    aiohttp = None

from tenant.shared.panel_clients.vpn_panel import PanelClient, PanelError

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

__all__ = ["HeimdallPanelClient", "build_wg_conf", "wg_public_key"]

_RBAC_HINT = ("نقشِ این ادمین در پنلِ HEIMDALL مجوزِ «مشاهدهٔ inboundها» (inbounds.view) را ندارد.\n"
              "راه‌حل: در پنل ← ادمین‌ها/نقش‌ها، به نقشِ این ادمین مجوزهایِ inbounds.view و کلاینت‌ها (clients) را بده، "
              "یا با ادمینِ اصلی (owner) وصل کن؛ یا شناسهٔ inboundها را در مرحلهٔ ۵ دستی بفرست (به‌جایِ «همه»).")


# ───────────────────────── وایرگارد ─────────────────────────

def wg_public_key(private_b64: str) -> str | None:
    """کلیدِ عمومیِ X25519 از رویِ کلیدِ خصوصیِ base64 (برایِ inboundی که pubKey نمی‌دهد)."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        raw = base64.b64decode(private_b64 + "=" * (-len(private_b64) % 4))
        if len(raw) != 32:
            return None
        pub = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(pub).decode()
    except Exception:  # noqa: BLE001
        return None


def _obj(v):
    """settings گاهی شیء است و گاهی رشتهٔ JSON (فرمِ قدیمی)."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _csv(v) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x).strip() for x in v if str(x).strip())
    return ", ".join(x.strip() for x in str(v or "").split(",") if x.strip())


def build_wg_conf(inbound: dict, client: dict, panel_url: str, dns: str = "") -> str | None:
    """فایلِ .conf وایرگارد برایِ یک کلاینت؛ None اگر داده‌هایِ لازم (کلید/آدرس) در دسترس نیست."""
    st = _obj(inbound.get("settings"))
    priv = str(client.get("privateKey") or "").strip()
    addr = _csv(client.get("allowedIPs"))
    psk = str(client.get("preSharedKey") or "").strip()
    keep = int(client.get("keepAlive") or 0)
    if not (priv and addr):                       # بعضی نسخه‌ها کلیدها را در peers نگه می‌دارند
        pub = str(client.get("publicKey") or "").strip()
        for p in st.get("peers") or []:
            if isinstance(p, dict) and (p.get("publicKey") == pub or p.get("email") == client.get("email")):
                priv = priv or str(p.get("privateKey") or "").strip()
                addr = addr or _csv(p.get("allowedIPs"))
                psk = psk or str(p.get("preSharedKey") or "").strip()
                keep = keep or int(p.get("keepAlive") or 0)
                break
    server_pub = str(st.get("pubKey") or st.get("publicKey") or "").strip() or \
        (wg_public_key(str(st.get("secretKey") or "")) or "")
    port = inbound.get("port")
    if not (priv and addr and server_pub and port):
        return None
    listen = str(inbound.get("listen") or "").strip()
    host = str(inbound.get("shareAddr") or "").strip() \
        or (listen if listen not in ("", "0.0.0.0", "::", "::0") else "") \
        or str(inbound.get("nodeAddress") or "").strip() \
        or (urlparse(panel_url).hostname or "")
    if not host:
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    mtu = st.get("mtu")
    lines = []
    name = str(inbound.get("remark") or "").strip()
    if name:
        lines.append(f"# Name = {name}")
    lines += ["[Interface]", f"PrivateKey = {priv}", f"Address = {addr}",
              f"DNS = {dns or st.get('dns') or '1.1.1.1'}"]
    if mtu:
        lines.append(f"MTU = {int(mtu)}")
    lines += ["", "[Peer]", f"PublicKey = {server_pub}"]
    if psk:
        lines.append(f"PresharedKey = {psk}")
    lines += ["AllowedIPs = 0.0.0.0/0, ::/0", f"Endpoint = {host}:{int(port)}"]
    if keep > 0:
        lines.append(f"PersistentKeepalive = {keep}")
    return "\n".join(lines)


# ───────────────────────── کلاینت ─────────────────────────

class HeimdallPanelClient(PanelClient):
    """کلاینتِ پنلِ HEIMDALL (نسلِ جدیدِ ۳X-UI با RBAC و CSRF)."""
    _LOGIN_EXTRA = {"twoFactorCode": ""}

    async def _csrf_endpoint(self):
        """GET /csrf-token (نیازمندِ نشستِ واردشده) ⇒ {"success":true,"obj":"<token>"}."""
        s = await self._ensure_session()
        try:
            async with s.get(f"{self.base_url}/csrf-token",
                             headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}) as r:
                if r.status != 200:
                    return None
                p = json.loads(await r.text())
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        obj = p.get("obj") if isinstance(p, dict) else None
        if isinstance(obj, dict):
            obj = obj.get("token") or obj.get("csrfToken")
        if isinstance(obj, str) and obj:
            self._csrf = obj
            return obj
        return None

    async def login(self, force=False):
        fresh = force or not self._logged_in
        ok = await super().login(force)
        if fresh and ok:
            await self._csrf_endpoint()      # توکنِ وابسته به نشستِ تازه
        return ok

    # ── RBAC: ادمینِ بدونِ inbounds.view ──
    async def list_inbounds(self):
        last = None
        for path in ("/panel/api/inbounds/list", "/panel/api/inbounds/options", "/panel/api/inbounds/list/slim"):
            try:
                res = await self._request("GET", path)
            except PanelError as e:
                last = e
                continue
            if res.get("success"):
                return res.get("obj", []) or []
            last = PanelError(str(res.get("msg") or res))
        msg = str(last or "")
        if "permission" in msg.lower() or "403" in msg:
            raise PanelError(_RBAC_HINT) from last
        raise PanelError(f"خطا در گرفتن لیست inbound: {msg}") from last

    async def active_inbound_ids(self):
        try:
            return await super().active_inbound_ids()
        except PanelError:
            if self.inbound_ids:      # لیست در دسترس نیست ولی ادمین شناسه‌ها را دستی داده
                return list(self.inbound_ids)
            raise

    # ── وایرگارد: لینکِ URL ندارد؛ از رویِ کلیدها ساخته می‌شود ──
    async def _wg_confs(self, email: str) -> list[str]:
        try:
            cl = await self.get_client(email)
            if not cl:
                return []
            out: list[str] = []
            for iid in [int(i) for i in (cl.get("inboundIds") or [])]:
                res = await self._request("GET", f"/panel/api/inbounds/get/{iid}")
                ib = res.get("obj") if res.get("success") else None
                if not isinstance(ib, dict) or str(ib.get("protocol") or "").lower() != "wireguard":
                    continue
                conf = build_wg_conf(ib, cl, self.base_url)
                if conf:
                    out.append(conf)
            return out
        except Exception:  # noqa: BLE001 — نبودِ کانفیگ نباید خریدِ کاربر را بشکند
            logger.exception("heimdall: ساختِ کانفیگِ وایرگارد ناموفق بود (%s)", email)
            return []

    async def get_client_links(self, email):
        links = await super().get_client_links(email)
        return links or await self._wg_confs(email)

    async def get_sub_links(self, sub_id):
        links = await super().get_sub_links(sub_id)
        if links:
            return links
        try:                          # subId → email از لیستِ کلاینت‌ها
            res = await self._request("GET", "/panel/api/clients/list")
            for c in (res.get("obj") or []) if res.get("success") else []:
                c = c.get("client", c) if isinstance(c, dict) else {}
                if c.get("subId") == sub_id and c.get("email"):
                    return await self._wg_confs(c["email"])
        except Exception:  # noqa: BLE001
            logger.exception("heimdall: پیداکردنِ کلاینت با subId ناموفق بود")
        return []
