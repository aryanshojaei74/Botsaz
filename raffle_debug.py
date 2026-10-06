#!/usr/bin/env python3
"""
raffle_debug.py — چرا رأی ثبت نمی‌شود (و لایک نمی‌خورد)؟

ری‌اکشنِ 👍 دقیقاً *بعد از* ثبتِ موفقِ رأی زده می‌شود. پس «لایک نمی‌کند»
یعنی رأی اصلاً ثبت نشده. مسیرِ ثبتِ رأی شش شرط دارد و **همه‌شان بی‌صدا
رد می‌کنند** (فقط `return`، بدونِ هیچ پیامی به کاربر یا لاگ):

    ۱) گروه در پنل وصل شده باشد        (bound_chat_id)
    ۲) پیامِ ریپلای‌شده یک قرعه‌کشیِ ثبت‌شده باشد
    ۳) وضعیتِ قرعه‌کشی هنوز "open" باشد
    ۴) الان داخلِ بازهٔ زمانی باشیم      (poll_started_at + duration)
    ۵) عدد معتبر باشد
    ۶) کاربر قبلاً رأی نداده باشد       (اولین رأی می‌چسبد)

این اسکریپت وضعیتِ واقعیِ دیتابیس را نشان می‌دهد تا معلوم شود کدام شرط
رد می‌کند — بدونِ حدس.

اجرا از ریشهٔ پروژه:
    python3 raffle_debug.py
"""
from __future__ import annotations

import asyncio
import datetime
import sys


async def main() -> int:
    from sqlalchemy import func, select
    from config import Session
    from models import ChannelRaffle, ChannelRaffleVote

    from platform_bot import get_pset
    gid = await get_pset("raffle_group_id", "")
    cid = await get_pset("raffle_channel_id", "")
    print("\n" + "=" * 66)
    print("۱) اتصالِ گروه/کانال در پنل")
    print("=" * 66)
    print(f"  گروهِ وصل‌شده : {gid or '❌ وصل نشده — بدونِ این هیچ رأیی ثبت نمی‌شود'}")
    print(f"  کانالِ وصل‌شده: {cid or '(اختیاری، وصل نشده)'}")

    now = datetime.datetime.now()
    async with Session() as s:
        rows = (await s.scalars(select(ChannelRaffle)
                                .order_by(ChannelRaffle.id.desc()).limit(8))).all()
        data = []
        for r in rows:
            n = await s.scalar(select(func.count()).select_from(ChannelRaffleVote)
                               .where(ChannelRaffleVote.raffle_id == r.id))
            data.append((r, n or 0))

    if "--close-expired" in sys.argv:
        # === بستنِ دوره‌هایِ منقضی ===
        # دوره‌ای که بازه‌اش تمام شده تا ابد status="open" می‌ماند و در
        # فهرست قاطی می‌شود. این گزینه آن‌ها را می‌بندد (رأی‌هایشان
        # دست‌نخورده می‌ماند و از پنل قابلِ مرورند).
        closed = 0
        async with Session() as s:
            for r in (await s.scalars(select(ChannelRaffle)
                                      .where(ChannelRaffle.status == "open"))).all():
                if now > r.poll_started_at + datetime.timedelta(hours=r.duration_hours):
                    r.status = "expired"
                    closed += 1
            await s.commit()
        print(f"\n✅ {closed} دورهٔ منقضی بسته شد.\n")

    print("\n" + "=" * 66)
    print("۲) آخرین قرعه‌کشی‌ها")
    print("=" * 66)
    if not data:
        print("  هیچ قرعه‌کشی‌ای ثبت نشده. یعنی ریپلایِ «N ساعت» هرگز نگرفته.")
        print("  ← احتمالاً گروه وصل نیست، یا Privacy Mode روشن است.")
        return 0

    for r, votes in data:
        end = r.poll_started_at + datetime.timedelta(hours=r.duration_hours)
        live = r.poll_started_at <= now <= end
        accepting = live and r.status == "open"
        print(f"\n  ── #{r.id}  «{(r.topic or '')[:40]}»")
        print(f"     چت      : {r.chat_id}"
              + ("" if str(r.chat_id) == str(gid) else "   ⚠️ با گروهِ وصل‌شده فرق دارد!"))
        print(f"     حالت    : {r.mode}")
        print(f"     وضعیت   : {r.status}"
              + ("" if r.status == "open" else "   ← رأیِ جدید قبول نمی‌شود"))
        print(f"     بازه    : {r.poll_started_at:%m-%d %H:%M} تا {end:%m-%d %H:%M}"
              f"  ({r.duration_hours:g} ساعت)")
        if not live:
            if now > end:
                mins = int((now - end).total_seconds() // 60)
                print(f"     ⏰ بازه {mins} دقیقه پیش تمام شده ← رأی‌ها بی‌صدا رد می‌شوند")
            else:
                print("     ⏰ بازه هنوز شروع نشده")
        print(f"     رأی‌ها  : {votes}")
        print(f"     ➜ الان رأی می‌پذیرد؟ {'✅ بله' if accepting else '❌ خیر'}")

    print("\n" + "-" * 66)
    live_ones = [r for r, _ in data
                 if r.status == "open"
                 and r.poll_started_at <= now <= r.poll_started_at
                 + datetime.timedelta(hours=r.duration_hours)]
    if live_ones:
        print(f"✅ {len(live_ones)} قرعه‌کشیِ باز وجود دارد.")
        print("   اگر باز هم لایک نمی‌خورد، یعنی رأی می‌رسد ولی ری‌اکشن رد می‌شود:")
        print("     • تنظیماتِ گروه → Reactions → مطمئن شو 👍 مجاز است")
        print("     • ربات باید در گروه ادمین باشد")
        print("   یا اصلاً پیام به ربات نمی‌رسد:")
        print("     • @BotFather → Group Privacy → Turn off")
        print("     • کاربر باید *ریپلای* بزند رویِ همان پیامِ سؤال")
    else:
        print("❌ هیچ قرعه‌کشیِ بازی نیست — به همین دلیل هیچ رأیی ثبت نمی‌شود.")
        print("   یک پیامِ تازه پست کن و رویش ریپلای بزن «۳ ساعت».")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
