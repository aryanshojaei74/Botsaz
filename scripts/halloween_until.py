#!/usr/bin/env python3
"""
scripts/halloween_until.py — «🎃 تم هالوین» را روشن می‌کند و تا ساعتِ مشخصی (به وقتِ تهران) نگه می‌دارد؛ بعدش خودبه‌خود خاموش می‌شود.

    venv/bin/python scripts/halloween_until.py 15:00          # امروز ساعتِ ۱۵:۰۰ تهران (اگر گذشته باشد: فردا)
    venv/bin/python scripts/halloween_until.py --off          # خاموشِ فوری

فقط تمِ مینی‌اپ (halloween_on/halloween_until) را دست می‌زند؛ بازی‌هایِ گروه/شیرینی/مسابقه دکمهٔ جدا دارند.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import halloween as HW  # noqa: E402

TEHRAN = timedelta(hours=3, minutes=30)


def until_utc(hhmm: str, now_utc: datetime) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    local = now_utc + TEHRAN
    target = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target - TEHRAN


async def main(hhmm: str | None, off: bool) -> None:
    now = datetime.utcnow()
    if off:
        await HW.set_on(False, now=now)
        print("🎃 خاموش شد.")
        return
    until = until_utc(hhmm, now)
    await HW._pset(HW.KEY_ON, "1")
    await HW._pset(HW.KEY_UNTIL, until.isoformat())
    print(f"🎃 روشن شد؛ تا {(until + TEHRAN):%Y-%m-%d %H:%M} به وقتِ تهران.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("hhmm", nargs="?", help="ساعتِ پایان به وقتِ تهران، مثل 15:00")
    ap.add_argument("--off", action="store_true")
    a = ap.parse_args()
    if not a.off and not a.hhmm:
        ap.error("ساعت یا --off لازم است")
    asyncio.run(main(a.hhmm, a.off))
