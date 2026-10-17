"""
trial_overuse_scan.py — فقط خواندنی. آخرین تست‌هایِ رایگانِ یک ربات را از «خودِ پنل» می‌خواند
و نشان می‌دهد کدام‌ها از سقفِ حجم رد شده‌اند و آیا هنوز فعال‌اند (برایِ گزارشِ «تست قطع نمی‌شود»).

اجرا:
    cd /opt/botsaz && source venv/bin/activate
    python3 trial_overuse_scan.py vpnkong_bot          # ۶۰ تستِ آخر
    python3 trial_overuse_scan.py vpnkong_bot 150
"""
import asyncio
import sys

from sqlalchemy import select

from config import Session
from models import Bot, PanelAccount, VpnPanel

MB = 1024 * 1024


async def main(uname, n):
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
    async with Session() as s:
        b = await s.scalar(select(Bot).where(Bot.username.ilike(uname.lstrip("@"))))
        if not b:
            print("ربات پیدا نشد."); return
        accs = list((await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == b.id, PanelAccount.plan_title == "تست رایگان")
            .order_by(PanelAccount.id.desc()).limit(n))).all())
        panels = {p.panel_key: p for p in (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == b.id))).all()}
    clients, rows = {}, []
    st = dict(checked=0, over=0, over_active=0, missing=0, nolimit=0, err=0)
    sem = asyncio.Semaphore(4)

    async def one(a):
        p = panels.get(a.panel_key)
        if not p:
            return
        c = clients.setdefault(a.panel_key, make_panel_client(p))
        async with sem:
            try:
                u = await asyncio.wait_for(c.get_usage(a.email), 40)
            except Exception as e:  # noqa: BLE001
                st["err"] += 1; return
        st["checked"] += 1
        if not u:
            st["missing"] += 1; return
        used = (u.get("up") or 0) + (u.get("down") or 0)
        total = u.get("total") or 0
        if not total:
            st["nolimit"] += 1
            rows.append((a, a.panel_key, "بدونِ سقف!", used, total, u.get("enable"))); return
        if used > total * 1.05:
            st["over"] += 1
            if u.get("enable"):
                st["over_active"] += 1
            rows.append((a, a.panel_key, "ردشده", used, total, u.get("enable")))

    await asyncio.gather(*[one(a) for a in accs])
    print(f"ربات @{b.username} | بررسی‌شده={st['checked']} خطا={st['err']} حذف‌شده‌روی‌پنل={st['missing']}")
    print(f"از سقف رد شده={st['over']} | از این‌ها هنوز فعال={st['over_active']} | بدونِ سقف={st['nolimit']}")
    for a, pk, why, used, total, en in sorted(rows, key=lambda r: -r[3])[:25]:
        ratio = f"{used / total:.1f}x" if total else "-"
        print(f"  اکانت {a.id} کاربر {a.user_telegram_id} پنل={pk} {why}: مصرف={used / MB:.0f}MB سقف={total / MB:.0f}MB ({ratio}) فعال={en}")
    for c in clients.values():
        try:
            await c.close()
        except Exception:  # noqa: BLE001
            print("(بستنِ کلاینت ناموفق؛ بی‌اهمیت)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("استفاده: python3 trial_overuse_scan.py USERNAME_RABAT [تعداد]")
    else:
        asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 60))
