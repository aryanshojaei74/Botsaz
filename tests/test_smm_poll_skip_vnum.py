"""PATCH_SMM_POLL_SKIP_VNUM_V1 — پیگیرِ وضعیتِ SMM نباید سفارشِ شمارهٔ مجازی را از پنلِ SMM بپرسد."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (smm, config, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="b", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.Setting(bot_id=5, key="smm_api_url", value="https://smm.example/api"))
        s.add(models.Setting(bot_id=5, key="smm_api_key", value="k"))
        for oid, sid, pid in ((1, "ozv:12", "1000142529"), (2, "sp:9", "777"), (3, "1234", "555")):
            s.add(models.SmmOrder(id=oid, bot_id=5, user_telegram_id=9, service_id=sid,
                                  service_name="x", link="l", quantity=1, price_toman=1000,
                                  status="sent", panel_order_id=pid))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_poller_skips_virtual_number_orders(db, monkeypatch):
    asked = []

    async def get_order_status(url, key, panel_oid):
        asked.append(panel_oid)
        return {"status": "Completed"}
    monkeypatch.setattr(smm.smm_api, "get_order_status", get_order_status)

    async def no_notify(*a, **k):
        return None
    monkeypatch.setattr(smm, "_notify_user", no_notify)
    from tenant.bots.virtual_service import smm_refill

    async def no_refill():
        return {}
    monkeypatch.setattr(smm_refill, "poll_once", no_refill)

    await smm.smm_status_poll_once()
    assert asked == ["555"]
    async with db() as s:
        assert (await s.get(models.SmmOrder, 1)).status == "sent"   # شمارهٔ OzVinoo دست‌نخورده
        assert (await s.get(models.SmmOrder, 2)).status == "sent"
        assert (await s.get(models.SmmOrder, 3)).status == "completed"
