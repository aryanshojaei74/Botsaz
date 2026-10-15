import time
import pytest
import tenant.bots.vpn.vpn_admin as va
from tenant.bots.vpn.vpn_user import _connection_status_line
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from models import Base, VpnPlan

pytestmark = pytest.mark.asyncio


def test_online_status():
    now_ms = int(time.time() * 1000)
    assert "آنلاین" in _connection_status_line({"last_online_ms": now_ms - 30_000}) and "🟢" in _connection_status_line({"last_online_ms": now_ms - 30_000})
    old = _connection_status_line({"last_online_ms": now_ms - 3_600_000})
    assert "آفلاین" in old and "🟢" not in old
    assert _connection_status_line({}) == ""


async def test_move_plan(monkeypatch, tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    S = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(va, "Session", S)
    async with S() as s:
        for t, cat in (("a", "x"), ("b", "x"), ("c", "y"), ("d", "x")):
            s.add(VpnPlan(bot_id=1, panel_key="k", title=t, volume_gb=1, days=1, price=1, category=cat))
        await s.commit()

    async def order():
        from sqlalchemy import select
        async with S() as s:
            return [p.title for p in (await s.scalars(select(VpnPlan).order_by(VpnPlan.sort_order, VpnPlan.id))).all()]
    assert await order() == ["a", "b", "c", "d"]
    ids = {}
    async with S() as s:
        from sqlalchemy import select
        for p in (await s.scalars(select(VpnPlan))).all():
            ids[p.title] = p.id
    assert await va.move_plan(1, "k", ids["b"], "up") is True
    assert await order() == ["b", "a", "c", "d"]
    assert await va.move_plan(1, "k", ids["b"], "up") is False      # بالاترین
    assert await va.move_plan(1, "k", ids["d"], "up") is True        # از روی c (دستهٔ دیگر) می‌پرد
    assert (await order()).index("d") < (await order()).index("a")
