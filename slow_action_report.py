#!/usr/bin/env python3
"""
slow_action_report.py — کدام دکمه/دستور ربات را کند می‌کند؟

عددها تا اینجا:
    ۵۴۳ آپدیتِ کند در ۲۴ ساعت
     ۱۳ عملیاتِ کندِ پنل
یعنی پنل فقط ۲٫۴٪ ماجراست و ۵۳۰ موردِ دیگر علتِ دیگری دارند.

`main_tenant.py` حالا در هر هشدارِ کندی، خودِ اکشن را هم می‌نویسد:

    slow update (token …abc123, kind=callback_query, 16.7s) → vpn:buy:12:3

این اسکریپت آن‌ها را دسته‌بندی می‌کند تا معلوم شود وقت کجا می‌رود. کلیدها
نرمال‌سازی می‌شوند (`vpn:buy:12:3` و `vpn:buy:88:1` یک دسته‌اند)، وگرنه
هر شناسه یک ردیفِ جدا می‌شد و الگو دیده نمی‌شد.

اجرا:
    journalctl -u botsaz-tenant --since "24 hours ago" | python3 slow_action_report.py
"""
from __future__ import annotations

import argparse
import collections
import re
import statistics
import sys

LINE = re.compile(
    r"slow update \(token …(\w+), kind=(\w+), ([\d.]+)s\)(?: → (.*?))?\s*$")


def norm(action: str) -> str:
    """`vpn:buy:12:3` → `vpn:buy:*` تا موردهایِ هم‌خانواده با هم جمع شوند."""
    a = (action or "?").strip()
    if not a or a == "?":
        return "(بدونِ جزئیات — نسخهٔ قدیمیِ لاگ)"
    if ":" in a:
        parts = a.split(":")
        keep = [p for p in parts if not p.isdigit()]
        out = ":".join(keep[:3])
        return out + (":*" if len(keep) < len(parts) else "")
    return a[:40]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    by_action: dict[str, list[float]] = collections.defaultdict(list)
    by_bot: dict[str, list[float]] = collections.defaultdict(list)
    by_kind: dict[str, list[float]] = collections.defaultdict(list)
    no_detail = 0

    for line in sys.stdin:
        m = LINE.search(line.rstrip())
        if not m:
            continue
        token, kind, secs, action = m.groups()
        d = float(secs)
        if not action:
            no_detail += 1
        by_action[norm(action)].append(d)
        by_bot[token].append(d)
        by_kind[kind].append(d)

    n = sum(len(v) for v in by_action.values())
    if not n:
        print("هیچ خطِ «slow update» پیدا نشد.")
        return 0

    allv = sorted(x for v in by_action.values() for x in v)
    print(f"\n{n} آپدیتِ کند")
    print(f"میانه={statistics.median(allv):.1f}s  "
          f"p90={allv[int(len(allv)*.9)]:.1f}s  بیشینه={allv[-1]:.1f}s")
    if no_detail:
        print(f"⚠️ {no_detail} مورد بدونِ جزئیاتِ اکشن (قبل از دیپلویِ "
              "PATCH_SLOW_UPDATE_DETAIL_V1 ثبت شده‌اند)")

    def block(title, d, unit=""):
        print("\n" + "=" * 76)
        print(title)
        print("=" * 76)
        print(f"  {'تعداد':>6} {'٪':>5} {'میانه':>8} {'بیشینه':>8}  {'مجموعِ ثانیه':>11}")
        rows = sorted(d.items(), key=lambda kv: -sum(kv[1]))[:args.top]
        for k, v in rows:
            print(f"  {len(v):>6} {len(v)*100/n:>4.0f}% {statistics.median(v):>7.1f}s "
                  f"{max(v):>7.1f}s  {sum(v):>10.0f}s  {unit}{k}")

    block("کندترین اکشن‌ها (مرتب بر اساسِ مجموعِ زمانِ تلف‌شده)", by_action)
    block("پرکندترین ربات‌ها", by_bot, unit="token …")
    block("بر اساسِ نوعِ رویداد", by_kind)

    print("\n" + "-" * 76)
    top = max(by_action.items(), key=lambda kv: sum(kv[1]))
    print("راهنمایِ خواندن:")
    print(f"  بیشترین زمانِ تلف‌شده: «{top[0]}» — {len(top[1])} بار، "
          f"مجموعاً {sum(top[1]):.0f} ثانیه")
    print("  • اگر یک اکشن سهمِ بزرگی دارد، همان‌جا را باید بهینه کرد.")
    print("  • اگر یک ربات سهمِ بزرگی دارد، مشکل پنل یا دادهٔ همان ربات است.")
    print("  • اگر پخش و یکنواخت است، گلوگاه مشترک است (دیتابیس/Redis).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
