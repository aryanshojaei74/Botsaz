#!/usr/bin/env python3
"""
scripts/diag_webhooks.py — «آپدیت‌ها از تلگرام دیر می‌رسند؟» (PATCH_WEBHOOK_DIAG_V1)

برای هر رباتِ فعال، getWebhookInfo را از تلگرام می‌پرسد و نشان می‌دهد:
  • pending  = تعدادِ آپدیتِ تحویل‌نشده (عددِ بزرگ یعنی تلگرام نمی‌تواند به سرورِ ما برساند)
  • last_error = آخرین خطای تلگرام در تحویل (مثلاً «Read timeout expired» / «Connection timed out»)
  • max_conn، ip

    cd /opt/botsaz && python3 scripts/diag_webhooks.py              # همهٔ رباتِ فعال؛ فقط مشکل‌دارها را کامل نشان می‌دهد
    python3 scripts/diag_webhooks.py whale                          # فقط رباتی که یوزرنیمش «whale» دارد
    python3 scripts/diag_webhooks.py --all                          # همه را کامل چاپ کن

توکن‌ها چاپ نمی‌شوند.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def summarize(info: dict, now: float | None = None) -> dict:
    """خروجیِ getWebhookInfo ⇒ {pending, err, err_age_min, max_conn, ip, problem}. problem=True اگر pending>0 یا خطای تازه (≤۶۰ دقیقه) باشد."""
    now = time.time() if now is None else now
    r = info.get("result") or {}
    err_ts = r.get("last_error_date")
    age = round((now - err_ts) / 60, 1) if err_ts else None
    pending = int(r.get("pending_update_count") or 0)
    recent_err = bool(err_ts and age is not None and age <= 60)
    return {"pending": pending, "err": r.get("last_error_message") or "", "err_age_min": age,
            "max_conn": r.get("max_connections"), "ip": r.get("ip_address") or "",
            "url_set": bool(r.get("url")), "problem": pending > 0 or recent_err}


async def _fetch(session, token: str) -> dict:
    try:
        async with session.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=15) as resp:
            return await resp.json()
    except Exception as e:  # noqa: BLE001
        return {"result": {"last_error_message": f"fetch failed: {type(e).__name__}", "last_error_date": int(time.time())}}


async def main(argv: list[str]) -> int:
    import aiohttp
    from sqlalchemy import select
    from config import Session
    from models import Bot

    show_all = "--all" in argv
    names = [a.lower() for a in argv if not a.startswith("--")]
    async with Session() as s:
        rows = (await s.execute(select(Bot.username, Bot.token).where(Bot.status == "active"))).all()
    rows = [(u or "?", t) for u, t in rows if t and (not names or any(n in (u or "").lower() for n in names))]
    sem = asyncio.Semaphore(8)
    async with aiohttp.ClientSession() as http:
        async def one(u, t):
            async with sem:
                return u, summarize(await _fetch(http, t))
        res = await asyncio.gather(*(one(u, t) for u, t in rows))
    bad = [(u, x) for u, x in res if x["problem"]]
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  رباتِ بررسی‌شده: {len(res)}  |  با مشکل (pending>0 یا خطای تازه): {len(bad)}")
    show = res if (show_all or names) else bad
    for u, x in sorted(show, key=lambda p: -p[1]["pending"]):
        age = f"{x['err_age_min']} دقیقه پیش" if x["err_age_min"] is not None else "—"
        print(f"  @{u:<28} pending={x['pending']:<5} max_conn={x['max_conn']} ip={x['ip']} "
              f"آخرین خطا: {x['err'] or '—'} ({age})")
    if not show:
        print("  هیچ ربات با آپدیتِ معلق یا خطای تازه نیست ⇒ تلگرام آپدیت‌ها را سالم تحویل می‌دهد.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
