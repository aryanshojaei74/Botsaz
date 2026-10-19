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
        await c.login()
        print(f"✅ ورود موفق | CSRF: {'دارد' if c._csrf else 'ندارد (فقط Bearer/GET کار می‌کند)'}")
        try:
            ibs = await c.list_inbounds()
        except PanelError as e:
            print(f"❌ لیستِ inbound: {e}")
            return 1
        print(f"📥 inboundها: {len(ibs)}")
        for ib in ibs:
            st = ib.get("settings") if isinstance(ib.get("settings"), dict) else {}
            extra = ""
            if str(ib.get("protocol")).lower() == "wireguard":
                extra = (f" | WG: secretKey={'✓' if st.get('secretKey') else '✗'} pubKey={'✓' if st.get('pubKey') else '✗'} "
                         f"mtu={st.get('mtu')} shareAddr={ib.get('shareAddr') or '—'}")
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
            for ln in links[:3]:
                print("  ", _mask(ln) if "[Interface]" in ln else ln[:80] + "…")
    except PanelError as e:
        print(f"❌ {e}")
        return 1
    finally:
        await c.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
