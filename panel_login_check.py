"""
panel_login_check.py — فقط خواندنی (فقط یک بار login؛ هیچ کاربری ساخته/تغییر داده نمی‌شود).
ثابت می‌کند «لاگین‌شدنِ دوباره» روی یک پنل از کشِ توکن می‌آید یا باید ≥۱۲ ثانیه صبر کند (محدودکنندهٔ لاگین).

برایِ هر پنل: دو کلاینتِ تازه (مثلِ مسیرِ تستِ رایگان) پشت‌سرِ‌هم login می‌زنند و زمانشان چاپ می‌شود.
    login#1 ≈ ۰٫۵–۲s  عادی (اتصال TLS + POST توکن)
    login#2 ≈ ۰٫۰s    یعنی توکن از کش آمد ✅
    login#2 ≈ ۱۲s     یعنی کش کار نکرد و ۱۲ ثانیه معطل شد ❌ (همان علتِ کندی)

اجرا:
    cd /opt/botsaz && source venv/bin/activate
    python3 panel_login_check.py speedur adamsfelfel panelsazan jetserves
(هر آرگومان بخشی از آدرسِ پنل است)
"""
import asyncio
import sys
import time

from sqlalchemy import select

from config import Session
from models import VpnPanel


def _exp_info(client):
    try:
        from tenant.shared.panel_clients.marzban_panel import _jwt_exp
        tok = getattr(client, "_token", None) or ""
        e = _jwt_exp(tok)
        if e:
            return f"JWT معتبر تا {int(e - time.time())}s دیگر"
        return "توکن JWT نیست/exp ندارد" if tok else "بدونِ توکن"
    except Exception as ex:  # noqa: BLE001
        return f"? ({type(ex).__name__})"


async def _t(coro, limit=40):
    t = time.monotonic()
    try:
        await asyncio.wait_for(coro, limit)
        return time.monotonic() - t, None
    except Exception as e:  # noqa: BLE001
        return time.monotonic() - t, type(e).__name__


async def main(pats):
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
    async with Session() as s:
        panels = []
        for pat in pats:
            panels += list((await s.scalars(select(VpnPanel).where(
                VpnPanel.base_url.ilike(f"%{pat}%"), VpnPanel.enabled == True))).all())  # noqa: E712
    seen = set()
    for p in panels:
        key = (p.base_url, p.username)
        if key in seen:
            continue
        seen.add(key)
        host = p.base_url.split("//")[-1].split("/")[0]
        print(f"\n— {host}  (پنل {p.id}، نوع {p.panel_key}، ربات {p.bot_id})")
        c1 = make_panel_client(p)
        d1, e1 = await _t(c1.login())
        print(f"   login#1: {d1:5.1f}s{'  !'+e1 if e1 else ''}   {_exp_info(c1)}")
        c2 = make_panel_client(p)
        d2, e2 = await _t(c2.login())
        verdict = "✅ از کش" if d2 < 1.0 and not e2 else ("❌ معطلِ محدودکننده/لاگینِ دوباره" if d2 >= 5 else "⚠️ کُند")
        print(f"   login#2: {d2:5.1f}s{'  !'+e2 if e2 else ''}   → {verdict}")
        for c in (c1, c2):
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                print("   (بستنِ کلاینت ناموفق بود؛ بی‌اهمیت)")
    if not seen:
        print("پنلی با این آدرس‌ها پیدا نشد.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
