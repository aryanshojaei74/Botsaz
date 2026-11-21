"""PATCH_SMM_SAME_ID_OTHER_PANEL_V1 — شناسهٔ سرویسِ یکسان در دو پنلِ مختلف باید هر دو قابلِ افزودن باشد."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant.bots.virtual_service.smm as SM


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        s.add(models.Bot(id=1, owner_id=1, token="1:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.SmmService(bot_id=1, service_id="223", name="A", panel_pk=None))
        s.add(models.SmmService(bot_id=1, service_id="223", name="B", panel_pk=7))   # دو پنل، شناسهٔ یکسان
        s.add(models.SmmService(bot_id=1, service_id="500", name="C", panel_pk=7))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_existing_ids_are_per_panel(db):
    async with db() as s:
        ex_default, same = await SM._existing_service_ids(s, 1, None)
        ex_p7, _ = await SM._existing_service_ids(s, 1, 7)
        ex_p9, _ = await SM._existing_service_ids(s, 1, 9)
    assert ex_default == {"223"}                 # پنلِ پیش‌فرض: فقط ۲۲۳ خودش
    assert ex_p7 == {"223", "500"}
    assert ex_p9 == set()                        # پنلِ سوم هیچ‌کدام را ندارد → هر دو قابلِ افزودن
    assert ex_default == same
