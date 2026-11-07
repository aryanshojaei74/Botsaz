#!/usr/bin/env python3
"""
check_telegram_latency.py — آیا واقعاً شبکه‌ی سرور تا تلگرام کند است؟

تحلیلِ لاگ نشان داد آپدیت‌هایِ کند در بازهٔ باریکِ ۱۳ تا ۱۸ ثانیه خوشه
بسته‌اند، و تایم‌اوتِ تماس‌هایِ تلگرام در config.py دقیقاً ۱۲ ثانیه است.
این یعنی: تماس سرِ ۱۲ ثانیه می‌ترکد و تلاشِ دوم موفق می‌شود.

اما این هنوز یک *استنتاج* است. این اسکریپت مستقیم اندازه می‌گیرد، بدونِ
اینکه منتظرِ باگ بمانیم:

    • N بار getMe می‌زند (سبک‌ترین متدِ API) و تأخیر را ثبت می‌کند
    • یک بار هم sendChatAction به خودِ ادمین، تا مسیرِ نوشتن هم سنجیده شود
    • صدک‌ها، نرخِ تایم‌اوت و نرخِ خطا را گزارش می‌کند

اجرا از ریشهٔ پروژه:
    python3 check_telegram_latency.py            # ۲۰ نمونه
    python3 check_telegram_latency.py -n 60
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=20, help="تعدادِ نمونه")
    ap.add_argument("--timeout", type=float, default=12.0,
                    help="همان تایم‌اوتی که در config.py تنظیم شده")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    token = os.getenv("PLATFORM_BOT_TOKEN", "").strip()
    if not token:
        print("❌ PLATFORM_BOT_TOKEN در .env پیدا نشد.")
        return 2

    import aiohttp
    url = f"https://api.telegram.org/bot{token}/getMe"

    lat: list[float] = []
    timeouts = 0
    errors = 0

    print(f"در حال گرفتنِ {args.n} نمونه از api.telegram.org "
          f"(تایم‌اوت {args.timeout:g}s)…\n")

    timeout = aiohttp.ClientTimeout(total=args.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        for i in range(args.n):
            t0 = time.monotonic()
            try:
                async with s.get(url) as r:
                    body = await r.read()
                    dt_ = time.monotonic() - t0
                    # فقط پاسخِ واقعیِ تلگرام «موفق» شمرده می‌شود. بدونِ این
                    # چک، صفحهٔ خطایِ یک پروکسی/فایروال هم به‌عنوانِ تماسِ
                    # سریعِ موفق ثبت می‌شد و نتیجه را کاملاً وارونه نشان
                    # می‌داد (در تستِ خودم ۰٫۰۱ ثانیه «سالم» گزارش شد،
                    # درحالی‌که اصلاً به تلگرام نرسیده بود).
                    import json as _json
                    try:
                        ok = _json.loads(body).get("ok") is True
                    except Exception:
                        ok = False
                    if not ok:
                        errors += 1
                        head = body[:80].decode("utf-8", "replace").replace("\n", " ")
                        print(f"  {i+1:3d}  {'BAD':>7}  HTTP {r.status} — {head}")
                        continue
                    lat.append(dt_)
                    bar = "█" * min(60, max(1, int(dt_ * 20)))
                    print(f"  {i+1:3d}  {dt_:6.2f}s  {bar}")
            except asyncio.TimeoutError:
                timeouts += 1
                print(f"  {i+1:3d}  {'TIMEOUT':>7}  ← سرِ {args.timeout:g}s ترکید")
            except Exception as e:
                errors += 1
                print(f"  {i+1:3d}  {'ERROR':>7}  {type(e).__name__}: {e}")
            await asyncio.sleep(0.3)

    n = args.n
    print("\n" + "=" * 62)
    print(f"نمونه‌ها: {n}  |  موفق: {len(lat)}  |  تایم‌اوت: {timeouts}  "
          f"|  خطا: {errors}")
    if lat:
        lat.sort()

        def pct(p):
            return lat[min(len(lat) - 1, int(len(lat) * p / 100))]

        print(f"\nتأخیر:  میانه={statistics.median(lat):.2f}s   "
              f"p90={pct(90):.2f}s   p99={pct(99):.2f}s   "
              f"بیشینه={lat[-1]:.2f}s")
        slow = sum(1 for x in lat if x > 2.0)
        print(f"کندتر از ۲ ثانیه: {slow} از {len(lat)} "
              f"({slow*100/len(lat):.0f}%)")
    fail_pct = (timeouts + errors) * 100 / n
    print(f"نرخِ شکست: {fail_pct:.1f}%")

    print("\n" + "-" * 62)
    print("تفسیر:")
    if fail_pct >= 5:
        print(f"  ❌ {fail_pct:.0f}% از تماس‌ها شکست می‌خورند. فرضیه تأیید شد:")
        print("     مشکل شبکهٔ سرور تا تلگرام است، نه کدِ ربات.")
        print("     هر شکست یعنی ۱۲ ثانیه انتظار + تلاشِ دوباره — دقیقاً")
        print("     همان خوشهٔ ۱۳ تا ۱۸ ثانیه‌ای که در لاگ دیدیم.")
        print("\n  راه‌حل‌ها به ترتیبِ هزینه:")
        print("     ۱) کاهشِ تایم‌اوت از ۱۲ به ۴–۵ ثانیه (یک خط)")
        print("     ۲) پروکسی یا مسیرِ بهتر به api.telegram.org")
        print("     ۳) Local Bot API Server")
    elif lat and statistics.median(lat) > 1.5:
        print(f"  ⚠️ تماس‌ها شکست نمی‌خورند ولی کندند "
              f"(میانه {statistics.median(lat):.1f}s).")
        print("     مسیرِ شبکه کیفیتِ خوبی ندارد؛ روی هر کلیک حس می‌شود.")
    else:
        print("  ✅ شبکه سالم است. پس کندی از تلگرام نیست و باید در")
        print("     خودِ هندلرها دنبالش گشت (لاگِ «slow update … →» را ببین).")
    print("\nنکته: این تستِ لحظه‌ای است. اگر مشکل رگباری باشد، در ساعتِ")
    print("      شلوغ دوباره اجرایش کن.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
