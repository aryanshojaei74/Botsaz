"""PATCH_BOOKING_V25 — تقویمِ ماهانه + سیستمِ تاریخِ شمسی/میلادی."""
import datetime as dt

from tenant.bots.booking import calendar_ui as C
from tenant.bots.booking import engine as E


def test_jalali_month_grid_and_neighbours():
    d = dt.date(2026, 11, 5)                       # ۱۴ آبان ۱۴۰۵ (پنج‌شنبه)
    ym = C.ym_of(d, "jalali")
    assert C.month_title(ym, "jalali") == "آبان 1405"
    days = C.month_days(ym, "jalali")
    assert len(days) == 30 and days[0] == dt.date(2026, 10, 23)      # ۱ آبان
    open_set = {d, d + dt.timedelta(days=1), dt.date(2026, 12, 20)}
    grid = C.build_month(open_set, ym, "jalali")
    assert [h for h, _ in grid[0]] == C.HEAD and all(len(r) == 7 for r in grid)
    sel = [x for r in grid[1:] for _t, x in r if x]
    assert sel == [d, d + dt.timedelta(days=1)]
    first = grid[1]                                 # ۱ آبان = جمعه ⇒ ۶ خانهٔ خالی قبلش
    assert [t for t, _ in first[:6]] == ["·"] * 6 and first[6][0] == "▫️"
    prv, nxt = C.neighbours(open_set, ym, "jalali")
    assert prv is None and C.month_title(nxt, "jalali").startswith("آذر")


def test_gregorian_grid():
    d = dt.date(2026, 11, 5)
    ym = C.ym_of(d, "gregorian")
    assert C.month_title(ym, "gregorian") == "نوامبر 2026"
    grid = C.build_month({d}, ym, "gregorian")
    assert [t for r in grid[1:] for t, x in r if x] == ["5"]


def test_fa_date_follows_calendar_and_parse_both():
    d = dt.date(2026, 11, 5)
    tok = E.set_cal("gregorian")
    try:
        assert E.fa_date(d) == "2026/11/05"
        assert E.parse_fa_date("2026/11/05") == d
    finally:
        E._CAL.reset(tok)
    assert E.fa_date(d) == "1405/08/14"
    assert E.parse_fa_date("1405/08/14") == d
    assert E.parse_fa_date("2026/11/05") == d        # میلادی هم با سال ≥ ۱۷۰۰ پذیرفته می‌شود
