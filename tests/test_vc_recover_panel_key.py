"""PATCH_VC_RECOVER_PANEL_KEY_V1 — «نشستِ خرید منقضی شده» وقتی کلیدِ پنل از state گم شده."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.vpn import vpn_user as VU


class FakeState:
    def __init__(self, **d):
        self.d = dict(d)

    async def get_data(self):
        return dict(self.d)

    async def update_data(self, **kw):
        self.d.update(kw)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (VU, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    enabled = {"pasargad": True}

    async def fake_vc(bot_id, key, panel_key=""):
        return "1" if (key == "vc_enabled" and enabled.get(panel_key)) else "0"
    monkeypatch.setattr(VU, "_vc", fake_vc)
    async with maker() as s:
        s.add(models.Bot(id=4, owner_id=1, token="4:T", username="w", field="vpn_sales",
                         plan="pro", status="active"))
        s.add(models.VpnPanel(bot_id=4, panel_key="pasargad", base_url="http://a", username="u", password="p", enabled=True))
        s.add(models.VpnPanel(bot_id=4, panel_key="sanai", base_url="http://b", username="u", password="p", enabled=True))
        await s.commit()
    yield enabled
    await engine.dispose()


@pytest.mark.asyncio
async def test_keeps_existing_key(db):
    st = FakeState(vc_panel_key="sanai")
    assert await VU._vc_recover_panel_key(st, 4) == "sanai"


@pytest.mark.asyncio
async def test_recovers_from_last_plans_page(db):
    st = FakeState(vc_gb=25, vbuy_panel_key="sanai")
    assert await VU._vc_recover_panel_key(st, 4) == "sanai"
    assert st.d["vc_panel_key"] == "sanai" and st.d["vc_bot_id"] == 4


@pytest.mark.asyncio
async def test_recovers_single_vc_enabled_panel(db):
    st = FakeState(vc_gb=25)
    assert await VU._vc_recover_panel_key(st, 4) == "pasargad"      # فقط یک پنل فروشِ دلخواه دارد
    assert st.d["vc_panel_key"] == "pasargad"


@pytest.mark.asyncio
async def test_ambiguous_stays_empty(db):
    db["sanai"] = True                                              # دو پنل → مبهم
    st = FakeState(vc_gb=25)
    assert await VU._vc_recover_panel_key(st, 4) == ""
    assert "vc_panel_key" not in st.d
