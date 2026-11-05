"""
engine.py — موتورِ رزروِ حرفه‌ای (PATCH_BOOKING_V2).

بخشِ «ریاضی» (بازه‌ها/ساعت‌هایِ خالی) خالص و بدونِ دیتابیس است تا کاملاً قابلِ تست باشد؛ بخشِ دیتابیس فقط داده را می‌آورد و می‌نویسد.

مدلِ زمان: تاریخ = datetime.date (وقتِ ایران)، ساعت = «دقیقه از نیمه‌شب» (۰..۱۴۴۰). هفته از شنبه شروع می‌شود (۰=شنبه … ۶=جمعه).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from sqlalchemy import select

from config import Session
from models import BkAppt, BkCustomer, BkHours, BkOff, BkPayment, BkService, BkStaff

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

WEEKDAY_FA = ["شنبه", "یک‌شنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه"]
ACTIVE_STATUSES = ("pending", "confirmed")          # وضعیت‌هایی که زمان را اشغال می‌کنند

DEFAULTS = {
    "bk_slot_step": "30",        # فاصلهٔ شروعِ نوبت‌ها (دقیقه)
    "bk_lead_min": "60",         # حداقل فاصلهٔ رزرو تا شروعِ نوبت (دقیقه)
    "bk_days_ahead": "14",       # تا چند روزِ آینده قابلِ رزرو است
    "bk_auto_confirm": "0",      # ۱ = نوبت بدونِ تأییدِ مالک قطعی می‌شود
    "bk_cancel_hours": "12",     # لغوِ توسطِ مشتری فقط تا این‌قدر ساعت قبل
    "bk_remind1_h": "24",        # یادآوریِ اول (ساعت قبل؛ ۰ = خاموش)
    "bk_remind2_h": "2",         # یادآوریِ دوم
    "bk_use_staff": "0",         # ۱ = مشتری پرسنل انتخاب می‌کند
    "bk_biz_name": "",
    "bk_biz_address": "",
    "bk_biz_phone": "",
    "bk_welcome": "",
}


# ───────────────────────── زمان ─────────────────────────

def iran_now() -> _dt.datetime:
    return _dt.datetime.utcnow() + _dt.timedelta(hours=3, minutes=30)


def fa_weekday(d: _dt.date) -> int:
    """۰=شنبه … ۶=جمعه (weekday()ِ پایتون: دوشنبه=۰ ⇒ شنبه=۵)."""
    return (d.weekday() + 2) % 7


def fmt_hm(m: int) -> str:
    m = max(0, min(int(m), 24 * 60))
    return f"{m // 60:02d}:{m % 60:02d}"


_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def to_en_digits(s: str) -> str:
    return (s or "").translate(_FA)


def parse_hm(s: str) -> int | None:
    """«۱۰:۳۰» / «10:30» / «1030» / «9» ⇒ دقیقه؛ نامعتبر ⇒ None."""
    t = to_en_digits(s).strip().replace("٫", ":").replace(".", ":")
    if not t:
        return None
    try:
        if ":" in t:
            h, m = t.split(":")[:2]
            h, m = int(h), int(m)
        elif len(t) <= 2:
            h, m = int(t), 0
        elif len(t) in (3, 4) and t.isdigit():
            h, m = int(t[:-2]), int(t[-2:])
        else:
            return None
    except ValueError:
        return None
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m):
        return None
    return h * 60 + m


def fa_date(d: _dt.date) -> str:
    try:
        import jdatetime as _jd
        return _jd.date.fromgregorian(date=d).strftime("%Y/%m/%d")
    except Exception:      # noqa: BLE001
        return d.isoformat()


def fa_date_long(d: _dt.date) -> str:
    return f"{WEEKDAY_FA[fa_weekday(d)]} {fa_date(d)}"


def parse_fa_date(s: str, today: _dt.date | None = None) -> _dt.date | None:
    """تاریخِ شمسیِ «1405/08/14» (یا «08/14» = سالِ جاری) ⇒ date میلادی؛ نامعتبر ⇒ None."""
    t = to_en_digits(s).strip().replace("-", "/")
    parts = [p for p in t.split("/") if p]
    try:
        import jdatetime as _jd
        nums = [int(p) for p in parts]
        if len(nums) == 2:
            y = _jd.date.fromgregorian(date=today or iran_now().date()).year
            nums = [y] + nums
        if len(nums) != 3:
            return None
        return _jd.date(nums[0], nums[1], nums[2]).togregorian()
    except Exception:      # noqa: BLE001
        return None


# ───────────────────────── ریاضیِ بازه‌ها (خالص) ─────────────────────────

Interval = tuple[int, int]


def merge(intervals: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for s, e in sorted((a, b) for a, b in intervals if b > a):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def subtract(avail: list[Interval], busy: list[Interval]) -> list[Interval]:
    """avail منهای busy (هر دو لیستِ بازه)."""
    res: list[Interval] = []
    b = merge(busy)
    for s, e in merge(avail):
        cur = s
        for bs, be in b:
            if be <= cur or bs >= e:
                continue
            if bs > cur:
                res.append((cur, bs))
            cur = max(cur, be)
            if cur >= e:
                break
        if cur < e:
            res.append((cur, e))
    return res


def free_starts(avail: list[Interval], busy: list[Interval], duration: int, step: int, earliest: int = 0) -> list[int]:
    """شروع‌هایِ ممکن (مضربِ step از نیمه‌شب) که کلِ مدت در بازهٔ خالی جا شود و از earliest زودتر نباشد."""
    step = max(int(step or 30), 5)
    duration = max(int(duration or 30), 5)
    starts: list[int] = []
    for s, e in subtract(avail, busy):
        t = max(s, earliest)
        t = -(-t // step) * step                      # گرد به بالا روی شبکهٔ step
        while t + duration <= e:
            starts.append(t)
            t += step
    return starts


# ───────────────────────── دسترسی به تنظیمات/داده ─────────────────────────

async def get_cfg(bot_id: int) -> dict:
    from tenant import get_setting
    cfg: dict = {}
    for k, dflt in DEFAULTS.items():
        cfg[k] = await get_setting(bot_id, k, dflt)

    def _i(k, lo, hi):
        try:
            return max(lo, min(hi, int(float(cfg[k]))))
        except (TypeError, ValueError):
            return int(DEFAULTS[k])
    return {
        "step": _i("bk_slot_step", 5, 240), "lead": _i("bk_lead_min", 0, 7 * 24 * 60),
        "days_ahead": _i("bk_days_ahead", 1, 60), "auto_confirm": cfg["bk_auto_confirm"] == "1",
        "cancel_h": _i("bk_cancel_hours", 0, 24 * 14), "remind1": _i("bk_remind1_h", 0, 24 * 7),
        "remind2": _i("bk_remind2_h", 0, 24 * 7), "use_staff": cfg["bk_use_staff"] == "1",
        "biz_name": cfg["bk_biz_name"], "biz_address": cfg["bk_biz_address"],
        "biz_phone": cfg["bk_biz_phone"], "welcome": cfg["bk_welcome"],
    }


async def list_services(bot_id: int, only_active: bool = True) -> list[BkService]:
    async with Session() as s:
        q = select(BkService).where(BkService.bot_id == bot_id)
        if only_active:
            q = q.where(BkService.active.is_(True))
        return list((await s.scalars(q.order_by(BkService.sort_order, BkService.id))).all())


async def list_staff(bot_id: int, only_active: bool = True) -> list[BkStaff]:
    async with Session() as s:
        q = select(BkStaff).where(BkStaff.bot_id == bot_id)
        if only_active:
            q = q.where(BkStaff.active.is_(True))
        return list((await s.scalars(q.order_by(BkStaff.id))).all())


def staff_does(staff: BkStaff, service_id: int) -> bool:
    ids = [int(x) for x in (staff.service_ids or "").split(",") if x.strip().isdigit()]
    return not ids or int(service_id) in ids


@dataclass
class Snapshot:
    """داده‌هایِ لازم برایِ محاسبهٔ ساعت‌هایِ خالیِ یک بازهٔ تاریخ (یک‌بار از دیتابیس خوانده می‌شود)."""
    hours: dict = field(default_factory=dict)         # (staff_id, weekday) → [(s,e)]
    staff_has_hours: set = field(default_factory=set)  # staff_idهایی که برنامهٔ اختصاصی دارند
    offs: dict = field(default_factory=dict)          # (staff_id, date_str) → [(s,e)] (کل روز = (0,1440))
    appts: dict = field(default_factory=dict)         # (staff_id, date_str) → [(s,e)]


async def load_snapshot(bot_id: int, d0: _dt.date, d1: _dt.date) -> Snapshot:
    snap = Snapshot()
    a, b = d0.isoformat(), d1.isoformat()
    async with Session() as s:
        for h in (await s.scalars(select(BkHours).where(BkHours.bot_id == bot_id))).all():
            snap.hours.setdefault((h.staff_id, h.weekday), []).append((h.start_min, h.end_min))
            snap.staff_has_hours.add(h.staff_id)
        for o in (await s.scalars(select(BkOff).where(
                BkOff.bot_id == bot_id, BkOff.date_str >= a, BkOff.date_str <= b))).all():
            iv = (0, 24 * 60) if o.start_min is None else (o.start_min, o.end_min or 24 * 60)
            snap.offs.setdefault((o.staff_id, o.date_str), []).append(iv)
        for ap in (await s.scalars(select(BkAppt).where(
                BkAppt.bot_id == bot_id, BkAppt.date_str >= a, BkAppt.date_str <= b,
                BkAppt.status.in_(ACTIVE_STATUSES)))).all():
            snap.appts.setdefault((ap.staff_id, ap.date_str), []).append((ap.start_min, ap.end_min))
    return snap


def _hours_of(snap: Snapshot, staff_id: int, wd: int) -> list[Interval]:
    """برنامهٔ اختصاصیِ پرسنل اگر دارد، وگرنه ساعتِ کاریِ مجموعه."""
    if staff_id and staff_id in snap.staff_has_hours:
        return list(snap.hours.get((staff_id, wd), []))
    return list(snap.hours.get((0, wd), []))


def slots_for_day(snap: Snapshot, d: _dt.date, duration: int, cfg: dict, resources: list[int],
                  now: _dt.datetime | None = None) -> list[tuple[int, int]]:
    """[(شروع، resource)] مرتب؛ هر شروع برایِ اولین resourceِ آزاد. resource=0 یعنی «بدونِ پرسنل»."""
    now = now or iran_now()
    today = now.date()
    if d < today or d > today + _dt.timedelta(days=cfg["days_ahead"]):
        return []
    earliest = 0
    if d == today:
        earliest = now.hour * 60 + now.minute + cfg["lead"]
    ds = d.isoformat()
    best: dict[int, int] = {}
    for r in resources:
        avail = _hours_of(snap, r, fa_weekday(d))
        if not avail:
            continue
        busy = snap.appts.get((r, ds), []) + snap.offs.get((r, ds), []) + (snap.offs.get((0, ds), []) if r else [])
        if r:
            busy = busy + snap.appts.get((0, ds), [])        # نوبتِ «بدونِ پرسنل» کلِ مجموعه را اشغال می‌کند
        for st in free_starts(avail, busy, duration, cfg["step"], earliest):
            best.setdefault(st, r)
    return sorted(best.items())


async def resources_for(bot_id: int, cfg: dict, service_id: int, staff_id: int | None) -> list[int]:
    """چه resourceهایی این خدمت را می‌دهند؟ [0] = بدونِ پرسنل."""
    if not cfg["use_staff"]:
        return [0]
    staff = [st for st in await list_staff(bot_id) if staff_does(st, service_id)]
    if staff_id:
        return [staff_id] if any(st.id == staff_id for st in staff) else []
    return [st.id for st in staff] or [0]


async def open_days(bot_id: int, service: BkService, staff_id: int | None, cfg: dict,
                    now: _dt.datetime | None = None) -> list[_dt.date]:
    now = now or iran_now()
    today = now.date()
    last = today + _dt.timedelta(days=cfg["days_ahead"])
    snap = await load_snapshot(bot_id, today, last)
    res = await resources_for(bot_id, cfg, service.id, staff_id)
    out = []
    d = today
    while d <= last:
        if slots_for_day(snap, d, service.duration_min, cfg, res, now):
            out.append(d)
        d += _dt.timedelta(days=1)
    return out


async def day_slots(bot_id: int, service: BkService, d: _dt.date, staff_id: int | None, cfg: dict,
                    now: _dt.datetime | None = None) -> list[tuple[int, int]]:
    snap = await load_snapshot(bot_id, d, d)
    res = await resources_for(bot_id, cfg, service.id, staff_id)
    return slots_for_day(snap, d, service.duration_min, cfg, res, now)


# ───────────────────────── ثبت/لغو ─────────────────────────

def has_telegram(tg_id) -> bool:
    """مشتریِ دستی (ثبت‌شده توسطِ مالک، بدونِ تلگرام) شناسهٔ منفی دارد؛ به او پیام/یادآوری فرستاده نمی‌شود."""
    try:
        return int(tg_id) > 0
    except (TypeError, ValueError):
        return False


def tg_link_html(tg_id) -> str:
    return f'<a href="tg://user?id={int(tg_id)}">پروفایل تلگرام</a>' if has_telegram(tg_id) else "(بدونِ تلگرام — ثبتِ دستی)"


async def book(bot_id: int, tg_id: int, service: BkService, d: _dt.date, start: int, staff_id: int | None,
               name: str, phone: str, note: str, cfg: dict, now: _dt.datetime | None = None, by_owner: bool = False):
    """نوبت را ثبت می‌کند؛ قبل از ثبت دوباره موجود بودنِ ساعت را از دیتابیس می‌سنجد.
    by_owner=True (ثبتِ دستیِ مالک): نوبت مستقیم «تأییدشده» است، «حداقل فاصله تا نوبت» و «مسدود بودنِ مشتری» اعمال نمی‌شود
    (تداخل با نوبت/تعطیلی/ساعتِ کاری همچنان کنترل می‌شود).
    خروجی: (BkAppt | None, خطا | None) — خطا: 'taken' | 'blocked'."""
    if by_owner:
        cfg = {**cfg, "lead": 0}
    else:
        async with Session() as s:
            cu = await s.scalar(select(BkCustomer).where(BkCustomer.bot_id == bot_id, BkCustomer.tg_id == int(tg_id)))
        if cu and cu.blocked:
            return None, "blocked"
    # «بررسیِ آزاد بودن + ثبت» برایِ یک روز پشتِ‌سرهم انجام می‌شود تا دو رزروِ هم‌زمانِ همان ساعت (در همین پروسه) هر دو جا نشوند
    async with _book_lock(bot_id, d):
        return await _book_locked(bot_id, tg_id, service, d, start, staff_id, name, phone, note, cfg, now, by_owner)


_BOOK_LOCKS: dict = {}


def _book_lock(bot_id: int, d: _dt.date):
    import asyncio
    k = (bot_id, d.isoformat())
    if len(_BOOK_LOCKS) > 512:           # حافظه محدود بماند
        for kk in [x for x, v in _BOOK_LOCKS.items() if not v.locked()][:256]:
            _BOOK_LOCKS.pop(kk, None)
    return _BOOK_LOCKS.setdefault(k, asyncio.Lock())


async def _book_locked(bot_id, tg_id, service, d, start, staff_id, name, phone, note, cfg, now, by_owner):
    snap = await load_snapshot(bot_id, d, d)
    res = await resources_for(bot_id, cfg, service.id, staff_id)
    slots = dict(slots_for_day(snap, d, service.duration_min, cfg, res, now))
    if start not in slots:
        return None, "taken"
    rid = slots[start]
    staff_name = None
    if rid:
        async with Session() as s:
            st = await s.get(BkStaff, rid)
            staff_name = st.name if st else None
    async with Session() as s:
        ap = BkAppt(bot_id=bot_id, tg_id=int(tg_id), service_id=service.id, service_name=service.name, staff_id=rid,
                    staff_name=staff_name, date_str=d.isoformat(), start_min=start, end_min=start + service.duration_min,
                    price=int(service.price or 0), customer_name=(name or "")[:80], phone=(phone or "")[:20],
                    note=(note or "")[:1000], status="confirmed" if (cfg["auto_confirm"] or by_owner) else "pending")
        s.add(ap)
        # پروندهٔ مشتری: ساخت/به‌روزرسانیِ نام و شماره (یادداشت و «مسدود» دست‌نخورده می‌ماند)
        cu = await s.scalar(select(BkCustomer).where(BkCustomer.bot_id == bot_id, BkCustomer.tg_id == int(tg_id)))
        if cu is None:
            s.add(BkCustomer(bot_id=bot_id, tg_id=int(tg_id), name=(name or "")[:80], phone=(phone or "")[:20]))
        else:
            cu.name, cu.phone = (name or cu.name or "")[:80], (phone or cu.phone or "")[:20]
        await s.commit()
        return ap, None


def appt_start_dt(ap: BkAppt) -> _dt.datetime:
    y, m, d = (int(x) for x in ap.date_str.split("-"))
    return _dt.datetime(y, m, d) + _dt.timedelta(minutes=int(ap.start_min))


def can_cancel(ap: BkAppt, cfg: dict, now: _dt.datetime | None = None) -> tuple[bool, str]:
    now = now or iran_now()
    if ap.status not in ACTIVE_STATUSES:
        return False, "این نوبت دیگر قابلِ لغو نیست."
    left = appt_start_dt(ap) - now
    if left.total_seconds() <= 0:
        return False, "زمانِ این نوبت گذشته است."
    if left < _dt.timedelta(hours=cfg["cancel_h"]):
        return False, f"لغوِ نوبت فقط تا {cfg['cancel_h']} ساعت قبل از شروع ممکن است؛ لطفاً با مجموعه تماس بگیرید."
    return True, ""


async def set_status(bot_id: int, appt_id: int, status: str, reason: str | None = None,
                     only_from: tuple | None = None) -> BkAppt | None:
    async with Session() as s:
        ap = await s.get(BkAppt, appt_id, with_for_update=True)
        if not ap or ap.bot_id != bot_id:
            return None
        if only_from and ap.status not in only_from:
            return None
        ap.status = status
        if reason:
            ap.cancel_reason = reason[:160]
        await s.commit()
        return ap


def reminder_due(ap: BkAppt, cfg: dict, now: _dt.datetime | None = None) -> int:
    """۰ = چیزی لازم نیست؛ ۱/۲ = یادآوریِ اول/دوم باید ارسال شود (فقط برایِ نوبتِ قطعی و آینده)."""
    now = now or iran_now()
    if ap.status != "confirmed":
        return 0
    left = appt_start_dt(ap) - now
    if left.total_seconds() <= 0:
        return 0
    if cfg["remind2"] and not ap.remind2_sent and left <= _dt.timedelta(hours=cfg["remind2"]):
        return 2
    if cfg["remind1"] and not ap.remind1_sent and left <= _dt.timedelta(hours=cfg["remind1"]):
        return 1
    return 0


# ───────────────────────── مالی ─────────────────────────

async def paid_of(bot_id: int, appt_id: int) -> int:
    from sqlalchemy import func as _f
    async with Session() as s:
        return int(await s.scalar(select(_f.coalesce(_f.sum(BkPayment.amount), 0)).where(
            BkPayment.bot_id == bot_id, BkPayment.appt_id == appt_id)) or 0)


async def add_payment(bot_id: int, appt_id: int, amount: int, method: str = "cash", note: str = "") -> BkPayment | None:
    async with Session() as s:
        ap = await s.get(BkAppt, appt_id)
        if not ap or ap.bot_id != bot_id or amount <= 0:
            return None
        p = BkPayment(bot_id=bot_id, appt_id=appt_id, amount=int(amount), method=method if method in ("cash", "card", "online", "other") else "other",
                      note=(note or "")[:160])
        s.add(p)
        await s.commit()
        return p


async def finance_summary(bot_id: int, now: _dt.datetime | None = None) -> dict:
    """گزارشِ مالیِ نقدی: دریافتی‌ها (بر اساسِ تاریخِ ثبتِ پرداخت)، مطالباتِ انجام‌شده‌هایِ بی‌پرداخت، و درآمدِ پیش‌رو."""
    from sqlalchemy import func as _f
    now = now or iran_now()
    today = now.date()
    d7, d30 = today - _dt.timedelta(days=6), today - _dt.timedelta(days=29)
    async with Session() as s:
        pays = (await s.scalars(select(BkPayment).where(BkPayment.bot_id == bot_id, BkPayment.created_at >= _dt.datetime.combine(d30, _dt.time())))).all()
        by_day: dict[str, int] = {}
        by_method: dict[str, int] = {}
        t_today = t7 = t30 = 0
        for p in pays:
            # created_at به UTC ذخیره می‌شود ⇒ تبدیل به وقتِ ایران
            ld = (p.created_at + _dt.timedelta(hours=3, minutes=30)).date()
            by_day[ld.isoformat()] = by_day.get(ld.isoformat(), 0) + p.amount
            by_method[p.method] = by_method.get(p.method, 0) + p.amount
            t30 += p.amount
            if ld >= d7:
                t7 += p.amount
            if ld == today:
                t_today += p.amount
        done = (await s.scalars(select(BkAppt).where(BkAppt.bot_id == bot_id, BkAppt.status == "done"))).all()
        paid_map = dict((await s.execute(select(BkPayment.appt_id, _f.sum(BkPayment.amount)).where(BkPayment.bot_id == bot_id)
                                        .group_by(BkPayment.appt_id))).all())
        owed = [(a, int(a.price or 0) - int(paid_map.get(a.id, 0) or 0)) for a in done]
        owed = [(a, o) for a, o in owed if o > 0]
        upcoming = int(await s.scalar(select(_f.coalesce(_f.sum(BkAppt.price), 0)).where(
            BkAppt.bot_id == bot_id, BkAppt.status == "confirmed", BkAppt.date_str >= today.isoformat())) or 0)
        by_service = (await s.execute(select(BkAppt.service_name, _f.count(), _f.coalesce(_f.sum(BkAppt.price), 0)).where(
            BkAppt.bot_id == bot_id, BkAppt.status == "done", BkAppt.date_str >= d30.isoformat())
            .group_by(BkAppt.service_name).order_by(_f.sum(BkAppt.price).desc()).limit(6))).all()
        by_staff = (await s.execute(select(BkAppt.staff_name, _f.count(), _f.coalesce(_f.sum(BkAppt.price), 0)).where(
            BkAppt.bot_id == bot_id, BkAppt.status == "done", BkAppt.date_str >= d30.isoformat(), BkAppt.staff_id != 0)
            .group_by(BkAppt.staff_name).order_by(_f.sum(BkAppt.price).desc()).limit(6))).all()
    return {"today": t_today, "d7": t7, "d30": t30, "by_day": by_day, "by_method": by_method,
            "owed_total": sum(o for _a, o in owed), "owed": owed, "upcoming": upcoming,
            "by_service": [(n, int(c), int(v)) for n, c, v in by_service], "by_staff": [(n or "—", int(c), int(v)) for n, c, v in by_staff]}


async def find_or_create_manual_customer(bot_id: int, name: str, phone: str) -> BkCustomer:
    """مشتریِ حضوری/تلفنی: اگر شماره‌اش قبلاً در پرونده‌ها هست همان پرونده (جلوگیریِ از تکرار)، وگرنه پروندهٔ تازه با شناسهٔ
    منفیِ یکتا (بدونِ تلگرام). شناسه‌هایِ منفی فقط داخلِ همین ربات یکتا هستند."""
    from sqlalchemy import func as _f
    from sqlalchemy.exc import IntegrityError
    name, phone = (name or "").strip()[:80], (phone or "").strip()[:20]
    async with Session() as s:
        if phone:
            c = await s.scalar(select(BkCustomer).where(BkCustomer.bot_id == bot_id, BkCustomer.phone == phone).order_by(BkCustomer.id).limit(1))
            if c:
                if name and not c.name:
                    c.name = name
                    await s.commit()
                return c
        for _ in range(4):
            mn = await s.scalar(select(_f.min(BkCustomer.tg_id)).where(BkCustomer.bot_id == bot_id, BkCustomer.tg_id <= 0))
            c = BkCustomer(bot_id=bot_id, tg_id=int(mn or 0) - 1, name=name or None, phone=phone or None)
            s.add(c)
            try:
                await s.commit()
                return c
            except IntegrityError:
                await s.rollback()
        raise RuntimeError("manual customer id allocation failed")
