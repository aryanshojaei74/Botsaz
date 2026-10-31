"""
server_status.py — PATCH_SERVER_STATUS_V1

مینی‌اپِ «وضعیتِ سرورها»: هر دقیقه سلامت و سرعتِ بخش‌هایِ مختلفِ بات‌ساز اندازه‌گیری و در جدولِ status_samples ذخیره می‌شود
و صفحهٔ /status (کاملاً مستقل؛ بدونِ هیچ فونت/اسکریپتِ خارجی، تا در ایران هم بدونِ فیلترشکن باز شود) آن را زنده نشان می‌دهد.

  • probe_loop()      — حلقهٔ پس‌زمینه (worker پلتفرم): HTTP به سرویس‌هایِ محلی، getMe به تلگرام، SELECT 1 به دیتابیس
  • build_report()    — جمع‌بندی برایِ /status/api (کشِ ۱۵ ثانیه‌ای؛ صفحه عمومی است و نباید دیتابیس را بکوبد)
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
from datetime import datetime, timedelta

from sqlalchemy import String, case, delete, func, select, text

from config import PLATFORM_BOT_TOKEN, Session
from logging_setup import get_logger as _get_logger
from models import StatusSample

logger = _get_logger(__name__)

PROBE_EVERY = 60            # ثانیه
KEEP_DAYS = 35
_TIMEOUT = 6.0

# (کلید، نامِ نمایشی، پورتِ محلی یا None) — پورت‌ها طبقِ فایل‌هایِ systemd. نامِ نمایشی عمداً فنی نیست (برایِ مشتری است).
_HTTP_COMPONENTS = [
    ("platform",  "ربات‌ساز (ربات اصلی)",         8000),
    ("tenant",    "ربات‌های مشتریان",            8001),
    ("vpn",       "ربات‌های فروش VPN",           8011),
    ("virtual",   "ربات‌های خدمات مجازی",        8012),
    ("digital",   "ربات‌های فروشگاه دیجیتال",     8013),
    ("booking",   "ربات‌های رزرو نوبت",          8014),
    ("community", "ربات‌های مدیریت گروه",        8015),
]
NAMES = {k: n for k, n, _p in _HTTP_COMPONENTS}
NAMES.update({"telegram": "اتصال به تلگرام", "database": "پایگاه داده"})
ORDER = [k for k, _n, _p in _HTTP_COMPONENTS] + ["telegram", "database"]


# ───────────────────────────── اندازه‌گیری ─────────────────────────────
async def _probe_http(port: int) -> tuple[bool, int | None]:
    import aiohttp
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as s:
            async with s.get(f"http://127.0.0.1:{port}/health") as r:
                await r.read()
                ok = r.status < 500                       # 404 هم یعنی برنامه زنده است
    except Exception:
        return False, None
    return ok, int((time.perf_counter() - t0) * 1000)


async def _probe_telegram() -> tuple[bool, int | None]:
    if not PLATFORM_BOT_TOKEN:
        return False, None
    import aiohttp
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as s:
            async with s.get(f"https://api.telegram.org/bot{PLATFORM_BOT_TOKEN}/getMe") as r:
                data = await r.json(content_type=None)
    except Exception:
        return False, None
    return bool((data or {}).get("ok")), int((time.perf_counter() - t0) * 1000)


async def _probe_db() -> tuple[bool, int | None]:
    t0 = time.perf_counter()
    try:
        async with Session() as s:
            await asyncio.wait_for(s.execute(text("SELECT 1")), _TIMEOUT)
    except Exception:
        return False, None
    return True, int((time.perf_counter() - t0) * 1000)


async def probe_once(now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    jobs = {k: _probe_http(p) for k, _n, p in _HTTP_COMPONENTS}
    jobs["telegram"], jobs["database"] = _probe_telegram(), _probe_db()
    keys = list(jobs)
    res = await asyncio.gather(*jobs.values(), return_exceptions=True)
    async with Session() as s:
        for k, r in zip(keys, res):
            ok, ms = (r if isinstance(r, tuple) else (False, None))
            s.add(StatusSample(ts=now, key=k, ok=bool(ok), ms=ms if ok else None))
        await s.commit()
    return len(keys)


async def prune(now: datetime | None = None) -> None:
    cutoff = (now or datetime.utcnow()) - timedelta(days=KEEP_DAYS)
    async with Session() as s:
        await s.execute(delete(StatusSample).where(StatusSample.ts < cutoff))
        await s.commit()


async def probe_loop() -> None:
    await asyncio.sleep(20)
    n = 0
    while True:
        try:
            await probe_once()
            n += 1
            if n % 360 == 1:
                await prune()
        except Exception:
            logger.exception("server_status: دورِ اندازه‌گیری خطا داد")
        await asyncio.sleep(PROBE_EVERY)


# ───────────────────────────── گزارش ─────────────────────────────
def _state(recent: list[tuple[bool, int | None]], baseline_ms: float | None, key: str) -> str:
    if not recent:
        return "unknown"
    if len(recent) >= 2 and not recent[-1][0] and not recent[-2][0]:
        return "down"
    if not recent[-1][0]:
        return "degraded"                                  # یک بار خطا؛ هنوز «قطع» نیست
    oks = [ms for ok, ms in recent[-5:] if ok and ms is not None]
    if oks:
        med = statistics.median(oks)
        floor = 2500 if key == "telegram" else 1000
        if med > max(floor, 3 * (baseline_ms or 0)):
            return "degraded"
    return "up"


def build_from_rows(rows24: list[tuple], daily: dict, now: datetime) -> dict:
    """rows24: [(ts, key, ok, ms)] مرتب‌شده؛ daily: {key: {'YYYY-MM-DD': (n, ok)}}."""
    by: dict[str, list] = {}
    for ts, key, ok, ms in rows24:
        by.setdefault(key, []).append((ts, bool(ok), ms))
    comps = []
    start = now - timedelta(hours=24)
    BUCKET = 10 * 60
    nb = 144
    for key in ORDER:
        days = daily.get(key, {})
        if not any(ok for (_n, ok) in days.values()):
            continue                                       # بخشی که در ۳۰ روزِ اخیر هرگز زنده نبوده (مثلاً سرویسِ مستقر‌نشده) نمایش داده نمی‌شود
        samples = by.get(key, [])
        all_ms = [ms for _t, ok, ms in samples if ok and ms is not None]
        baseline = statistics.median(all_ms) if all_ms else None
        state = _state([(ok, ms) for _t, ok, ms in samples], baseline, key)
        # سری‌ی ۲۴ ساعته: ۱۴۴ سطلِ ۱۰ دقیقه‌ای؛ مقدار = میانگینِ ms، یا None (داده نیست)، و پرچمِ قطع
        buckets: list[dict] = [{"ms": None, "down": False, "n": 0} for _ in range(nb)]
        acc: list[list[int]] = [[] for _ in range(nb)]
        for ts, ok, ms in samples:
            i = int((ts - start).total_seconds() // BUCKET)
            if 0 <= i < nb:
                buckets[i]["n"] += 1
                if ok and ms is not None:
                    acc[i].append(ms)
                else:
                    buckets[i]["down"] = True
        for i in range(nb):
            if acc[i]:
                buckets[i]["ms"] = int(sum(acc[i]) / len(acc[i]))
        total = len(samples)
        up24 = round(100.0 * sum(1 for _t, ok, _m in samples if ok) / total, 2) if total else None
        dlist = []
        for d in range(29, -1, -1):
            day = (now - timedelta(days=d)).strftime("%Y-%m-%d")
            n_ok = days.get(day)
            dlist.append({"d": day, "p": (round(100.0 * n_ok[1] / n_ok[0], 2) if n_ok and n_ok[0] else None)})
        comps.append({
            "key": key, "name": NAMES.get(key, key), "state": state,
            "ms": samples[-1][2] if samples and samples[-1][1] else None,
            "avg_ms": int(baseline) if baseline else None,
            "max_ms": max(all_ms) if all_ms else None,
            "uptime24": up24,
            "series": [(b["ms"], 1 if b["down"] else 0) for b in buckets],
            "days": dlist,
        })
    states = [c["state"] for c in comps]
    overall = "down" if "down" in states else ("degraded" if "degraded" in states else ("up" if states else "unknown"))
    return {"now": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "overall": overall, "components": comps, "bucket_s": BUCKET}


_CACHE: dict = {"t": 0.0, "v": None}


async def build_report(force: bool = False) -> dict:
    if not force and _CACHE["v"] is not None and time.monotonic() - _CACHE["t"] < 15:
        return _CACHE["v"]
    now = datetime.utcnow()
    async with Session() as s:
        r24 = (await s.execute(
            select(StatusSample.ts, StatusSample.key, StatusSample.ok, StatusSample.ms)
            .where(StatusSample.ts >= now - timedelta(hours=24)).order_by(StatusSample.ts))).all()
        day = func.date(StatusSample.ts, type_=String)   # sqlite و PostgreSQL هر دو date() دارند
        r30 = (await s.execute(
            select(day, StatusSample.key, func.count(StatusSample.id),
                   func.sum(case((StatusSample.ok.is_(True), 1), else_=0)))
            .where(StatusSample.ts >= now - timedelta(days=30)).group_by(day, StatusSample.key))).all()
    daily: dict[str, dict] = {}
    for d, key, n, ok in r30:
        daily.setdefault(key, {})[str(d)[:10]] = (int(n or 0), int(ok or 0))
    rep = build_from_rows([tuple(x) for x in r24], daily, now)
    _CACHE["t"], _CACHE["v"] = time.monotonic(), rep
    return rep
