"""PATCH_REF_CAP_V1 — سقفِ پاداشِ دعوت: حداکثر تعدادِ دعوتِ پاداش‌دار و حداکثر مجموعِ پاداش (۰ = بدونِ سقف)."""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.shared import extras
from tests.test_referral_percent import _Bot


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", mk)
    monkeypatch.setattr(extras, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=3, owner_id=1, token="3:T", username="b", field="virtual_service", plan="pro", status="active"))
        inv = models.BotUser(bot_id=3, telegram_id=100, balance=Decimal(0))
        s.add(inv)
        await s.flush()
        s.add(models.ReferralLink(bot_id=3, user_id=inv.id, code="abc", reward_per_join=0))
        for k, v in (("referral_reward", "1000"), ("referral_reward_mode", "join"), ("referral_reward_type", "fixed")):
            s.add(models.Setting(bot_id=3, key=k, value=v))
        for i in range(5):
            s.add(models.BotUser(bot_id=3, telegram_id=200 + i, balance=Decimal(0)))
        await s.commit()
    yield mk
    await eng.dispose()


async def _join_all(mk, n=5):
    bot = _Bot()
    for i in range(n):
        async with mk() as s:
            u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 200 + i))
        await extras.handle_referral_join(3, u, "abc", bot)
    async with mk() as s:
        bal = float(await s.scalar(select(models.BotUser.balance).where(models.BotUser.telegram_id == 100)))
    return bal, bot


async def _set(mk, k, v):
    async with mk() as s:
        s.add(models.Setting(bot_id=3, key=k, value=v))
        await s.commit()


@pytest.mark.asyncio
async def test_no_cap_pays_everyone(env):
    bal, _ = await _join_all(env)
    assert bal == 5000


@pytest.mark.asyncio
async def test_invite_count_cap(env):
    await _set(env, "referral_max_invites", "3")
    bal, bot = await _join_all(env)
    assert bal == 3000
    assert any("سقف" in t for _c, t in bot.sent)          # دعوت‌کننده خبردار می‌شود


@pytest.mark.asyncio
async def test_total_amount_cap_clips_last_reward(env):
    await _set(env, "referral_max_total", "2500")
    bal, _ = await _join_all(env)
    assert bal == 2500                                      # ۱۰۰۰+۱۰۰۰+۵۰۰
