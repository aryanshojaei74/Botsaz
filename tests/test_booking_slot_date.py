"""PATCH_BOOKING_SLOT_DATE_V1: تاریخِ نوبت = نزدیک‌ترین روزِ همان اسلات (نه همیشه «امروز»)."""
import datetime

import pytest

from tenant.bots.booking import handlers as H


@pytest.mark.parametrize("now_ir, wd_fa, time_str, expected", [
    # ۱۴۰۵/۰۷/۱۵ = چهارشنبه ۲۰۲۶-۱۰-۰۷ (چهارشنبه در WEEKDAY_FA ایندکسِ ۴ است)
    (datetime.datetime(2026, 10, 7, 12, 0), 4, "15:00", datetime.date(2026, 10, 7)),   # امروز، هنوز نگذشته
    (datetime.datetime(2026, 10, 7, 12, 0), 4, "10:00", datetime.date(2026, 10, 14)),  # امروز گذشته → هفتهٔ بعد
    (datetime.datetime(2026, 10, 7, 12, 0), 0, "10:00", datetime.date(2026, 10, 10)),  # شنبه
    (datetime.datetime(2026, 10, 7, 12, 0), 2, "09:00", datetime.date(2026, 10, 12)),  # دوشنبه
])
def test_next_slot_date(now_ir, wd_fa, time_str, expected):
    assert H._next_slot_date(wd_fa, time_str, now=now_ir) == expected
