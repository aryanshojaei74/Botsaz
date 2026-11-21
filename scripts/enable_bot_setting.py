#!/usr/bin/env python3
"""
scripts/enable_bot_setting.py — یک تنظیمِ مخفیِ «فقط برایِ یک ربات» را روشن/خاموش می‌کند (بدونِ دکمه در پنلِ همهٔ مالک‌ها).

    venv/bin/python scripts/enable_bot_setting.py --bot-username ExpressPanel_Bot --key group_transfer_by_username --value 1

کلیدهایِ مجاز:
  group_transfer_by_username  انتقالِ گروهی با ریپلای رویِ پیامِ خودِ فرستنده که یوزرنیمِ گیرنده در آن است (درخواستِ سینا)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from config import Session  # noqa: E402
from models import Bot as BotRow, Setting  # noqa: E402

ALLOWED = {"group_transfer_by_username"}


async def main(username: str, key: str, value: str) -> None:
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.username == username.lstrip("@")))
        if not row:
            print("ربات پیدا نشد:", username)
            return
        st = await s.scalar(select(Setting).where(Setting.bot_id == row.id, Setting.key == key))
        if st:
            st.value = value
        else:
            s.add(Setting(bot_id=row.id, key=key, value=value))
        await s.commit()
    print(f"✅ {key}={value} برایِ @{username.lstrip('@')} (id={row.id}) ثبت شد. تا چند ثانیه کش می‌ماند؛ ریستارت لازم نیست.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-username", required=True)
    ap.add_argument("--key", required=True, choices=sorted(ALLOWED))
    ap.add_argument("--value", default="1", choices=("0", "1"))
    a = ap.parse_args()
    asyncio.run(main(a.bot_username, a.key, a.value))
