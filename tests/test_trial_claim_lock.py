"""سقفِ «تست رایگانِ دکمه»: تکرار/هم‌زمانی نباید دو تست بدهد."""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", maker)

    async def not_exempt(*a, **k):
        return False
    monkeypatch.setattr(tenant, "_trial_exempt", not_exempt)
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:T", username="x", field="vpn_sales", plan="pro", status="active")
        s.add(b); await s.flush()
        mb = models.MainButton(bot_id=b.id, row=0, col=0, label="تست", action="vpn_trial")
        s.add(mb); await s.flush()
        ids = (b.id, mb.id)
        await s.commit()
    yield ids
    await engine.dispose()


def _ctx(mb, limit=1, gen=0):
    return {"mb_id": mb, "limit": limit, "gen": gen}


@pytest.mark.asyncio
async def test_second_claim_is_refused(env):
    bid, mb = env
    assert (await tenant._reserve_button_trial_claim(bid, 5, _ctx(mb)))[0]
    ok, err = await tenant._reserve_button_trial_claim(bid, 5, _ctx(mb))
    assert not ok and "سقف" in err


@pytest.mark.asyncio
async def test_simultaneous_first_taps_give_exactly_one_trial(env):
    bid, mb = env
    res = await asyncio.gather(*[tenant._reserve_button_trial_claim(bid, 9, _ctx(mb)) for _ in range(5)],
                               return_exceptions=True)
    assert not [r for r in res if isinstance(r, Exception)]       # IntegrityError به خطای کاربر تبدیل نمی‌شود
    assert sum(1 for r in res if r[0]) == 1


@pytest.mark.asyncio
async def test_reset_for_everyone_allows_one_more(env):
    bid, mb = env
    assert (await tenant._reserve_button_trial_claim(bid, 5, _ctx(mb, gen=0)))[0]
    assert not (await tenant._reserve_button_trial_claim(bid, 5, _ctx(mb, gen=0)))[0]
    assert (await tenant._reserve_button_trial_claim(bid, 5, _ctx(mb, gen=1)))[0]
    assert not (await tenant._reserve_button_trial_claim(bid, 5, _ctx(mb, gen=1)))[0]
