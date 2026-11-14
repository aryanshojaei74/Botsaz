"""rate_limit.py — PATCH_RELINK_RATE_LIMIT_V1

خواندنِ پشتِ‌سرِ‌همِ اطلاعاتِ کاربرانِ یک پنل (بازسازیِ لینک، بروزرسانیِ ساب‌لینک‌ها) به سقفِ نرخِ پنل (مثلاً RickPanel:
HTTP 429 + «حدوداً N ثانیه دیگر») می‌خورد و هر ۴۲۹ به‌اشتباه «پیدا نشد» حساب می‌شد. این ماژولِ کوچک فاصلهٔ بینِ
درخواست‌ها و صبر+تلاشِ مجدد را یک‌جا نگه می‌دارد تا هر دو مسیر از یک منطق استفاده کنند.
"""
import asyncio

# گزارش (تیکتِ ۸۸۱ «محصولات ریست نمی‌کند» + لاگ): بازسازیِ لینکِ یک پنلِ RickPanel با ده‌ها اکانت، درخواست‌ها را پشتِ‌سرِ هم
# می‌فرستاد؛ پنل برایِ هر کاربر سقفِ نرخ دارد (HTTP 429 + «حدوداً N ثانیه دیگر») و هر ۴۲۹ به‌اشتباه «روی پنل پیدا نشد»
# حساب می‌شد (۷۱ مورد در ۶ ساعت). حالا: فاصلهٔ کوتاه بینِ درخواست‌ها، صبر به‌اندازهٔ Retry-After و تلاشِ مجدد، و گزارشِ جدا
# برایِ «خطای پنل» در برابرِ «پیدا نشد».
_PACE_SLEEP = 0.4
_MAX_RETRIES = 4
_DEFAULT_RETRY_WAIT = 10.0
_MAX_RETRY_WAIT = 30.0


def _retry_wait_from(exc: Exception) -> float | None:
    """اگر خطا ۴۲۹ باشد، ثانیه‌هایِ انتظارِ پیشنهادیِ پنل (یا پیش‌فرض)؛ وگرنه None."""
    import re
    t = str(exc)
    if "429" not in t and "rate limit" not in t.lower():
        return None
    m = re.search(r"(\d+)\s*ثانیه", t)
    wait = float(m.group(1)) + 1.0 if m else _DEFAULT_RETRY_WAIT
    return min(max(wait, 2.0), _MAX_RETRY_WAIT)


async def _get_client_retry(client, email: str):
    """(نتیجه، خطا): get_client با صبر و تلاشِ مجدد روی ۴۲۹. خطایِ غیرِ‌۴۲۹ فوراً برمی‌گردد."""
    last: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return await client.get_client(email), None
        except Exception as e:      # noqa: BLE001
            last = e
            wait = _retry_wait_from(e)
            if wait is None or attempt >= _MAX_RETRIES:
                break
            await asyncio.sleep(wait)
    return None, last


