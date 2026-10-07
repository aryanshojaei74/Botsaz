"""PATCH_API_SVC_EXPORT_V1 — خروجیِ services در /api/v2: توضیحات، دسته با پلتفرم، بدونِ شمارهٔ مجازی."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import public_api as pa


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (pa, config, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    from tenant.bots.virtual_service import smm
    monkeypatch.setattr(smm, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service",
                         plan="pro", status="active", api_key="BOTKEY"))
        s.add(models.SmmCategory(id=1, bot_id=5, platform="telegram", name="ممبر کانال"))
        s.add(models.SmmCategory(id=2, bot_id=5, platform="rubika", name="ممبر کانال"))
        s.add(models.SmmService(id=10, bot_id=5, service_id="111", name="ممبر تلگرام", category_pk=1,
                                panel_rate=1000, sell_rate=2000, min_qty=10, max_qty=1000, is_active=True,
                                description="بدونِ ریزش، شروع فوری"))
        s.add(models.SmmService(id=11, bot_id=5, service_id="222", name="ممبر روبیکا", category_pk=2,
                                panel_rate=1000, sell_rate=2000, min_qty=10, max_qty=1000, is_active=True,
                                provider_desc="Refill 30 days"))
        s.add(models.SmmService(id=12, bot_id=5, service_id="ozv:tn1:US", name="🇺🇸 آمریکا",
                                panel_rate=90000, sell_rate=99000000, min_qty=1, max_qty=1, is_active=True,
                                type_key="virtual_number"))
        await s.commit()
    yield maker
    await engine.dispose()


class _Req:
    def __init__(self, q):
        self.query_params = q
        self.headers = {}

    async def form(self):
        return {}

    async def json(self):
        return {}


@pytest.mark.asyncio
async def test_services_export(db, monkeypatch):
    async def params(_r):
        return {"key": "BOTKEY", "action": "services"}
    monkeypatch.setattr(pa, "_v2_params", params)
    r = await pa.smm_v2(_Req({}))
    import json
    rows = {x["service"]: x for x in json.loads(r.body)}
    assert set(rows) == {10, 11}                                   # شمارهٔ مجازی بیرون
    assert rows[10]["desc"] == rows[10]["description"] == "بدونِ ریزش، شروع فوری"
    assert rows[11]["desc"] == "Refill 30 days"                    # توضیحِ ارائه‌دهنده وقتی ادمین چیزی ننوشته
    assert rows[10]["category"] == "تلگرام - ممبر کانال"
    assert rows[11]["category"] == "روبیکا - ممبر کانال"
