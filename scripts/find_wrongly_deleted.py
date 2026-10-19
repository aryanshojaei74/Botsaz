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
فقط می‌خواند و چیزی را تغییر نمی‌دهد. اجرا از /opt/botsaz:

    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago"
    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago" --bot 313
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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="3 days ago")
    ap.add_argument("--unit", default="botsaz-tenant-worker")
    ap.add_argument("--bot", type=int, help="فقط همین bot_id")
    ap.add_argument("--stdin", action="store_true", help="لاگ را از stdin بخوان (به‌جای journalctl)")
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
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
