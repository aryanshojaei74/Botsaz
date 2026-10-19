#!/usr/bin/env python3
"""
scripts/check_heimdall.py — تستِ فقط‌خواندنیِ اتصال به پنلِ HEIMDALL (PATCH_HEIMDALL_PANEL_V1).

هیچ کلاینت/inbound نمی‌سازد و چیزی را تغییر نمی‌دهد: ورود، توکنِ CSRF، لیستِ inboundها (با نقشِ RBAC)،
آدرسِ ساب، و برایِ inboundهایِ وایرگارد اینکه کلیدهایِ لازمِ ساختِ .conf در پاسخِ پنل هست یا نه.
رمز را در خط فرمان نگذار: از متغیرِ HEIMDALL_PASS یا پرسشِ تعاملی خوانده می‌شود. اجرا از /opt/botsaz:

    HEIMDALL_PASS='...' venv/bin/python scripts/check_heimdall.py --url 'https://host:port/<secret>' --user admin
    venv/bin/python scripts/check_heimdall.py --url 'https://host:port/<secret>' --user admin --email SOME_EXISTING_CLIENT
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mask(conf: str) -> str:
    out = []
    for ln in conf.splitlines():
        k = ln.split("=", 1)[0].strip().lower()
        out.append(f"{ln.split('=', 1)[0]}= ***" if k in ("privatekey", "presharedkey") else ln)
    return "\n".join(out)


def _mask_uri(uri: str) -> str:
    """لینکِ کلاینت را بدونِ رازها چاپ می‌کند: بخشِ کاربر (uuid/کلیدِ خصوصی) و مقدارِ کلیدهایِ حساس ماسک می‌شود."""
    from urllib.parse import parse_qsl, urlsplit
    try:
        u = urlsplit(uri)
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        secret_keys = {"privatekey", "presharedkey", "psk", "password", "pbk", "sid", "secret"}
        q = [f"{k}={'***' if k.lower() in secret_keys else v[:40]}" for k, v in parse_qsl(u.query, keep_blank_values=True)]
        return f"{u.scheme}://***@{host}{port}?{'&'.join(q)}#{u.fragment[:30]}"
    except Exception:  # noqa: BLE001
        return uri.split("@")[-1][:80]


async def _explain_login_failure(c, user: str, pw: str) -> None:
    """پاسخِ خامِ پنل به ورود را (بدونِ رمز) نشان می‌دهد تا علت معلوم شود: رمز/یوزرنیم، 2FA، CSRF یا آدرس."""
    import aiohttp
    base = c.base_url
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                         connector=aiohttp.TCPConnector(ssl=False)) as s:
            async with s.get(base + "/") as r:
                print(f"   GET / → HTTP {r.status} ({r.headers.get('Content-Type', '')[:30]})")
            async with s.post(base + "/login", json={"username": user, "password": pw, "twoFactorCode": ""},
                              headers={"X-Requested-With": "XMLHttpRequest", "Origin": base.split("/", 3)[0] + "//" + base.split("/")[2]}) as r:
                txt = (await r.text())[:300].replace(pw, "***") if pw else (await r.text())[:300]
                print(f"   POST /login → HTTP {r.status}: {txt}")
    except Exception as e:  # noqa: BLE001
        print(f"   (تشخیصِ بیشتر ممکن نشد: {type(e).__name__}: {e})")
    print("   راهنما: «Wrong username or password» = یوزرنیم/رمز؛ «csrf» = توکن؛ «2fa/otp» = کدِ دومرحله‌ای روشن است؛ "
          "HTTP 404 = مسیرِ مخفیِ آدرس اشتباه است.")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="آدرسِ پنل با مسیرِ مخفی (بدونِ /panel/admins)")
    ap.add_argument("--user", required=True)
    ap.add_argument("--email", help="یک کلاینتِ موجود؛ لینک‌ها/کانفیگِ وایرگاردش را نشان بده")
    a = ap.parse_args()
    pw = os.environ.get("HEIMDALL_PASS") or getpass.getpass("رمزِ پنل: ")
    from tenant.shared.panel_clients.heimdall_panel import HeimdallPanelClient, build_wg_conf
    from tenant.shared.panel_clients.vpn_panel import PanelError
    c = HeimdallPanelClient(a.url, a.user, pw)
    try:
        try:
            await c.login()
        except PanelError as e:
            print(f"❌ {e}")
            await _explain_login_failure(c, a.user, pw)
            return 1
        print(f"✅ ورود موفق | CSRF: {'دارد' if c._csrf else 'ندارد (فقط Bearer/GET کار می‌کند)'}")
        try:
            ibs = await c.list_inbounds()
        except PanelError as e:
            print(f"❌ لیستِ inbound: {e}")
            return 1
        print(f"📥 inboundها: {len(ibs)}")
        for ib in ibs:
            extra = ""
            if str(ib.get("protocol")).lower() == "wireguard":
                # /inbounds/list معمولاً settings را null می‌دهد؛ جزئیات فقط از /inbounds/get/{id}
                det = ib
                try:
                    r = await c._request("GET", f"/panel/api/inbounds/get/{ib.get('id')}")
                    det = r.get("obj") or ib
                except PanelError as e:
                    print(f"   ℹ️ get/{ib.get('id')}: {str(e)[:90]} (برایِ ادمینِ بدونِ inbounds.view طبیعی است؛ از /options استفاده می‌شود)")
                st = det.get("settings")
                if isinstance(st, str):
                    import json as _j
                    try:
                        st = _j.loads(st)
                    except ValueError:
                        st = {}
                st = st if isinstance(st, dict) else {}
                extra = (f" | WG: wgPublicKey={'✓' if ib.get('wgPublicKey') else '✗'} wgMtu={ib.get('wgMtu') or '—'} "
                         f"wgDns={ib.get('wgDns') or '—'} (از /options) | settings: "
                         f"secretKey={'✓' if st.get('secretKey') else '✗'} pubKey={'✓' if st.get('pubKey') else '✗'} "
                         f"mtu={st.get('mtu')} peers={len(st.get('peers') or [])} "
                         f"clients={len(st.get('clients') or [])} listen={det.get('listen') or '—'} "
                         f"shareAddr={det.get('shareAddr') or '—'} | کلیدهایِ settings: {sorted(st.keys())}")
            print(f"  #{ib.get('id')} {ib.get('protocol')}:{ib.get('port')} «{ib.get('remark')}» "
                  f"{'روشن' if ib.get('enable', True) else 'خاموش'} کلاینت={len(ib.get('clientStats') or [])}{extra}")
        await c.ensure_sub_base()
        print(f"🔗 آدرسِ ساب: {c.sub_base or '— (پیدا نشد؛ در تنظیمِ پنل دستی بده)'}")
        if a.email:
            cl = await c.get_client(a.email)
            if not cl:
                print(f"⚠️ کلاینتِ {a.email} پیدا نشد")
                return 0
            print(f"👤 {a.email}: inboundها={cl.get('inboundIds')} subId={cl.get('subId')}")
            links = await c.get_client_links(a.email)
            print(f"🔌 لینک/کانفیگ: {len(links)}")
            for ln in links[:6]:
                print("  ", _mask(ln) if "[Interface]" in ln else _mask_uri(ln))
    except PanelError as e:
        print(f"❌ {e}")
        return 1
    finally:
        await c.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
