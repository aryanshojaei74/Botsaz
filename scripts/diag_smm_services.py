#!/usr/bin/env python3
"""
scripts/diag_smm_services.py — «چرا توضیحاتِ سرویس‌هایِ پنلِ SMM نمی‌آید؟» (=== PATCH_DIAG_SMM_SERVICES_V1 ===)

برایِ پنل(های) SMMِ ثبت‌شده که آدرسشان شاملِ متنِ داده‌شده باشد، خروجیِ خامِ «services» را می‌گیرد و می‌گوید:
  • چه فیلدهایی دارد و کدام‌ها شبیهِ «توضیحات»اند و در چند درصدِ سرویس‌ها پر هستند؛
  • اگر هیچ فیلدِ توضیحاتی نبود، یعنی خودِ پنل توضیح نمی‌فرستد (کاری از ربات برنمی‌آید).
کلیدِ API هرگز چاپ نمی‌شود. فقط می‌خواند.

    cd /opt/botsaz && venv/bin/python scripts/diag_smm_services.py whalesmm [--probe]
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STD = {"service", "name", "type", "category", "rate", "min", "max", "dripfeed", "refill", "cancel", "id"}


async def main(needle: str, probe: bool = False) -> int:
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
        # === PATCH_SMM_DOC_TYPES_V1 === مستنداتِ WhaleSMM: type = Default | Package | SEO | Custom Comments |
        # Mentions Hashtag | Comment Likes | Subscriptions. ربات فقط «Default» (لینک + تعداد) را درست ثبت می‌کند.
        types: dict[str, int] = {}
        for it in items:
            t = str(it.get("type") or "—")
            types[t] = types.get(t, 0) + 1
        print("\nنوعِ سرویس‌ها (type):")
        for t, n in sorted(types.items(), key=lambda kv: -kv[1]):
            note = "" if t.lower() in ("default", "—") else "   ← ربات فقط لینک+تعداد می‌فرستد؛ این نوع پارامترِ اضافه می‌خواهد"
            print(f"  {t:<22} {n:>6}{note}")
        print(f"ریفیل دارد: {sum(1 for it in items if it.get('refill') is True)} | "
              f"لغو دارد: {sum(1 for it in items if it.get('cancel') is True)}")
        # سرویس‌هایی که *این ربات* واقعاً فعال کرده و نوعشان Default نیست (اینها هنگامِ سفارش از پنل خطا می‌گیرند)
        odd = {str(it.get("service") or it.get("id")): str(it.get("type")) for it in items
               if str(it.get("type") or "default").lower() != "default"}
        if odd:
            from models import SmmService
            async with Session() as s2:
                mine = (await s2.scalars(select(SmmService).where(
                    SmmService.bot_id == p.bot_id, SmmService.is_active == True,   # noqa: E712
                    SmmService.service_id.in_(list(odd))))).all()
            mine = [m for m in mine if m.panel_pk in (None, p.id)]
            print(f"\nسرویس‌هایِ فعالِ این رباتِ غیر-Default: {len(mine)}")
            for m in mine[:40]:
                print(f"  • #{m.service_id} [{odd.get(str(m.service_id))}] {str(m.name)[:70]}")
        print("\nسه سرویسِ نمونه (نام):")
        for it in items[:3]:
            print("  •", str(it.get("name"))[:100])
        if probe:
            await _probe(p, items[0])
    return 0


async def _probe(p, first: dict) -> None:
    """=== PATCH_DIAG_SMM_DESC_PROBE_V1 === بعضی پنل‌ها توضیحات را فقط با اکشن/پارامترِ دیگری می‌دهند.
    چند حالتِ رایج را امتحان می‌کنیم و فقط شکلِ پاسخ (نامِ فیلدها / ۱۲۰ کاراکترِ اول) را چاپ می‌کنیم."""
    from tenant.bots.virtual_service import smm_api
    sid = first.get("service") or first.get("id")
    tries = [
        ("services + details=1", {"action": "services", "details": 1}),
        ("services + full=1", {"action": "services", "full": 1}),
        ("services + description=1", {"action": "services", "description": 1}),
        ("service (id)", {"action": "service", "service": sid, "id": sid}),
        ("service_details", {"action": "service_details", "service": sid}),
        ("service_info", {"action": "service_info", "service": sid}),
        ("descriptions", {"action": "descriptions"}),
        ("categories", {"action": "categories"}),
    ]
    print("\n── probe: امتحانِ اکشن‌هایِ دیگر برایِ توضیحات ──")
    for label, extra in tries:
        data = {"key": p.api_key, **extra}
        resp = await smm_api._post_ex(p.api_url, data, 40)
        d = resp.data
        if isinstance(d, list) and d and isinstance(d[0], dict):
            shape = "list[dict] keys=" + ",".join(sorted(d[0].keys()))[:160]
        elif isinstance(d, dict):
            shape = "dict keys=" + ",".join(sorted(d.keys()))[:160]
        else:
            shape = (resp.text or "")[:120].replace("\n", " ")
        print(f"  {label:<26} HTTP={resp.status}  {shape}")


if __name__ == "__main__":
    _args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(asyncio.run(main(_args[0] if _args else "", probe="--probe" in sys.argv)))
