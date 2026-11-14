import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_vip_addall as VA

NOW = dt.datetime(2026, 11, 9, 12, 0, 0)


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    d = dt.timedelta
    async with mk() as s:
        s.add_all([
            models.HostedApp(id=1, owner_id=11, name="paidok", runtime="node", status="running", paid_until=NOW + d(days=5)),
            models.HostedApp(id=2, owner_id=12, name="expired", runtime="node", status="suspended", paid_until=NOW - d(days=1)),
            models.HostedApp(id=3, owner_id=13, name="freeapp", runtime="node", status="running", paid_until=None),
            models.HostedApp(id=4, owner_id=14, name="adminoff", runtime="node", status="suspended", paid_until=NOW + d(days=3)),
            models.HostedApp(id=5, owner_id=15, name="withmini", runtime="node", status="running", paid_until=NOW + d(days=1),
                             miniapp_sub="m1a2b3c4d", miniapp_until=NOW + d(days=10)),
            models.HostedApp(id=6, owner_id=16, name="archived", runtime="node", status="archived", paid_until=NOW + d(days=1)),
        ])
        await s.commit()
    monkeypatch.setattr(VA, "Session", mk)
    yield mk
    await engine.dispose()


@pytest.mark.asyncio
async def test_plan_counts(maker):
    p = await VA.plan(NOW)
    assert p == {"servers": 4, "resume": 1, "minis": 1, "free": 1}


@pytest.mark.asyncio
async def test_apply_extends_resumes_and_skips_free_and_archived(maker):
    r = await VA.apply(NOW, 2)
    assert r["servers"] == 4 and r["resume"] == 1 and r["minis"] == 1 and sorted(r["owners"]) == [11, 12, 14, 15]
    async with maker() as s:
        g = lambda i: s.get(models.HostedApp, i)
        a1, a2, a3, a4, a5, a6 = [await g(i) for i in range(1, 7)]
    d = dt.timedelta
    assert a1.paid_until == NOW + d(days=7) and a1.status == "running"
    assert a2.paid_until == NOW + d(days=2) and a2.status == "stopped"          # منقضی ⇒ از «همین حالا» + روز، اجازهٔ روشن‌شدن
    assert a3.paid_until is None and a3.status == "running"                      # تستِ رایگان دست‌نخورده
    assert a4.paid_until == NOW + d(days=5) and a4.status == "suspended"        # خاموش‌شدهٔ دستیِ مدیر فعال نمی‌شود
    assert a5.paid_until == NOW + d(days=3) and a5.miniapp_until == NOW + d(days=12)
    assert a6.paid_until == NOW + d(days=1) and a6.status == "archived"          # بایگانی دست‌نخورده


@pytest.mark.asyncio
async def test_double_run_guard(maker):
    assert not await VA._recent_run(NOW)
    await VA.apply(NOW, 1)
    assert await VA._recent_run(NOW + dt.timedelta(minutes=3))
    assert not await VA._recent_run(NOW + dt.timedelta(minutes=30))


def test_parse_days_and_button_in_vip_screen():
    assert VA._parse_days("۲") == 2 and VA._parse_days("+3 روز") == 3
    assert VA._parse_days("0") is None and VA._parse_days("999") is None and VA._parse_days("abc") is None
    assert 'callback_data="vip:addall"' in open("platform_bot.py", encoding="utf8").read()
