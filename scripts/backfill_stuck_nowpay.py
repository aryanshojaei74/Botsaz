"""
backfill_stuck_nowpay.py — جبران شارژهای گیرکردهٔ قدیمی NOWPayments.

مشکل: برای شارژ کیف پول خودِ بات‌ساز (kind="platform")، اگر IPN نوپیمنت به هر
دلیلی به سرور نمی‌رسید (قطعی شبکه، ری‌استارت سرور، ...)، هیچ راه دیگری برای
تشخیص و شارژ آن پرداخت وجود نداشت — چون fallback فعالِ استعلام مستقیم فقط برای
kind="tenant" پیاده شده بود. این اسکریپت همان منطق را روی رکوردهای قدیمیِ هنوز
"waiting/priced" اجرا می‌کند تا اگر واقعاً روی نوپیمنت "finished/confirmed" شده
باشند، کیف پول کاربر را (یک‌بار، idempotent) شارژ کند.

استفاده:
  python backfill_stuck_nowpay.py                 # حالت آزمایشی (dry-run) — فقط گزارش می‌دهد
  python backfill_stuck_nowpay.py --apply          # واقعاً کیف پول‌ها را شارژ می‌کند
  python backfill_stuck_nowpay.py --apply --days 60  # بازهٔ جست‌وجو را عوض می‌کند (پیش‌فرض ۳۰ روز)

نکات ایمنی:
  - قبل از اجرای --apply حتماً از دیتابیس بک‌آپ بگیرید.
  - اسکریپت idempotent است: رکوردی که قبلاً paid/nowpay_done شده دوباره شارژ نمی‌شود.
  - فقط رکوردهایی که واقعاً روی نوپیمنت finished/confirmed هستند شارژ می‌شوند؛
    رکوردهای هنوز "waiting" روی نوپیمنت دست‌نخورده باقی می‌مانند.
"""
import argparse
import asyncio
import datetime
import os
import sys

from logging_setup import get_logger as _get_logger, PrintLogger as _PrintLogger
logger = _get_logger(__name__)
print = _PrintLogger(__name__)  # هدایتِ print()های موجود در این فایل به logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv("/opt/botsaz/.env")

from sqlalchemy import select

from config import Session
from tenant.gateways.payment_page import ChargeDraft, _active_check_tenant, _active_check_platform


async def main(days: int, apply: bool):
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    async with Session() as s:
        drafts = (await s.scalars(
            select(ChargeDraft).where(
                ChargeDraft.status.in_(("waiting", "priced")),
                ChargeDraft.np_payment_id != "",
                ChargeDraft.np_payment_id.isnot(None),
                ChargeDraft.created_at >= cutoff,
            )
        )).all()

    print(f"بازهٔ جست‌وجو: {days} روز اخیر (از {cutoff:%Y-%m-%d})")
    print(f"تعداد رکورد در انتظار با شناسهٔ پرداخت: {len(drafts)}")
    print("حالت:", "اعمال واقعی (--apply)" if apply else "آزمایشی (dry-run) — چیزی تغییر نمی‌کند")
    print("-" * 60)

    checked = credited = still_waiting = errors = 0

    for d in drafts:
        kind = getattr(d, "kind", "tenant") or "tenant"
        label = f"[{kind}] order={d.order_id} tc={d.tracking_code} np_id={d.np_payment_id}"
        checked += 1
        try:
            if not apply:
                # حالت آزمایشی: فقط وضعیت را از نوپیمنت می‌خوانیم، چیزی نمی‌نویسیم
                from tenant.gateways import crypto_pay
                if kind == "platform":
                    from platform_bot import get_pset
                    api_key = await get_pset("platform_nowpay_api_key", "")
                else:
                    from tenant import get_setting
                    api_key = await get_setting(d.bot_id, "nowpay_api_key", "")
                if not api_key:
                    print(f"  ⚠️  {label} — کلید API یافت نشد، رد شد")
                    continue
                data = await crypto_pay.get_payment_status(api_key, d.np_payment_id)
                pay_status = (data or {}).get("payment_status", "؟").lower()
                if pay_status in ("finished", "confirmed"):
                    print(f"  💰 {label} — روی نوپیمنت «{pay_status}» است؛ با --apply شارژ می‌شود")
                    credited += 1
                else:
                    print(f"  ⏳ {label} — هنوز «{pay_status}»")
                    still_waiting += 1
                continue

            # حالت اعمال واقعی
            ok = (await _active_check_platform(d) if kind == "platform"
                  else await _active_check_tenant(d))
            if ok:
                async with Session() as s:
                    dd = await s.scalar(select(ChargeDraft).where(
                        ChargeDraft.tracking_code == d.tracking_code))
                    if dd and dd.status != "paid":
                        dd.status = "paid"
                        await s.commit()
                print(f"  ✅ {label} — شارژ شد")
                credited += 1
            else:
                print(f"  ⏳ {label} — هنوز تأیید نشده یا قبلاً پردازش شده")
                still_waiting += 1
        except Exception as e:
            errors += 1
            print(f"  ❌ {label} — خطا: {type(e).__name__}: {str(e)[:120]}")

    print("-" * 60)
    print(f"بررسی‌شده: {checked} | شارژشده: {credited} | هنوز در انتظار: {still_waiting} | خطا: {errors}")
    if not apply and credited:
        print("\nبرای اعمال واقعی این تغییرات: python backfill_stuck_nowpay.py --apply")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="جبران شارژهای گیرکردهٔ NOWPayments")
    parser.add_argument("--apply", action="store_true",
                        help="واقعاً کیف پول‌ها را شارژ کن (پیش‌فرض: فقط گزارش/dry-run)")
    parser.add_argument("--days", type=int, default=30,
                        help="چند روز اخیر بررسی شود (پیش‌فرض ۳۰)")
    args = parser.parse_args()
    asyncio.run(main(args.days, args.apply))
