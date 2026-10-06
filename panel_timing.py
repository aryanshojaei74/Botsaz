"""
panel_timing.py — فقط خواندنی. سرعتِ پنل‌هایِ VPN را همان‌طورِ که مسیرِ خرید می‌رود می‌سنجد:
login (یا توکنِ کش) → لیستِ اینباند/گروه (همان کاری که create_account اول انجام می‌دهد) → خواندنِ یک کاربرِ ساختگی.
هیچ کاربری ساخته/تغییر داده نمی‌شود.

اجرا:
    cd /opt/botsaz && source venv/bin/activate
    python3 panel_timing.py              # ۲۵ پنلِ پرفروشِ ۷ روزِ اخیر
    python3 panel_timing.py --bot 1234   # همهٔ پنل‌هایِ یک ربات (شمارهٔ داخلی/آیدی تلگرام/یوزرنیم)
    python3 panel_timing.py --top 60
"""
import argparse
import asyncio
import datetime as dt
import re
import time

from sqlalchemy import func, select

from config import Session
from models import Bot, Order, VpnPanel


async def _t(coro, limit=25):
    t = time.monotonic()
    try:
        await asyncio.wait_for(coro, limit)
        return time.monotonic() - t, None
    except Exception as e:  # noqa: BLE001
        return time.monotonic() - t, type(e).__name__


async def _one(p, sem):
    from tenant.bots.vpn.vpn_user import _client_from_panel
    out = {"id": p.id, "bot": p.bot_id, "key": p.panel_key, "host": re.sub(r"^https?://", "", p.base_url).split("/")[0][:34]}
    async with sem:
        c = _client_from_panel(p)
        out["login"] = await _t(c.login())
        if hasattr(c, "active_inbound_ids"):
            out["inb"] = await _t(c.active_inbound_ids())
        else:
            out["inb"] = (0.0, "—")
        out["get"] = await _t(c.get_client("zz_probe_nonexistent"))
        # دور دوم: کانکشنِ گرم (همان چیزی که کاربرِ بعدی می‌بیند)
        out["get2"] = await _t(c.get_client("zz_probe_nonexistent"))
    return out


def _f(x):
    d, e = x
    return f"{d:5.1f}s" + (f"!{e[:8]}" if e else "")


async def main(top, bot_arg):
    async with Session() as s:
        if bot_arg:
            a = bot_arg.strip().lstrip("@")
            if a.isdigit():
                n = int(a)
                bots = [await s.get(Bot, n)] if n <= 2147483647 else []
                if not bots or not bots[0]:
                    bots = list((await s.scalars(select(Bot).where(Bot.token.like(f"{n}:%")))).all())
            else:
                bots = list((await s.scalars(select(Bot).where(Bot.username.ilike(f"%{a}%")))).all())
            ids = [b.id for b in bots if b]
            panels = list((await s.scalars(select(VpnPanel).where(VpnPanel.bot_id.in_(ids), VpnPanel.enabled == True))).all())  # noqa: E712
        else:
            since = dt.datetime.utcnow() - dt.timedelta(days=7)
            rows = (await s.execute(
                select(Order.bot_id, Order.panel_key, func.count()).where(Order.created_at >= since, Order.panel_key.isnot(None))
                .group_by(Order.bot_id, Order.panel_key).order_by(func.count().desc()).limit(top))).all()
            panels = []
            for b, k, _n in rows:
                p = await s.scalar(select(VpnPanel).where(VpnPanel.bot_id == b, VpnPanel.panel_key == k, VpnPanel.enabled == True))  # noqa: E712
                if p:
                    panels.append(p)
    print(f"پنل‌هایِ بررسی‌شده: {len(panels)}\n")
    sem = asyncio.Semaphore(6)
    res = await asyncio.gather(*[_one(p, sem) for p in panels])
    print(f"{'bot':>5} {'id':>5} {'نوع':<10} {'هاست':<34} {'login':>9} {'inbound':>9} {'get#1':>9} {'get#2':>9}")
    slow = 0
    for r in sorted(res, key=lambda r: -(r["login"][0] + r["inb"][0] + r["get"][0])):
        tot = r["login"][0] + r["inb"][0] + r["get"][0]
        slow += tot > 4
        print(f"{r['bot']:>5} {r['id']:>5} {r['key']:<10} {r['host']:<34} {_f(r['login']):>9} {_f(r['inb']):>9} {_f(r['get']):>9} {_f(r['get2']):>9}" + ("  ⚠️کند" if tot > 4 else ""))
    print(f"\nپنل با مجموعِ سه مرحله > ۴ ثانیه: {slow} از {len(res)}")
    print("خوانش: login/inbound/get#1 سه تماسِ پشتِ‌هم در خرید است؛ get#2 سرعتِ اتصالِ گرم را نشان می‌دهد.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--bot", default="")
    a = ap.parse_args()
    asyncio.run(main(a.top, a.bot))
