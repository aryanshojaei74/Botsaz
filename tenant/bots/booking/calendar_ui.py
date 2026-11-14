"""calendar_ui.py — تقویمِ ماهانهٔ شیشه‌ای برایِ انتخابِ روز (PATCH_BOOKING_V25).

خالص و بدونِ تلگرام/دیتابیس تا تست‌پذیر باشد. هفته از شنبه شروع می‌شود (مثلِ بقیهٔ ربات). تقویم شمسی یا میلادی.
"""
from __future__ import annotations

import datetime as _dt

JALALI_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
GREG_MONTHS = ["ژانویه", "فوریه", "مارس", "آوریل", "مه", "ژوئن", "ژوئیه", "اوت", "سپتامبر", "اکتبر", "نوامبر", "دسامبر"]
HEAD = ["ش", "ی", "د", "س", "چ", "پ", "ج"]          # شنبه … جمعه


def _wd(d: _dt.date) -> int:
    return (d.weekday() + 2) % 7                      # ۰=شنبه


def ym_of(d: _dt.date, cal: str) -> int:
    """شناسهٔ ماه = سال*۱۲ + (ماه-۱) در تقویمِ انتخابی."""
    if cal == "gregorian":
        return d.year * 12 + d.month - 1
    import jdatetime as jd
    j = jd.date.fromgregorian(date=d)
    return j.year * 12 + j.month - 1


def month_days(ym: int, cal: str) -> list[_dt.date]:
    """همهٔ روزهایِ آن ماه (به‌صورتِ date میلادی) به ترتیب."""
    y, m = divmod(ym, 12)
    m += 1
    out: list[_dt.date] = []
    if cal == "gregorian":
        d = _dt.date(y, m, 1)
        while d.month == m:
            out.append(d)
            d += _dt.timedelta(days=1)
        return out
    import jdatetime as jd
    for day in range(1, 32):
        try:
            out.append(jd.date(y, m, day).togregorian())
        except ValueError:
            break
    return out


def month_title(ym: int, cal: str) -> str:
    y, m = divmod(ym, 12)
    names = GREG_MONTHS if cal == "gregorian" else JALALI_MONTHS
    return f"{names[m]} {y}"


def day_number(d: _dt.date, cal: str) -> int:
    if cal == "gregorian":
        return d.day
    import jdatetime as jd
    return jd.date.fromgregorian(date=d).day


def build_month(open_dates: set[_dt.date], ym: int, cal: str) -> list[list[tuple[str, _dt.date | None]]]:
    """ردیف‌هایِ هفتگی: هر خانه (متن، تاریخِ قابلِ انتخاب|None). ردیفِ اول = سرستون‌ها."""
    days = month_days(ym, cal)
    rows: list[list[tuple[str, _dt.date | None]]] = [[(h, None) for h in HEAD]]
    week: list[tuple[str, _dt.date | None]] = [("·", None)] * _wd(days[0])
    for d in days:
        n = str(day_number(d, cal))
        week.append((n, d) if d in open_dates else (f"▫️", None))
        if len(week) == 7:
            rows.append(week)
            week = []
    if week:
        week += [("·", None)] * (7 - len(week))
        rows.append(week)
    return rows


def neighbours(open_dates: set[_dt.date], ym: int, cal: str) -> tuple[int | None, int | None]:
    """نزدیک‌ترین ماهِ قبل/بعدی که روزِ باز دارد (برای فعال‌بودنِ ◀️ ▶️)."""
    yms = sorted({ym_of(d, cal) for d in open_dates})
    prev = [x for x in yms if x < ym]
    nxt = [x for x in yms if x > ym]
    return (prev[-1] if prev else None, nxt[0] if nxt else None)
