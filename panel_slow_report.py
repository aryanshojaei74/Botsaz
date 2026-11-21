#!/usr/bin/env python3
"""
panel_slow_report.py — کدام پنلِ کدام مشتری کند است؟

اندازه‌گیری‌ها ثابت کرد گلوگاهِ «کندیِ ربات» نه شبکهٔ تلگرام است (۰٪ شکست
در ۶۰ نمونه) و نه لاگینِ تکراری (استخر کار می‌کند: `ورود=0.0s`)، بلکه
خودِ APIِ پنل‌هاست:

    پنلِ کند: POST /panel/api/clients/bulkAdjust | درخواست=12.7s

این اسکریپت خروجیِ journalctl را می‌خواند و جمع‌بندی می‌کند: کدام پنل،
کدام endpoint، چند بار، و چقدر کند. با این گزارش می‌شود به مالکِ همان
ربات گفت «سرورِ پنلت کند است» به‌جای اینکه دنبالِ باگ در کد بگردیم.

اجرا:
    journalctl -u botsaz-tenant --since "24 hours ago" | python3 panel_slow_report.py
    journalctl -u botsaz-tenant --since "3 days ago" | python3 panel_slow_report.py --top 30
"""
from __future__ import annotations

import argparse
import collections
import re
import statistics
import sys

# پنلِ کند: POST /panel/api/clients/bulkAdjust | مجموع=12.7s (ورود=0.0s، …) | https://…
LINE = re.compile(
    r"پنلِ کند: (\w+) (\S+) \| مجموع=([\d.]+)s"
    r"(?: \(ورود=([\d.]+)s[^)]*\))?"
    r"(?: \| (\S+))?")
# نسخهٔ سادهٔ زیرکلاس‌ها: پنلِ کند: POST /api/user | 9.9s | https://…
LINE2 = re.compile(r"پنلِ کند: (\w+) (\S+) \| ([\d.]+)s \| (\S+)")


def _host(url: str) -> str:
    m = re.match(r"https?://([^/:]+)", url or "")
    return m.group(1) if m else (url or "?")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    per_panel: dict[str, list[float]] = collections.defaultdict(list)
    per_ep: dict[str, list[float]] = collections.defaultdict(list)
    per_pair: dict[tuple, list[float]] = collections.defaultdict(list)
    relogins = 0
    shared = 0

    for line in sys.stdin:
        m = LINE.search(line)
        if m:
            method, path, total, login, url = m.groups()
            dur = float(total)
            if login is not None:
                (relogins if float(login) > 0.05 else shared)
                if float(login) > 0.05:
                    relogins += 1
                else:
                    shared += 1
        else:
            m = LINE2.search(line)
            if not m:
                continue
            method, path, total, url = m.groups()
            dur = float(total)
        h = _host(url)
        ep = f"{method} {path}"
        per_panel[h].append(dur)
        per_ep[ep].append(dur)
        per_pair[(h, ep)].append(dur)

    n = sum(len(v) for v in per_panel.values())
    if not n:
        print("هیچ خطِ «پنلِ کند» پیدا نشد.\n"
              "یا در این بازه کندی نبوده، یا زمان‌سنجی هنوز دیپلوی نشده:\n"
              "  grep -c PATCH_PANEL_TIMING_LOG_V1 /opt/botsaz/tenant/vpn_panel.py")
        return 0

    def block(title, d, keyfmt=str):
        print("\n" + "=" * 74)
        print(title)
        print("=" * 74)
        print(f"  {'تعداد':>6} {'میانه':>8} {'بیشینه':>8}   ")
        rows = sorted(d.items(), key=lambda kv: -len(kv[1]))[:args.top]
        for k, v in rows:
            print(f"  {len(v):>6} {statistics.median(v):>7.1f}s {max(v):>7.1f}s   {keyfmt(k)}")

    print(f"\n{n} عملیاتِ کندِ پنل (بالای ۲ ثانیه)")
    if relogins or shared:
        tot = relogins + shared
        print(f"وضعیتِ استخرِ نشست: {shared} مشترک · {relogins} ورودِ تازه "
              f"({relogins * 100 / max(1, tot):.0f}% نیاز به لاگین)")
        if relogins * 100 / max(1, tot) > 30:
            print("  ⚠️ نرخِ ورودِ تازه بالاست — استخر درست کار نمی‌کند.")

    block("کندترین پنل‌ها (سرورِ مشتری)", per_panel)
    block("کندترین endpointها", per_ep)
    block("ترکیبِ پنل × endpoint", per_pair,
          keyfmt=lambda k: f"{k[0]}  →  {k[1]}")

    worst = max(per_panel.items(), key=lambda kv: statistics.median(kv[1]))
    print("\n" + "-" * 74)
    print("نتیجه‌گیری:")
    print(f"  کندترین پنل: {worst[0]}  (میانه {statistics.median(worst[1]):.1f}s"
          f" در {len(worst[1])} عملیات)")
    print("  این کندیِ سرورِ خودِ آن پنل است، نه کدِ ربات. راه‌حل‌ها:")
    print("    • به مالکِ آن ربات اطلاع بده سرور/پنلش را بررسی کند")
    print("    • اگر endpointِ خاصی کند است (مثلاً bulkAdjust)، شاید")
    print("      دیتابیسِ پنل نیاز به ایندکس یا پاک‌سازی دارد")
    print("    • در سمتِ ما: پیامِ «در حال انجام…» قبل از تماس، تا کاربر")
    print("      حس نکند ربات هنگ کرده")
    return 0


if __name__ == "__main__":
    sys.exit(main())
