"""PATCH_PSEL_FREE_TRIAL_V1 — دسترسیِ چندروزهٔ «پنل‌فروشی (تست)» یعنی ساختِ رایگانِ پلنِ حرفه‌ای، نه پلنِ پولی."""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import plan_trials
import platform_panel_seller as PPS
from tenant.bots.panel_seller import gate as GT


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, GT, plan_trials):
        monkeypatch.setattr(mod, "Session", m)
    monkeypatch.setattr(plan_trials, "ensure_trials_table", _noop)
    yield m
    await engine.dispose()


async def _noop(*a, **k):
    return None


@pytest.mark.asyncio
async def test_remaining_days_rounds_up_and_ignores_permanent(env):
    now = datetime.utcnow()
    await GT.grant(1, 7, now=now)
    assert await GT.remaining_days(1, now=now) == 7
    assert await GT.remaining_days(1, now=now + timedelta(days=6, hours=2)) == 1       # کمتر از یک روز ⇒ ۱
    assert await GT.remaining_days(1, now=now + timedelta(days=8)) is None            # منقضی
    await GT.grant(2, 0)
    assert await GT.remaining_days(2) is None                                          # دائمی ⇒ تستِ رایگان نمی‌سازد
    assert await GT.remaining_days(3) is None                                          # بدونِ دسترسی


@pytest.mark.asyncio
async def test_seven_day_grant_becomes_free_pro_trial(env):
    await GT.grant(55, 7)
    assert await plan_trials.active_trial_for(55, "pro") is None                       # قبل از ensure: «بخر» می‌گفت
    assert await PPS.ensure_trial(55, "Ali") is True
    tr = await plan_trials.active_trial_for(55, "pro")
    assert tr and tr["days"] == 7
    assert await PPS.ensure_trial(55, "Ali") is True                                   # idempotent
    assert (await plan_trials.active_trial_for(55, "pro"))["id"] == tr["id"]


@pytest.mark.asyncio
async def test_permanent_or_missing_access_gets_no_free_trial(env):
    await GT.grant(66, 0)
    assert await PPS.ensure_trial(66) is False and await plan_trials.active_trial_for(66, "pro") is None
    assert await PPS.ensure_trial(77) is False
