import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import hosted_miniapp as HM

TOKEN = "123456:" + "A" * 35


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    async with mk() as s:
        s.add(models.HostedApp(id=1, owner_id=7, name="alpha", runtime="node"))
        s.add(models.HostedApp(id=2, owner_id=8, name="beta", runtime="python"))
        s.add(models.HostedApp(id=3, owner_id=9, name="gamma", runtime="python"))
        s.add(models.HostedAppEnv(app_id=1, key="BOT_TOKEN", value=TOKEN))
        s.add(models.HostedAppEnv(app_id=2, key="NAME", value="x"))
        await s.commit()
    monkeypatch.setattr(config, "Session", mk)
    yield mk
    await engine.dispose()


@pytest.mark.asyncio
async def test_fill_bot_usernames_stores_username_from_getme(maker):
    calls = []

    async def call(token, method, payload=None, **k):
        calls.append((token, method))
        return {"ok": True, "result": {"username": "Combat_X2_bot"}}
    n = await HM.fill_bot_usernames(call=call)
    assert n == 1 and calls == [(TOKEN, "getMe")]
    async with maker() as s:
        a = await s.get(models.HostedApp, 1)
        b = await s.get(models.HostedApp, 2)
    assert a.bot_username == "Combat_X2_bot" and b.bot_username is None


@pytest.mark.asyncio
async def test_fill_bot_usernames_ignores_bad_token_and_errors(maker):
    async def call(token, method, payload=None, **k):
        return {"ok": False, "description": "Unauthorized"}
    assert await HM.fill_bot_usernames(call=call) == 0

    async def boom(token, method, payload=None, **k):
        raise RuntimeError("net")
    assert await HM.fill_bot_usernames(call=boom) == 0
