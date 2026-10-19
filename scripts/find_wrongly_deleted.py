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
بدونِ فلگ فقط می‌خواند و چیزی را تغییر نمی‌دهد. با `--refund` مبلغِ پرداخت‌شده به کیف پولِ کاربر برمی‌گردد
(اول فهرستِ دقیق + تأییدِ دستی؛ هر مورد فقط یک‌بار — اجرای دوباره دوباره‌کاری نمی‌کند). اجرا از /opt/botsaz:

    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago"
    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago" --bot 313
    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago" --refund                 # فهرست + تأیید
    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago" --refund --exclude-email mc1277427337qv
    venv/bin/python scripts/find_wrongly_deleted.py --since "3 days ago" --refund --only-user 7375002338 --yes
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


REFUND_NOTE_PREFIX = "عودتِ حذفِ اشتباهِ سرویس "


def refund_note(email: str) -> str:
    return (REFUND_NOTE_PREFIX + email)[:200]


def refund_amount(item: dict) -> int:
    """مجموعِ پرداخت‌هایِ داخلِ مهلت (مبلغ‌ها در دفتر منفی‌اند)."""
    return int(sum(-t[2] for t in item["txns"] if t[2] < 0))


async def _send(token: str, chat_id: int, text: str) -> bool:
    """پیامِ best-effort از طریقِ خودِ رباتِ مشتری؛ شکست مهم نیست."""
    try:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        b = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await b.send_message(chat_id, text)
            return True
        finally:
            await b.session.close()
    except Exception:  # noqa: BLE001
        return False


async def apply_refunds(found: list[dict], Session, send=_send) -> list[dict]:
    """مبلغِ پرداخت‌شده را به کیف پولِ هر کاربر برمی‌گرداند. هر (ربات، کاربر، سرویس) فقط یک‌بار
    (یادداشتِ تراکنشِ عودت کلیدِ جلوگیری از تکرار است). نتیجه: لیستِ {…, status: refunded|skipped, amount}."""
    from sqlalchemy import select
    from models import Bot as BotRow, BotUser, WalletTxn
    results = []
    for it in found:
        amount = refund_amount(it)
        res = {**it, "amount": amount, "status": "skipped"}
        if amount <= 0:
            results.append(res)
            continue
        note = refund_note(it["email"])
        async with Session() as s:
            u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == it["bot_id"], BotUser.telegram_id == it["telegram_id"]).with_for_update())
            if not u:
                results.append(res)
                continue
            dup = await s.scalar(select(WalletTxn.id).where(
                WalletTxn.bot_id == it["bot_id"], WalletTxn.user_id == u.id,
                WalletTxn.kind == "refund", WalletTxn.note == note).limit(1))
            if dup:
                res["status"] = "already"
                results.append(res)
                continue
            u.balance = float(u.balance or 0) + amount
            s.add(WalletTxn(bot_id=it["bot_id"], user_id=u.id, amount=amount, kind="refund", note=note))
            brow = await s.get(BotRow, it["bot_id"])
            token, owner_id = (brow.token, brow.owner_id) if brow else (None, None)
            new_balance = float(u.balance)
            await s.commit()
        res["status"] = "refunded"
        results.append(res)
        if token:
            await send(token, it["telegram_id"],
                       "🙏 <b>عذرخواهی بابت یک خطای فنی</b>\n\n"
                       f"سرویسِ <code>{it['email']}</code> پس از تمدید/افزایشِ حجم به‌اشتباه حذف شد. "
                       f"مبلغِ <b>{amount:,} تومان</b> به کیف پولت برگردانده شد (موجودی: {int(new_balance):,} تومان). "
                       "می‌توانی دوباره سرویس تهیه کنی.")
            if owner_id:
                await send(token, owner_id,
                           f"ℹ️ به‌خاطرِ باگِ حذفِ خودکار، {amount:,} تومان به کیف پولِ کاربر "
                           f"<code>{it['telegram_id']}</code> (سرویسِ <code>{it['email']}</code>) برگردانده شد.")
    return results


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="3 days ago")
    ap.add_argument("--unit", default="botsaz-tenant-worker")
    ap.add_argument("--bot", type=int, help="فقط همین bot_id")
    ap.add_argument("--stdin", action="store_true", help="لاگ را از stdin بخوان (به‌جای journalctl)")
    ap.add_argument("--refund", action="store_true", help="مبلغِ پرداخت‌شده را به کیف پولِ کاربرانِ فهرست برگردان")
    ap.add_argument("--yes", action="store_true", help="با --refund: بدونِ پرسشِ تأیید اجرا کن")
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
    if not a.refund:
        print("\nبرایِ برگشتِ مبلغ به کیف پول: همین دستور را با --refund اجرا کن (اول فهرستِ تأیید نشان داده می‌شود).")
        return 1

    plan = [f for f in found
            if f["email"] not in a.exclude_email and (not a.only_user or f["telegram_id"] in a.only_user)]
    if not plan:
        print("هیچ موردی برایِ عودت باقی نماند (فیلترها).")
        return 1
    total = sum(refund_amount(f) for f in plan)
    print(f"\n══ برنامهٔ عودت ({len(plan)} مورد، مجموع {total:,} تومان) ══")
    for f in plan:
        print(f"  ربات {f['bot_id']} · کاربر {f['telegram_id']} ← {refund_amount(f):,} تومان ({f['email']})")
    print("⚠️ اگر سرویسی هنوز رویِ پنل زنده است (حذفش از پنل شکست خورده بود)، آن را با --exclude-email کنار بگذار؛ "
          "وگرنه کاربر هم سرویس را دارد و هم پولش را.")
    if not a.yes:
        if input("\nادامه و واریز به کیف پول؟ (بنویس yes): ").strip().lower() != "yes":
            print("لغو شد؛ چیزی تغییر نکرد.")
            return 1
    res = await apply_refunds(plan, Session)
    for r in res:
        label = {"refunded": "✅ برگشت داده شد", "already": "↩️ قبلاً عودت شده بود", "skipped": "⏭ رد شد"}[r["status"]]
        print(f"  {label}: کاربر {r['telegram_id']} — {r['amount']:,} تومان")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
