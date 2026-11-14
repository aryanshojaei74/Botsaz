#!/usr/bin/env python3
"""
scripts/diag_smm_cancel.py — شکلِ واقعیِ پاسخِ پنلِ SMM به cancel / status / refill (=== PATCH_DIAG_SMM_CANCEL_V1 ===)

گزارشِ فدایی: لغو با WhaleSMM درست جواب نمی‌گیرد و خطایِ پنل به مشتری نمی‌رسد. این اسکریپت با یک شناسهٔ سفارشِ *غیرِ واقعی*
(پیش‌فرض ۹۹۹۹۹۹۹۹۹۹۹۹) چند اکشن را می‌زند و پاسخِ خامِ پنل (کدِ HTTP + بدنه) را چاپ می‌کند تا قالبِ خطا دیده شود.
چون شناسه ساختگی است هیچ سفارشِ واقعی لغو/ریفیل نمی‌شود. کلیدِ API چاپ نمی‌شود.

    cd /opt/botsaz && venv/bin/python scripts/diag_smm_cancel.py whalesmm [شناسهٔ ساختگی]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main(needle: str, fake: str) -> int:
    from sqlalchemy import select
    from config import Session
    from models import SmmPanel
    from tenant.bots.virtual_service import smm_api
    async with Session() as s:
        panels = (await s.scalars(select(SmmPanel).where(SmmPanel.api_url.contains(needle)))).all()
    if not panels:
        print(f"پنلی با آدرسِ شامل «{needle}» پیدا نشد.")
        return 2
    p = panels[0]
    print(f"پنل #{p.id} — {p.api_url}  (شناسهٔ ساختگی: {fake})")
    calls = [
        ("cancel (orders)", {"action": "cancel", "orders": fake}),
        ("cancel (order)", {"action": "cancel", "order": fake}),
        ("status (order)", {"action": "status", "order": fake}),
        ("status (orders)", {"action": "status", "orders": fake}),
        ("refill (order)", {"action": "refill", "order": fake}),
        ("refill_status", {"action": "refill_status", "refill": fake}),
    ]
    for label, extra in calls:
        resp = await smm_api._post_ex(p.api_url, {"key": p.api_key, **extra})
        body = resp.data if resp.data is not None else getattr(resp, "text", None)
        try:
            shown = json.dumps(body, ensure_ascii=False)[:400]
        except Exception:
            shown = str(body)[:400]
        print(f"\n── {label}\n   HTTP={resp.status}  exc={getattr(resp, 'exc', None)}\n   {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2] if len(sys.argv) > 2 else "999999999999")))
