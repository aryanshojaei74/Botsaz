"""
worker_booking.py — ورکرِ اختصاصیِ رباتِ رزرو و نوبت‌دهی (PATCH_BOOKING_V2).

یک پروسهٔ کاملاً جدا (botsaz-booking-worker.service) که فقط یادآوریِ نوبت‌ها و پاک‌سازیِ نوبت‌هایِ منتظرِ گذشته را انجام می‌دهد؛
با ورکرِ عمومیِ مستأجرها (worker_tenant) و وب‌هوکِ رزرو (botsaz-tenant-booking) هیچ ارتباطی ندارد، پس ری‌استارتِ آن‌ها
یادآوری‌ها را قطع نمی‌کند و برعکس.

امنیتِ ری‌استارت/اجرایِ هم‌زمان:
  • پرچم‌هایِ «یادآوری فرستاده شد» در دیتابیس‌اند و قبل از ارسال به‌صورتِ اتمی «ثبت» می‌شوند ⇒ بعد از ری‌استارت چیزی دوبار
    نمی‌رود و اگر دو نمونه هم‌زمان بالا باشند فقط یکی می‌فرستد.
  • یادآوریِ سررسیده‌ای که وقتِ خاموشیِ پروسه از دست رفته، بعد از بالا آمدن (تا شروعِ نوبت) فرستاده می‌شود.
  • SIGTERM/SIGINT ⇒ توقفِ تمیز (بدونِ قطعِ نیمه‌کارهٔ ارسال).
  • هر دور یک «ضربان» در دیتابیس ثبت می‌شود؛ داشبوردِ مالک اگر ضربان قدیمی باشد هشدار می‌دهد.

اجرا:  python3 worker_booking.py     (یا از طریقِ systemd: deploy/botsaz-booking-worker.service)
"""
from __future__ import annotations

import asyncio
import logging
import signal

logger = logging.getLogger("worker_booking")


async def _main() -> None:
    from config import init_db
    await init_db()                      # اگر جدول‌هایِ رزرو هنوز ساخته نشده باشند (اولین نصب)
    from tenant.bots.booking.loop import INTERVAL, beat, run_once
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # ویندوز
            pass
    try:
        import loopwatch
        loopwatch.start("booking-worker")
    except Exception:      # noqa: BLE001
        logger.exception("loopwatch")
    logger.info("worker_booking: شروع شد (فاصلهٔ هر دور %ss)", INTERVAL)
    while not stop.is_set():
        try:
            await beat()
            n = await run_once()
            if n:
                logger.info("worker_booking: %s یادآوری فرستاده شد", n)
        except Exception:      # noqa: BLE001
            logger.exception("worker_booking: خطا در یک دور (دورِ بعد دوباره)")
        try:
            await asyncio.wait_for(stop.wait(), timeout=INTERVAL)
        except asyncio.TimeoutError:
            pass
    logger.info("worker_booking: متوقف شد (تمیز)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(_main())
