import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import models
from tenant.bots.vpn import vpn_admin as A


def test_orphans(monkeypatch, tmp_path):
    async def go():
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        async with eng.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        S = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(A, "Session", S)
        async with S() as s:
            s.add(models.VpnPanel(bot_id=1, panel_key="sanai", base_url="x", username="u", password="p"))
            for k in ("sanai", "marzban", "marzban", "wgres"):
                s.add(models.VpnPlan(bot_id=1, panel_key=k, title="t", volume_gb=1, days=1, price=1))
            s.add(models.VpnPlan(bot_id=2, panel_key="zzz", title="t", volume_gb=1, days=1, price=1))
            await s.commit()
        assert sorted(await A._orphan_plan_groups(1)) == [("marzban", 2), ("wgres", 1)]
    asyncio.run(go())
