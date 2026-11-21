"""PATCH_TOKEN_SWAP_NONFK_V1 — تعویضِ توکن نباید افزونهٔ پولیِ «بازی‌هایِ گروهی» و بازی‌هایِ باز را گم کند."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bot_addons
import config
import models


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", m)
    monkeypatch.setattr(bot_addons.config, "Session", m)
    bot_addons._cache.clear()
    async with m() as s:
        for i, tok in ((1, "1:OLD"), (2, "2:NEW")):
            s.add(models.Bot(id=i, owner_id=9, token=tok, username=f"b{i}", field="virtual_service", plan="pro", status="active"))
        await s.commit()
    yield m
    await engine.dispose()


@pytest.mark.asyncio
async def test_addon_and_open_wagers_follow_the_new_bot(maker):
    async with maker() as s:
        s.add(models.BotAddon(bot_id=1, addon="wager_games", amount=50000, paid_by=9))
        s.add(models.PlatformWager(bot_id=1, chat_id=-100, game="speed", creator_id=5, amount=1000, status="pending"))
        s.add(models.PlatformWager(bot_id=0, chat_id=-100, game="speed", creator_id=5, amount=1000, status="pending"))   # رباتِ اصلی دست‌نخورده
        s.add(models.BotAuditLog(bot_id=1, username="b1", action="delete"))
        await s.commit()
    assert await bot_addons.addon_active(2) is False
    await config.redirect_all_bot_id_references(1, 2)
    async with maker() as s:
        assert (await s.scalar(select(models.BotAddon.bot_id).where(models.BotAddon.addon == "wager_games"))) == 2
        assert sorted((await s.scalars(select(models.PlatformWager.bot_id))).all()) == [0, 2]
        assert (await s.scalar(select(models.BotAuditLog.bot_id))) == 1                  # سابقهٔ رباتِ قدیم همان‌جا می‌ماند
    assert await bot_addons.addon_active(2) is True and await bot_addons.addon_active(1) is False


@pytest.mark.asyncio
async def test_duplicate_addon_on_new_bot_does_not_break_the_swap(maker):
    async with maker() as s:
        s.add(models.BotAddon(bot_id=1, addon="wager_games", amount=50000, paid_by=9))
        s.add(models.BotAddon(bot_id=2, addon="wager_games", amount=50000, paid_by=9))
        s.add(models.BotAddon(bot_id=1, addon="other", amount=1, paid_by=9))
        await s.commit()
    await config.redirect_all_bot_id_references(1, 2)
    async with maker() as s:
        rows = (await s.execute(select(models.BotAddon.bot_id, models.BotAddon.addon))).all()
    assert sorted(rows) == [(2, "other"), (2, "wager_games")]
