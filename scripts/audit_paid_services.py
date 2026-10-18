#!/usr/bin/env python3
"""
scripts/audit_paid_services.py — ممیزیِ فقط‌خواندنیِ «رایگان‌خورها» (PATCH_MARKET_AUDIT_V1).

سه بخشِ پولی را روی سرور چک می‌کند و هر موردی را که «بدونِ پرداخت/بعد از انقضا هنوز کار می‌کند» فهرست می‌کند:

  ۱) تستِ «ربات اختصاصی آماده» (MarketPurchase kind=trial): مدتش گذشته ولی اپش هنوز روشن است.
  ۲) «سرور اختصاصی/فعال‌سازی هوشمند» (HostedApp): اپِ روشنِ بدونِ پرداخت/انقضاگذشته،
     و کانتینرِ داکرِ روشن که در دیتابیس suspended/archived/ناموجود است.
  ۳) SFTP VIP (/opt/sftpvip/sftpvip.db): اشتراکِ نامحدود (expires_at خالی)، منقضیِ فعال‌نشده‌ی مسدودنشده، و
     اتصالِ استفاده‌شده بعد از انقضا.

هیچ‌چیز را تغییر نمی‌دهد. اجرا از /opt/botsaz:

    venv/bin/python scripts/audit_paid_services.py
    venv/bin/python scripts/audit_paid_services.py --free-minutes 60     # سقفِ مهلتِ رایگانِ سرویسِ میزبانی

کدِ خروج: ۰ = تمیز، ۱ = مورد پیدا شد (برایِ cron/پایش).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from config import Session  # noqa: E402
from models import HostedApp, MarketPurchase, Setting  # noqa: E402

SFTP_DB = os.getenv("SFTPVIP_DB", "/opt/sftpvip/sftpvip.db")
GRACE = dt.timedelta(minutes=5)       # ناظرِ سرویسِ میزبانی هر ~۲ دقیقه اجرا می‌شود


def _fmt(d) -> str:
    return d.strftime("%Y-%m-%d %H:%M") if d else "—"


def _docker_running() -> set[str]:
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=20)
        return {n.strip() for n in out.stdout.splitlines() if n.strip()}
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ docker ps اجرا نشد ({e}); بخشِ کانتینر رد شد.")
        return set()


async def audit_hosting(free_minutes: int) -> int:
    bad = 0
    now = dt.datetime.now()
    async with Session() as s:
        apps = (await s.scalars(select(HostedApp))).all()
        trials = (await s.scalars(select(MarketPurchase).where(
            MarketPurchase.kind == "trial", MarketPurchase.hosted_app_id > 0))).all()
    by_id = {a.id: a for a in apps}
    running = _docker_running()

    print("\n══ ۱) تستِ ربات اختصاصی ══")
    n = 0
    for t in trials:
        a = by_id.get(t.hosted_app_id)
        if not a or not t.trial_expires_at or t.trial_expires_at + GRACE > now:
            continue
        paid = a.paid_until and a.paid_until > now
        live = a.status in ("running", "building") or f"bsapp-{a.name}" in running
        if live and not paid:
            n += 1
            print(f"  ❌ تستِ تمام‌شده هنوز روشن: اپ {a.name} (id={a.id}) مالک {a.owner_id} — "
                  f"پایانِ تست {_fmt(t.trial_expires_at)} — وضعیتِ DB: {a.status}")
    print("  ✅ مورد ندارد" if not n else f"  ← {n} مورد")
    bad += n

    print("\n══ ۲) سرور اختصاصی (میزبانی) ══")
    n = 0
    for a in apps:
        if a.status != "running":
            continue
        if a.paid_until and a.paid_until + GRACE < now:
            n += 1
            print(f"  ❌ انقضا گذشته ولی روشن: {a.name} (id={a.id}) مالک {a.owner_id} — تا {_fmt(a.paid_until)}")
        elif not a.paid_until and a.first_started_at and a.first_started_at + dt.timedelta(
                minutes=free_minutes) + GRACE < now:
            n += 1
            print(f"  ❌ بدونِ پرداخت و مهلتِ رایگان ({free_minutes}دقیقه) تمام: {a.name} (id={a.id}) "
                  f"مالک {a.owner_id} — شروع {_fmt(a.first_started_at)}")
    names_ok = {f"bsapp-{a.name}" for a in apps if a.status == "running"}
    for c in sorted(running):
        if not c.startswith("bsapp-"):
            continue
        if c not in names_ok:
            a = next((x for x in apps if f"bsapp-{x.name}" == c), None)
            n += 1
            print(f"  ❌ کانتینرِ روشن ولی در DB «{a.status if a else 'ناموجود'}»: {c}")
    print("  ✅ مورد ندارد" if not n else f"  ← {n} مورد")
    bad += n
    return bad


def audit_sftp() -> int:
    print("\n══ ۳) SFTP VIP ══")
    if not os.path.exists(SFTP_DB):
        print(f"  ⚠️ دیتابیس پیدا نشد: {SFTP_DB}")
        return 0
    con = sqlite3.connect(f"file:{SFTP_DB}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    bad = 0
    now = dt.datetime.utcnow()

    def _exp(v):
        if not v:
            return None
        try:
            return dt.datetime.fromisoformat(str(v).replace("Z", "")[:19].replace("T", " "))
        except ValueError:
            return None
    try:
        subs = con.execute("select telegram_id, expires_at, is_blocked, note from sftp_subscriptions").fetchall()
    except sqlite3.Error as e:
        print(f"  ⚠️ جدولِ sftp_subscriptions خوانده نشد: {e}")
        print("  ساختارِ دیتابیس:")
        for r in con.execute("select sql from sqlite_master where type='table'"):
            print("   ", (r[0] or "")[:200])
        return 0
    unlimited = [r for r in subs if not r["is_blocked"] and not r["expires_at"]]
    if unlimited:
        print(f"  ⚠️ اشتراکِ نامحدود (expires_at خالی) — {len(unlimited)} مورد؛ فقط اگر دستی اعطا کرده‌اید درست است:")
        for r in unlimited[:30]:
            print(f"     {r['telegram_id']}  note={r['note'] or ''}")
        bad += len(unlimited)
    expired_ids = {r["telegram_id"] for r in subs
                   if not r["is_blocked"] and _exp(r["expires_at"]) and _exp(r["expires_at"]) <= now}
    try:
        conns = con.execute("select telegram_id, last_used_at from sftp_connections "
                            "where last_used_at is not null").fetchall()
    except sqlite3.Error:
        conns = []
    exp_map = {r["telegram_id"]: _exp(r["expires_at"]) for r in subs}
    late = []
    for c in conns:
        e = exp_map.get(c["telegram_id"])
        u = _exp(c["last_used_at"])
        if e and u and u > e + GRACE:
            late.append((c["telegram_id"], e, u))
    if late:
        print(f"  ❌ استفاده بعد از انقضا — {len(late)} مورد:")
        for tid, e, u in late[:30]:
            print(f"     {tid}: انقضا {_fmt(e)} ⟵ آخرین استفاده {_fmt(u)}")
        bad += len(late)
    nobill = [c for c in conns if c["telegram_id"] not in exp_map]
    if nobill:
        print(f"  ❌ اتصالِ بدونِ هیچ اشتراک — {len(nobill)} مورد: "
              + ", ".join(str(c["telegram_id"]) for c in nobill[:15]))
        bad += len(nobill)
    print(f"  (اشتراک‌ها: {len(subs)} — منقضی‌شده و مسدودنشده: {len(expired_ids)})")
    if not bad:
        print("  ✅ مورد ندارد")
    con.close()
    return bad


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--free-minutes", type=int, default=0,
                    help="مهلتِ رایگانِ سرویسِ میزبانی؛ پیش‌فرض از تنظیمِ vip_free_minutes (۶۰)")
    a = ap.parse_args()
    fm = a.free_minutes
    if not fm:
        async with Session() as s:
            v = await s.scalar(select(Setting.value).where(Setting.bot_id == 0, Setting.key == "vip_free_minutes"))
        try:
            fm = int(float(v or 60))
        except ValueError:
            fm = 60
    bad = await audit_hosting(fm) + audit_sftp()
    print("\n" + ("✅ همه‌چیز تمیز است." if not bad else f"❌ مجموعاً {bad} مورد نیاز به بررسی دارد."))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
