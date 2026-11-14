"""PATCH_GB_SOLD_STATS_V1 (پیشنهاد #213): آمار حجمِ فروخته‌شده (گیگ) در آمار زمانی و گزارش روزانه."""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant.shared.period_stats as PS
import tenant.shared.daily_report as DR

NOW = dt.datetime(2026, 10, 14, 7, 0)          # ۱۰:۳۰ ایران


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(PS, "Session", mk)
    monkeypatch.setattr(DR, "Session", mk)
    h = lambda **k: NOW - dt.timedelta(**k)
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=1, token="1:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=1, telegram_id=10, full_name="A"))
        for gb, cnt, st, age in [(50, 2, "paid", dict(minutes=30)),      # ۱۰۰ گیگ، امروز
                                 (10, 1, "paid", dict(hours=1)),          # ۱۰ گیگ، امروز
                                 (100, 1, "pending", dict(minutes=5)),    # پرداخت نشده ⇒ نه
                                 (0, 1, "paid", dict(minutes=5)),         # نامحدود ⇒ در جمعِ گیگ نه
                                 (30, 1, "paid", dict(days=3)),           # ۷ روزه
                                 (20, 1, "paid", dict(days=40))]:         # خارج از ماه
            s.add(models.Order(bot_id=1, user_id=1, volume_gb=gb, days=30, count=cnt, amount=1000, pay_method="wallet",
                               status=st, created_at=NOW - dt.timedelta(**age)))
        await s.commit()
    yield mk
    await eng.dispose()


@pytest.mark.asyncio
async def test_gb_report_by_period(db):
    rows = {l: (n, gb) for l, n, gb in await PS.gb_report(1, NOW)}
    assert rows["کل"] == (4, 100 + 10 + 30 + 20)
    assert rows["امروز (تا 10:30)"] == (2, 110)
    assert rows["۷ روزِ گذشته"] == (3, 140)
    assert rows["۱ ماهِ گذشته"] == (3, 140)


@pytest.mark.asyncio
async def test_period_text_shows_gb_block_only_for_bots_that_sold(db):
    t = await PS.build_text(1, NOW)
    assert "حجمِ فروخته‌شده" in t and "110" in t
    t2 = await PS.build_text(2, NOW)                 # رباتِ دیگر (بدونِ فروشِ VPN)
    assert "حجمِ فروخته‌شده" not in t2


@pytest.mark.asyncio
async def test_daily_report_has_gb_variable_and_default_line(db):
    m = await DR.metrics(1, NOW + dt.timedelta(hours=1))
    assert m["gb_sold"] == "110" and "gb_sold" in DR.VARS
    txt = DR.render("امروز {{gb_sold}} گیگ", m)
    assert txt == "امروز 110 گیگ"
