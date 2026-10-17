"""
tenant/bots/virtual_service/smm_refill.py — ریفیل (جبرانی/گارانتی) سفارش‌هایِ SMM.

=== PATCH_SMM_REFILL_V1 ===
درخواستِ ادمین (فدایی): «برایِ دستورِ جبرانی/ریفیل، چرا هر محصولی می‌تواند درخواست را ثبت کند و فقط
پیامِ ثبتش می‌آید و پاسخِ پنل (تکمیل/لغو) نمی‌آید؟ باید چک کند ارائه‌دهنده اصلاً ریفیل دارد، بعد
ثبت شود و وضعیتش مشخص شود؛ و در لیستِ سفارش‌ها گزینهٔ «سفارشاتِ گارانتی‌دار» باشد.»

سه مشکلِ قبلی:
  ۱) دکمه برایِ همهٔ سرویس‌ها نشان داده می‌شد (پرچمِ refill پنل ذخیره نمی‌شد).
  ۲) هر پاسخی — حتی قطعِ ارتباط یا پاسخِ خالی — «✅ ثبت شد» حساب می‌شد.
  ۳) هیچ‌کس وضعیتِ ریفیل را از پنل نمی‌پرسید؛ مشتری هرگز نتیجه را نمی‌فهمید.

این ماژول: (الف) پرچمِ ریفیلِ هر سرویس را *زنده* از فهرستِ سرویس‌هایِ خودِ پنل می‌خواند (کش‌شده؛
بدونِ تغییرِ ستونِ سرویس‌ها و بدونِ نیاز به همگام‌سازیِ دوباره)، (ب) درخواست را با قفل و
جلوگیری از دوبار-کلیک ثبت می‌کند و فقط با شناسهٔ معتبرِ پنل موفق می‌شمارد، (ج) وضعیت را دوره‌ای
از پنل می‌پرسد و نتیجه را یک‌بار به مشتری می‌فرستد.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select

from config import Session
from models import SmmOrder, SmmRefill

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

# سفارش‌هایی که اصلاً می‌شود ریفیلشان کرد (باید رویِ پنل ثبت شده باشند)
REFILLABLE_ORDER_STATUSES = ("sent", "completed")
ACTIVE = ("requesting", "pending", "in_progress")
TERMINAL = ("completed", "rejected", "expired")
_REQUESTING_STALE = timedelta(minutes=2)
_EXPIRE_AFTER = timedelta(days=10)
_MIN_POLL_GAP = timedelta(seconds=60)

STATUS_LABEL = {
    "requesting": "⏳ در حالِ ثبت",
    "pending": "⏳ در انتظارِ پنل",
    "in_progress": "🔄 در حالِ انجام",
    "completed": "✅ انجام شد",
    "rejected": "❌ توسطِ ارائه‌دهنده انجام نشد",
    "expired": "⌛️ بی‌نتیجه ماند",
}


# ═══════════════ ۱) پرچمِ ریفیلِ سرویس (زنده، کش‌شده) ═══════════════

_SUPPORT_TTL = 20 * 60          # ثانیه
_SUPPORT_MAX_PANELS = 200
# key -> (ts, {service_id: bool}, has_flag)
_SUPPORT: dict[str, tuple[float, dict[str, bool], bool]] = {}
_INFLIGHT: dict[str, asyncio.Future] = {}
_CANCEL: dict[str, dict[str, bool]] = {}      # PATCH_SMM_CANCEL_FLAG_V1


def cached_cancel(api_url: str, api_key: str, service_id) -> Optional[bool]:
    """True/False از کش اگر پنل برایِ این سرویس پرچمِ cancel داده؛ وگرنه None (نامعلوم ⇒ دکمه نشان داده می‌شود)."""
    return (_CANCEL.get(_panel_key(api_url, api_key)) or {}).get(str(service_id))


def _panel_key(api_url: str, api_key: str) -> str:
    return f"{api_url}|{hashlib.sha1((api_key or '').encode()).hexdigest()[:10]}"


async def _fetch_support(api_url: str, api_key: str, timeout: float) -> tuple[dict[str, bool], bool]:
    from tenant.bots.virtual_service import smm_api
    services = await asyncio.wait_for(smm_api.get_services(api_url, api_key), timeout)
    table: dict[str, bool] = {}
    has_flag = False
    # === PATCH_SMM_CANCEL_FLAG_V1 === پرچمِ «cancel» هر سرویس (از همان فهرست) برایِ پنهان‌کردنِ دکمهٔ لغو
    ctable: dict[str, bool] = {}
    for it in services or []:
        if it.get("cancel_known"):
            ctable[str(it.get("service"))] = bool(it.get("cancel"))
    _CANCEL[_panel_key(api_url, api_key)] = ctable
    for it in services or []:
        table[str(it.get("service"))] = bool(it.get("refill"))
        if it.get("refill_known"):
            has_flag = True
    return table, has_flag


async def _load(api_url: str, api_key: str, timeout: float) -> Optional[tuple[dict[str, bool], bool]]:
    """فهرست را می‌گیرد و کش می‌کند. چند درخواستِ هم‌زمان برایِ یک پنل، یک تماس می‌شوند."""
    key = _panel_key(api_url, api_key)
    fut = _INFLIGHT.get(key)
    if fut is not None:
        try:
            return await asyncio.shield(fut)
        except Exception:
            return None
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _INFLIGHT[key] = fut
    try:
        table, has_flag = await _fetch_support(api_url, api_key, timeout)
        if len(_SUPPORT) >= _SUPPORT_MAX_PANELS:
            _SUPPORT.pop(next(iter(_SUPPORT)))
        if table:
            _SUPPORT[key] = (time.monotonic(), table, has_flag)
        fut.set_result((table, has_flag))
        return table, has_flag
    except Exception as e:  # noqa: BLE001
        logger.warning("[refill] گرفتنِ فهرستِ سرویس‌هایِ پنل ناموفق بود (%s)", type(e).__name__)
        fut.set_result(None)
        return None
    finally:
        _INFLIGHT.pop(key, None)


def _lookup(table: dict[str, bool], has_flag: bool, service_id) -> Optional[bool]:
    if not has_flag:
        return None                       # پنل اصلاً پرچمِ refill نمی‌فرستد ⇒ نمی‌دانیم
    sid = str(service_id)
    if sid not in table:
        return None                       # سرویس دیگر در فهرست نیست ⇒ نمی‌دانیم
    return table[sid]


def cached_support(api_url: str, api_key: str, service_id) -> Optional[bool]:
    """بدونِ شبکه: True/False از کش، یا None (نامعلوم/کشِ سرد). برایِ رندرِ سریعِ صفحه‌ها."""
    ent = _SUPPORT.get(_panel_key(api_url, api_key))
    if not ent:
        return None
    return _lookup(ent[1], ent[2], service_id)


async def support(api_url: str, api_key: str, service_id, *, timeout: float = 12.0) -> Optional[bool]:
    """True = ارائه‌دهنده ریفیل دارد؛ False = ندارد؛ None = نامعلوم (پنل پرچم نمی‌دهد/در دسترس نیست)."""
    if not api_url or not api_key:
        return None
    ent = _SUPPORT.get(_panel_key(api_url, api_key))
    if ent and (time.monotonic() - ent[0]) < _SUPPORT_TTL:
        return _lookup(ent[1], ent[2], service_id)
    loaded = await _load(api_url, api_key, timeout)
    if loaded is None:
        # پنل در دسترس نیست: از کشِ کهنه (اگر هست) استفاده کن، وگرنه نامعلوم
        return _lookup(ent[1], ent[2], service_id) if ent else None
    return _lookup(loaded[0], loaded[1], service_id)


def warm(api_url: str, api_key: str) -> None:
    """کش را در پس‌زمینه گرم می‌کند (fire-and-forget)؛ هرگز خطا نمی‌دهد و مسدود نمی‌کند."""
    if not api_url or not api_key:
        return
    ent = _SUPPORT.get(_panel_key(api_url, api_key))
    if ent and (time.monotonic() - ent[0]) < _SUPPORT_TTL:
        return
    try:
        task = asyncio.get_running_loop().create_task(_load(api_url, api_key, 12.0))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except RuntimeError:
        pass


def clear_cache() -> None:
    _SUPPORT.clear()
    _INFLIGHT.clear()


# ═══════════════ ۲) وضعیت‌ها ═══════════════

def normalize_status(raw) -> str:
    """وضعیتِ خامِ پنل ⇒ یکی از pending / in_progress / completed / rejected."""
    r = str(raw or "").strip().lower().replace("_", " ")
    if r in ("completed", "complete", "done", "success", "successful", "finished"):
        return "completed"
    if r in ("rejected", "reject", "canceled", "cancelled", "cancel", "failed", "fail", "error",
             "declined", "refused", "denied"):
        return "rejected"
    if r in ("in progress", "inprogress", "processing", "started", "running", "active"):
        return "in_progress"
    return "pending"                       # Pending و هر چیزِ ناشناخته: منتظر می‌مانیم


# ═══════════════ ۳) ثبتِ درخواست ═══════════════

@dataclass
class RefillResult:
    ok: bool
    code: str          # ok | not_found | bad_status | unsupported | duplicate | panel_error | no_panel
    text: str          # پیامِ آمادهٔ نمایش (HTML)
    refill_id: Optional[str] = None
    refill_pk: Optional[int] = None


async def latest(order_pk: int) -> Optional[SmmRefill]:
    async with Session() as s:
        return await s.scalar(select(SmmRefill).where(SmmRefill.order_pk == order_pk)
                              .order_by(SmmRefill.id.desc()).limit(1))


def is_active(rf: Optional[SmmRefill]) -> bool:
    if rf is None or rf.status not in ACTIVE:
        return False
    if rf.status == "requesting":
        ts = rf.updated_at or rf.created_at
        return bool(ts and datetime.utcnow() - ts < _REQUESTING_STALE)
    return True


async def request_refill(bot_id: int, order_pk: int, user_tg: int) -> RefillResult:
    """ثبتِ یک ریفیل. تنها جایی که رویِ پنل «refill» می‌زند."""
    from tenant.bots.virtual_service import smm_api
    from tenant.bots.virtual_service.smm import _panel_creds

    # ۱) بررسی و رزروِ ردیف — زیرِ قفلِ سفارش تا دو کلیکِ هم‌زمان دو درخواست نسازند
    async with Session() as s:
        o = await s.get(SmmOrder, order_pk, with_for_update=True)
        if not o or o.bot_id != bot_id or o.user_telegram_id != user_tg:
            return RefillResult(False, "not_found", "❌ این سفارش پیدا نشد.")
        if o.status not in REFILLABLE_ORDER_STATUSES or not o.panel_order_id:
            return RefillResult(False, "bad_status", "❌ این سفارش (در وضعیتِ فعلی) قابلِ ریفیل نیست.")
        prev = await s.scalar(select(SmmRefill).where(SmmRefill.order_pk == order_pk)
                              .order_by(SmmRefill.id.desc()).limit(1))
        if is_active(prev):
            lbl = STATUS_LABEL.get(prev.status, prev.status)
            return RefillResult(False, "duplicate",
                                f"⏳ یک درخواستِ ریفیل برایِ این سفارش همین حالا در جریان است ({lbl}).\n"
                                "نتیجه که مشخص شد همین‌جا خبرت می‌کنیم.",
                                refill_id=prev.panel_refill_id, refill_pk=prev.id)
        service_id, panel_pk, panel_order_id = o.service_id, o.panel_pk, o.panel_order_id
        rf = SmmRefill(bot_id=bot_id, order_pk=order_pk, user_telegram_id=user_tg, panel_pk=panel_pk,
                       status="requesting", updated_at=datetime.utcnow())
        s.add(rf)
        await s.flush()
        rf_pk = rf.id
        await s.commit()

    async def _drop(status: str = "rejected", note: str = "") -> None:
        async with Session() as s2:
            r2 = await s2.get(SmmRefill, rf_pk)
            if r2 is not None:
                r2.status = status
                r2.panel_status = (note or "")[:60] or r2.panel_status
                r2.notified_status = status            # شکستِ همان لحظه را جدا به کاربر می‌گوییم؛ دوباره اعلان نشود
                r2.updated_at = datetime.utcnow()
                await s2.commit()

    api_url, api_key, _ = await _panel_creds(bot_id, panel_pk)
    if not api_url or not api_key:
        await _drop(note="پنل در دسترس نیست")
        return RefillResult(False, "no_panel", "❌ اتصال به ارائه‌دهنده برقرار نیست؛ کمی بعد دوباره امتحان کن.")

    # ۲) آیا ارائه‌دهنده برایِ این سرویس ریفیل دارد؟
    sup = await support(api_url, api_key, service_id)
    if sup is False:
        await _drop(note="سرویس ریفیل ندارد")
        return RefillResult(False, "unsupported",
                            "🚫 ارائه‌دهنده برایِ این سرویس <b>ریفیل (جبرانی) ندارد</b>؛ پس درخواستی ثبت نشد.")

    # ۳) درخواست رویِ پنل — فقط شناسهٔ معتبر = موفق
    d = await smm_api.refill_order(api_url, api_key, panel_order_id)
    if not d.get("refill"):
        err = str(d.get("error") or "پاسخِ نامعتبر از پنل")
        await _drop(note=err)
        from tenant.bots.virtual_service.smm import panel_err_friendly   # PATCH_SMM_PANEL_ERR_FRIENDLY_V1
        return RefillResult(False, "panel_error", "❌ " + panel_err_friendly(err, "درخواستِ ریفیل"))
    rid = str(d["refill"])
    async with Session() as s3:
        r3 = await s3.get(SmmRefill, rf_pk)
        if r3 is not None:
            r3.status = "pending"
            r3.panel_refill_id = rid
            r3.updated_at = datetime.utcnow()
        oo = await s3.get(SmmOrder, order_pk)
        if oo is not None:
            oo.note = ((oo.note or "") + f" | درخواستِ ریفیل (refill_id={rid})")[:1000]
        await s3.commit()
    return RefillResult(True, "ok", "", refill_id=rid, refill_pk=rf_pk)


def _esc(v) -> str:
    import html
    return html.escape(str(v), quote=False)


# ═══════════════ ۴) پیگیریِ وضعیت (پس‌زمینه) ═══════════════

async def poll_once(limit: int = 200) -> dict:
    """ریفیل‌هایِ در جریان را از پنل می‌پرسد، تغییرها را ذخیره و به مشتری اطلاع می‌دهد.
    خروجی: شمارنده‌ها (برایِ لاگ/تست). هرگز exception نمی‌دهد."""
    from tenant.bots.virtual_service import smm_api
    from tenant.bots.virtual_service.smm import _panel_creds, _notify_user, _smm_txt

    stats = {"checked": 0, "completed": 0, "rejected": 0, "expired": 0, "errors": 0, "notified": 0}
    now = datetime.utcnow()
    try:
        async with Session() as s:
            rows = (await s.scalars(select(SmmRefill).where(
                SmmRefill.status.in_(("pending", "in_progress")),
                SmmRefill.panel_refill_id.isnot(None),
            ).order_by(SmmRefill.updated_at.asc()).limit(limit))).all()
            due = [(r.id, r.bot_id, r.order_pk, r.panel_pk, r.panel_refill_id, r.status, r.created_at,
                    r.updated_at) for r in rows]
            # ریفیل‌هایِ «requesting» کهنه (کرش وسطِ تماس): رها کن
            stale = (await s.scalars(select(SmmRefill).where(SmmRefill.status == "requesting"))).all()
            for r in stale:
                ts = r.updated_at or r.created_at
                if ts and now - ts > _REQUESTING_STALE * 5:
                    r.status = "rejected"
                    r.notified_status = "rejected"
                    r.panel_status = "ثبت ناقص ماند"
            await s.commit()
    except Exception:
        logger.exception("[refill] خواندنِ ریفیل‌هایِ در جریان ناموفق بود")
        stats["errors"] += 1
        return stats

    creds_cache: dict[tuple[int, Optional[int]], tuple[str, str, str]] = {}
    for rid_pk, bot_id, order_pk, panel_pk, panel_refill_id, cur_status, created_at, updated_at in due:
        try:
            if created_at and now - created_at > _EXPIRE_AFTER:
                await _set_status(rid_pk, "expired", None)
                stats["expired"] += 1
                continue
            if updated_at and now - updated_at < _MIN_POLL_GAP:
                continue
            ck = (bot_id, panel_pk)
            if ck not in creds_cache:
                creds_cache[ck] = await _panel_creds(bot_id, panel_pk)
            api_url, api_key, _ = creds_cache[ck]
            if not api_url or not api_key:
                continue
            res = await smm_api.get_refill_status(api_url, api_key, panel_refill_id)
            stats["checked"] += 1
            if res.get("error"):
                stats["errors"] += 1
                logger.warning("[refill] پیگیریِ ریفیل %s ناموفق: %s", panel_refill_id, res["error"])
                await _touch(rid_pk)
                continue
            new = normalize_status(res["status"])
            await _set_status(rid_pk, new, str(res["status"])[:60])
            if new in ("completed", "rejected"):
                stats[new] += 1
                key = "refill_done" if new == "completed" else "refill_rejected"
                if await _claim_notification(rid_pk, new):
                    await _notify_user(bot_id, order_pk, await _smm_txt(bot_id, key))
                    stats["notified"] += 1
            await asyncio.sleep(0.05)
        except Exception:
            stats["errors"] += 1
            logger.exception("[refill] پردازشِ ریفیل #%s خطا داد", rid_pk)
    return stats


async def _set_status(pk: int, status: str, panel_status: Optional[str]) -> None:
    async with Session() as s:
        r = await s.get(SmmRefill, pk)
        if r is not None:
            r.status = status
            if panel_status is not None:
                r.panel_status = panel_status
            r.updated_at = datetime.utcnow()
            await s.commit()


async def _touch(pk: int) -> None:
    async with Session() as s:
        r = await s.get(SmmRefill, pk)
        if r is not None:
            r.updated_at = datetime.utcnow()
            await s.commit()


async def _claim_notification(pk: int, status: str) -> bool:
    """فقط یک‌بار (حتی با چند پروسه) اجازهٔ اعلان می‌دهد."""
    async with Session() as s:
        r = await s.get(SmmRefill, pk, with_for_update=True)
        if r is None or r.notified_status == status:
            return False
        r.notified_status = status
        await s.commit()
        return True
