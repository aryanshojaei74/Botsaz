#!/usr/bin/env python3
"""
scripts/diag_wager.py — تشخیص فقط‌خواندنی «در گروه نوشتم تاس ۲۰۰۰۰ و هیچ واکنشی نبود» (PATCH_WAGER_DIAG_V1).

هیچ‌چیز را تغییر نمی‌دهد. نشان می‌دهد:
  • گروه متصل به محافظ و روشن/خاموش بودن آن، کلید شرط‌بندی و بازی‌ها، کارمزد/حداقل/حداکثر؛
  • اگر --chat بدهی: آیا شرط‌بندی در همان گروه اجازه دارد (و اگر نه چرا)؛
  • اگر --user بدهی: آیا آن کاربر بن/مسدود آنتی‌اسپم است و موجودی‌اش چقدر است؛
  • وجود جدول شرط‌ها، تعداد شرط‌ها به تفکیک وضعیت و چند شرط آخر.

اجرا از /opt/botsaz:

    venv/bin/python scripts/diag_wager.py --chat -1001234567890 --user 344284721
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select, text  # noqa: E402

from config import Session  # noqa: E402
from models import PlatformUser, PlatformWager, Setting  # noqa: E402
from platform_group_money import blocked_reasons  # noqa: E402

KEYS = (
    ("group_guard_chat_id", ""), ("group_guard_enabled", "off"),
    ("platform_group_betting_enabled", "off"),
    ("platform_wager_xo", "on"), ("platform_wager_rps", "on"), ("platform_wager_dice", "on"),
    ("platform_group_betting_min", "1000"), ("platform_group_betting_max", "0"),
    ("platform_group_betting_fee_enabled", "off"), ("platform_group_betting_fee_pct", "0"),
    ("platform_group_betting_ttl", "300"), ("platform_wager_move_secs", "180"),
)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", type=int, default=None, help="شناسهٔ عددی گروهی که در آن نوشتی (مثل -100…)")
    ap.add_argument("--user", type=int, default=None, help="شناسهٔ عددی تلگرام کسی که نوشت")
    a = ap.parse_args()

    async with Session() as s:
        vals = {}
        for k, d in KEYS:
            r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == k))
            vals[k] = r.value if (r and r.value is not None) else d
    print("── تنظیمات")
    for k, _ in KEYS:
        print(f"  {k:36s} = {vals[k]!r}")
    problems = []
    if vals["platform_group_betting_enabled"] != "on":
        problems.append("کلید اصلی شرط‌بندی خاموش است (تنظیمات شرط‌بندی گروه ← روشن‌کردن)")
    if a.chat is not None:
        problems += blocked_reasons(vals["group_guard_chat_id"], vals["group_guard_enabled"] == "on", a.chat)
    elif not vals["group_guard_chat_id"].strip() or vals["group_guard_enabled"] != "on":
        problems.append("گروه محافظ تنظیم/روشن نیست")

    print("\n── جدول شرط‌ها")
    try:
        async with Session() as s:
            rows = (await s.execute(select(PlatformWager.status, func.count()).group_by(PlatformWager.status))).all()
            print("  به تفکیک وضعیت:", dict(rows) or "— (هنوز شرطی ثبت نشده)")
            last = (await s.scalars(select(PlatformWager).order_by(PlatformWager.id.desc()).limit(5))).all()
            for w in last:
                print(f"  #{w.id} {w.game:4s} {w.status:9s} مبلغ={int(w.amount):,} chat={w.chat_id} سازنده={w.creator_id}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"جدول platform_wagers در دسترس نیست ({type(e).__name__}) — سرویس platform را ریستارت کن تا ساخته شود")

    if a.user is not None:
        async with Session() as s:
            u = await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == a.user))
        print("\n── کاربر", a.user)
        if u is None:
            print("  در PlatformUser وجود ندارد (اولین تعاملش ساخته می‌شود)")
        else:
            print(f"  موجودی={int(u.balance):,}  بن/مسدود={bool(u.is_banned)}  اخطار اسپم={u.spam_warnings}")
            if u.is_banned:
                problems.append("این کاربر در بات‌ساز بن/مسدود آنتی‌اسپم است؛ پیام‌هایش کاملا نادیده گرفته می‌شود")

    print("\n── نتیجه")
    if problems:
        for p in problems:
            print("  ✗", p)
        return 1
    print("  ✓ تنظیمات برای کار کردن شرط‌بندی کافی است؛ اگر باز هم بی‌واکنش بود لاگ platform را ببین:")
    print("    journalctl -u botsaz-platform --since '15 min ago' | grep -i -E 'wager|Traceback'")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
