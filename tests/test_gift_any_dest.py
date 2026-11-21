"""MoBiN: گیرندهٔ گیفت لازم نیست عضوِ ربات باشد (کانال/سورپرایز) ⇒ جانشینِ منفی + مسیرِ دستی/پشتیبانی."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
from tenant.bots.virtual_service import smm as S

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(S, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="b", field="virtual_service", plan="pro", status="active"))
        await s.commit()
    yield maker
    await eng.dispose()


def test_username_validation():
    assert S._valid_tg_username("mychannel") and S._valid_tg_username("My_Chan_1")
    assert not S._valid_tg_username("abc") and not S._valid_tg_username("1abcdef") and not S._valid_tg_username("a b cde")


async def test_placeholder_is_negative_stable_blocked_and_labelled(db):
    a = await S._ensure_placeholder_dest(7, "SomeChannel")
    b = await S._ensure_placeholder_dest(7, "somechannel")
    assert a < 0 and a == b                                   # هر بار همان آیدی (بدونِ تکرارِ ردیف)
    async with db() as s:
        rows = (await s.scalars(select(models.BotUser).where(models.BotUser.bot_id == 7))).all()
    assert len(rows) == 1 and rows[0].is_blocked is True and rows[0].username.lower() == "somechannel"
    label = await S._resolve_dest_label(7, a)
    assert "@" in label and "-" not in label.split("@")[0]    # آیدیِ ساختگی نشان داده نمی‌شود
