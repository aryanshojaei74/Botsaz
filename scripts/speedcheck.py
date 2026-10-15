#!/usr/bin/env python3
"""
speedcheck.py — یک‌جا اندازه‌گیریِ کندیِ سرور (فقط می‌خواند، هیچ‌چیز را تغییر نمی‌دهد).

    cd /opt/botsaz && venv/bin/python scripts/speedcheck.py
    venv/bin/python scripts/speedcheck.py --hours 6 --top 12

بخش‌ها: منابعِ سرور، تأخیرِ Redis/Postgres/Telegram، آپدیت‌هایِ کند به‌تفکیکِ دکمه،
بلوکه‌شدنِ event loop، کندیِ پنل‌ها، ایندکس‌هایِ کم و کوئری‌هایِ سنگین، و در آخر
«نتیجه و پیشنهاد».
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ISSUES: list[str] = []


def h(t: str) -> None:
    print("\n" + "═" * 64 + f"\n{t}\n" + "═" * 64)


def pct(vals: list[float], p: float) -> float:
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * p))] if vals else 0.0


def sh(cmd: str, timeout: int = 60) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as e:  # noqa: BLE001
        return f"({type(e).__name__})"


def system_report() -> None:
    h("۱) منابعِ سرور")
    load = os.getloadavg()
    cpus = os.cpu_count() or 1
    print(f"CPU: {cpus} هسته | load 1/5/15 دقیقه: {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}")
    if load[1] > cpus:
        ISSUES.append(f"load سرور ({load[1]:.1f}) بیشتر از تعداد هسته‌ها ({cpus}) است → CPU کم است یا پروسه‌ای زیادی می‌خورد.")
    try:
        mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
        tot, av = mem["MemTotal"] / 1024, mem["MemAvailable"] / 1024
        sw = (mem["SwapTotal"] - mem["SwapFree"]) / 1024
        print(f"RAM: کل {tot:.0f}MB | آزاد {av:.0f}MB ({av / tot * 100:.0f}٪) | swap مصرفی {sw:.0f}MB")
        if av / tot < 0.12:
            ISSUES.append("RAM آزاد کمتر از ۱۲٪ است → کندی/swap؛ RAM بیشتر یا کمتر کردنِ workerها.")
        if sw > 300:
            ISSUES.append(f"swap مصرفی {sw:.0f}MB است → سرور زیرِ فشارِ RAM است.")
    except Exception:  # noqa: BLE001
        pass
    du = shutil.disk_usage("/")
    print(f"دیسک: {du.used / 1e9:.1f}GB از {du.total / 1e9:.1f}GB ({du.used / du.total * 100:.0f}٪)")
    if du.used / du.total > 0.9:
        ISSUES.append("دیسک بیش از ۹۰٪ پر است.")
    print("\nپرمصرف‌ترین پروسه‌ها:")
    print(sh("ps -eo pid,pcpu,pmem,etime,comm --sort=-pcpu | head -6"))
    for svc in ("botsaz-tenant", "botsaz-platform", "botsaz-worker"):
        st = sh(f"systemctl is-active {svc} 2>/dev/null").strip()
        if st:
            nr = sh(f"systemctl show {svc} -p NRestarts --value 2>/dev/null").strip()
            print(f"  {svc}: {st} | ریستارت‌ها: {nr or '?'}")


async def latency_report(n: int) -> None:
    h("۲) تأخیرِ Redis / Postgres / Telegram")
    try:
        from config import DATABASE_URL, Session
        from sqlalchemy import text
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            async with Session() as s:
                await s.execute(text("select 1"))
            ts.append((time.perf_counter() - t0) * 1000)
        print(f"دیتابیس (select 1): میانه {statistics.median(ts):.1f}ms | p95 {pct(ts, .95):.1f}ms | بیشینه {max(ts):.1f}ms")
        if statistics.median(ts) > 25:
            ISSUES.append("تأخیرِ دیتابیس برای select ساده بالاست (>25ms) → اتصالِ شبکه‌ای/pool را چک کن.")
        if DATABASE_URL.startswith("postgres"):
            async with Session() as s:
                c = (await s.execute(text("select state, count(*) from pg_stat_activity group by 1"))).all()
                mx = (await s.execute(text("show max_connections"))).scalar()
            print("اتصال‌های Postgres:", ", ".join(f"{a or 'خالی'}={b}" for a, b in c), f"| سقف {mx}")
            if sum(b for _, b in c) > int(mx) * 0.8:
                ISSUES.append("بیش از ۸۰٪ ظرفیتِ اتصال‌هایِ Postgres پر است.")
            if any(a == "idle in transaction" and b > 5 for a, b in c):
                ISSUES.append("چند اتصال در حالت «idle in transaction» مانده → قفل/کندیِ احتمالی.")
    except Exception as e:  # noqa: BLE001
        print("⚠️ تستِ دیتابیس ناموفق:", type(e).__name__, e)
    try:
        import redis.asyncio as aioredis
        url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        r = aioredis.from_url(url)
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            await r.ping()
            ts.append((time.perf_counter() - t0) * 1000)
        await r.aclose()
        print(f"Redis (ping): میانه {statistics.median(ts):.1f}ms | p95 {pct(ts, .95):.1f}ms | بیشینه {max(ts):.1f}ms")
        if pct(ts, .95) > 20:
            ISSUES.append("Redis کند است (p95>20ms) → FSM و کش‌ها کند می‌شوند.")
    except Exception as e:  # noqa: BLE001
        print("ℹ️ تستِ Redis انجام نشد:", type(e).__name__)
    try:
        import aiohttp
        ts, fails = [], 0
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as cs:
            for _ in range(n):
                t0 = time.perf_counter()
                try:
                    async with cs.get("https://api.telegram.org/") as resp:
                        await resp.read()
                    ts.append((time.perf_counter() - t0) * 1000)
                except Exception:  # noqa: BLE001
                    fails += 1
        if ts:
            print(f"Telegram API: میانه {statistics.median(ts):.0f}ms | p95 {pct(ts, .95):.0f}ms | بیشینه {max(ts):.0f}ms | خطا {fails}/{n}")
            if statistics.median(ts) > 600 or fails:
                ISSUES.append("اتصال سرور به تلگرام کند/ناپایدار است → عامل اصلیِ کندیِ دکمه‌ها؛ سرور/دیتاسنتر یا مسیر شبکه.")
        else:
            print("⚠️ هیچ پاسخی از تلگرام نیامد.")
            ISSUES.append("سرور به تلگرام وصل نشد (بدونِ پاسخ) → شبکه/فیلترینگِ سرور.")
    except Exception as e:  # noqa: BLE001
        print("ℹ️ تستِ Telegram انجام نشد:", type(e).__name__)


SLOW_RE = re.compile(r"slow update \(token …(?P<tok>\w+), kind=(?P<kind>[\w?]+), (?P<sec>[\d.]+)s\) → (?P<what>.*?)(?: \||$)")


def journal_report(hours: int, top: int) -> None:
    h(f"۳) آپدیت‌هایِ کند در {hours} ساعتِ اخیر")
    out = sh(f'journalctl -u botsaz-tenant -u botsaz-platform --since "{hours} hours ago" --no-pager', timeout=180)
    by_what: dict[str, list[float]] = defaultdict(list)
    by_tok: dict[str, list[float]] = defaultdict(list)
    allv: list[float] = []
    blocked, panel_to, cf, tg_to = [], 0, 0, 0
    for line in out.splitlines():
        m = SLOW_RE.search(line)
        if m:
            sec = float(m["sec"])
            by_what[m["what"][:45]].append(sec)
            by_tok[m["tok"]].append(sec)
            allv.append(sec)
            continue
        if "event loop blocked" in line:
            mm = re.search(r"blocked ([\d.]+)s", line)
            if mm:
                blocked.append(float(mm[1]))
        if re.search(r"TimeoutError|ReadTimeout|ConnectTimeout|timed out", line):
            panel_to += 1
        if re.search(r"\b52[0-9]\b|cloudflare", line, re.I):
            cf += 1
        if "Request timeout error" in line or "TelegramNetworkError" in line:
            tg_to += 1
    if not allv:
        print("✅ هیچ آپدیتِ کندی (>۲ ثانیه) ثبت نشده.")
    else:
        print(f"تعداد: {len(allv)} | میانه {statistics.median(allv):.1f}s | p95 {pct(allv, .95):.1f}s | بیشینه {max(allv):.1f}s\n")
        print("کندترین دکمه‌ها/پیام‌ها (بر اساسِ تعداد):")
        for k, v in sorted(by_what.items(), key=lambda kv: -len(kv[1]))[:top]:
            print(f"  {k:<46}{len(v):>5} بار | میانه {statistics.median(v):>5.1f}s | بیشینه {max(v):>5.1f}s")
        print("\nرباتهایی که بیشترین کندی را دارند (۶ رقمِ آخرِ توکن):")
        for k, v in sorted(by_tok.items(), key=lambda kv: -len(kv[1]))[:5]:
            print(f"  …{k}  {len(v)} بار | بیشینه {max(v):.1f}s")
        if len(allv) > 50:
            ISSUES.append(f"{len(allv)} آپدیتِ کند در {hours} ساعت — ردیفِ اولِ جدولِ بالا را اول بهینه کن.")
    print(f"\nبلوکه‌شدنِ event loop: {len(blocked)} بار" + (f" | بیشینه {max(blocked):.1f}s" if blocked else ""))
    if blocked:
        ISSUES.append(f"event loop {len(blocked)} بار بلوکه شده (بیشینه {max(blocked):.1f}s) → کارِ سنگینِ همگام در کد؛ "
                      "stack را با: journalctl -u botsaz-tenant | grep -A8 'event loop blocked' ببین.")
    print(f"خطایِ تایم‌اوت (پنل/شبکه): {panel_to} | نشانهٔ Cloudflare 52x: {cf} | خطایِ شبکهٔ تلگرام: {tg_to}")
    if tg_to > 30:
        ISSUES.append(f"{tg_to} خطای شبکهٔ تلگرام → ارتباط سرور با تلگرام ناپایدار است.")
    if panel_to > 100:
        ISSUES.append(f"{panel_to} تایم‌اوت → پنل‌هایِ کندِ مشتری‌ها؛ باعثِ کندیِ همان رباتشان می‌شود نه همه.")


async def db_report(top: int) -> None:
    h("۴) ایندکس‌هایِ کم و کوئری‌هایِ سنگین (Postgres)")
    from config import DATABASE_URL, Session
    from sqlalchemy import text
    if not DATABASE_URL.startswith("postgres"):
        print("ℹ️ دیتابیس Postgres نیست؛ رد شد.")
        return
    async with Session() as s:
        rows = (await s.execute(text("""
            SELECT relname, seq_scan, seq_tup_read, coalesce(idx_scan,0), n_live_tup
            FROM pg_stat_user_tables WHERE seq_scan > 100 AND n_live_tup > 2000
            ORDER BY seq_tup_read DESC LIMIT :n"""), {"n": top})).all()
    if rows:
        print(f"  {'جدول':<26}{'seq':>9}{'خوانده':>15}{'idx':>9}{'ردیف':>9}")
        for r in rows:
            print(f"  {r[0]:<26}{r[1]:>9}{r[2]:>15}{r[3]:>9}{r[4]:>9}")
            if r[2] > 50_000_000 and r[3] < r[1]:
                ISSUES.append(f"جدولِ {r[0]} زیاد Sequential Scan می‌خورد ({r[2]:,} ردیف) → کاندیدِ ایندکس.")
    else:
        print("✅ جدولِ بزرگ با Sequential Scanِ زیاد نیست.")
    try:
        async with Session() as s:
            rows = (await s.execute(text("""
                SELECT calls, mean_exec_time, total_exec_time, query FROM pg_stat_statements
                ORDER BY total_exec_time DESC LIMIT :n"""), {"n": min(top, 6)})).all()
        print("\nسنگین‌ترین کوئری‌ها (کلِ زمان):")
        for r in rows:
            print(f"  {r[0]:>8} بار | میانگین {r[1]:>7.1f}ms | جمع {r[2] / 1000:>8.1f}s\n     {' '.join(str(r[3]).split())[:110]}")
    except Exception:  # noqa: BLE001
        print("ℹ️ pg_stat_statements نصب نیست (اختیاری).")


def verdict() -> None:
    h("۵) نتیجه و پیشنهاد")
    if not ISSUES:
        print("✅ مشکلِ مشخصی پیدا نشد؛ اگر کاربری کندی می‌بیند، شمارهٔ ۳ (ربات و دکمهٔ مشخص) را برایِ همان بررسی کن.")
        return
    for i, t in enumerate(ISSUES, 1):
        print(f"{i}. {t}")
    print("\nاین خروجی را برایِ من بفرست تا مورد به مورد در کد رفع کنم.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("-n", type=int, default=15, help="تعدادِ نمونه برایِ تست‌هایِ تأخیر")
    ap.add_argument("--no-db", action="store_true")
    a = ap.parse_args()
    system_report()
    asyncio.run(latency_report(a.n))
    journal_report(a.hours, a.top)
    if not a.no_db:
        try:
            asyncio.run(db_report(a.top))
        except Exception as e:  # noqa: BLE001
            print("⚠️ بخشِ دیتابیس ناموفق:", type(e).__name__, e)
    verdict()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
