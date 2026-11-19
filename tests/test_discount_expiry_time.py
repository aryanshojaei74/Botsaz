"""PATCH_DISCOUNT_EXPIRY_TIME_V1: انقضای کد تخفیف علاوه بر تاریخ، ساعت و «از همین الان» را هم می‌پذیرد."""
import datetime as dt

import pytest

import tenant

P = tenant._parse_exp_jalali
IRAN = dt.timedelta(hours=3, minutes=30)


def _utc(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm) - IRAN


def test_date_only_is_local_midnight_like_before():
    g = tenant._jdt.date(1404, 10, 15).togregorian()
    assert P("1404-10-15") == _utc(g.year, g.month, g.day) == P("1404/10/15") == P("۱۴۰۴-۱۰-۱۵")


def test_date_with_time_is_exact_iran_time():
    g = tenant._jdt.date(1404, 10, 15).togregorian()
    assert P("1404-10-15 18:30") == _utc(g.year, g.month, g.day, 18, 30)
    assert P("1404/10/15  07:05") == _utc(g.year, g.month, g.day, 7, 5)
    assert P("۱۴۰۴-۱۰-۱۵ ۱۸:۳۰") == P("1404-10-15 18:30")
    assert P("1404-10-15t18:30") == P("1404-10-15 18:30")
    out = tenant._fmt_exp_jalali(P("1404-10-15 18:30"))
    assert out == "1404/10/15 18:30"                                   # نمایشِ برگشتی با ساعت


def test_relative_units_start_from_now():
    now = dt.datetime.utcnow()
    for text, secs in (("3 ساعت", 3 * 3600), ("2 روز", 2 * 86400), ("30 دقیقه", 1800), ("۳ ساعت", 3 * 3600),
                       ("3h", 3 * 3600), ("2d", 2 * 86400), ("30m", 1800), ("12 hours", 12 * 3600)):
        got = P(text)
        assert got is not None and abs((got - now).total_seconds() - secs) < 5, text


@pytest.mark.parametrize("bad", ["", "abc", "1404-13-01", "1404-10-32", "1404-10-15 25:00", "1404-10-15 18:60",
                                 "0 ساعت", "-3 ساعت", "3 ثانیه", "1404-10", "15-10-1404 18:30"])
def test_invalid_inputs_are_rejected(bad):
    assert P(bad) is None


def test_prompts_mention_the_new_formats():
    for needle in ("18:30", "ساعت", "روز"):
        assert needle in tenant._EXP_FORMAT_HELP
    src = open(tenant.__file__, encoding="utf-8").read()
    assert "YYYY-MM-DD</code>" not in src                              # راهنمایِ قدیمی (فقط تاریخ) در هیچ پرامپتی نمانده
