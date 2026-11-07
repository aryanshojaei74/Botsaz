#!/usr/bin/env python3
"""
perf_report.py — گزارشِ کندیِ ربات، بدونِ حدس‌وگمان.

سه کار می‌کند و هیچ‌چیز را تغییر نمی‌دهد:

  ۱) لاگ را می‌خواند و خطوطِ «slow update» (آپدیت‌هایی که بیش از ۲ ثانیه طول
     کشیده‌اند — همان چیزی که main_tenant.py خودش ثبت می‌کند) را دسته‌بندی
     می‌کند: چند تا، از چه نوعی، کدام ربات، و میانه/بیشینهٔ زمان.
  ۲) اگر دیتابیس PostgreSQL باشد، جدول‌هایی را که بیشترین Sequential Scan را
     خورده‌اند فهرست می‌کند — یعنی جاهایی که ایندکس کم است.
  ۳) کندترین کوئری‌ها را از pg_stat_statements می‌خواند (اگر افزونه‌اش نصب
     باشد؛ اگر نه، فقط رد می‌شود).

    python3 scripts/perf_report.py
    python3 scripts/perf_report.py --log /opt/botsaz/error.log --top 15
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SLOW_RE = re.compile(
    r"slow update \(token …(?P<token>\w+), kind=(?P<kind>[\w?]+), (?P<sec>[\d.]+)s\)")


def log_report(paths: list[str], top: int) -> None:
    by_kind: dict[str, list[float]] = defaultdict(list)
    by_token: dict[str, list[float]] = defaultdict(list)
    total = 0
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                m = SLOW_RE.search(line)
                if not m:
                    continue
                total += 1
                sec = float(m.group("sec"))
                by_kind[m.group("kind")].append(sec)
                by_token[m.group("token")].append(sec)

    print("═" * 60)
    print("۱) آپدیت‌های کند (بیش از ۲ ثانیه)")
    print("═" * 60)
    if not total:
        print("✅ هیچ آپدیتِ کندی در لاگ نیست — گلوگاه جای دیگری است.")
        return
    print(f"مجموع: {total} مورد\n")

    def show(title: str, data: dict[str, list[float]]) -> None:
        print(f"— {title} —")
        for name, vals in sorted(data.items(), key=lambda kv: -len(kv[1]))[:top]:
            vals.sort()
            print(f"  {name:<20} {len(vals):>5} بار | "
                  f"میانه {statistics.median(vals):.1f}s | بیشینه {vals[-1]:.1f}s")
        print()

    show("بر اساس نوعِ آپدیت", by_kind)
    show("بر اساس ربات (۶ رقمِ آخرِ توکن)", by_token)


async def db_report(top: int) -> None:
    from config import DATABASE_URL, Session
    from sqlalchemy import text

    if not DATABASE_URL.startswith("postgres"):
        print("ℹ️ دیتابیس PostgreSQL نیست؛ گزارشِ دیتابیس رد شد.")
        return

    print("═" * 60)
    print("۲) جدول‌هایی با بیشترین Sequential Scan (کاندیدِ ایندکس)")
    print("═" * 60)
    async with Session() as s:
        rows = (await s.execute(text("""
            SELECT relname, seq_scan, seq_tup_read, idx_scan, n_live_tup
            FROM pg_stat_user_tables
            WHERE seq_scan > 0
            ORDER BY seq_tup_read DESC LIMIT :n
        """), {"n": top})).all()
    print(f"  {'جدول':<24}{'seq':>10}{'خوانده':>14}{'idx':>10}{'ردیف':>10}")
    for r in rows:
        print(f"  {r[0]:<24}{r[1]:>10}{r[2]:>14}{(r[3] or 0):>10}{(r[4] or 0):>10}")
    print("\n💡 جدولی که seq_scan و «خوانده» بالایی دارد و idx پایین، ایندکس کم دارد.\n")

    print("═" * 60)
    print("۳) کندترین کوئری‌ها (pg_stat_statements)")
    print("═" * 60)
    try:
        async with Session() as s:
            rows = (await s.execute(text("""
                SELECT calls, mean_exec_time, total_exec_time, query
                FROM pg_stat_statements
                ORDER BY total_exec_time DESC LIMIT :n
            """), {"n": top})).all()
        for r in rows:
            q = " ".join(str(r[3]).split())[:110]
            print(f"  {r[0]:>7} بار | میانگین {r[1]:>7.1f}ms | جمع {r[2]/1000:>8.1f}s\n     {q}")
    except Exception:
        print("ℹ️ افزونهٔ pg_stat_statements نصب نیست؛ این بخش رد شد.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", action="append", default=None)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--no-db", action="store_true")
    args = ap.parse_args()

    paths = args.log or ["error.log", "/opt/botsaz/error.log"]
    # حذفِ مسیرهایِ تکراری: وقتی اسکریپت از داخلِ /opt/botsaz اجرا شود،
    # «error.log» و «/opt/botsaz/error.log» یک فایل‌اند و آمار دوبرابر می‌شد.
    seen, uniq = set(), []
    for pth in paths:
        real = os.path.realpath(pth)
        if real not in seen:
            seen.add(real)
            uniq.append(pth)
    paths = uniq
    log_report(paths, args.top)
    if not args.no_db:
        asyncio.run(db_report(args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
