#!/usr/bin/env python3
"""رمزنگاریِ رمزِ پنل‌هایِ VPN که از قبل متنِ ساده در دیتابیس هستند (PATCH_SECRET_BOX_V1).

پیش‌شرط: BOTSAZ_SECRET_KEY (یا PLATFORM_BOT_TOKEN) ست باشد؛ **کلید را جایِ امن نگه دار** — بدونِ آن رمزها خوانده نمی‌شوند.
پیش‌فرض فقط گزارش می‌دهد؛ با --apply می‌نویسد (و بعدش هر مقدار را باز می‌کند و با اصل مقایسه می‌کند).
یادآوری: برایِ رمزنگاریِ رمزهایِ *تازه* هم باید BOTSAZ_ENCRYPT_SECRETS=1 در .env باشد.
  python3 scripts/encrypt_panel_secrets.py            # فقط گزارش
  python3 scripts/encrypt_panel_secrets.py --apply    # رمزنگاری
"""
from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from sqlalchemy import text  # noqa: E402

import secret_box as SB  # noqa: E402
from config import Session  # noqa: E402

COLS = (("vpn_panels", "password", 200), ("vpn_panels", "api_key", 300))


async def run(apply: bool) -> int:
    if SB._fernet() is None:
        print("❌ کلید ست نیست (BOTSAZ_SECRET_KEY یا PLATFORM_BOT_TOKEN).")
        return 2
    changed = skipped = long_ = 0
    async with Session() as s:
        for table, col, limit in COLS:
            rows = (await s.execute(text(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''"))).all()
            for rid, val in rows:
                if str(val).startswith(SB.PREFIX):
                    skipped += 1
                    continue
                enc = SB.encrypt(str(val))
                if len(enc) > limit:
                    long_ += 1
                    continue
                assert SB.decrypt(enc) == str(val)
                changed += 1
                if apply:
                    await s.execute(text(f"UPDATE {table} SET {col} = :v WHERE id = :i"), {"v": enc, "i": rid})
        if apply:
            await s.commit()
    print(f"{'رمزنگاری شد' if apply else 'قابلِ رمزنگاری'}: {changed} · قبلاً رمزشده: {skipped} · بلندتر از ستون (ماند متنِ ساده): {long_}")
    if not apply and changed:
        print("برایِ اعمال: --apply  (قبلش از دیتابیس بکاپ بگیر)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    sys.exit(asyncio.run(run(ap.parse_args().apply)))
