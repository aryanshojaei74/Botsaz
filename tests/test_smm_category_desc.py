"""PATCH_SMM_CATEGORY_DESC_V1 — توضیحاتِ مشترکِ دسته وقتی پنل و خودِ سرویس توضیح ندارند."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.virtual_service import smm


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, smm):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="virtual_service", plan="pro", status="active"))
        await s.flush()
        s.add(models.SmmCategory(id=5, bot_id=9, platform="telegram", name="ممبر", description="سرعت عادی؛ <b>ضمانت</b> ۳۰ روز"))
        s.add(models.SmmCategory(id=6, bot_id=9, platform="telegram", name="بدون توضیح"))
        await s.commit()
    monkeypatch.setattr(smm, "_get_setting", _fake_setting({}))
    yield maker
    await engine.dispose()


def _fake_setting(store):
    async def f(bot_id, key, default=""):
        return store.get(key, default)
    return f


def _svc(**kw):
    base = dict(id=1, category_pk=5, description="", provider_desc="", platform_key="telegram", type_key="member",
                category="x", min_qty=10, max_qty=1000, provider_desc_fa=None, provider_desc_fa_hash=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_category_description_shown_and_escaped_when_service_has_none(db):
    out = await smm.build_description(9, _svc())
    assert "سرعت عادی" in out and "&lt;b&gt;ضمانت" in out and "<b>ضمانت" not in out


@pytest.mark.asyncio
async def test_service_own_description_wins_over_category(db):
    out = await smm.build_description(9, _svc(description="توضیحِ اختصاصیِ محصول"))
    assert "توضیحِ اختصاصیِ محصول" in out and "سرعت عادی" not in out
    out2 = await smm.build_description(9, _svc(provider_desc="Provider says hi", provider_desc_fa="ارائه‌دهنده سلام می‌گوید",
                                               provider_desc_fa_hash=smm_hash("Provider says hi")))
    assert "سرعت عادی" not in out2


def smm_hash(t):
    from tenant.shared.fa_translate import text_hash
    return text_hash(t)


@pytest.mark.asyncio
async def test_category_without_description_falls_back_to_generated_lines_and_off_mode(db, monkeypatch):
    out = await smm.build_description(9, _svc(category_pk=6))
    assert "بازهٔ سفارش" in out or "دسته" in out                     # خلاصهٔ ساختگیِ قبلی
    monkeypatch.setattr(smm, "_get_setting", _fake_setting({"smm_desc_mode": "off"}))
    assert await smm.build_description(9, _svc()) == ""
    monkeypatch.setattr(smm, "_get_setting", _fake_setting({"smm_desc_mode": "manual"}))
    assert "سرعت عادی" in await smm.build_description(9, _svc())
