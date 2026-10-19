#!/usr/bin/env python3
"""
scripts/diag_bots_off.py — تشخیصِ فقط‌خواندنیِ «ربات‌ها خاموش شده‌اند» (PATCH_DIAG_BOTS_OFF_V1).

هیچ‌چیز را تغییر نمی‌دهد (نه DB، نه وبهوک). سه بخش:

  ۱) وضعیتِ ربات‌هایِ مشتری در DB: به تفکیکِ status، پایانِ پلن، بایگانی (و دلیل).
  ۲) اپ‌هایِ اختصاصی/میزبانی‌شده (HostedApp): suspended/stopped که پرداختِ معتبر دارند (مظنونِ مثبتِ کاذب).
  ۳) بررسیِ زندهٔ تلگرام برایِ هر رباتِ فعال: getMe (توکن زنده است؟) + getWebhookInfo
     (آدرسِ وبهوک درست است؟ خطایِ تحویل؟ آپدیتِ گیرکرده؟). این‌جا مشخص می‌شود ربات واقعاً آپدیت می‌گیرد یا نه.

اجرا از /opt/botsaz:

    venv/bin/python scripts/diag_bots_off.py
    venv/bin/python scripts/diag_bots_off.py --no-live           # بدونِ تماس با تلگرام
    venv/bin/python scripts/diag_bots_off.py --bot-username Jx07tR_JoYP4_bot
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from config import WEBHOOK_BASE, Session  # noqa: E402
from models import Bot as BotRow, HostedApp  # noqa: E402

ERR_RECENT = dt.timedelta(hours=1)       # خطایِ تحویلِ وبهوک در این بازه = «الان مشکل دارد»
PENDING_STUCK = 50                       # آپدیتِ صف‌شدهٔ بیشتر از این = گیرکرده


def _fmt(d) -> str:
    return d.strftime("%Y-%m-%d %H:%M") if d else "—"


def classify(info: dict, expected: str | None, now: dt.datetime) -> list[str]:
    """info = {url, pending, last_error_date, last_error_message}. فهرستِ مشکل‌ها؛ خالی = سالم."""
    out: list[str] = []
    url = info.get("url") or ""
    if not url:
        out.append("وبهوک ست نیست (هیچ آپدیتی نمی‌رسد)")
    elif expected and url != expected:
        out.append("وبهوک رویِ آدرسِ دیگری است (ربات/سرورِ دیگری وبهوک را عوض کرده)")
    led = info.get("last_error_date")
    if led and now - led <= ERR_RECENT:
        out.append(f"خطایِ تحویلِ اخیر: {(info.get('last_error_message') or '')[:120]}")
    if (info.get("pending") or 0) >= PENDING_STUCK:
        out.append(f"{info['pending']} آپدیتِ گیرکرده در صف")
    return out


async def db_report() -> None:
    now = dt.datetime.utcnow()
    async with Session() as s:
        by_status = (await s.execute(select(BotRow.status, func.count()).where(
            BotRow.archived_at.is_(None)).group_by(BotRow.status))).all()
        by_arch = (await s.execute(select(BotRow.archive_reason, func.count()).where(
            BotRow.archived_at.is_not(None)).group_by(BotRow.archive_reason))).all()
        recent_arch = (await s.execute(select(
            BotRow.id, BotRow.username, BotRow.owner_id, BotRow.archive_reason, BotRow.archived_at, BotRow.plan
        ).where(BotRow.archived_at >= now - dt.timedelta(days=3)).order_by(BotRow.archived_at.desc()).limit(40))).all()
        inactive = (await s.execute(select(
            BotRow.id, BotRow.username, BotRow.owner_id, BotRow.status, BotRow.plan, BotRow.plan_expires_at,
            BotRow.last_activity_at, BotRow.lock_reason
        ).where(BotRow.archived_at.is_(None), BotRow.status != "active").order_by(BotRow.id.desc()).limit(60))).all()
        expired_active = (await s.execute(select(
            BotRow.id, BotRow.username, BotRow.plan, BotRow.plan_expires_at
        ).where(BotRow.archived_at.is_(None), BotRow.status == "active",
                BotRow.plan_expires_at.is_not(None), BotRow.plan_expires_at < now).limit(30))).all()
    print("══ ۱) ربات‌هایِ مشتری (DB) ══")
    print("  وضعیت (غیربایگانی):", {k or "—": v for k, v in by_status})
    print("  بایگانی‌شده به تفکیکِ دلیل:", {k or "نامشخص": v for k, v in by_arch})
    print(f"\n  بایگانی‌شده در ۳ روزِ اخیر ({len(recent_arch)}):")
    for r in recent_arch:
        print(f"    #{r.id} @{r.username} مالک {r.owner_id} پلن {r.plan} دلیل={r.archive_reason or '—'} در {_fmt(r.archived_at)}")
    print(f"\n  غیرفعال‌ها (status≠active، غیربایگانی) — {len(inactive)} مورد (حداکثر ۶۰):")
    for r in inactive:
        print(f"    #{r.id} @{r.username} مالک {r.owner_id} status={r.status} پلن={r.plan} "
              f"انقضا={_fmt(r.plan_expires_at)} آخرین‌فعالیت={_fmt(r.last_activity_at)}"
              + (f" قفل: {(r.lock_reason or '')[:50]}" if r.lock_reason else ""))
    print(f"\n  فعال ولی پلن گذشته — {len(expired_active)} مورد (باید ظرفِ مدتِ کوتاه پاز/بایگانی شوند):")
    for r in expired_active:
        print(f"    #{r.id} @{r.username} پلن={r.plan} انقضا={_fmt(r.plan_expires_at)}")


async def hosted_report() -> None:
    now = dt.datetime.now()
    async with Session() as s:
        by_status = (await s.execute(select(HostedApp.status, func.count()).group_by(HostedApp.status))).all()
        suspicious = (await s.scalars(select(HostedApp).where(
            HostedApp.status.in_(("suspended", "stopped", "failed")), HostedApp.paid_until > now))).all()
        recent = (await s.scalars(select(HostedApp).where(
            HostedApp.updated_at >= now - dt.timedelta(hours=24),
            HostedApp.status.in_(("suspended", "stopped", "failed"))).order_by(HostedApp.updated_at.desc()).limit(40))).all()
    print("\n══ ۲) ربات‌هایِ اختصاصی (HostedApp) ══")
    print("  وضعیت:", {k or "—": v for k, v in by_status})
    print(f"  ❗ متوقف/suspended/failed ولی پرداختِ معتبر دارند — {len(suspicious)} مورد:")
    for a in suspicious:
        print(f"    #{a.id} {a.name} مالک {a.owner_id} status={a.status} paid_until={_fmt(a.paid_until)} "
              f"updated={_fmt(a.updated_at)} خطا: {(a.last_error or '')[:80]}")
    print(f"  تغییرِ وضعیتِ ۲۴ ساعتِ اخیر به متوقف/suspended/failed — {len(recent)} مورد:")
    for a in recent:
        print(f"    #{a.id} {a.name} مالک {a.owner_id} status={a.status} paid_until={_fmt(a.paid_until)} "
              f"updated={_fmt(a.updated_at)}")


async def live_report(only_username: str | None) -> None:
    from bot_utils import _temp_bot
    async with Session() as s:
        q = select(BotRow.id, BotRow.username, BotRow.token).where(
            BotRow.archived_at.is_(None), BotRow.status == "active")
        if only_username:
            q = q.where(func.lower(BotRow.username) == only_username.lstrip("@").lower())
        rows = (await s.execute(q)).all()
    print(f"\n══ ۳) بررسیِ زندهٔ تلگرام ({len(rows)} رباتِ فعال) ══")
    if not WEBHOOK_BASE:
        print("  ⚠️ WEBHOOK_BASE تنظیم نیست؛ آدرسِ وبهوک مقایسه نمی‌شود.")
    sem = asyncio.Semaphore(8)
    results: list[tuple] = []
    now = dt.datetime.utcnow()

    async def one(r) -> None:
        async with sem:
            try:
                async with _temp_bot(r.token) as tb:
                    await asyncio.wait_for(tb.get_me(), timeout=12)
                    wh = await asyncio.wait_for(tb.get_webhook_info(), timeout=12)
                led = wh.last_error_date
                if led is not None and led.tzinfo is not None:
                    led = led.astimezone(dt.timezone.utc).replace(tzinfo=None)
                info = {"url": wh.url, "pending": wh.pending_update_count,
                        "last_error_date": led, "last_error_message": wh.last_error_message}
                exp = f"{WEBHOOK_BASE}/webhook/{r.token}" if WEBHOOK_BASE else None
                results.append((r, classify(info, exp, now), info))
            except Exception as e:  # noqa: BLE001 — Unauthorized/timeout/شبکه
                results.append((r, [f"تماس با تلگرام ناموفق: {type(e).__name__}: {str(e)[:100]}"], {}))

    await asyncio.gather(*(one(r) for r in rows))
    bad = [x for x in results if x[1]]
    print(f"  ✅ سالم: {len(results) - len(bad)} · ❌ مشکل‌دار: {len(bad)}")
    for r, probs, info in sorted(bad, key=lambda x: x[0].id):
        print(f"    #{r.id} @{r.username}: " + " | ".join(probs)
              + (f" [صف={info.get('pending')}]" if info else ""))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-live", action="store_true", help="تماس با تلگرام نگیر")
    ap.add_argument("--bot-username", help="فقط همین ربات را در بخشِ زنده چک کن")
    a = ap.parse_args()
    await db_report()
    await hosted_report()
    if not a.no_live:
        await live_report(a.bot_username)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
