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


async def test_move_plan_and_category(monkeypatch, tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    S = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(va, "Session", S)
    from sqlalchemy import select
    from tenant.bots.vpn.vpn_user import plans_ordered, bucket_order
    async with S() as s:
        # حجم‌ها طوری که ترتیبِ خودکار (حجم ↑) a,b,c,d باشد
        for i, (t, cat) in enumerate((("a", "x"), ("b", "x"), ("c", "y"), ("d", "x")), 1):
            s.add(VpnPlan(bot_id=1, panel_key="k", title=t, volume_gb=i, days=1, price=1, category=cat))
        await s.commit()

    async def titles():
        async with S() as s:
            return [p.title for p in plans_ordered((await s.scalars(select(VpnPlan))).all())]
    async def ids():
        async with S() as s:
            return {p.title: p.id for p in (await s.scalars(select(VpnPlan))).all()}
    assert await titles() == ["a", "b", "c", "d"]            # حالتِ خودکار
    I = await ids()
    assert await va.move_plan(1, "k", I["b"], "up") is True
    assert await titles() == ["b", "a", "c", "d"]
    assert await va.move_plan(1, "k", I["b"], "up") is False  # بالاترین
    assert await va.move_plan(1, "k", I["d"], "up") is True   # از روی c (دستهٔ دیگر) می‌پرد
    assert (await titles()).index("d") < (await titles()).index("a")
    # دسته‌ها: ترتیبِ فعلی x (اولین) سپس y
    assert await va.panel_category_order(1, "k") == ["x", "y"]
    assert await va.move_plan_category(1, "k", "y", "up") is True
    assert await va.panel_category_order(1, "k") == ["y", "x"]
    assert await va.move_plan_category(1, "k", "y", "up") is False
    async with S() as s:
        plans = (await s.scalars(select(VpnPlan))).all()
    assert bucket_order(plans, {"x", "y", "انبار"}) == ["y", "x", "انبار"]
    # پلنِ تازه (sort_order=0) آخر می‌نشیند، نه اول
    async with S() as s:
        s.add(VpnPlan(bot_id=1, panel_key="k", title="new", volume_gb=0, days=1, price=1, category="x"))
        await s.commit()
    assert (await titles())[-1] == "new"


async def test_move_category(monkeypatch, tmp_path):
    import tenant.shared.miniapp_admin as ma
    from models import MiniAppCategory
    from sqlalchemy import select
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/c.db")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    S = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(ma, "Session", S)
    async with S() as s:
        for t in "abc":
            s.add(MiniAppCategory(bot_id=1, title=t))
        await s.commit()

    async def order():
        async with S() as s:
            return [c.title for c in (await s.scalars(select(MiniAppCategory).order_by(MiniAppCategory.sort_order, MiniAppCategory.id))).all()]
    async with S() as s:
        ids = {c.title: c.id for c in (await s.scalars(select(MiniAppCategory))).all()}
    assert await ma.move_category(1, ids["c"], "up") is True
    assert await order() == ["a", "c", "b"]
    assert await ma.move_category(1, ids["a"], "up") is False
    assert await ma.move_category(1, ids["b"], "dn") is False
    assert await ma.move_category(1, ids["a"], "dn") is True
    assert await order() == ["c", "a", "b"]
