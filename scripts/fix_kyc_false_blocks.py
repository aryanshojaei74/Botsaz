#!/usr/bin/env python3
"""
scripts/fix_kyc_false_blocks.py — آزادکردنِ کاربرانی که «الکی» به‌خاطرِ بازی/انتقال/پاداش مسدودِ احراز شده‌اند
(PATCH_KYC_DEPOSIT_WHITELIST_V1).

قبلاً جایزهٔ بازی، دریافتِ انتقال و پاداش جزو «شارژ روزانه» حساب می‌شد و کاربرِ سطح ۱ از سقف رد و مسدود می‌شد. این اسکریپت
برایِ هر کاربرِ مسدودِ «عبور از سقفِ سطح ۱»، مجموعِ *واریزِ واقعی* در ۲۴ ساعتِ پیش از زمانِ مسدودی (و ۲۴ ساعتِ اخیر) را با
منطقِ جدید حساب می‌کند؛ اگر هرکدام ≤ سقفِ سطح ۱ باشد او را «الکی‌مسدود» می‌داند. پیش‌فرض فقط گزارش می‌دهد؛ با --release آزاد می‌کند.

    cd /opt/botsaz && venv/bin/python scripts/fix_kyc_false_blocks.py <یوزرنیمِ ربات> [--release]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from config import Session  # noqa: E402
from models import Bot as BotRow, BotUser, WalletTxn  # noqa: E402
from tenant.shared import kyc as K  # noqa: E402


async def real_deposits(s, bot_id: int, user_id: int, start: dt.datetime, end: dt.datetime) -> int:
    v = await s.scalar(select(func.coalesce(func.sum(WalletTxn.amount), 0)).where(
        WalletTxn.bot_id == bot_id, WalletTxn.user_id == user_id, K.deposit_txn_filter(),
        WalletTxn.created_at >= start, WalletTxn.created_at <= end))
    return int(float(v or 0))


async def main(q: str, release: bool) -> None:
    now = dt.datetime.utcnow()
    async with Session() as s:
        bots = (await s.execute(select(BotRow).where(BotRow.username.ilike(f"%{q}%")))).scalars().all()
        if not bots:
            print("رباتی پیدا نشد.")
            return
        for b in bots:
            cap = await K.cap_for(b.id, 1)
            users = (await s.execute(select(BotUser).where(
                BotUser.bot_id == b.id, BotUser.kyc_blocked == True,           # noqa: E712
                BotUser.kyc_note.like("عبور از سقفِ سطح ۱%")))).scalars().all()
            print(f"\n=== @{b.username} id={b.id} | سقفِ سطح ۱: {cap:,} | مسدودشدهٔ «عبور از سقف»: {len(users)}")
            freed = 0
            for u in users:
                # زمانِ دقیقِ مسدودی ثبت نشده؛ آخرین تراکنشِ کاربر را مرجع می‌گیریم + ۲۴ساعتِ اخیر
                last = await s.scalar(select(func.max(WalletTxn.created_at)).where(
                    WalletTxn.bot_id == b.id, WalletTxn.user_id == u.id)) or now
                a = await real_deposits(s, b.id, u.id, last - dt.timedelta(hours=24), last)
                c = await real_deposits(s, b.id, u.id, now - dt.timedelta(hours=24), now)
                false_block = cap > 0 and max(a, c) <= cap
                print(f"  {u.telegram_id} @{u.username or '—'} | واریزِ واقعیِ ۲۴ساعتِ آخرِ فعالیت: {a:,} | ۲۴ساعتِ اخیر: {c:,} "
                      f"| {'⬅️ الکی‌مسدود' if false_block else 'واقعاً از سقف رد شده'}")
                if false_block and release:
                    u.kyc_blocked = False
                    u.kyc_note = None
                    freed += 1
            if release:
                await s.commit()
                print(f"  ✅ آزاد شد: {freed}")
            else:
                print("  (فقط گزارش؛ برایِ آزادکردن --release بزن)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("bot")
    ap.add_argument("--release", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.bot, a.release))
