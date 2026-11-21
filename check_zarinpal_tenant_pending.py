#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_zarinpal_tenant_pending.py — تشخیصِ اینکه آیا مشکلِ «پول کم شد،
کیف‌پول شارژ نشد» رو ربات‌های مستأجر واقعاً یعنی نبودِ محافظ، یا مکانیزمِ
موجود کار نمی‌کند.

نکتهٔ مهم (نتیجهٔ بررسیِ کد): برخلافِ کیف‌پولِ خودِ پلتفرم (که تا پچِ
patch_zarinpal_reconcile.py هیچ محافظی نداشت)، شارژِ زرین‌پالِ کیف‌پولِ
ربات‌هایِ مستأجر از قبل یک حلقهٔ پس‌زمینه دارد:
  tenant/gateways/zarinpal_wallet.py → zarinpal_poll_once()
  main_tenant.py                     → zarinpal_wallet_poll_loop() (هر ۶۰ثانیه)
  worker_tenant.py                   → mt.zarinpal_wallet_poll_loop() اسپان می‌شود

یعنی اگر سرویسِ botsaz-tenant-worker.service روی سرور بالا و سالم باشد،
فاکتورهایِ pending باید خودشان ظرفِ ~۱ دقیقه تسویه شوند — نیازی به پچِ
جدید نیست. این اسکریپت فقط می‌خواند (هیچ رکوردی را عوض نمی‌کند) تا معلوم
شود مشکلِ واقعی کدام است:
  الف) سفارش‌هایِ pendingِ قدیمی روی هم تلنبار شده‌اند → یعنی خودِ حلقه اجرا
       نمی‌شود (سرویسِ ورکر خاموش/کرش‌کرده) — باید systemctl را چک کنید.
  ب) سفارشی pendingِ قدیمی نیست → یعنی حلقه دارد کارش را می‌کند و موردِ
     خاصی که گزارش شده احتمالاً یا خیلی به‌تازگی اتفاق افتاده (چند دقیقه
     صبر کنید)، یا از مسیرِ دیگری (مثلاً زیبال) بوده، یا از قبلِ همین چند
     روز حل شده.

استفاده (فقط می‌خواند؛ --apply ندارد):
    cd /opt/botsaz
    python3 check_zarinpal_tenant_pending.py
    python3 check_zarinpal_tenant_pending.py --bot 123        # فیلترِ یک ربات
    python3 check_zarinpal_tenant_pending.py --tg 987654321   # فیلترِ یک کاربر
"""
import asyncio
import sys
from datetime import datetime, timedelta


async def main():
    from config import Session
    from models import Order, Bot as BotRow, BotUser
    from sqlalchemy import select

    bot_filter = None
    tg_filter = None
    args = sys.argv[1:]
    if "--bot" in args:
        bot_filter = int(args[args.index("--bot") + 1])
    if "--tg" in args:
        tg_filter = int(args[args.index("--tg") + 1])

    async with Session() as s:
        q = select(Order).where(Order.pay_method == "zarinpal_wallet",
                                 Order.status == "pending")
        if bot_filter:
            q = q.where(Order.bot_id == bot_filter)
        rows = (await s.scalars(q.order_by(Order.created_at))).all()

        out = []
        for o in rows:
            u = await s.get(BotUser, o.user_id)
            if tg_filter and (not u or u.telegram_id != tg_filter):
                continue
            b = await s.get(BotRow, o.bot_id)
            age = datetime.utcnow() - o.created_at
            out.append((o, u, b, age))

    if not out:
        print("✅ هیچ فاکتورِ pendingِ زرین‌پالی روی ربات‌های مستأجر پیدا نشد"
              + (f" (فیلترِ bot={bot_filter})" if bot_filter else "")
              + (f" (فیلترِ tg={tg_filter})" if tg_filter else "") + ".")
        print("   اگر موردِ خاصی گزارش شده، یا خیلی تازه است (چند دقیقه صبر کن)،")
        print("   یا از درگاهِ دیگری (زیبال/کارت‌به‌کارت) بوده، یا قبلاً حل شده.")
        return

    print(f"📋 {len(out)} فاکتورِ pendingِ زرین‌پال (مستأجر) پیدا شد:\n")
    stale_count = 0
    for o, u, b, age in out:
        mins = int(age.total_seconds() // 60)
        stale = mins > 5  # بیشتر از ۵ دقیقه یعنی حلقهٔ ۶۰ثانیه‌ای باید تا الان امتحانش کرده باشد
        if stale:
            stale_count += 1
        flag = "🟡 قدیمی‌تر از حدِ انتظار — بررسی کن حلقه واقعاً اجرا می‌شود" if stale else "🟢 تازه — طبیعی است"
        bot_label = f"@{b.username}" if (b and b.username) else f"#{o.bot_id}"
        tg_label = u.telegram_id if u else "?"
        print(f"  order#{o.id} | ربات {bot_label} | کاربر {tg_label} | "
              f"{int(o.amount or 0):,} تومان | {mins} دقیقه پیش | {flag}")

    print()
    if stale_count:
        print(f"⚠️  {stale_count} موردِ قدیمی‌تر از ۵ دقیقه هست که هنوز pending مونده.")
        print("   این یعنی احتمالاً خودِ حلقهٔ پس‌زمینه اجرا نمی‌شه. چک کن:")
        print("       systemctl status botsaz-tenant-worker")
        print("       journalctl -u botsaz-tenant-worker -n 100 --no-pager")
        print("   اگه سرویس بالا نیست یا لوپ داره crash می‌کنه، بعد از رفعش این فاکتورها")
        print("   خودشون ظرف کمتر از ۱ دقیقه تسویه می‌شن — نیازی به شارژِ دستی نیست.")
    else:
        print("همه‌چیز طبیعیه؛ فاکتورهای موجود هنوز به اندازه‌ای قدیمی نشدن که نگران‌کننده باشن.")


if __name__ == "__main__":
    asyncio.run(main())
