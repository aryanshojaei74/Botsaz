#!/usr/bin/env python3
"""tonpays_recheck.py — بررسی/تسویهٔ دستیِ فاکتورهایِ TonPays که پرداخت شده ولی شارژ نشده‌اند.

اجرا (از پوشهٔ /opt/botsaz):
    python3 tonpays_recheck.py                 # فقط گزارش (چیزی شارژ نمی‌شود)
    python3 tonpays_recheck.py --hours 72      # بازهٔ بزرگ‌تر
    python3 tonpays_recheck.py --user 123456   # فقط یک کاربر (آیدی عددی تلگرام)
    python3 tonpays_recheck.py --apply         # اگر درگاه «پرداخت‌شده» گفت، شارژ هم می‌کند

شارژ فقط از مسیرِ امنِ settle_charge انجام می‌شود (استعلام از خودِ درگاه + تطبیقِ مبلغ + قفلِ اتمی)."""
import argparse
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from config import Session
from models import TonPaysCharge
from tenant.gateways import tonpays as T


async def main(hours: int, user: int | None, apply: bool) -> None:
    cut = datetime.utcnow() - timedelta(hours=hours)
    async with Session() as s:
        q = select(TonPaysCharge).where(TonPaysCharge.status != "paid", TonPaysCharge.created_at >= cut)
        if user:
            q = q.where(TonPaysCharge.user_telegram_id == user)
        rows = (await s.scalars(q.order_by(TonPaysCharge.id.desc()))).all()
        items = [(c.bot_id, c.order_id, c.invoice_id, c.user_telegram_id, c.request_amount, c.status, c.created_at)
                 for c in rows]
    print(f"{len(items)} فاکتورِ تسویه‌نشده در {hours} ساعتِ اخیر")
    for bot_id, oid, inv, tg, amt, st, ts in items:
        line = f"bot={bot_id} user={tg} order={oid} amount={amt} db={st} at={ts:%m-%d %H:%M}"
        try:
            if apply:
                ok, note, credit = await T.settle_charge(bot_id, oid)
                print(line, "=>", "✅ شارژ شد" if ok else "—", note, credit or "")
            else:
                key = await T.get_key(bot_id, T.is_custom_order(oid))
                info = await T.check_invoice(key, inv, custom=T.is_custom_order(oid))
                print(line, "=> درگاه:", info.get("status"), "paid=" + str(T.is_paid(info)))
        except Exception as e:  # noqa: BLE001
            print(line, "=> خطا:", e)
        await asyncio.sleep(1.1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--user", type=int)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.hours, a.user, a.apply))
