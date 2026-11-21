#!/usr/bin/env python3
"""
scripts/expiry_report.py — گزارش و اصلاحِ ناسازگاریِ «سررسید گذشته ولی ربات فعال»
و عکسِ آن («در بایگانی ولی سررسیدِ واقعی هنوز آینده است»).

اجرا از ریشهٔ پروژه (/opt/botsaz) با پایتونِ venv:

    venv/bin/python scripts/expiry_report.py                 # فقط گزارش
    venv/bin/python scripts/expiry_report.py --grant 30 --ids 12,34,56
    venv/bin/python scripts/expiry_report.py --grant 30 --group trial
    venv/bin/python scripts/expiry_report.py --archive --group nonpayment
    venv/bin/python scripts/expiry_report.py --clear-expiry --ids 7
    venv/bin/python scripts/expiry_report.py --restore --group wrongly_archived --dry-run
    venv/bin/python scripts/expiry_report.py --restore --group wrongly_archived

هیچ حالتی بدونِ فلگ چیزی را تغییر نمی‌دهد؛ همهٔ تغییرها هم اول با
--dry-run قابلِ دیدن‌اند.

گروه‌ها:
  expired_active    سررسید گذشته، archived_at خالی، هنوز جواب می‌دهد ← منشأ باگ
  ghost_archived    archived_at پر ولی status=="active" (حالتِ ناسازگار)
  no_expiry         plan_expires_at خالی روی پلنِ غیررایگان (هرگز منقضی نمی‌شود)
  wrongly_archived  در بایگانی، ولی سررسیدِ واقعی هنوز آینده است — معمولاً
                    قربانیِ باگِ expire_due_trials که فقط اسمِ پلن را چک
                    می‌کرد، نه اینکه سررسیدِ واقعی هنوز معتبر است یا نه
                    (رفع شد در plan_trials.py؛ این گروه قربانی‌هایِ قبل از
                    آن رفع را پیدا می‌کند). دستور بازگردانی: --restore
  trial             رباتِ تست (PartnerSale.kind == "trial")
  nonpayment        بقیهٔ expired_active
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from config import Session  # noqa: E402
from models import Bot as BotRow  # noqa: E402


async def _trial_bot_ids(s) -> set[int]:
    try:
        from models import PartnerSale
        rows = (await s.scalars(
            select(PartnerSale.sold_bot_id).where(PartnerSale.kind == "trial"))).all()
        return {r for r in rows if r}
    except Exception:
        return set()


async def collect():
    now = datetime.datetime.now()
    async with Session() as s:
        bots = (await s.scalars(select(BotRow))).all()
        trials = await _trial_bot_ids(s)
        out = {"expired_active": [], "ghost_archived": [], "no_expiry": [],
              "wrongly_archived": []}  # === PATCH_FIND_WRONGLY_ARCHIVED_V1 ===
        for b in bots:
            paid = (b.plan or "free") != "free"
            rec = {
                "id": b.id, "username": b.username, "owner": b.owner_id,
                "plan": b.plan, "status": b.status,
                "expires": b.plan_expires_at, "archived_at": b.archived_at,
                "archive_reason": b.archive_reason,  # PATCH_FIND_WRONGLY_ARCHIVED_V1
                "created": b.created_at, "trial": b.id in trials,
                "days_over": ((now - b.plan_expires_at).days
                              if b.plan_expires_at else None),
            }
            if b.archived_at and b.status == "active":
                out["ghost_archived"].append(rec)
            elif not b.plan_expires_at and paid:
                out["no_expiry"].append(rec)
            elif (b.plan_expires_at and b.plan_expires_at < now
                  and not b.archived_at and b.status == "active"):
                out["expired_active"].append(rec)
            # === PATCH_FIND_WRONGLY_ARCHIVED_V1 ===
            # عکسِ expired_active: رباتی که در بایگانی نشسته، ولی سررسیدِ
            # واقعی‌اش هنوز آینده است — یعنی یا با باگِ چک‌کردنِ فقط‌اسمِ‌پلن
            # در expire_due_trials بایگانی شده، یا به هر دلیلِ دیگر نباید
            # آنجا باشد.
            if b.archived_at and b.plan_expires_at and b.plan_expires_at > now:
                rec["days_left"] = (b.plan_expires_at - now).days
                out["wrongly_archived"].append(rec)
    return out


def _line(r):
    exp = r["expires"].strftime("%Y-%m-%d") if r["expires"] else "—"
    tag = "TRIAL" if r["trial"] else "PAID "
    over = f"{r['days_over']}d over" if r["days_over"] is not None else ""
    return (f"  #{r['id']:<5} @{(r['username'] or '?'):<24} {tag} "
            f"plan={r['plan']:<10} exp={exp} {over:<12} owner={r['owner']}")


def _line_wrongly_archived(r):
    from config import archive_reason_text
    exp = r["expires"].strftime("%Y-%m-%d") if r["expires"] else "—"
    reason = archive_reason_text(r.get("archive_reason"))
    return (f"  #{r['id']:<5} @{(r['username'] or '?'):<24} "
            f"plan={r['plan']:<10} exp={exp} ({r['days_left']}d left)  "
            f"دلیلِ بایگانی: {reason}  owner={r['owner']}")


def report(data):
    total = sum(len(v) for v in data.values())
    print("=" * 78)
    for name, title in (
        ("expired_active", "سررسید گذشته ولی هنوز فعال (اصلِ مشکل)"),
        ("ghost_archived", "archived_at پر ولی status=active (ناسازگار)"),
        ("no_expiry", "پلنِ پولی بدونِ سررسید (هرگز منقضی نمی‌شود)"),
        # === PATCH_FIND_WRONGLY_ARCHIVED_V1 ===
        ("wrongly_archived", "در بایگانی، ولی سررسیدِ واقعی هنوز آینده است (باگِ trial ↔ خریدِ واقعی)"),
    ):
        rows = data[name]
        print(f"\n[{name}] {title} — {len(rows)} مورد")
        if name == "wrongly_archived":
            for r in sorted(rows, key=lambda x: -x["days_left"]):
                print(_line_wrongly_archived(r))
        else:
            for r in sorted(rows, key=lambda x: -(x["days_over"] or 0)):
                print(_line(r))
    trials = [r for r in data["expired_active"] if r["trial"]]
    print(f"\nجمع: {total} | از expired_active: {len(trials)} تست، "
          f"{len(data['expired_active']) - len(trials)} غیرتست")
    print("=" * 78)


def _select(data, group, ids):
    if ids:
        want = {int(x) for x in ids.split(",") if x.strip()}
        pool = [r for v in data.values() for r in v]
        return [r for r in pool if r["id"] in want]
    if group == "trial":
        return [r for r in data["expired_active"] if r["trial"]]
    if group == "nonpayment":
        return [r for r in data["expired_active"] if not r["trial"]]
    if group in data:
        return data[group]
    return []


async def apply(rows, *, grant=0, archive=False, clear=False, restore=False, dry=False):
    now = datetime.datetime.now()
    async with Session() as s:
        for r in rows:
            b = await s.get(BotRow, r["id"])
            if not b:
                continue
            if grant:
                base = (b.plan_expires_at
                        if (b.plan_expires_at and b.plan_expires_at > now) else now)
                new = base + datetime.timedelta(days=grant)
                print(f"  #{b.id} @{b.username}: سررسید → {new:%Y-%m-%d}"
                      + (" [dry]" if dry else ""))
                if not dry:
                    b.plan_expires_at = new
                    b.last_expiry_notice = None
                    if b.archived_at and b.status in ("paused", "active"):
                        b.archived_at = None
                        b.archive_reason = None
                        b.status = "active"
            elif archive:
                print(f"  #{b.id} @{b.username}: → بایگانی" + (" [dry]" if dry else ""))
                if not dry:
                    b.status = "paused"
                    b.archived_at = now
                    b.archive_reason = "trial_expired" if r["trial"] else "nonpayment"
            elif clear:
                print(f"  #{b.id} @{b.username}: archived_at پاک شد"
                      + (" [dry]" if dry else ""))
                if not dry:
                    b.archived_at = None
                    b.archive_reason = None
            elif restore:
                # === PATCH_FIND_WRONGLY_ARCHIVED_V1 ===
                # برخلافِ --clear-expiry (که فقط archived_at را پاک می‌کند)،
                # اینجا وضعیت را هم به‌درستی به فعال برمی‌گردانیم — همان کاری
                # که منطقِ خودترمیمی در main_platform.py برایِ حالتِ ناسازگار
                # انجام می‌دهد.
                print(f"  #{b.id} @{b.username}: بازگردانی از بایگانی"
                      + (" [dry]" if dry else ""))
                if not dry:
                    b.status = "active"
                    b.archived_at = None
                    b.archive_reason = None
                    b.last_expiry_notice = None
        if not dry:
            await s.commit()
    if not dry and (archive or grant or restore):
        try:
            import tenant_status_bus
            async with Session() as s:
                for r in rows:
                    b = await s.get(BotRow, r["id"])
                    if b and b.token:
                        await tenant_status_bus.publish_tenant_invalidate(b.token)
        except Exception as e:
            print("هشدار: ابطالِ کشِ tenant ناموفق بود "
                  f"({e}) — یک restart سرویسِ tenant کافی است.")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", help="فهرستِ id ربات‌ها با کاما")
    ap.add_argument("--group", default=None,
                    help="expired_active | ghost_archived | no_expiry | wrongly_archived | trial | nonpayment")
    ap.add_argument("--grant", type=int, default=0, help="افزودن N روز از «امروز»")
    ap.add_argument("--archive", action="store_true", help="انتقال به بایگانی")
    ap.add_argument("--clear-expiry", dest="clear", action="store_true",
                    help="فقط پاک‌کردنِ archived_at ناسازگار")
    # === PATCH_FIND_WRONGLY_ARCHIVED_V1 ===
    ap.add_argument("--restore", action="store_true",
                    help="بازگردانیِ کاملِ ربات از بایگانی (status=active + پاک‌کردنِ دلیل)")
    ap.add_argument("--dry-run", dest="dry", action="store_true")
    a = ap.parse_args()

    data = await collect()
    report(data)
    if not (a.grant or a.archive or a.clear or a.restore):
        return
    rows = _select(data, a.group, a.ids)
    if not rows:
        print("\nهیچ رباتی با این فیلتر انتخاب نشد.")
        return
    print(f"\nاعمال روی {len(rows)} ربات:")
    await apply(rows, grant=a.grant, archive=a.archive, clear=a.clear,
               restore=a.restore, dry=a.dry)
    print("تمام." + (" (چیزی ذخیره نشد — dry-run)" if a.dry else ""))


if __name__ == "__main__":
    asyncio.run(main())
