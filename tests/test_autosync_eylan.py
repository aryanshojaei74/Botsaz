"""PATCH_AUTOSYNC_EYLAN_V1 — همگام‌سازیِ خودکار نباید اینباندِ (پروتکلِ) ایلان را با int() بترکاند یا بازنویسی کند."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant.shared.panel_clients.panel_autosync as PA


class FakeClient:
    def __init__(self, inbounds):
        self.inbounds, self.list_calls = inbounds, 0

    async def login(self):
        pass

    async def list_inbounds(self):
        self.list_calls += 1
        return self.inbounds

    async def close(self):
        pass


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, PA):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=1, owner_id=1, token="1:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.VpnPanel(id=7, bot_id=1, panel_key="eylan#2", base_url="https://x", username="u", password="p",
                              enabled=True, inbound_ids="openvpn,wireguard"))
        s.add(models.VpnPanel(id=8, bot_id=1, panel_key="sanai", base_url="https://y", username="u", password="p",
                              enabled=True))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_eylan_inbounds_are_not_touched(db, monkeypatch, caplog):
    fc = FakeClient([{"id": "openvpn"}, {"id": "wireguard"}, {"id": "l2tp"}])
    monkeypatch.setattr(PA, "make_panel_client", lambda p: fc)
    await PA.sync_panel_once(7)
    async with db() as s:
        assert (await s.get(models.VpnPanel, 7)).inbound_ids == "openvpn,wireguard"      # دست نخورد
    assert fc.list_calls == 0
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


@pytest.mark.asyncio
async def test_numeric_panels_still_sync_and_odd_ids_do_not_crash(db, monkeypatch, caplog):
    fc = FakeClient([{"id": 3}, {"id": "5"}, {"id": "weird"}])
    monkeypatch.setattr(PA, "make_panel_client", lambda p: fc)
    await PA.sync_panel_once(8)
    async with db() as s:
        assert (await s.get(models.VpnPanel, 8)).inbound_ids == "3,5,weird"
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
