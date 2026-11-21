"""PATCH_BACKUP_VPN_V1 — پنل‌ها و پلن‌هایِ VPN در بک‌آپ و بازگردانی به رباتِ جدید."""
import json

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
from tenant.shared import backup_restore as br


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(br, "Session", maker)
    async with maker() as s:
        for bid in (7, 8):
            s.add(models.Bot(id=bid, owner_id=1, token=f"{bid}:T", username=f"b{bid}",
                             field="vpn_sales", plan="pro", status="active"))
        s.add(models.VpnPanel(bot_id=7, panel_key="marzban", base_url="https://p.example",
                              username="u", password="pw", sub_base="https://s", inbound_ids="a,b",
                              enabled=True, default_limit_ip=2))
        s.add(models.VpnPlan(bot_id=7, panel_key="marzban", title="۳۰ گیگ", volume_gb=30, days=30,
                             price=150000, limit_ip=1, active=True, category="اقتصادی"))
        s.add(models.Setting(bot_id=7, key="welcome", value="سلام"))
        s.add(models.VpnPanel(bot_id=8, panel_key="sanai", base_url="https://old", username="x",
                              password="y"))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_vpn_panels_and_plans_roundtrip(db):
    data = await br._build_backup(7)
    data = json.loads(json.dumps(data, default=br._json_default))   # مثلِ فایلِ واقعی
    assert len(data["tables"]["vpn_panels"]) == 1 and len(data["tables"]["vpn_plans"]) == 1
    await br._restore_backup(8, data)
    async with db() as s:
        panels = (await s.scalars(select(models.VpnPanel).where(models.VpnPanel.bot_id == 8))).all()
        plans = (await s.scalars(select(models.VpnPlan).where(models.VpnPlan.bot_id == 8))).all()
        st = await s.scalar(select(models.Setting).where(models.Setting.bot_id == 8))
    assert [(p.panel_key, p.base_url, p.password, p.inbound_ids, p.enabled, p.default_limit_ip)
            for p in panels] == [("marzban", "https://p.example", "pw", "a,b", True, 2)]
    assert [(p.title, p.volume_gb, int(p.price), p.category) for p in plans] == [("۳۰ گیگ", 30, 150000, "اقتصادی")]
    assert st.value == "سلام"


@pytest.mark.asyncio
async def test_old_backup_without_vpn_keys_keeps_panels(db):
    data = await br._build_backup(7)
    data["tables"].pop("vpn_panels")
    data["tables"].pop("vpn_plans")
    await br._restore_backup(8, data)
    async with db() as s:
        keys = (await s.scalars(select(models.VpnPanel.panel_key).where(models.VpnPanel.bot_id == 8))).all()
    assert keys == ["sanai"]
