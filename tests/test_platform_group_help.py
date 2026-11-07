"""PATCH_PLATFORM_GROUP_HELP_V1 — «راهنما گروه» در رباتِ اصلی."""
import sys

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_bot as pb
import platform_group_help as PH

N = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for m in list(sys.modules.values()):
        if getattr(m, "Session", None) is orig:
            monkeypatch.setattr(m, "Session", mk, raising=False)
    monkeypatch.setattr(config, "Session", mk)
    yield mk
    await eng.dispose()


def test_trigger():
    for t in ("راهنما گروه", "راهنمای گروه", "📖 راهنما گروه", "راهنما‌گروه"):
        assert PH._TRIGGER_RE.match(t), t
    for t in ("راهنما", "راهنما گروه چیه"):
        assert not PH._TRIGGER_RE.match(t), t


@pytest.mark.asyncio
async def test_sections_follow_settings(db):
    empty = (await PH.build_platform_group_help(-1, "BotSazBot")).translate(N)
    assert "هیچ قابلیتی" in empty or "بازی‌هایِ رایگان" in empty
    assert "انتقال 5000" not in empty and "بازیِ دونفره" not in empty         # کیف پول/شرط خاموش ⇒ نیست
    await pb.set_pset("platform_group_wallet_enabled", "on")
    await pb.set_pset("platform_group_transfer_min", "2000")
    await pb.set_pset("platform_group_betting_enabled", "on")
    await pb.set_pset("platform_group_transfer_fee_enabled", "on")
    await pb.set_pset("platform_group_transfer_fee_pct", "2")
    async with db() as s:
        s.add(models.PlatformDiceGame(emoji="🎯", title="دارت", active=True, in_groups=True,
                                      prizes='{"6":8000}', tries_limit=2, tries_period="day"))
        await s.commit()
    t = (await PH.build_platform_group_help(-1, "BotSazBot")).translate(N)
    assert "@BotSazBot" in t and "انتقال 5000" in t and "2,000 تومان" in t and "کارمزد: <b>2٪" in t
    assert "بازیِ دونفره" in t and "بازی 10000" in t
    assert "دارت" in t and "2 بار در هر روز" in t and "8,000 تومان" in t
