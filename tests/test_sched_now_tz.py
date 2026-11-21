"""PATCH_SCHED_NOW_EXPLICIT_TZ_V1 — ساعتِ تخفیفِ زمان‌دار به تهران است، حتی وقتی ساعتِ سیستم UTC باشد."""
import datetime
import os
import time
from zoneinfo import ZoneInfo

import pytest

import platform_bot as PB


@pytest.fixture
def system_utc():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_sched_now_is_tehran_even_if_system_clock_is_utc(system_utc):
    tehran = datetime.datetime.now(ZoneInfo("Asia/Tehran")).replace(tzinfo=None)
    utc = datetime.datetime.now()
    got = PB._sched_now()
    assert abs((got - tehran).total_seconds()) < 5
    assert abs((got - utc).total_seconds() - 3.5 * 3600) < 5 or abs((got - utc).total_seconds() - 4.5 * 3600) < 5


def test_discount_window_uses_tehran_clock(system_utc, monkeypatch):
    now = PB._sched_now()
    monkeypatch.setitem(PB._SCHED_DISC, "pro", {"pct": 20, "from": now - datetime.timedelta(hours=1),
                                                "to": now + datetime.timedelta(hours=1)})
    assert PB.sched_disc_pct("pro") == 20
    # پنجره‌ای که به وقتِ تهران هنوز شروع نشده (۱ ساعت بعد) نباید فعال باشد، هرچند به وقتِ UTC فعال به نظر می‌رسد
    monkeypatch.setitem(PB._SCHED_DISC, "pro", {"pct": 20, "from": now + datetime.timedelta(hours=1), "to": None})
    assert PB.sched_disc_pct("pro") == 0
