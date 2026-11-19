#!/usr/bin/env python3
"""
scripts/diag_bots.py — «چرا این ربات کار نمی‌کند، با اینکه مهلتش تمام نشده؟» (PATCH_BOT_DIAG_V1)

برای هر رباتِ غیربایگانی که پلنش هنوز معتبر است، از تلگرام می‌پرسد (getMe + getWebhookInfo) و دلیلِ احتمالیِ کار نکردن را دسته‌بندی می‌کند:

  DEAD_TOKEN      توکن در BotFather عوض/باطل شده (getMe ⇒ Unauthorized). راه‌حل: مالک توکنِ جدید را در بات‌ساز بدهد.   [خودکار قابلِ‌اصلاح نیست]
  STATUS_BLOCKED  وضعیتِ دیتابیس active نیست (paused/locked/admin_paused/disabled_bad_token) با اینکه پلن معتبر است.
                  disabled_bad_token + getMe سالم ⇒ با --fix دوباره active می‌شود؛ بقیه (خاموشیِ مالک/قفلِ ادمین) دست‌نمی‌خورد.
  WRONG_WEBHOOK   وبهوکِ ربات در تلگرام خالی است یا به آدرسِ دیگری اشاره می‌کند ⇒ با --fix درست ست می‌شود.
  MISSING_UPDATES وبهوک فهرستِ allowed_updates کامل ندارد (مثلاً بدونِ callback_query) ⇒ با --fix ست می‌شود.
  DELIVERY_ERROR  آپدیتِ تحویل‌نشده (pending) یا خطای تازهٔ تحویل (≤۶۰ دقیقه)؛ سمتِ سرور/شبکه است نه ربات.
  OK              سالم.

    cd /opt/botsaz && python3 scripts/diag_bots.py                # فقط گزارش
    python3 scripts/diag_bots.py whale                            # فقط رباتی که یوزرنیمش «whale» دارد
    python3 scripts/diag_bots.py --fix                            # اصلاحِ امنِ WRONG_WEBHOOK / MISSING_UPDATES / disabled_bad_token
    python3 scripts/diag_bots.py --all                            # حتی سالم‌ها را هم چاپ کن

توکن‌ها هرگز چاپ نمی‌شوند.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CLASSES = ("DEAD_TOKEN", "STATUS_BLOCKED", "WRONG_WEBHOOK", "MISSING_UPDATES", "DELIVERY_ERROR", "OK")
_NEED_UPDATES = ("message", "callback_query")


def classify(db_status: str, getme: str, wh: dict, expected: str | None, now: float | None = None,
             allowed_all: tuple | list = ()) -> tuple[str, str]:
    """(دسته، توضیح). getme: 'ok' | 'unauthorized' | 'error'. wh: خروجیِ getWebhookInfo (dict با کلیدهایِ تلگرام)."""
    now = time.time() if now is None else now
    if getme == "unauthorized":
        return "DEAD_TOKEN", "تلگرام توکن را نمی‌شناسد (در BotFather عوض/باطل شده)"
    if db_status != "active":
        return "STATUS_BLOCKED", f"وضعیتِ دیتابیس: {db_status}"
    if getme == "error":
        return "DELIVERY_ERROR", "پاسخِ getMe نیامد (شبکه/تایم‌اوت)؛ دوباره امتحان کن"
    url = wh.get("url") or ""
    if expected and url != expected:
        return "WRONG_WEBHOOK", "وبهوک خالی است" if not url else "وبهوک به آدرسِ دیگری اشاره می‌کند"
    got = set(wh.get("allowed_updates") or [])
    missing = [u for u in (allowed_all or _NEED_UPDATES) if u not in got]
    if missing and (not got or any(u in _NEED_UPDATES for u in missing) or "chat_member" in missing):
        return "MISSING_UPDATES", "allowed_updates ناقص: " + ("پیش‌فرضِ تلگرام" if not got else ",".join(missing[:4]))
    err_ts = wh.get("last_error_date")
    age = (now - err_ts) / 60 if err_ts else None
    pending = int(wh.get("pending_update_count") or 0)
    if pending > 0 or (age is not None and age <= 60):
        return "DELIVERY_ERROR", f"pending={pending}؛ آخرین خطا: {wh.get('last_error_message') or '—'}" + (
            f" ({age:.0f} دقیقه پیش)" if age is not None else "")
    return "OK", ""


async def _inspect(b, expected, allowed_all, fix: bool):
    from aiogram.exceptions import TelegramUnauthorizedError
    from bot_utils import _temp_bot
    out = {"cls": "OK", "note": "", "fixed": ""}
    try:
        async with _temp_bot(b["token"]) as tb:
            try:
                await asyncio.wait_for(tb.get_me(), timeout=12)
                getme = "ok"
            except TelegramUnauthorizedError:
                getme = "unauthorized"
            except Exception:  # noqa: BLE001
                getme = "error"
            wh = {}
            if getme == "ok":
                try:
                    info = await asyncio.wait_for(tb.get_webhook_info(), timeout=12)
                    wh = info.model_dump()
                except Exception:  # noqa: BLE001
                    getme = "error"
            out["cls"], out["note"] = classify(b["status"], getme, wh, expected, allowed_all=allowed_all)
            if fix and out["cls"] == "DEAD_TOKEN" and b["status"] == "active":
                # توکنِ باطل ولی وضعیت «فعال» ⇒ در پنلِ مدیر «❌ توکن نامعتبر» نشان داده شود (selfheal اگر روزی توکن سالم شد برش می‌گرداند)
                from config import Session
                from models import Bot as BotRow
                async with Session() as s:
                    br = await s.get(BotRow, b["id"])
                    if br and br.status == "active" and br.archived_at is None:
                        br.status = "disabled_bad_token"
                        await s.commit()
                out["fixed"] = "وضعیت ⇒ disabled_bad_token (مالک باید توکنِ جدید بدهد)"
            if fix and getme == "ok" and (out["cls"] in ("WRONG_WEBHOOK", "MISSING_UPDATES") or
                                          (out["cls"] == "STATUS_BLOCKED" and b["status"] == "disabled_bad_token")):
                if b["status"] == "disabled_bad_token":
                    from config import Session
                    from models import Bot as BotRow
                    async with Session() as s:
                        br = await s.get(BotRow, b["id"])
                        if br and br.status == "disabled_bad_token" and br.archived_at is None:
                            br.status = "active"
                            await s.commit()
                    out["fixed"] = "وضعیت ⇒ active"
                if expected:
                    await asyncio.wait_for(tb.set_webhook(expected, drop_pending_updates=False,
                                                          allowed_updates=list(allowed_all)), timeout=15)
                    out["fixed"] = (out["fixed"] + " + " if out["fixed"] else "") + "وبهوک ست شد"
                try:
                    import tenant_status_bus
                    await tenant_status_bus.publish_tenant_invalidate(b["token"])
                except Exception:  # noqa: BLE001 — کشِ وضعیت تا چند ثانیه بعد خودش تازه می‌شود
                    pass
    except Exception as e:  # noqa: BLE001
        out["cls"], out["note"] = "DELIVERY_ERROR", f"بررسی ناموفق: {type(e).__name__}"
    return out


async def main(argv: list[str]) -> int:
    from sqlalchemy import select
    from config import Session, WEBHOOK_BASE
    from models import Bot
    from webhook_utils import TENANT_ALLOWED_UPDATES

    fix, show_all = "--fix" in argv, "--all" in argv
    names = [a.lower() for a in argv if not a.startswith("--")]
    now = dt.datetime.utcnow()
    async with Session() as s:
        rows = (await s.execute(select(Bot.id, Bot.username, Bot.token, Bot.status, Bot.owner_id, Bot.plan,
                                       Bot.plan_expires_at, Bot.lock_reason).where(Bot.archived_at.is_(None)))).all()
    bots = [dict(id=r.id, user=r.username or str(r.id), token=r.token, status=r.status or "", owner=r.owner_id,
                 plan=r.plan, exp=r.plan_expires_at, reason=r.lock_reason or "")
            for r in rows if (r.plan_expires_at is None or r.plan_expires_at > now)
            and (not names or any(n in (r.username or "").lower() for n in names))]
    print(f"WEBHOOK_BASE={WEBHOOK_BASE or '(خالی!)'} | رباتِ غیربایگانیِ با پلنِ معتبر: {len(bots)} | حالت: {'اصلاح' if fix else 'فقط گزارش'}")
    sem = asyncio.Semaphore(8)

    async def one(b):
        async with sem:
            exp = f"{WEBHOOK_BASE}/webhook/{b['token']}" if WEBHOOK_BASE else None
            return b, await _inspect(b, exp, TENANT_ALLOWED_UPDATES, fix)
    res = await asyncio.gather(*(one(b) for b in bots))
    counts = {c: sum(1 for _, o in res if o["cls"] == c) for c in CLASSES}
    print("خلاصه: " + " | ".join(f"{c}={n}" for c, n in counts.items()))
    for cls in CLASSES[:-1] + (("OK",) if show_all else ()):
        items = [(b, o) for b, o in res if o["cls"] == cls]
        if not items:
            continue
        print(f"\n── {cls} ({len(items)}) ──")
        for b, o in items:
            extra = f" | {b['reason'][:50]}" if b["status"] in ("paused", "locked") and b["reason"] else ""
            print(f"  @{b['user']:<26} owner={b['owner']} plan={b['plan']} status={b['status']}{extra}\n"
                  f"      {o['note']}" + (f"\n      ✅ اصلاح شد: {o['fixed']}" if o["fixed"] else ""))
    if not fix and (counts["WRONG_WEBHOOK"] or counts["MISSING_UPDATES"] or counts["STATUS_BLOCKED"]):
        print("\nبرای اصلاحِ خودکارِ موارد قابلِ‌اصلاح:  python3 scripts/diag_bots.py --fix")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
