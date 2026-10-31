"""PATCH_BOT_CHANGE_LOG_V1 — هر تغییرِ مهمِ ربات با علت و عامل در bot_change_log ثبت می‌شود."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bot_change_log as BL
import models


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    # فایلِ تست «کدِ پروژه» حساب شود
    monkeypatch.setattr(BL, "_is_mine", lambda fn: fn.endswith("test_bot_change_log.py"))
    yield m
    await engine.dispose()


async def _logs(m):
    async with m() as s:
        return (await s.scalars(select(models.BotChangeLog).order_by(models.BotChangeLog.id))).all()


async def adm_bot_field_apply(cb, m):                   # نامِ واقعیِ هندلر در REASONS است
    async with m() as s:
        r = await s.get(models.Bot, 1)
        r.field = "vpn_sales"
        await s.commit()


@pytest.mark.asyncio
async def test_insert_then_field_change_logged_with_reason_and_actor(maker):
    async with maker() as s:
        s.add(models.Bot(id=1, owner_id=5, token="1:AAA", username="b", field="partner", plan="partner", status="active"))
        await s.commit()
    cb = SimpleNamespace(from_user=SimpleNamespace(id=777))
    await adm_bot_field_apply(cb, maker)
    logs = await _logs(maker)
    created = [l for l in logs if l.reason == "ساختِ ربات"]
    assert {l.col for l in created} >= {"field", "plan", "owner_id", "token"}
    ch = [l for l in logs if l.col == "field" and l.old == "partner"]
    assert len(ch) == 1 and ch[0].new == "vpn_sales"
    assert ch[0].reason == "تغییر نوع ربات توسطِ ادمینِ پلتفرم" and ch[0].actor == 777


@pytest.mark.asyncio
async def test_token_value_is_never_stored(maker):
    async with maker() as s:
        s.add(models.Bot(id=2, owner_id=5, token="2:SECRETTOKEN", username="b", field="vpn_sales"))
        await s.commit()
    async with maker() as s:
        r = await s.get(models.Bot, 2)
        r.token = "2:OTHERSECRET"
        await s.commit()
    for l in await _logs(maker):
        assert "SECRET" not in (l.old or "") + (l.new or "")


@pytest.mark.asyncio
async def test_unchanged_columns_and_activity_not_logged(maker):
    async with maker() as s:
        s.add(models.Bot(id=3, owner_id=5, token="3:T", username="b", field="vpn_sales"))
        await s.commit()
    n0 = len(await _logs(maker))
    async with maker() as s:
        r = await s.get(models.Bot, 3)
        r.field = "vpn_sales"                         # بدونِ تغییرِ واقعی
        r.last_activity_at = None
        await s.commit()
    assert len(await _logs(maker)) == n0
