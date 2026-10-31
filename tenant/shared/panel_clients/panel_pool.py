"""
panel_pool.py — استخرِ کلاینتِ پنل، برایِ حذفِ لاگینِ تکراری در هر خرید.

=== PATCH_PANEL_CLIENT_POOL_V1 ===
گزارشِ ادمین: «بات یکم ساب‌ها رو طول می‌کشه تا می‌سازه جدیدا، کلاً سرعت
کم شده». در error.log هم ۵۴ هشدارِ `slow update` بود؛ میانه ۷٫۵ ثانیه،
بیشینه ۵۵ ثانیه، در ۲۱ رباتِ مختلف.

ریشه: مسیرِ خرید برایِ *هر عملیات* یک کلاینتِ تازه می‌ساخت —

    client = make_panel_client_from_fields(...)   # ClientSession تازه
    acc = await client.create_account(...)
    await client.close()                          # و بلافاصله بسته

و `_request` هم اولِ کار `if not self._logged_in: await self.login()`
می‌زند. یعنی هر ساختِ کانفیگ این‌ها را از صفر انجام می‌داد:

    ۱) دست‌دادنِ TCP + TLS با پنل
    ۲) GET صفحهٔ اصلی برایِ csrf-token
    ۳) POST /login
    ۴) GET فهرستِ اینباندها (اگر inbound_ids تنظیم نشده باشد)
    ۵) POST ساختِ کلاینت

پنجِ رفت‌وبرگشت به سروری که معمولاً خارج از کشور است. هیچ‌کدام از این
چهارتایِ اول کارِ مفید نیستند؛ فقط هزینهٔ راه‌اندازیِ دوباره‌اند.

جالب اینکه راه‌حل از قبل در پروژه بود: `PATCH_VPN_FIRST_CONNECT_REUSE_CLIENT_V1`
در main_tenant.py دقیقاً همین کار را برایِ حلقهٔ پس‌زمینه می‌کند و کلاینتِ
لاگین‌شده را نگه می‌دارد. ولی مسیرِ کاربر — که تنها جایی است که کاربر
منتظرش می‌ماند — از آن بی‌بهره بود.

این ماژول همان الگو را به یک استخرِ مشترک تبدیل می‌کند: کلاینت زنده و
لاگین‌شده می‌ماند و درخواستِ بعدی روی همان اتصال می‌رود.

نکاتِ ایمنی:
  • کلیدِ کش شاملِ همهٔ فیلدهایِ اتصال است، پس تغییرِ رمز/آدرس/اینباند
    خودکار کلاینتِ قدیمی را دور می‌ریزد.
  • قفلِ per-key تا اگر ده کاربر هم‌زمان بخرند، فقط یک لاگین انجام شود
    نه ده تا.
  • TTL دارد تا نشستِ خیلی قدیمی برایِ همیشه نماند.
  • `_request` از قبل روی ۴۰۱/۴۰۳ خودش دوباره لاگین می‌کند، پس کوکیِ
    منقضی خودبه‌خود ترمیم می‌شود.
  • اگر خطایِ شبکه‌ای رخ دهد کلاینت از استخر بیرون انداخته می‌شود تا
    درخواستِ بعدی از نو وصل شود.
"""
from __future__ import annotations

import asyncio
import inspect
import time

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

# کلیدِ اتصال → (کلاینت, آخرین‌استفاده)
_pool: dict[tuple, tuple[object, float]] = {}
_locks: dict[tuple, asyncio.Lock] = {}

# === PATCH_PANEL_POOL_INFLIGHT_GUARD_V1 ===
# گزارشِ ادمین: «Session is closed» وسطِ گرفتنِ کانفیگِ تکی، برایِ سرویسی که
# قبلش (توسطِ همین استخر) مشکلی نداشت.
#
# ریشه: اگر یک عملیات رویِ یک کلاینتِ پول‌شده طول بکشد (پنلِ کند، شبکهٔ
# بد، تلاشِ مجدد)، و در همین حین یک درخواستِ کاملاً نامرتبط (برایِ کلیدِ
# دیگری) باعثِ اجرایِ evictِ منقضی‌ها بشود، و همین کلید هم به‌خاطرِ گذشتنِ
# TTL از آخرین checkout (نه پایانِ استفاده — این دو با هم فرق دارند)
# منقضی به‌حساب بیاید، همان کلاینتی که یک کوروتینِ دیگر وسطِ استفاده‌ازش
# هست close می‌شود؛ عملیاتِ در حالِ اجرا با «Session is closed» می‌ترکد.
# کاهشِ اخیرِ TTL (۱۵ دقیقه → ۳ دقیقه، PATCH_PANEL_POOL_KEEPALIVE_V1)
# این ریسک را بیشتر هم کرده.
#
# راه‌حل: شمارنده‌ی «در حالِ استفاده» به‌ازایِ هر کلید. هر متدِ async رویِ
# کلاینتِ پول‌شده، خودکار (بدونِ نیاز به تغییرِ ده‌ها نقطه‌ی صدازننده) این
# شمارنده را قبل از await بالا و بعدش پایین می‌برد؛ evict فقط وقتی کلیدی
# را می‌بندد که این شمارنده صفر باشد — یعنی هیچ کوروتینی الان وسطِ
# استفاده از آن نیست.
_busy: dict[tuple, int] = {}


def _mark_busy(k: tuple) -> None:
    _busy[k] = _busy.get(k, 0) + 1


def _mark_free(k: tuple) -> None:
    n = _busy.get(k, 0) - 1
    if n <= 0:
        _busy.pop(k, None)
    else:
        _busy[k] = n

# نشستِ بی‌استفاده بعد از این مدت دور ریخته می‌شود.
# === PATCH_PANEL_POOL_KEEPALIVE_V1 ===
# از ۱۵ دقیقه به ۳ دقیقه کم شد. کوکیِ لاگین و اتصالِ TCP هر دو بعد از چند
# دقیقه بی‌کاری ممکن است از سمتِ سرور باطل شوند؛ نگه‌داشتنِ نشستِ کهنه
# سودی ندارد و فقط ریسکِ «درخواست رویِ سوکتِ مرده» را زیاد می‌کند.
# سه دقیقه برایِ پوششِ رگبارِ خریدها کافی است — که تنها جایی است که
# استخر واقعاً فرق می‌کند.
POOL_TTL_SEC = 3 * 60
# سقفِ تعدادِ نشست‌هایِ هم‌زمان (هر ربات چند پنل، چند ربات...)
POOL_MAX = 200


def _key(panel_key, base, user, pw, sub_base, inbound, api_key) -> tuple:
    return (panel_key or "", base or "", user or "", pw or "",
            sub_base or "", str(inbound or ""), api_key or "")


async def _evict_expired(now: float) -> None:
    for k, (client, last) in list(_pool.items()):
        if now - last <= POOL_TTL_SEC:
            continue
        # === PATCH_PANEL_POOL_INFLIGHT_GUARD_V1 ===
        if _busy.get(k, 0) > 0:
            continue
        _pool.pop(k, None)
        _locks.pop(k, None)
        try:
            await client.close()
        except Exception:
            logger.exception("panel_pool: بستنِ نشستِ منقضی ناموفق بود")


async def get_client(panel_key, base, user, pw, *, sub_base=None,
                     inbound_ids_str=None, api_key=None):
    """کلاینتِ زنده و لاگین‌شدهٔ این پنل را برمی‌گرداند (یا تازه می‌سازد).

    **کلاینت را close نکن** — مالِ استخر است و درخواستِ بعدی از همان
    استفاده می‌کند. اگر عملیات با خطایِ شبکه‌ای شکست خورد،
    `drop()` را صدا بزن.
    """
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client_from_fields

    now = time.monotonic()
    await _evict_expired(now)
    k = _key(panel_key, base, user, pw, sub_base, inbound_ids_str, api_key)

    lock = _locks.get(k)
    if lock is None:
        lock = _locks[k] = asyncio.Lock()

    async with lock:
        hit = _pool.get(k)
        if hit is not None:
            client, _ = hit
            _pool[k] = (client, now)
            # === PATCH_PANEL_POOL_INFLIGHT_GUARD_V1 ===
            return _PooledClient(client, k)

        if len(_pool) >= POOL_MAX:
            # === PATCH_PANEL_POOL_INFLIGHT_GUARD_V1 ===
            # قدیمی‌ترینِ غیر busy را قربانی کن.
            candidates = {kk: v for kk, v in _pool.items() if _busy.get(kk, 0) == 0}
            if candidates:
                oldest = min(candidates, key=lambda x: candidates[x][1])
                old_client, _ = _pool.pop(oldest)
                _locks.pop(oldest, None)
                try:
                    await old_client.close()
                except Exception:
                    logger.exception("panel_pool: بستنِ قدیمی‌ترین نشست ناموفق بود")

        client = make_panel_client_from_fields(
            panel_key, base, user, pw, sub_base=sub_base,
            inbound_ids_str=inbound_ids_str, api_key=api_key)
        _pool[k] = (client, now)
        # === PATCH_PANEL_POOL_INFLIGHT_GUARD_V1 ===
        return _PooledClient(client, k)


async def drop(panel_key, base, user, pw, *, sub_base=None,
               inbound_ids_str=None, api_key=None) -> None:
    """کلاینتِ خراب را از استخر بیرون بینداز تا دفعهٔ بعد از نو ساخته شود."""
    k = _key(panel_key, base, user, pw, sub_base, inbound_ids_str, api_key)
    hit = _pool.pop(k, None)
    _locks.pop(k, None)
    if hit is None:
        return
    client, _ = hit
    try:
        await client.close()
    except Exception:
        logger.exception("panel_pool: بستنِ کلاینتِ خراب ناموفق بود")


async def close_all() -> None:
    """هنگامِ خاموشیِ سرویس."""
    for k, (client, _) in list(_pool.items()):
        _pool.pop(k, None)
        try:
            await client.close()
        except Exception:
            logger.exception("panel_pool: بستنِ نشست هنگامِ خاموشی ناموفق بود")
    _locks.clear()


def stats() -> dict:
    """برایِ عیب‌یابی: چند نشستِ زنده در استخر هست."""
    now = time.monotonic()
    return {"sessions": len(_pool),
            "idle_sec": sorted(round(now - t) for _, t in _pool.values())}


# === PATCH_PANEL_CLIENT_POOL_V2 ===
# نسخهٔ V1 فقط مسیرِ «خریدِ جدید» را پوشش داد، چون get_client آسنکرون بود و
# جایگزینی‌اش در ۱۴ نقطهٔ دیگر یعنی دست‌زدن به ۱۴ تابع.
#
# اما ساختِ کلاینت اصلاً کارِ آسنکرونی نیست — `make_panel_client_from_fields`
# سازندهٔ ساده است و لاگین *تنبل* است (داخلِ `_request` اتفاق می‌افتد).
# پس یک نسخهٔ همگام هم می‌شود داشت، و آن‌وقت کافی است تنها همان تابعِ
# `_client_from_panel` عوض شود تا هر ۱۴ نقطه یکجا از استخر استفاده کنند.
#
# مشکلِ دوم: آن ۱۴ نقطه بعدِ کارشان `await client.close()` می‌زنند و بستنِ
# نشستِ مشترک یعنی برگشت به نقطهٔ اول. به‌جایِ ویرایشِ تک‌تکِ آن‌ها،
# کلاینت را در پروکسی‌ای می‌پیچیم که `close()` اش بی‌اثر است؛ چرخهٔ عمرِ
# واقعی را خودِ استخر با TTL مدیریت می‌کند. بقیهٔ متدها بدونِ تغییر به
# کلاینتِ اصلی می‌روند.


class _PooledClient:
    """پروکسیِ نازک: هر چیزی جز close() مستقیم به کلاینتِ اصلی می‌رود.

    === PATCH_PANEL_POOL_INFLIGHT_GUARD_V1 ===
    هر متدِ async، خودکار قبل از اجرا شمارنده‌ی busy را برایِ کلیدِ همین
    کلاینت بالا می‌برد و بعدِ اتمام (چه موفق چه با خطا) پایین — تا evict
    بفهمد این کلید الان «در حالِ استفاده»ست و نبندتش.
    """

    __slots__ = ("_c", "_k")

    def __init__(self, client, key: tuple | None = None):
        object.__setattr__(self, "_c", client)
        object.__setattr__(self, "_k", key)

    def __getattr__(self, name):
        attr = getattr(object.__getattribute__(self, "_c"), name)
        k = object.__getattribute__(self, "_k")
        if k is not None and inspect.iscoroutinefunction(attr):
            async def _guarded(*args, **kwargs):
                _mark_busy(k)
                try:
                    return await attr(*args, **kwargs)
                finally:
                    _mark_free(k)
            return _guarded
        return attr

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_c"), name, value)

    async def close(self):
        """بی‌اثر — نشست مالِ استخر است."""
        return None

    @property
    def raw(self):
        return object.__getattribute__(self, "_c")


def get_sync(panel_key, base, user, pw, *, sub_base=None,
             inbound_ids_str=None, api_key=None):
    """نسخهٔ همگامِ get_client. کلاینتِ برگشتی close()اش بی‌اثر است."""
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client_from_fields

    now = time.monotonic()
    k = _key(panel_key, base, user, pw, sub_base, inbound_ids_str, api_key)
    hit = _pool.get(k)
    if hit is not None:
        client, _ = hit
        _pool[k] = (client, now)
        return _PooledClient(client, k)

    # پاک‌سازیِ منقضی‌ها بدونِ انتظار (بستنِ نشست خودش آسنکرون است)
    # === PATCH_PANEL_POOL_INFLIGHT_GUARD_V1 ===
    # کلیدهایِ busy (یعنی یک کوروتینِ دیگر همین الان دارد از کلاینتشان
    # استفاده می‌کند) هرچند منقضی باشند، evict/close نمی‌شوند.
    expired = [kk for kk, (_, last) in _pool.items()
               if now - last > POOL_TTL_SEC and _busy.get(kk, 0) == 0]
    for kk in expired:
        c, _ = _pool.pop(kk, (None, 0))
        _locks.pop(kk, None)
        if c is not None:
            try:
                asyncio.get_event_loop().create_task(c.close())
            except Exception:
                logger.exception("panel_pool: زمان‌بندیِ بستنِ نشست ناموفق بود")

    if len(_pool) >= POOL_MAX:
        # === PATCH_PANEL_POOL_INFLIGHT_GUARD_V1 ===
        # از بینِ کلیدهایِ غیرِ busy، قدیمی‌ترین را قربانی کن؛ اگر همه busy
        # بودند (خیلی بعیده)، فعلاً از جایگزینی صرف‌نظر کن — بهتر از قطعِ
        # یک درخواستِ در حالِ اجراست.
        candidates = {kk: v for kk, v in _pool.items() if _busy.get(kk, 0) == 0}
        if candidates:
            oldest = min(candidates, key=lambda x: candidates[x][1])
            c, _ = _pool.pop(oldest, (None, 0))
            _locks.pop(oldest, None)
            if c is not None:
                try:
                    asyncio.get_event_loop().create_task(c.close())
                except Exception:
                    logger.exception("panel_pool: زمان‌بندیِ بستنِ نشست ناموفق بود")

    client = make_panel_client_from_fields(
        panel_key, base, user, pw, sub_base=sub_base,
        inbound_ids_str=inbound_ids_str, api_key=api_key)
    _pool[k] = (client, now)
    return _PooledClient(client, k)


def drop_sync(panel_key, base, user, pw, *, sub_base=None,
              inbound_ids_str=None, api_key=None) -> None:
    """نشستِ خراب را بیرون بینداز (بدونِ انتظار)."""
    k = _key(panel_key, base, user, pw, sub_base, inbound_ids_str, api_key)
    hit = _pool.pop(k, None)
    _locks.pop(k, None)
    if hit is None:
        return
    client, _ = hit
    try:
        asyncio.get_event_loop().create_task(client.close())
    except Exception:
        logger.exception("panel_pool: بستنِ کلاینتِ خراب ناموفق بود")
