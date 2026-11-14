"""
backfill_stuck_zarinpal_tenant.py — جبرانِ شارژهای گیرکردهٔ قدیمیِ زرین‌پالِ
کیف‌پولِ ربات‌هایِ مستأجر (پرداختی که پول از کاربر کسر شده ولی سفارش هنوز
pendingه).

چرا لازم شد: تشخیص دادیم که مکانیزمِ خودکارِ موجود
(zarinpal_wallet_poll_loop در main_tenant.py) فقط سفارش‌هایِ pendingِ
تازه‌تر از ۶ ساعت را دوباره استعلام می‌گیرد (_STALE_AFTER_HOURS=6). اگر
verify_payment برایِ سفارشی در همان ۶ ساعتِ اول به هر دلیلی (تأخیرِ
تسویهٔ بانک، خطایِ لحظه‌ایِ شبکه/زرین‌پال) موفق نشود، سفارش برای همیشه
pending می‌ماند و حلقه دیگر دوباره امتحانش نمی‌کند — بدونِ هیچ اطلاعی به
ادمین. با اسکریپتِ تشخیصیِ check_zarinpal_tenant_pending.py روی سرور
دیدیم دهها فاکتورِ pendingِ قدیمی (بعضی تا ۳۸ روز) روی چند ربات هست.

این اسکریپت همان تابعِ idempotentِ خودِ کد را (_settle_zarinpal_order، که
دکمهٔ «بررسی و تأیید پرداخت» هم همینو صدا می‌زنه) روی همهٔ آن سفارش‌هایِ
قدیمی اجرا می‌کند تا هرکدام که واقعاً روی زرین‌پال پرداخت‌شده هستند، الان
شارژ و به کاربر اطلاع داده شوند؛ سفارش‌هایی که واقعاً پرداخت نشده‌اند
(کاربر منصرف شده) دست‌نخورده باقی می‌مانند.

استفاده:
  cd /opt/botsaz
  python3 scripts/backfill_stuck_zarinpal_tenant.py                  # dry-run — فقط گزارش
  python3 scripts/backfill_stuck_zarinpal_tenant.py --apply           # واقعاً شارژ می‌کند
  python3 scripts/backfill_stuck_zarinpal_tenant.py --apply --days 60 # بازهٔ جست‌وجو (پیش‌فرض ۴۵ روز)
  python3 scripts/backfill_stuck_zarinpal_tenant.py --bot 123         # فقط یک ربات
  python3 scripts/backfill_stuck_zarinpal_tenant.py --tg 8857456774   # فقط یک کاربر (با --apply هم ترکیب می‌شود)

نکاتِ ایمنی:
  - قبل از --apply از دیتابیس بک‌آپ بگیر.
  - idempotent است: سفارشی که status آن دیگر pending نیست (چه با همین
    اسکریپت، چه با دکمهٔ ربات، چه با خودِ حلقهٔ ۶۰ثانیه‌ای) رد می‌شود.
  - در حالتِ dry-run هم verify_payment واقعاً به زرین‌پال زده می‌شود (فقط
    خواندنِ وضعیت است، چیزی در دیتابیسِ ما نوشته نمی‌شود) — طبقِ مستندِ
    زرین‌پال verify چندبار صدا زدن روی تراکنشِ تأییدشده بی‌خطر است (کدِ ۱۰۱).
"""
import argparse
import asyncio
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

from dotenv import load_dotenv
load_dotenv("/opt/botsaz/.env")

from sqlalchemy import select

from config import Session
from models import Order, Setting, Bot as BotRow, BotUser
import platform_zarinpal
from tenant.gateways.zarinpal_wallet import (
    _settle_zarinpal_order, _notify_credited, _zp_parse_receipt,
)


async def _dry_run_check(order: Order) -> str:
    """فقط استعلام از زرین‌پال؛ هیچ‌چیزی در دیتابیس نمی‌نویسد."""
    async with Session() as s:
        merchant_rec = await s.scalar(select(Setting).where(
            Setting.bot_id == order.bot_id, Setting.key == "zarinpal_merchant_id"))
    merchant = (merchant_rec.value if merchant_rec else "").strip()
    if not merchant:
        return "error: مرچنت‌ایدی این ربات تنظیم نشده"
    authority, fee = _zp_parse_receipt(order.receipt_text or "")
    if not authority:
        return "error: authority در سفارش پیدا نشد"
    amount = int(order.amount or 0)
    ok, ref_or_msg = await platform_zarinpal.verify_payment(merchant, amount + fee, authority)
    return "paid" if ok else f"not_paid: {ref_or_msg}"


async def main(days: int, apply: bool, bot_filter: int | None, tg_filter: int | None):
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    async with Session() as s:
        q = select(Order).where(
            Order.pay_method == "zarinpal_wallet",
            Order.status == "pending",
            Order.created_at >= cutoff,
        )
        if bot_filter:
            q = q.where(Order.bot_id == bot_filter)
        orders = (await s.scalars(q.order_by(Order.created_at))).all()

        if tg_filter:
            filtered = []
            for o in orders:
                u = await s.get(BotUser, o.user_id)
                if u and u.telegram_id == tg_filter:
                    filtered.append(o)
            orders = filtered

    print(f"بازهٔ جست‌وجو: {days} روز اخیر (از {cutoff:%Y-%m-%d})")
    print(f"تعداد سفارشِ pendingِ زرین‌پال (مستأجر): {len(orders)}")
    print("حالت:", "اعمال واقعی (--apply)" if apply else "آزمایشی (dry-run) — چیزی تغییر نمی‌کند")
    print("-" * 60)

    checked = credited = still_pending = errors = 0

    for o in orders:
        async with Session() as s:
            b = await s.get(BotRow, o.bot_id)
        bot_label = f"@{b.username}" if (b and b.username) else f"#{o.bot_id}"
        label = f"order#{o.id} [{bot_label}] {int(o.amount or 0):,} تومان"
        checked += 1
        try:
            if not apply:
                res = await _dry_run_check(o)
                if res == "paid":
                    print(f"  💰 {label} — روی زرین‌پال تأییدشده؛ با --apply شارژ می‌شود")
                    credited += 1
                elif res.startswith("not_paid"):
                    print(f"  ⏳ {label} — {res}")
                    still_pending += 1
                else:
                    print(f"  ⚠️  {label} — {res}")
                    errors += 1
                continue

            status, msg, amount, bot_id, telegram_id, new_balance, ref_id = \
                await _settle_zarinpal_order(o.id)
            if status == "credited":
                print(f"  ✅ {label} — شارژ شد (ref_id={ref_id})")
                credited += 1
                if bot_id and telegram_id:
                    try:
                        await _notify_credited(bot_id, telegram_id, amount, new_balance, ref_id)
                    except Exception:
                        logger.exception(f"backfill: اطلاع به کاربر ناموفق بود ({label})")
            elif status == "already":
                print(f"  ↩️  {label} — قبلاً (هم‌زمان با اجرایِ این اسکریپت) شارژ شده بود")
                still_pending += 1
            elif status == "not_paid":
                print(f"  ⏳ {label} — هنوز تأیید نشده: {msg}")
                still_pending += 1
            else:
                print(f"  ⚠️  {label} — {msg}")
                errors += 1
        except Exception as e:
            errors += 1
            print(f"  ❌ {label} — خطا: {type(e).__name__}: {str(e)[:150]}")

    print("-" * 60)
    print(f"بررسی‌شده: {checked} | شارژشده: {credited} | هنوز pending: {still_pending} | خطا: {errors}")
    if not apply and credited:
        print("\nبرای اعمال واقعی این تغییرات: python3 scripts/backfill_stuck_zarinpal_tenant.py --apply")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="جبرانِ شارژهای گیرکردهٔ زرین‌پالِ کیف‌پولِ مستأجرها")
    parser.add_argument("--apply", action="store_true",
                        help="واقعاً کیف پول‌ها را شارژ کن (پیش‌فرض: فقط گزارش/dry-run)")
    parser.add_argument("--days", type=int, default=45,
                        help="چند روز اخیر بررسی شود (پیش‌فرض ۴۵)")
    parser.add_argument("--bot", type=int, default=None, help="فیلترِ یک ربات با bot_id")
    parser.add_argument("--tg", type=int, default=None, help="فیلترِ یک کاربر با telegram_id")
    args = parser.parse_args()
    asyncio.run(main(args.days, args.apply, args.bot, args.tg))
