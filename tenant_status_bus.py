"""
tenant_status_bus.py — اطلاع‌رسانیِ آنیِ بین‌پروسه‌ای برای باطل‌کردنِ کشِ
وضعیتِ بات‌های مستأجر (_tenant_status_cache در main.py).

چرا لازم است
─────────────
main.py یک کشِ کوتاه‌مدت (۵ ثانیه، _TENANT_STATUS_TTL) از وضعیتِ هر بات
مستأجر (فعال/موقوف/قفل/حذف) نگه می‌داشت تا هر آپدیتِ ورودی به دیتابیس کوئری
نزند. وقتی همه‌چیز در یک پروسه بود، invalidate_tenant_status() همان لحظه
که ادمین از پنلِ بات‌ساز یک بات را حذف/قفل می‌کرد، این کش را پاک می‌کرد و
اثر فوری بود.

حالا که قراره بات‌ساز (پلتفرم) و بات‌های مستأجر روی دو سرویسِ جداگانه
(مثلاً main_platform.py / main_tenant.py) اجرا بشن، invalidate_tenant_status
دیگه فقط کشِ همون پروسه‌ای که صداش زده رو پاک می‌کنه — پروسه‌ی دیگه فقط با
انقضای TTL (حداکثر ۵ ثانیه بعد) باخبر می‌شه. این ماژول با استفاده از
Postgres LISTEN/NOTIFY (بدون نیاز به زیرساختِ اضافه‌ای مثل Redis — چون
دیتابیسِ پروژه از قبل Postgres است) این تأخیر رو به صفر می‌رسونه.

رفتار روی SQLite (حالتِ dev/تستِ محلی)
──────────────────────────────────────
SQLite از LISTEN/NOTIFY پشتیبانی نمی‌کنه. چون در اون حالت معمولاً همه‌چیز
توی یه پروسه اجرا می‌شه (بدونِ اسپلیت)، publish/listen اینجا بی‌صدا no-op
می‌شن؛ رفتارِ قبلی (پاک‌شدنِ فوریِ کشِ همون پروسه، بدونِ سیگنالِ بین‌پروسه‌ای)
دقیقاً حفظه — هیچ تغییرِ رفتاری/ریسکی برای محیطِ dev نداره.

نکته‌ی طراحیِ مهم: این ماژول هرگز نباید فلوی اصلی (حذف/قفل‌کردنِ بات) رو
کرش بده. هر خطا (قطعیِ دیتابیس، تایم‌اوت و...) فقط لاگ می‌شه؛ fallback امن
همون کشِ ۵ثانیه‌ای قبلیه که دست‌نخورده باقی مونده.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from config import DATABASE_URL
from logging_setup import get_logger

logger = get_logger(__name__)

_CHANNEL = "tenant_status_bus"
_IS_POSTGRES = not DATABASE_URL.startswith("sqlite")


def _raw_dsn() -> str:
    """DATABASE_URL به‌فرمِ SQLAlchemy (مثلاً postgresql+asyncpg://...) رو به
    یه DSN خام قابل‌قبول برای asyncpg.connect (postgresql://...) تبدیل می‌کنه."""
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


async def publish_tenant_invalidate(token: str | None = None) -> None:
    """به همه‌ی پروسه‌های شنونده اطلاع می‌ده که کشِ وضعیتِ این توکن (یا اگه
    None بود، همه‌ی توکن‌ها) باید فوراً باطل بشه.

    اگه دیتابیس Postgres نباشه یا اتصالِ لحظه‌ای برقرار نشه، بی‌صدا (فقط با
    لاگِ warning) رد می‌شه — این فقط یه بهینه‌سازیِ سرعته؛ کشِ ۵ثانیه‌ایِ
    قبلی هم‌چنان fallbackِ امنه.
    """
    if not _IS_POSTGRES:
        return
    import asyncpg
    payload = token or ""
    conn = None
    try:
        conn = await asyncio.wait_for(asyncpg.connect(_raw_dsn()), timeout=3)
        await conn.execute("SELECT pg_notify($1, $2)", _CHANNEL, payload)
    except Exception:
        logger.warning(
            "tenant_status_bus: NOTIFY ناموفق بود (fallback به کشِ ۵ثانیه‌ای اعمال می‌شه)",
            exc_info=True)
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")


async def run_tenant_invalidate_listener(on_invalidate: Callable[[str | None], None]) -> None:
    """حلقهٔ پس‌زمینه‌ای که تا ابد روی کانالِ NOTIFY گوش می‌ده و به‌ازای هر
    پیام، on_invalidate(token) رو صدا می‌زنه (token=None یعنی «همه رو پاک کن»).

    اگه اتصال قطع بشه، با backoff نمایی (۱ تا ۳۰ ثانیه) دوباره وصل می‌شه.
    این تابع هیچ‌وقت نباید برنامه رو متوقف کنه — این فقط یه بهینه‌سازیِ روی
    کشِ ۵ثانیه‌ایه، نه یه وابستگیِ حیاتی.

    استفاده (در on_startup هر پروسه‌ای که کشِ وضعیتِ بات مستأجر داره):
        _spawn(run_tenant_invalidate_listener(invalidate_tenant_status_local))
    """
    if not _IS_POSTGRES:
        return
    import asyncpg
    backoff = 1
    while True:
        conn = None
        try:
            conn = await asyncpg.connect(_raw_dsn())

            def _cb(_conn, _pid, _channel, payload):  # noqa: ANN001
                try:
                    on_invalidate(payload or None)
                except Exception:
                    logger.exception("tenant_status_bus: callback باگ داشت")

            await conn.add_listener(_CHANNEL, _cb)
            logger.info(f"tenant_status_bus: listener فعال شد (کانال={_CHANNEL})")
            backoff = 1  # اتصال موفق بود؛ backoff رو ریست کن
            while not conn.is_closed():
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                f"tenant_status_bus: اتصالِ listener قطع شد، تلاشِ مجدد تا {backoff} ثانیهٔ دیگر",
                exc_info=True)
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)
