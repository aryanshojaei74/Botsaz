"""PATCH_VPN_CATEGORY_MANAGER_V1 — حذفِ دسته‌بندیِ پلن‌هایِ VPN (پلن‌ها می‌مانند، بدون دسته می‌شوند)."""
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.vpn import vpn_admin as V


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for m in list(sys.modules.values()):
        if getattr(m, "Session", None) is orig:
            monkeypatch.setattr(m, "Session", mk, raising=False)
    monkeypatch.setattr(config, "Session", mk)
    yield mk
    await eng.dispose()


class Msg:
    def __init__(self):
        self.out = []

    async def edit_text(self, text, **k):
        self.out.append((text, k.get("reply_markup")))

    answer = edit_text


class Cb:
    def __init__(self, data):
        self.data, self.message, self.from_user, self.alerts = data, Msg(), SimpleNamespace(id=5), []

    async def answer(self, text="", **k):
        self.alerts.append(text)


@pytest.mark.asyncio
async def test_manager_lists_counts_and_delete_clears_only_that_category(db, monkeypatch):
    async with db() as s:
        b = models.Bot(owner_id=5, token="t:1", field="vpn_sales", plan="pro")
        s.add(b)
        await s.flush()
        for i, c in enumerate(["تانل جدید", "تانل جدید", "گیمینگ", None]):
            s.add(models.VpnPlan(bot_id=b.id, panel_key="sanai", title=f"p{i}", volume_gb=10, days=30,
                                 price=1000, category=c))
        await s.commit()
        row = b
        pid = (await s.scalars(select(models.VpnPlan.id).limit(1))).first()

    async def _pg(cb, bot, plan_id):
        async with db() as s:
            return row, await s.get(models.VpnPlan, plan_id)
    monkeypatch.setattr(V, "_plan_get", _pg)
    cats = await V._plan_cats_with_counts(row.id)
    assert cats == [("تانل جدید", 2), ("گیمینگ", 1)]
    cb = Cb(f"tvpn:catdel:sanai:{pid}:i0")
    await V.vpn_cat_delete_ask(cb, None)
    assert "2 پلن" in cb.message.out[-1][0] or "۲" in cb.message.out[-1][0]
    go = Cb(f"tvpn:catdelgo:sanai:{pid}:i0")
    await V.vpn_cat_delete_go(go, None)
    assert "حذف شد" in go.message.out[-1][0]
    async with db() as s:
        rows = (await s.scalars(select(models.VpnPlan).order_by(models.VpnPlan.id))).all()
    assert len(rows) == 4                                         # هیچ پلنی حذف نشد
    assert [r.category for r in rows] == [None, None, "گیمینگ", None]
