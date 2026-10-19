#!/usr/bin/env python3
"""
scripts/find_wrongly_deleted.py — فهرستِ سرویس‌هایی که «بعد از تمدید/خریدِ حجم» به‌اشتباه حذف شدند (PATCH_TRAFFIC_RENEW_GUARD_V1).

تیکتِ تعزیان: حلقهٔ traffic_cleanup سرویسِ حجم‌تمام‌شده را ۵ ساعت بعد از مهر حذف می‌کرد، حتی اگر کاربر در همین فاصله تمدید
کرده بود. لاگِ حذف فقط «نامِ کاربری روی پنل» را دارد (و ردیفِ PanelAccount دیگر نیست)، ولی نامِ کاربری شاملِ
تلگرام‌آیدیِ کاربر است (پیشوند + آیدی + چند نویسهٔ تصادفی). اسکریپت:

  ۱) خطوطِ «traffic_cleanup: سرویسِ '…' (bot_id=…) … حذف شد» را از journalctl می‌خواند،
  ۲) آیدیِ کاربر را از رویِ نامِ کاربری حدس می‌زند (همهٔ پیشوندهایِ ۸ تا ۱۰ رقمیِ رقم‌هایِ پشتِ‌سرِهم)،
  ۳) در WalletTxn دنبالِ «تمدید» یا «افزایش حجم» می‌گردد که *بعد از مهر و قبل از حذف* ثبت شده باشد.

هر موردِ پیداشده یعنی کاربر پول داده و سرویسش حذف شده؛ باید برایش سرویسِ جدید ساخت/جبران کرد.
بدونِ فلگ فقط می‌خواند و چیزی را تغییر نمی‌دهد. با `--propose` برایِ مالکِ *همان ربات* (با خودِ رباتش) یک «پیشنهادِ عودت» با دکمهٔ
✅ تأیید / ❌ رد می‌رود؛ فقط بعد از تأییدِ ادمینِ ربات، مبلغ به کیف پولِ کاربر برمی‌گردد. کسی که ادمین بعد از حذف دستی
شارژش کرده (kind=admin) پیشنهاد نمی‌گیرد (یا فقط مابه‌التفاوت). هر مورد فقط یک‌بار پیشنهاد می‌شود. اجرا از /opt/botsaz:

    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago"
    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago" --bot 313
    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago" --propose                # پیش‌نمایش + تأیید
    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago" --propose --exclude-email mc1277427337qv
    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago" --propose --only-user 7375002338 --yes
    journalctl -u botsaz-tenant-worker --since "7 days ago" --no-pager | venv/bin/python scripts/find_wrongly_deleted.py --stdin
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LINE_RE = re.compile(
    r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+\[\w+\].*?traffic_cleanup: سرویسِ '([^']+)' "
    r"\(bot_id=(\d+), panel=([^)]*)\).*?پس از ([\d.]+) ساعت")


def parse_lines(lines) -> list[dict]:
    out = []
    for ln in lines:
        m = LINE_RE.search(ln)
        if not m:
            continue
        out.append({"deleted_at": dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                    "email": m.group(2), "bot_id": int(m.group(3)), "panel": m.group(4),
                    "hours": float(m.group(5))})
    return out


def candidate_ids(email: str) -> set[int]:
    """تلگرام‌آیدیِ محتمل از رویِ نامِ کاربری: هر پیشوندِ ۸ تا ۱۰ رقمیِ هر رشته رقمِ پشتِ‌سرِهم."""
    cands: set[int] = set()
    for run in re.findall(r"\d{8,}", email):
        for n in range(8, min(10, len(run)) + 1):
            cands.add(int(run[:n]))
    return cands


async def analyze(events: list[dict], Session, margin_min: int = 20) -> list[dict]:
    """برایِ هر حذف، تمدید/خریدِ حجمِ بینِ «مهر» و «حذف» را پیدا می‌کند. فقط می‌خواند."""
    from sqlalchemy import select
    from models import BotUser, WalletTxn
    found = []
    async with Session() as s:
        for ev in events:
            cands = candidate_ids(ev["email"])
            if not cands:
                continue
            users = (await s.scalars(select(BotUser).where(
                BotUser.bot_id == ev["bot_id"], BotUser.telegram_id.in_(cands)))).all()
            if not users:
                continue
            start = ev["deleted_at"] - dt.timedelta(hours=ev["hours"], minutes=margin_min)
            for u in users:
                txns = (await s.scalars(select(WalletTxn).where(
                    WalletTxn.bot_id == ev["bot_id"], WalletTxn.user_id == u.id,
                    WalletTxn.created_at >= start, WalletTxn.created_at <= ev["deleted_at"]))).all()
                hit = [t for t in txns if t.kind == "renew"
                       or (t.kind == "purchase" and (t.note or "").startswith("افزایش حجم"))]
                if hit:
                    found.append({**ev, "telegram_id": u.telegram_id,
                                  "txns": [(t.created_at, t.kind, float(t.amount), t.note) for t in hit]})
    return found


def refund_amount(item: dict) -> int:
    """مجموعِ پرداخت‌هایِ داخلِ مهلت (مبلغ‌ها در دفتر منفی‌اند)."""
    return int(sum(-t[2] for t in item["txns"] if t[2] < 0))


SKIP_FA = {"exists": "قبلاً پیشنهاد داده شده", "manual_done": "ادمین قبلاً دستی جبران کرده",
           "already_refunded": "قبلاً عودت شده", "no_user": "کاربر در ربات نیست"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="3 days ago")
    ap.add_argument("--unit", default="botsaz-tenant-worker")
    ap.add_argument("--bot", type=int, help="فقط همین bot_id")
    ap.add_argument("--stdin", action="store_true", help="لاگ را از stdin بخوان (به‌جای journalctl)")
    ap.add_argument("--propose", action="store_true",
                    help="برایِ مالکِ هر ربات پیشنهادِ عودت (با دکمهٔ تأیید/رد) بفرست؛ مبلغ تا تأییدِ ادمین برنمی‌گردد")
    ap.add_argument("--yes", action="store_true", help="با --propose: بدونِ پرسشِ تأیید اجرا کن")
    ap.add_argument("--only-user", type=int, action="append", default=[], help="فقط این تلگرام‌آیدی (تکرارپذیر)")
    ap.add_argument("--exclude-email", action="append", default=[],
                    help="این سرویس را کنار بگذار (مثلاً سرویسی که هنوز رویِ پنل زنده است)")
    a = ap.parse_args()
    if a.stdin:
        lines = sys.stdin.read().splitlines()
    else:
        out = subprocess.run(["journalctl", "-u", a.unit, "--since", a.since, "--no-pager"],
                             capture_output=True, text=True, timeout=120)
        lines = out.stdout.splitlines()
    events = parse_lines(lines)
    if a.bot:
        events = [e for e in events if e["bot_id"] == a.bot]
    print(f"حذف‌هایِ اتمامِ حجم در لاگ: {len(events)}")
    from config import Session
    found = await analyze(events, Session)
    if not found:
        print("✅ هیچ حذفی پیدا نشد که کاربر ظرفِ مهلت تمدید/خریدِ حجم کرده باشد.")
        return 0
    print(f"\n❌ {len(found)} سرویس بعد از تمدید/خریدِ حجم حذف شده‌اند:\n")
    by_bot: dict[int, list[dict]] = {}
    for f in found:
        by_bot.setdefault(f["bot_id"], []).append(f)
    for bid, items in sorted(by_bot.items()):
        print(f"── ربات {bid} ({len(items)} مورد)")
        for f in items:
            print(f"  • کاربر {f['telegram_id']} — سرویسِ {f['email']} ({f['panel']}) — حذف {f['deleted_at']:%m-%d %H:%M}")
            for t in f["txns"]:
                print(f"      پرداخت: {t[0]:%m-%d %H:%M} {t[1]} {t[2]:,.0f} — {t[3]}")
    if not a.propose:
        print("\nبرایِ ارسالِ پیشنهادِ عودت به مالکانِ رباتِ هر مورد: همین دستور را با --propose اجرا کن.")
        return 1

    from tenant.shared.wrong_delete_refund import create_proposal, evaluate, send_proposal
    plan = [f for f in found
            if f["email"] not in a.exclude_email and (not a.only_user or f["telegram_id"] in a.only_user)]
    if not plan:
        print("هیچ موردی باقی نماند (فیلترها).")
        return 1
    print("\n══ پیش‌نمایشِ پیشنهادها ══")
    todo = []
    for f in plan:
        paid = refund_amount(f)
        ev = await evaluate(f["bot_id"], f["telegram_id"], f["email"], paid, f["deleted_at"])
        if ev["status"] == "new":
            extra = f" (قبلاً {ev['credited']:,} دستی شارژ شده؛ مابه‌التفاوت)" if ev["credited"] else ""
            print(f"  ✉️ ربات {f['bot_id']} · کاربر {f['telegram_id']} ← پیشنهاد {ev['amount']:,} تومان ({f['email']}){extra}")
            todo.append((f, paid))
        else:
            print(f"  ⏭ ربات {f['bot_id']} · کاربر {f['telegram_id']} — {SKIP_FA.get(ev['status'], ev['status'])} ({f['email']})")
    if not todo:
        print("\nچیزی برایِ ارسال نیست.")
        return 0
    print(f"\nپیشنهاد برایِ مالکِ {len({f['bot_id'] for f, _ in todo})} ربات فرستاده می‌شود؛ تا تأییدِ ادمینِ ربات هیچ مبلغی برنمی‌گردد.")
    print("⚠️ سرویسی که حذفش از پنل شکست خورده و هنوز زنده است را با --exclude-email کنار بگذار.")
    if not a.yes:
        if input("\nارسالِ پیشنهادها؟ (بنویس yes): ").strip().lower() != "yes":
            print("لغو شد؛ چیزی ارسال یا ثبت نشد.")
            return 1
    for f, paid in todo:
        r = await create_proposal(f["bot_id"], f["telegram_id"], f["email"], paid, f["deleted_at"])
        if r["status"] != "created":
            print(f"  ⏭ {f['email']}: {SKIP_FA.get(r['status'], r['status'])}")
            continue
        sent = await send_proposal(f["bot_id"], r["pid"], r["data"])
        print(f"  {'✅ ارسال شد' if sent else '⚠️ ثبت شد ولی به مالک نرسید (ربات را استارت نکرده؟)'}: "
              f"ربات {f['bot_id']} ← {f['email']} ({r['amount']:,} تومان)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
