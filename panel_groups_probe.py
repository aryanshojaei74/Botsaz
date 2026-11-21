"""
panel_groups_probe.py — فقط خواندنی (login + چند GET). نشان می‌دهد «GET /api/groups» کجا کند است:
  hdr  = زمانِ رسیدنِ هدرِ پاسخ (اتصال + پردازشِ پنل)
  body = زمانِ خواندنِ بدنهٔ پاسخ (انتقالِ داده)         ← این بخش در شمارندهٔ perfctx دیده نمی‌شد
  size = اندازهٔ پاسخ
هیچ کاربری ساخته/تغییر داده نمی‌شود.

اجرا:
    cd /opt/botsaz && source venv/bin/activate
    python3 panel_groups_probe.py 257 500 344      # شمارهٔ پنل‌ها (ستونِ id در panel_login_check)
    python3 panel_groups_probe.py 257 --n 6        # تعدادِ تکرار
"""
import argparse
import asyncio
import time

from config import Session
from models import VpnPanel


async def _probe(p, n):
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
    host = p.base_url.split("//")[-1].split("/")[0]
    print(f"\n— {host}  (پنل {p.id}، نوع {p.panel_key})")
    c = make_panel_client(p)
    t = time.monotonic()
    try:
        await c.login()
    except Exception as e:  # noqa: BLE001
        print(f"   login ناموفق: {type(e).__name__}: {str(e)[:120]}")
        return
    print(f"   login: {time.monotonic() - t:.2f}s")
    s = await c._ensure_session()
    hdrs = {"Authorization": f"Bearer {c._token}", "Accept": "application/json"}
    for path in ("/api/groups", "/api/groups/simple"):
        for i in range(n):
            t0 = time.monotonic()
            try:
                async with s.get(f"{c.base_url}{path}", headers=hdrs) as r:
                    t1 = time.monotonic()
                    body = await r.read()
                    t2 = time.monotonic()
                    print(f"   {path:<20} #{i + 1} HTTP {r.status}  hdr={t1 - t0:5.2f}s  body={t2 - t1:5.2f}s  size={len(body):>7}B")
            except Exception as e:  # noqa: BLE001
                print(f"   {path:<20} #{i + 1} خطا بعد از {time.monotonic() - t0:.1f}s: {type(e).__name__}")
            await asyncio.sleep(0.3)
    await c.close()


async def main(ids, n):
    async with Session() as s:
        for pid in ids:
            p = await s.get(VpnPanel, pid)
            if p:
                await _probe(p, n)
            else:
                print(f"پنل {pid} پیدا نشد")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+", type=int)
    ap.add_argument("--n", type=int, default=4)
    a = ap.parse_args()
    asyncio.run(main(a.ids, a.n))
