#!/usr/bin/env python3
"""
scripts/diag_smm_services.py — «چرا توضیحاتِ سرویس‌هایِ پنلِ SMM نمی‌آید؟» (=== PATCH_DIAG_SMM_SERVICES_V1 ===)

برایِ پنل(های) SMMِ ثبت‌شده که آدرسشان شاملِ متنِ داده‌شده باشد، خروجیِ خامِ «services» را می‌گیرد و می‌گوید:
  • چه فیلدهایی دارد و کدام‌ها شبیهِ «توضیحات»اند و در چند درصدِ سرویس‌ها پر هستند؛
  • اگر هیچ فیلدِ توضیحاتی نبود، یعنی خودِ پنل توضیح نمی‌فرستد (کاری از ربات برنمی‌آید).
کلیدِ API هرگز چاپ نمی‌شود. فقط می‌خواند.

    cd /opt/botsaz && venv/bin/python scripts/diag_smm_services.py whalesmm
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STD = {"service", "name", "type", "category", "rate", "min", "max", "dripfeed", "refill", "cancel", "id"}


async def main(needle: str) -> int:
    from sqlalchemy import select
    from config import Session
    from models import SmmPanel
    from tenant.bots.virtual_service import smm_api
    async with Session() as s:
        panels = (await s.scalars(select(SmmPanel).where(SmmPanel.api_url.contains(needle)))).all()
    if not panels:
        print(f"پنلی با آدرسِ شامل «{needle}» پیدا نشد.")
        return 2
    for p in panels:
        print(f"\n══ پنل #{p.id} ربات {p.bot_id} — {p.api_url} ══")
        d = await smm_api._post(p.api_url, {"key": p.api_key, "action": "services"}, timeout=60, retry=True)
        if isinstance(d, dict):
            for k in ("services", "data", "result"):
                if isinstance(d.get(k), list):
                    d = d[k]
                    break
        if not isinstance(d, list) or not d:
            print("پاسخِ لیستِ سرویس نبود:", str(d)[:200], "|", smm_api.last_error(p.api_url))
            continue
        items = [x for x in d if isinstance(x, dict)]
        print(f"تعدادِ سرویس: {len(items)}")
        keys: dict[str, int] = {}
        for it in items:
            for k, v in it.items():
                if v not in (None, "", [], {}):
                    keys[k] = keys.get(k, 0) + 1
        print("فیلدها (تعدادِ سرویسِ دارایِ مقدارِ پر):")
        for k, n in sorted(keys.items(), key=lambda kv: -kv[1]):
            tag = "" if k in STD else "   ← غیراستاندارد"
            print(f"  {k:<24} {n:>6}{tag}")
        ours = [k for k in smm_api._DESC_KEYS if keys.get(k)]
        print("\nفیلدِ توضیحاتی که ربات می‌خواند و پر است:", ours or "— هیچ‌کدام —")
        extra = [k for k in keys if k not in STD and k not in smm_api._DESC_KEYS]
        if extra:
            print("فیلدهایِ دیگر که شاید توضیحات باشد:", extra)
            for k in extra[:6]:
                ex = next((str(it[k]) for it in items if it.get(k)), "")
                print(f"   نمونهٔ {k}: {ex[:120]!r}")
        print("\nسه سرویسِ نمونه (نام):")
        for it in items[:3]:
            print("  •", str(it.get("name"))[:100])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "")))
