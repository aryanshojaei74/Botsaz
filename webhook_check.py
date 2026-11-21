"""
webhook_check.py — فقط خواندنی. وضعیتِ webhookِ همهٔ رباتِ فعال را از تلگرام می‌پرسد (getWebhookInfo) و خلاصه می‌دهد:
صفِ آپدیت‌هایِ نرسیده (pending)، آخرین خطایِ تحویل، رباتِ بدونِ webhook، آدرسِ اشتباه، توکنِ باطل.
توکن‌ها هرگز چاپ نمی‌شوند (فقط شمارهٔ ربات/یوزرنیم و ۶ حرفِ آخر).

اجرا:
    cd /opt/botsaz && source venv/bin/activate
    python3 webhook_check.py              # همهٔ رباتِ فعال
    python3 webhook_check.py --limit 100  # فقط ۱۰۰ ربات (تست سریع)
"""
import argparse
import asyncio
import collections
import datetime
import os
import re
import sys
import time

import aiohttp
from sqlalchemy import select

from config import Session
from models import Bot, Setting

TG_API = os.environ.get("TG_API_BASE", "https://api.telegram.org")


async def _info(cs, sem, b):
    async with sem:
        try:
            async with cs.get(f"{TG_API}/bot{b.token}/getWebhookInfo") as r:
                j = await r.json(content_type=None)
                return b, r.status, (j or {}).get("result") or {}, (j or {}).get("description")
        except Exception as e:  # noqa: BLE001
            return b, 0, {}, type(e).__name__


async def main(limit: int, hours: float):
    from config import WEBHOOK_BASE
    async with Session() as s:
        base = await s.scalar(select(Setting.value).where(Setting.bot_id == 0, Setting.key == "webhook_base"))
        q = select(Bot).where(Bot.status == "active", Bot.archived_at.is_(None))
        bots = [b for b in (await s.scalars(q)).all()
                if b.token and re.match(r"^\d+:", b.token) and "#merged" not in (b.username or "")]
    base = (base or WEBHOOK_BASE or "").rstrip("/")
    host = re.sub(r"^https?://", "", base).split("/")[0]
    if limit:
        bots = bots[:limit]
    print(f"رباتِ فعال: {len(bots)} | آدرسِ مورد انتظار webhook: {host or '— (تنظیم نشده)'}")
    sem = asyncio.Semaphore(15)
    t0 = time.monotonic()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as cs:
        res = await asyncio.gather(*[_info(cs, sem, b) for b in bots])
    print(f"پرسش از تلگرام: {time.monotonic() - t0:.1f}s\n")

    no_hook, wrong, revoked, neterr = [], [], [], []
    pend, errs, ips, maxc = [], collections.Counter(), collections.Counter(), collections.Counter()
    cut = time.time() - hours * 3600
    recent_err = []
    for b, status, info, desc in res:
        tag = f"#{b.id} @{b.username} …{b.token[-6:]}"
        if status == 0:
            neterr.append((tag, desc)); continue
        if status in (401, 404):
            revoked.append(tag); continue
        url = info.get("url") or ""
        if not url:
            no_hook.append(tag); continue
        if host and host not in url and not re.search(r"/webhook/", url):
            wrong.append((tag, re.sub(r"/bot?\d+:[\w-]+", "/…", url)[:80]))
        pc = int(info.get("pending_update_count") or 0)
        if pc:
            pend.append((pc, tag))
        ips[info.get("ip_address") or "—"] += 1
        maxc[info.get("max_connections") or "—"] += 1
        le = int(info.get("last_error_date") or 0)
        if le and le >= cut:
            msg = re.sub(r"\d+", "N", str(info.get("last_error_message") or ""))[:70]
            errs[msg] += 1
            recent_err.append((le, tag, str(info.get("last_error_message"))[:70]))
    print(f"✅ webhook سالم و بدونِ صف: {len(res) - len(no_hook) - len(wrong) - len(revoked) - len(neterr) - len(pend)}")
    print(f"⏳ رباتِ دارایِ صفِ نرسیده (pending>0): {len(pend)}  | مجموعِ آپدیتِ در صف: {sum(p for p, _ in pend)}")
    for p, t in sorted(pend, reverse=True)[:12]:
        print(f"     {p:>6}  {t}")
    print(f"🚫 بدونِ webhook: {len(no_hook)}" + ("".join(f"\n     {t}" for t in no_hook[:10])))
    print(f"⚠️ آدرسِ webhook با دامنهٔ مورد انتظار نمی‌خواند: {len(wrong)}" +
          "".join(f"\n     {t}  → {u}" for t, u in wrong[:10]))
    print(f"🔑 توکنِ باطل/ربات حذف‌شده در تلگرام: {len(revoked)}" + "".join(f"\n     {t}" for t in revoked[:10]))
    print(f"🌐 خطایِ شبکه در پرسش: {len(neterr)}")
    print(f"\nخطایِ تحویلِ webhook در {hours:g} ساعتِ اخیر (تعداد رباتِ دچار): {len(recent_err)}")
    for m, n in errs.most_common(8):
        print(f"   {n:>4}×  {m}")
    for le, t, m in sorted(recent_err, reverse=True)[:6]:
        print(f"     {datetime.datetime.fromtimestamp(le):%H:%M:%S}  {t}  {m}")
    print(f"\nIPِ مقصدِ webhook (از دیدِ تلگرام): {dict(ips.most_common(5))}")
    print(f"max_connections تنظیم‌شده: {dict(maxc.most_common(5))}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--hours", type=float, default=3)
    a = ap.parse_args()
    asyncio.run(main(a.limit, a.hours))
