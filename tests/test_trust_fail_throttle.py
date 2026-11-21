"""PATCH_TRUST_FAIL_THROTTLE_V1 — خطایِ کانالِ اعتمادسازی با هر خرید تکرار نمی‌شود."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import trust_report as tr


@pytest_asyncio.fixture
async def db(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    import log_throttle
    monkeypatch.setattr(log_throttle, "_PATH", str(tmp_path / "t.json"))
    monkeypatch.setattr(log_throttle, "_cache", None)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=680, owner_id=77, token="1:T", username="x", field="vpn_sales",
                         plan="pro", status="active"))
        s.add(models.Setting(bot_id=680, key="trust_channel_id", value="-1001"))
        await s.commit()
    yield maker
    await engine.dispose()


class _Bot:
    def __init__(self):
        self.dms = []

    async def send_message(self, chat_id, text, **kw):
        if chat_id == -1001:
            raise RuntimeError("Telegram server says - Bad Request: CHAT_RESTRICTED")
        self.dms.append((chat_id, text))


@pytest.mark.asyncio
async def test_restricted_channel_alerts_owner_once(db):
    b = _Bot()
    for _ in range(3):
        await tr.send_trust_channel_post(b, 680, buyer_telegram_id=5, product_name="p", amount=1000)
    assert len(b.dms) == 1 and b.dms[0][0] == 77
    assert "محدود شده" in b.dms[0][1]
