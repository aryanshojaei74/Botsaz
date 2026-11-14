"""PATCH_EXPIRED_DELETE_DAYS_V1 — تنظیمِ «حذفِ خودکارِ سرویسِ منقضی N روز بعد از انقضا»."""
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.vpn import svc_cleanup as SC


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
    tenant._SETTING_CACHE.clear()
    yield mk
    await eng.dispose()


def test_label_and_options():
    assert SC.EXPDEL_OPTIONS[0] == 0 and 2 in SC.EXPDEL_OPTIONS
    assert "پیش‌فرض" in SC.expdel_text(0) and SC.expdel_text(2) == "2 روز بعد از انقضا"


@pytest.mark.asyncio
async def test_cycle_goes_off_1_2_3_7_14_30_off(db, monkeypatch):
    async with db() as s:
        b = models.Bot(owner_id=5, token="t:1", field="vpn_sales", plan="pro")
        s.add(b)
        await s.commit()
        row = b
    from tenant.bots.vpn import vpn_admin as VA

    async def _o(cb, bot):
        return row
    monkeypatch.setattr(VA, "_owner", _o)

    async def _noop(cb, bot):
        return None
    monkeypatch.setattr(tenant, "tadm_sec_settings", _noop)

    class Cb:
        from_user = SimpleNamespace(id=5)
        async def answer(self, *a, **k): self.last = a[0] if a else ""
    seen = []
    for _ in range(8):
        c = Cb()
        await SC.expdel_cycle(c, None)
        tenant._SETTING_CACHE.clear()
        seen.append(await tenant.get_setting(row.id, "expired_delete_days", "0"))
    assert seen == ["1", "2", "3", "7", "14", "30", "0", "1"]
    assert "پیش‌فرض" in await SC.expdel_label(row.id) or "روز" in await SC.expdel_label(row.id)
