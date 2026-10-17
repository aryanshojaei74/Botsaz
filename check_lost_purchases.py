#!/usr/bin/env python3
"""
check_lost_purchases.py — «پول کم شد ولی کانفیگ نیامد» را با داده بررسی می‌کند.

گزارشِ ادمین: «چند بار پیش اومده پول از کیف پول کم شده ولی ساب ساخته و
ارسال نشده». سه سناریویِ کاملاً متفاوت می‌تواند پشتش باشد و هر کدام
راه‌حلِ دیگری دارد — این اسکریپت تفکیکشان می‌کند:

  A) پول کم شد، حسابِ پنل هم ساخته شد → فقط «تحویل» شکست خورده.
     کانفیگ سالم است و در «کانفیگ‌های من» هست.
     ← این همان چیزی است که PATCH_DELIVERY_NEVER_LOST_V1 می‌بندد.

  B) پول کم شد، هیچ حسابی ساخته نشد، عودت هم نخورد → ضررِ واقعیِ مالی.
     ← یعنی مسیرِ عودت هم شکسته و باگِ جدی‌تری داریم.

  C) پول کم شد و عودت خورد → رفتارِ درست، مشکلی نیست.

روش: هر تراکنشِ `kind="purchase"` را برمی‌دارد و می‌گردد ببیند در بازهٔ
±۱۰ دقیقه، برایِ همان کاربر و همان ربات، حسابی ساخته شده یا عودتی خورده.

اجرا از ریشهٔ پروژه:
    python3 check_lost_purchases.py            # ۲۴ ساعتِ گذشته
    python3 check_lost_purchases.py --hours 72
    python3 check_lost_purchases.py --bot 12
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys


WINDOW_MIN = 10          # فاصلهٔ مجازِ بینِ کسرِ وجه و ساختِ حساب

# === اصلاحِ نسخهٔ اول ===
# نسخهٔ اولِ این اسکریپت هر تراکنشِ kind="purchase" را «خریدِ VPN» فرض کرد و
# نبودِ PanelAccount را «ضررِ مالی» شمرد. اما بررسی نشان داد ۱۸ مسیرِ
# مختلف همین kind را می‌نویسند — SMM، فروشگاهِ دیجیتال، همکار، وب‌پنل و
# «افزایشِ حجم» — که هیچ‌کدام قرار نیست حسابِ تازه بسازند. پس آن گزارشِ
# «۳۳ موردِ ضررِ واقعی» تقریباً همه‌اش مثبتِ کاذب بود.
#
# فقط این دو الگو حسابِ تازه می‌سازند (tenant/vpn_user.py):
#     خرید VPN: <نامِ پلن>
#     خرید <n> کانفیگ <gb>G/<days>D
# «افزایش حجم +NG» عمداً بیرون است: حسابِ موجود را شارژ می‌کند.
VPN_NOTE_PREFIXES = ("خرید VPN:", "خرید ")
VPN_NOTE_EXCLUDE = ("افزایش حجم", "خرید:", "خریدِ", "خرید سفارش")


def _is_vpn_purchase(note: str) -> bool:
    n = (note or "").strip()
    if any(n.startswith(x) for x in VPN_NOTE_EXCLUDE):
        return False
    if n.startswith("خرید VPN:"):
        return True
    # «خرید ۲ کانفیگ 50G/30D»
    return n.startswith("خرید ") and "کانفیگ" in n


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--bot", type=int, default=None)
    args = ap.parse_args()

    from sqlalchemy import select, and_
    from config import Session
    from models import WalletTxn, PanelAccount, BotUser, Bot as BotRow

    since = dt.datetime.now() - dt.timedelta(hours=args.hours)
    win = dt.timedelta(minutes=WINDOW_MIN)

    async with Session() as s:
        q = select(WalletTxn).where(
            WalletTxn.kind == "purchase", WalletTxn.created_at >= since)
        if args.bot:
            q = q.where(WalletTxn.bot_id == args.bot)
        purchases = list((await s.scalars(q)).all())

        rows = []
        skipped = []
        for p in purchases:
            if not _is_vpn_purchase(p.note):
                skipped.append(p)
                continue
            u = await s.get(BotUser, p.user_id)
            if not u:
                continue
            tg = u.telegram_id
            lo, hi = p.created_at - win, p.created_at + win

            acct = await s.scalar(select(PanelAccount).where(and_(
                PanelAccount.bot_id == p.bot_id,
                PanelAccount.user_telegram_id == tg,
                PanelAccount.created_at >= lo,
                PanelAccount.created_at <= hi)))

            refund = await s.scalar(select(WalletTxn).where(and_(
                WalletTxn.bot_id == p.bot_id,
                WalletTxn.user_id == p.user_id,
                WalletTxn.kind == "refund",
                WalletTxn.created_at >= p.created_at,
                WalletTxn.created_at <= hi)))

            if acct is not None:
                verdict = "A"
            elif refund is not None:
                verdict = "C"
            else:
                verdict = "B"
            rows.append((verdict, p, tg, acct, refund))

        bots = {}
        for _, p, *_ in rows:
            if p.bot_id not in bots:
                b = await s.get(BotRow, p.bot_id)
                bots[p.bot_id] = (b.username if b else None) or str(p.bot_id)

    total = len(rows)
    a = [r for r in rows if r[0] == "A"]
    b = [r for r in rows if r[0] == "B"]
    c = [r for r in rows if r[0] == "C"]

    print(f"\nبازه: {args.hours} ساعتِ گذشته")
    print(f"  کلِ تراکنش‌هایِ purchase : {len(purchases)}")
    print(f"  خریدِ VPN (حساب می‌سازد) : {total}")
    print(f"  سایر (SMM/فروشگاه/حجم)  : {len(skipped)}  ← بررسی نمی‌شوند\n")
    # === اصلاح ===
    # نسخهٔ قبل دستهٔ A را «تحویلِ مشکوک» برچسب زد که گمراه‌کننده بود:
    # A یعنی حساب ساخته شده — همان نتیجهٔ *موفقِ* یک خرید. اگر ۹۵٪ خریدها
    # در این دسته باشند یعنی همه‌چیز سالم است، نه اینکه ۹۵٪ تحویل نشده‌اند.
    # این فهرست فقط وقتی به کار می‌آید که کاربرِ مشخصی شکایت کرده باشد؛
    # آن‌وقت نشان می‌دهد کانفیگش سالم است و فقط پیام نرسیده.
    ok_pct = len(a) * 100 / max(1, total)
    print(f"  A) موفق — حساب ساخته شد            : {len(a)}  ({ok_pct:.0f}%)")
    print(f"  B) نه حساب، نه عودت ← ضررِ واقعی    : {len(b)}")
    print(f"  C) عودت خورد (رفتارِ درست)          : {len(c)}")

    if b:
        print("\n" + "=" * 70)
        print("❌ موردهایِ B — پول کم شد، حسابی ساخته نشد، عودتی هم نخورد:")
        print("=" * 70)
        print(f"  {'زمان':<20}{'ربات':<18}{'کاربر':<13}{'مبلغ':>11}  شرح")
        for _, p, tg, _, _ in sorted(b, key=lambda r: r[1].created_at):
            print(f"  {p.created_at:%Y-%m-%d %H:%M:%S}  {bots[p.bot_id][:16]:<18}"
                  f"{tg:<13}{abs(float(p.amount)):>11,.0f}  {(p.note or '')[:34]}")
        print("\n  ← این‌ها باید دستی عودت داده شوند.")

    if a:
        print("\n" + "=" * 70)
        print("ℹ️  خریدهایِ موفق (مرجع) — این‌ها مشکل نیستند.")
        print("    فقط اگر کاربرِ مشخصی شکایت کرد، اینجا را نگاه کن:")
        print("    اگر اسمش هست، یعنی کانفیگش ساخته شده و فقط پیام نرسیده.")
        print("=" * 70)
        print(f"  {'زمان':<20}{'ربات':<18}{'کاربر':<14}{'کدِ سرویس':<12}{'ساب‌لینک'}")
        for _, p, tg, acct, _ in sorted(a, key=lambda r: r[1].created_at)[-25:]:
            has = "دارد" if (acct.sub_link or "") else "ندارد"
            print(f"  {p.created_at:%Y-%m-%d %H:%M:%S}  {bots[p.bot_id][:16]:<18}"
                  f"{tg:<14}{str(acct.svc_code or '—'):<12}{has}")

    print("\n" + "-" * 70)
    if b:
        print(f"نتیجه: {len(b)} خرید از {total} ({len(b)*100/max(1,total):.0f}%) "
              "پول کم کرد ولی نه حساب ساخت نه عودت داد.")
        print("       این‌ها باید دستی عودت داده شوند.")
    elif a:
        print("نتیجه: پول جایی گم نشده. حساب‌ها ساخته شده‌اند و مشکل فقط در")
        print("       *تحویلِ* پیام بوده — دقیقاً همان چیزی که")
        print("       PATCH_DELIVERY_NEVER_LOST_V1 می‌بندد.")
    else:
        print("نتیجه: در این بازه هیچ خریدِ مشکوکی پیدا نشد.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
