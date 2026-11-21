#!/usr/bin/env python3
"""scripts/find_hosted_by_bot.py — اپِ میزبانی‌شدهٔ یک ربات را از روی یوزرنیمش پیدا می‌کند و کدش را برایِ مشکلِ پنل می‌گردد.

    venv/bin/python scripts/find_hosted_by_bot.py Xnjxjdjsidiebot

خروجی: id/نام/مالکِ اپ، گزارشِ «بررسیِ هوشمند» و خط‌هایی از کد که /api/groups یا groups را صدا می‌زنند.
اگر ربات روی سرورِ میزبانیِ بات‌ساز نباشد «پیدا نشد» می‌دهد (یعنی کدش جای دیگری است).
"""
import asyncio
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


async def main(username: str) -> int:
    for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8", errors="ignore"):
        if "=" in ln and not ln.lstrip().startswith("#"):
            k, _, v = ln.strip().partition("=")
            os.environ.setdefault(k, v.strip("'\""))
    from sqlalchemy import select
    from config import Session
    from models import HostedApp
    import hosted_ai_fix as H
    c = H.VipClient()
    async with Session() as s:
        apps = (await s.execute(select(HostedApp.id, HostedApp.name, HostedApp.owner_id, HostedApp.status))).all()
    want = username.lstrip("@").lower()
    for app_id, name, owner, st in apps:
        ok, d = await c._req("GET", f"/api/hosting/admin/{app_id}/botlink")
        if ok and isinstance(d, dict) and str(d.get("username") or "").lower() == want:
            print(f"✅ پیدا شد: id={app_id} نام={name} مالک={owner} وضعیت={st}")
            rep, texts = await H.diagnose(c, app_id)
            print(re.sub(r"<[^>]+>", "", rep.text()))
            print("\n— خط‌هایِ مربوط به groups در کد —")
            for p, t in texts.items():
                for n, line in enumerate(t.splitlines(), 1):
                    if re.search(r"api/groups|groups\.read|/api/admin/token|group_ids", line):
                        print(f"{p}:{n}: {line.strip()[:160]}")
            return 0
    print("❌ این ربات بینِ اپ‌هایِ میزبانیِ بات‌ساز نیست؛ کدش روی سرورِ دیگری است.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "")))
