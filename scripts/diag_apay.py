#!/usr/bin/env python3
"""
scripts/diag_apay.py — تشخیصِ فقط‌خواندنیِ «آپی بدونِ واریزِ فاکتور شارژ کرد» (PATCH_DIAG_APAY_V1).

برایِ رباتِ داده‌شده شارژهایِ آپیِ ۲۴ ساعتِ اخیر را نشان می‌دهد: مبلغِ فاکتور، مبلغِ دقیقِ واریز (با رقمِ یکتا)، زمانِ ساخت و
پرداخت، وضعیتِ فعلیِ درگاه (روشن/خاموش) و — با یک استعلامِ verify (فقط خواندن) — *فیلدهایِ پاسخِ آپی* تا ببینیم آپی
مبلغِ واقعاً واریزشده را هم می‌دهد یا نه. توکن هرگز چاپ نمی‌شود.

    cd /opt/botsaz && venv/bin/python scripts/diag_apay.py <یوزرنیمِ ربات یا بخشی از آن> [--hours 24] [--no-verify]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from config import Session  # noqa: E402
from models import ApayCharge, Bot as BotRow, BotUser, Setting, WalletTxn  # noqa: E402


async def main(q: str, hours: int, verify: bool) -> None:
    from tenant.gateways import apay as AP
    since = dt.datetime.utcnow() - dt.timedelta(hours=hours)
    async with Session() as s:
        bots = (await s.execute(select(BotRow).where(BotRow.username.ilike(f"%{q}%")))).scalars().all()
        if not bots:
            print("رباتی پیدا نشد.")
            return
        for b in bots:
            async def st(key, d=""):
                v = await s.scalar(select(Setting.value).where(Setting.bot_id == b.id, Setting.key == key))
                return v if v is not None else d
            en, tok, sec = await st("apay_enabled", "0"), await st("apay_token", ""), await st("apay_secret", "")
            print(f"\n=== @{b.username} id={b.id} | درگاه: {'روشن' if en == '1' else 'خاموش'} | توکن: {'دارد' if tok else 'ندارد'} | وب‌هوک-secret: {'دارد' if sec else 'ندارد'}")
            rows = (await s.scalars(select(ApayCharge).where(
                ApayCharge.bot_id == b.id, ApayCharge.created_at >= since).order_by(ApayCharge.id))).all()
            print(f"شارژهایِ {hours} ساعتِ اخیر: {len(rows)}")
            for c in rows:
                u = await s.scalar(select(BotUser).where(BotUser.bot_id == b.id, BotUser.telegram_id == c.user_telegram_id))
                who = ("@" + u.username) if u and u.username else (u.full_name if u else "—")
                gap = f"{int((c.paid_at - c.created_at).total_seconds() // 60)}دقیقه" if c.paid_at and c.created_at else "—"
                print(f"  #{c.id} {c.status:<9} کاربر={c.user_telegram_id} ({who}) فاکتور={c.amount_toman:,}ت +کارمزد {c.fee_toman or 0:,} "
                      f"| واریزِ دقیق={((c.pay_amount_rial or 0) // 10):,}ت | ساخت={c.created_at:%m-%d %H:%M} پرداخت={c.paid_at:%H:%M} ({gap})"
                      if c.paid_at else
                      f"  #{c.id} {c.status:<9} کاربر={c.user_telegram_id} ({who}) فاکتور={c.amount_toman:,}ت | واریزِ دقیق={((c.pay_amount_rial or 0) // 10):,}ت | ساخت={c.created_at:%m-%d %H:%M}")
                if verify and tok and c.payment_id:
                    code, res = await AP.verify_payment(tok, c.payment_id)
                    pay = (res or {}).get("payment") if isinstance(res, dict) else None
                    if isinstance(pay, dict):
                        keys = {k: v for k, v in pay.items() if k not in ("pay_url",)}
                        print("      پاسخِ آپی:", keys)
            txs = (await s.scalars(select(WalletTxn).where(
                WalletTxn.bot_id == b.id, WalletTxn.kind == "apay_charge", WalletTxn.created_at >= since))).all()
            print(f"تراکنشِ کیف‌پولِ apay_charge در همین بازه: {len(txs)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("bot")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.bot, a.hours, not a.no_verify))
