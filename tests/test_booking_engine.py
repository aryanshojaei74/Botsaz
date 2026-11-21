"""PATCH_BOOKING_V2 — موتورِ رزرو: ریاضیِ بازه‌ها + ساعت‌هایِ خالی + ثبت/لغو/یادآوری."""
import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.booking import engine as E

BOT = 11


# ───────────── ریاضیِ خالص ─────────────

def test_parse_and_fmt_time_and_dates():
    assert E.parse_hm("۱۰:۳۰") == 630 and E.parse_hm("9") == 540 and E.parse_hm("1430") == 870 and E.parse_hm("24:00") == 1440
    assert E.parse_hm("25:00") is None and E.parse_hm("10:75") is None and E.parse_hm("ab") is None and E.parse_hm("") is None
    assert E.fmt_hm(630) == "10:30" and E.fmt_hm(0) == "00:00"
    d = E.parse_fa_date("1405/08/14")
    assert d == dt.date(2026, 11, 5) and E.fa_date(d) == "1405/08/14" and E.WEEKDAY_FA[E.fa_weekday(d)] == "پنج‌شنبه"
    assert E.parse_fa_date("۱۴۰۵/۱۳/۴۰") is None and E.parse_fa_date("x") is None


def test_merge_subtract_and_free_starts():
    assert E.merge([(60, 120), (100, 200), (300, 400)]) == [(60, 200), (300, 400)]
    assert E.subtract([(0, 100)], [(20, 30), (50, 60)]) == [(0, 20), (30, 50), (60, 100)]
    assert E.subtract([(0, 100)], [(0, 100)]) == [] and E.subtract([(0, 100)], []) == [(0, 100)]
    # ساعتِ کاری ۹ تا ۱۲، خدمتِ ۶۰ دقیقه‌ای، گام ۳۰
    assert E.free_starts([(540, 720)], [], 60, 30) == [540, 570, 600, 630, 660]
    # نوبتی از ۱۰ تا ۱۱ ⇒ فقط ۹:۰۰ (تا ۱۰) و ۱۱:۰۰ (تا ۱۲)
    assert E.free_starts([(540, 720)], [(600, 660)], 60, 30) == [540, 660]
    # earliest: شروع زودتر از ۱۰:۱۰ ممکن نیست ⇒ گردشده روی شبکه
    assert E.free_starts([(540, 720)], [], 60, 30, earliest=610) == [630, 660]
    assert E.free_starts([(540, 560)], [], 60, 30) == []                 # مدت جا نمی‌شود


def _cfg(**kw):
    base = dict(step=30, lead=60, days_ahead=14, auto_confirm=False, cancel_h=12, remind1=24, remind2=2, use_staff=False,
                biz_name="", biz_address="", biz_phone="", welcome="")
    base.update(kw)
    return base


def test_slots_for_day_respects_today_lead_breaks_offs_and_range():
    snap = E.Snapshot()
    d = dt.date(2026, 11, 7)                                     # شنبه
    wd = E.fa_weekday(d)
    assert wd == 0
    snap.hours[(0, wd)] = [(540, 720), (840, 1020)]               # ۹–۱۲ و ۱۴–۱۷ (استراحت ۱۲ تا ۱۴)
    snap.staff_has_hours.add(0)
    now = dt.datetime(2026, 11, 5, 8, 0)
    got = [s for s, _ in E.slots_for_day(snap, d, 60, _cfg(), [0], now)]
    assert got[0] == 540 and 720 not in got and 780 not in got and 840 in got and got[-1] == 960
    # روزِ تعطیل
    snap.offs[(0, d.isoformat())] = [(0, 1440)]
    assert E.slots_for_day(snap, d, 60, _cfg(), [0], now) == []
    # خارج از بازهٔ days_ahead
    assert E.slots_for_day(snap, dt.date(2026, 12, 30), 60, _cfg(), [0], now) == []
    # گذشته
    assert E.slots_for_day(snap, dt.date(2026, 11, 1), 60, _cfg(), [0], now) == []
    # امروز با lead: ساعتِ ۱۰:۰۰ الان ⇒ شروعِ زودتر از ۱۱:۰۰ نیست
    t = dt.date(2026, 11, 5)
    snap.hours[(0, E.fa_weekday(t))] = [(540, 720)]
    assert [s for s, _ in E.slots_for_day(snap, t, 30, _cfg(), [0], dt.datetime(2026, 11, 5, 10, 0))][0] == 660


def test_staff_specific_hours_and_any_staff_assignment():
    snap = E.Snapshot()
    d = dt.date(2026, 11, 7)
    wd = E.fa_weekday(d)
    snap.hours[(1, wd)] = [(540, 600)]       # پرسنل ۱: فقط ۹–۱۰
    snap.hours[(2, wd)] = [(540, 660)]       # پرسنل ۲: ۹–۱۱
    snap.staff_has_hours.update({1, 2})
    snap.appts[(1, d.isoformat())] = [(540, 600)]      # پرسنل ۱ پر است
    now = dt.datetime(2026, 11, 5, 8, 0)
    got = dict(E.slots_for_day(snap, d, 60, _cfg(use_staff=True), [1, 2], now))
    assert got == {540: 2, 570: 2, 600: 2}                # همه به پرسنل ۲ می‌رسد
    # پرسنلِ بدونِ برنامهٔ اختصاصی ⇒ ساعتِ کاریِ مجموعه
    snap.hours[(0, wd)] = [(600, 660)]
    snap.staff_has_hours.add(0)
    assert [s for s, _ in E.slots_for_day(snap, d, 60, _cfg(use_staff=True), [3], now)] == [600]


# ───────────── با دیتابیس ─────────────

@pytest_asyncio.fixture
async def db(monkeypatch):
    import sys
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for m in list(sys.modules.values()):
        if getattr(m, "Session", None) is orig:
            monkeypatch.setattr(m, "Session", mk, raising=False)
    monkeypatch.setattr(config, "Session", mk)
    import tenant
    tenant._SETTING_CACHE.clear()
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=1, token="1:T", username="salon", field="booking", plan="pro", status="active"))
        s.add(models.BkService(id=1, bot_id=BOT, name="کوتاهی", duration_min=60, price=300000))
        wd = E.fa_weekday(dt.date(2026, 11, 7))
        s.add(models.BkHours(bot_id=BOT, staff_id=0, weekday=wd, start_min=540, end_min=720))
        await s.commit()
    yield mk
    await eng.dispose()


NOW = dt.datetime(2026, 11, 5, 8, 0)
SAT = dt.date(2026, 11, 7)


@pytest.mark.asyncio
async def test_book_blocks_double_booking_and_overlaps(db):
    cfg = await E.get_cfg(BOT)
    svc = (await E.list_services(BOT))[0]
    assert SAT in await E.open_days(BOT, svc, None, cfg, NOW)
    ap, err = await E.book(BOT, 100, svc, SAT, 600, None, "علی", "09120000000", "", cfg, NOW)
    assert err is None and ap.status == "pending" and (ap.start_min, ap.end_min) == (600, 660)
    # همان ساعت و ساعتِ هم‌پوشان رد می‌شود
    assert (await E.book(BOT, 101, svc, SAT, 600, None, "x", "09", "", cfg, NOW))[1] == "taken"
    assert (await E.book(BOT, 101, svc, SAT, 630, None, "x", "09", "", cfg, NOW))[1] == "taken"
    # ساعتِ دیگر آزاد است
    assert (await E.book(BOT, 101, svc, SAT, 540, None, "x", "09", "", cfg, NOW))[0] is not None
    left = [s for s, _ in await E.day_slots(BOT, svc, SAT, None, cfg, NOW)]
    assert left == [660]                                      # ۹–۱۰ و ۱۰–۱۱ پر؛ فقط ۱۱–۱۲ می‌ماند
    # لغو، ساعت را آزاد می‌کند
    await E.set_status(BOT, ap.id, "cancelled", "test")
    assert 600 in [s for s, _ in await E.day_slots(BOT, svc, SAT, None, cfg, NOW)]


@pytest.mark.asyncio
async def test_auto_confirm_cancel_window_and_reminders(db):
    from tenant import set_setting
    await set_setting(BOT, "bk_auto_confirm", "1")
    import tenant
    tenant._SETTING_CACHE.clear()
    cfg = await E.get_cfg(BOT)
    svc = (await E.list_services(BOT))[0]
    ap, _ = await E.book(BOT, 100, svc, SAT, 600, None, "علی", "0912", "", cfg, NOW)
    assert ap.status == "confirmed"
    start = E.appt_start_dt(ap)                                   # شنبه ۱۰:۰۰
    ok, _m = E.can_cancel(ap, cfg, start - dt.timedelta(hours=13))
    assert ok
    ok, msg = E.can_cancel(ap, cfg, start - dt.timedelta(hours=11))
    assert not ok and "12 ساعت" in msg
    assert not E.can_cancel(ap, cfg, start + dt.timedelta(minutes=1))[0]
    # یادآوری‌ها
    assert E.reminder_due(ap, cfg, start - dt.timedelta(hours=30)) == 0
    assert E.reminder_due(ap, cfg, start - dt.timedelta(hours=23)) == 1
    ap.remind1_sent = True
    assert E.reminder_due(ap, cfg, start - dt.timedelta(hours=23)) == 0
    assert E.reminder_due(ap, cfg, start - dt.timedelta(hours=1)) == 2
    ap.status = "pending"
    assert E.reminder_due(ap, cfg, start - dt.timedelta(hours=1)) == 0       # فقط نوبتِ قطعی
