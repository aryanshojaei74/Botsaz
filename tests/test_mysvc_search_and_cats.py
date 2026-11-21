"""«مدیریتِ محصولات (سرویس‌هایِ من)»: جستجوی محصول (شناسه/اسم) و مرورِ محصولاتِ هر دسته (درخواستِ سینا)."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.bots.virtual_service.smm as SMM


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, SMM):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="vs", field="virtual_service", plan="pro", status="active"))
        s.add(models.SmmCategory(id=11, bot_id=7, platform="instagram", name="فالوور"))
        s.add(models.SmmCategory(id=12, bot_id=7, platform="instagram", name="لایک"))
        for i in range(1, 26):
            s.add(models.SmmService(id=i, bot_id=7, service_id=str(1000 + i), name=f"فالوور ایرانی {i}" if i <= 15 else f"لایک سریع {i}",
                                    category_pk=11 if i <= 15 else 12, sell_rate=1000, panel_rate=1))
        s.add(models.SmmService(id=99, bot_id=7, service_id="5555", name="سرویس بی‌دسته", sell_rate=1000, panel_rate=1))
        await s.commit()
    yield maker
    await engine.dispose()


def test_match_by_number_name_and_words():
    v = NS(id=47, service_id="3001", name="فالوور ایرانی ۱۰۰", category="Instagram", description=None)
    assert SMM._mysvc_match(v, "47") and SMM._mysvc_match(v, "3001")           # شمارهٔ محصول / شناسهٔ پنل
    assert SMM._mysvc_match(v, "فالوور") and SMM._mysvc_match(v, "ایرانی فالوور")
    assert not SMM._mysvc_match(v, "لایک") and not SMM._mysvc_match(v, "48") and not SMM._mysvc_match(v, "")
    assert SMM._mysvc_match(NS(id=1, service_id="1", name="كيفيت عالي", category=None, description=None), "کیفیت")   # ی/ک عربی


@pytest.mark.asyncio
async def test_list_filtered_by_category_and_by_search(db):
    await SMM._set_setting(7, "smm_mysvc_filter_cat", "12")
    text, kb = await SMM._smm_myservices_list_content(7, 1)
    names = [b.text for r in kb.inline_keyboard for b in r if b.callback_data.startswith("smm:svcview:")]
    assert len(names) == 10 and all("لایک" in n for n in names) and "مجموعاً 10" in text.replace("۱۰", "10")
    await SMM._set_setting(7, "smm_mysvc_filter_cat", "")
    await SMM._set_setting(7, "smm_mysvc_filter_q", "1007")                    # شناسهٔ پنل
    text, kb = await SMM._smm_myservices_list_content(7, 1)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data.startswith("smm:svcview:")]
    assert cbs == ["smm:svcview:7:1"]
    await SMM._set_setting(7, "smm_mysvc_filter_q", "بی‌دسته")
    _t, kb = await SMM._smm_myservices_list_content(7, 1)
    assert [b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data.startswith("smm:svcview:")] == ["smm:svcview:99:1"]


@pytest.mark.asyncio
async def test_groups_screen_offers_search_and_categories(db):
    _t, kb = await SMM._smm_myservices_groups_content(7)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "smm:mysvc:search" in cbs and "smm:mysvc:cats" in cbs
