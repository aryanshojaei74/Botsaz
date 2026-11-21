#!/usr/bin/env python3
"""scripts/repair_orphan_addons.py — PATCH_TOKEN_SWAP_NONFK_V1

قبل از اصلاحِ تعویضِ توکن، افزونهٔ پولیِ «بازی‌هایِ گروهی» (BotAddon) بعد از تعویضِ توکن به ردیفِ حذف‌شدهٔ رباتِ قدیم می‌ماند و رباتِ جدید
آن را از دست می‌داد. این ابزار افزونه‌هایِ «یتیم» (bot_id که دیگر ربات نیست) را پیدا می‌کند و اگر مالکِ پرداخت‌کننده (paid_by) دقیقاً یک ربات
بدونِ آن افزونه دارد، به همان ربات وصلش می‌کند.

    venv/bin/python scripts/repair_orphan_addons.py            # فقط گزارش
    venv/bin/python scripts/repair_orphan_addons.py --apply    # اعمال (فقط مواردِ بی‌ابهام)

بازی‌هایِ باز (pending/playing)ِ یتیم فقط گزارش می‌شوند؛ مبلغشان رزرو شده و باید دستی بررسی شود.
"""
import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


async def move_wagers(old: int, new: int, apply: bool) -> int:
    """بازی‌هایِ بازِ یتیمِ رباتِ قدیم را به رباتِ جدید وصل می‌کند تا سویِ‌پِ انقضا مبلغِ رزروشده را به کیف‌پولِ بازیکن‌ها برگرداند."""
    from sqlalchemy import select, update
    from config import Session
    from models import Bot, PlatformWager
    async with Session() as s:
        if not await s.get(Bot, new) or await s.get(Bot, old):
            print("❌ شناسهٔ جدید باید ربات باشد و شناسهٔ قدیم نباید دیگر ربات باشد؛ چیزی تغییر نکرد.")
            return 1
        rows = (await s.scalars(select(PlatformWager).where(PlatformWager.bot_id == old))).all()
        for w in rows:
            print(f"  بازی #{w.id} وضعیت={w.status} مبلغ={int(w.amount):,} سازنده={w.creator_id} حریف={w.opponent_id or '-'}")
        print(f"{len(rows)} بازی از bot_id={old}")
        if apply and rows:
            await s.execute(update(PlatformWager).where(PlatformWager.bot_id == old).values(bot_id=new))
            await s.commit()
            print(f"✅ به bot_id={new} منتقل شد؛ باز‌های منقضی در چرخهٔ بعدیِ سویِ‌پ برگشت می‌خورند.")
        elif not apply:
            print("(فقط گزارش؛ برایِ اعمال: --apply)")
    return 0


async def main(apply: bool) -> int:
    from sqlalchemy import func, select
    from config import Session
    from models import Bot, BotAddon, PlatformWager
    async with Session() as s:
        bot_ids = set((await s.scalars(select(Bot.id))).all())
        orphans = [a for a in (await s.scalars(select(BotAddon))).all() if a.bot_id not in bot_ids]
        print(f"افزونهٔ یتیم: {len(orphans)}")
        fixed = 0
        for a in orphans:
            cands = (await s.scalars(select(Bot).where(Bot.owner_id == a.paid_by, Bot.status != "archived"))).all()
            cands = [b for b in cands
                     if not await s.scalar(select(BotAddon.id).where(BotAddon.bot_id == b.id, BotAddon.addon == a.addon))]
            tag = f"addon={a.addon} bot_id(قدیمی)={a.bot_id} paid_by={a.paid_by} مبلغ={int(a.amount or 0):,}"
            if len(cands) == 1:
                print(f"  ✅ {tag} → ربات @{cands[0].username} (id={cands[0].id})")
                if apply:
                    a.bot_id = cands[0].id
                    fixed += 1
            else:
                print(f"  ⚠️ {tag} → {len(cands)} نامزد؛ دستی وصل کن: " + ", ".join(f"@{b.username}(id={b.id})" for b in cands[:5]))
        wag = (await s.execute(select(PlatformWager.bot_id, func.count()).where(
            PlatformWager.bot_id != 0, PlatformWager.status.in_(("pending", "playing"))).group_by(PlatformWager.bot_id))).all()
        for bid, n in wag:
            if bid not in bot_ids:
                print(f"  ⚠️ {n} بازیِ بازِ یتیم برایِ bot_id={bid} (مبلغِ رزروشده؛ دستی بررسی کن)")
        if apply:
            await s.commit()
            print(f"\nاعمال شد: {fixed} افزونه. تا ~۱۵ ثانیه بعد در ربات دیده می‌شود.")
        else:
            print("\n(فقط گزارش؛ برایِ اعمال: --apply)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--move-wagers", metavar="OLD:NEW", help="بازی‌هایِ یتیمِ رباتِ قدیم را به رباتِ جدید وصل کن (مثلاً 1170:1607)")
    a = ap.parse_args()
    if a.move_wagers:
        o, n = a.move_wagers.split(":")
        raise SystemExit(asyncio.run(move_wagers(int(o), int(n), a.apply)))
    raise SystemExit(asyncio.run(main(a.apply)))
