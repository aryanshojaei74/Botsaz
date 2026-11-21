"""PATCH_VNUM_CATEGORY_APPLY_V1 — «دسته‌بندی سرویس‌هایِ مستقل ← شمارهٔ مجازی» واقعاً کشورها را در دسته می‌گذارد."""
import json
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.virtual_service import smm, ozvinoo_vnum as OZ


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (tenant, smm, OZ):
        monkeypatch.setattr(mod, "Session", mk)
    tenant._SETTING_CACHE.clear()
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=9, token="1:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.SmmCategory(id=5, bot_id=1, platform="telegram", name="شماره مجازی", is_active=True))
        s.add(models.SmmCategory(id=6, bot_id=1, platform="telegram", name="دیگر", is_active=True))

        def svc(sid, name, **kw):
            return models.SmmService(bot_id=1, service_id=sid, name=name, panel_rate=0, sell_rate=1000, min_qty=1,
                                     max_qty=1, is_active=True, **kw)
        s.add_all([
            svc("man1", "📱 ایران", category=smm._VNUM_CATEGORY, is_manual=True),
            svc("sp:us:tg", "📱 آمریکا", category=smm._VNUM_CATEGORY),
            svc("ozv:1:DE", "آلمان", category="x", type_key="virtual_number"),
            svc("plain", "لایک", category="لایک"),                                   # سرویسِ SMMِ معمولی: دست‌نخورده
        ])
        await s.commit()
    row = NS(id=1, owner_id=9, token="1:T", plan="pro", status="active", field="virtual_service")

    async def owner(cb, bot):
        return row
    monkeypatch.setattr(smm, "_owner", owner)
    return mk


class _CB:
    def __init__(self, data):
        self.data = data
        self.alerts = []
        self.message = NS(edit_text=self._noop, answer=self._noop)

    async def _noop(self, *a, **k):
        return None

    async def answer(self, text="", **k):
        self.alerts.append(text)


async def _pks(mk):
    async with mk() as s:
        return {v.service_id: v.category_pk for v in (await s.scalars(select(models.SmmService))).all()}


@pytest.mark.asyncio
async def test_choosing_a_category_puts_all_vnum_countries_in_it_and_nothing_else(env):
    cb = _CB("smm:manualcat:set:vnum:5")
    await smm.smm_special_manual_category_set(cb, object())
    assert await _pks(env) == {"man1": 5, "sp:us:tg": 5, "ozv:1:DE": 5, "plain": None}
    assert "3 کشور" in cb.alerts[0]
    # تغییرِ دسته و «بدونِ دسته»
    await smm.smm_special_manual_category_set(_CB("smm:manualcat:set:vnum:6"), object())
    assert (await _pks(env))["man1"] == 6
    await smm.smm_special_manual_category_set(_CB("smm:manualcat:set:vnum:0"), object())
    assert set((await _pks(env)).values()) == {None}


@pytest.mark.asyncio
async def test_other_special_keys_do_not_touch_services(env):
    await smm.smm_special_manual_category_set(_CB("smm:manualcat:set:gift:5"), object())
    assert set((await _pks(env)).values()) == {None}


@pytest.mark.asyncio
async def test_new_countries_get_the_default_category(env):
    await smm._save_special_manual_category_map(1, {"vnum": 5})
    assert await smm._vnum_default_category_pk(1) == 5
    assert await OZ._oz_default_category_pk(1) == 5
    await smm._save_special_manual_category_map(1, {})
    assert await smm._vnum_default_category_pk(1) is None and await OZ._oz_default_category_pk(1) is None
