"""PATCH_SMM_CUSTOMER_TREE_CLASSIFY_V1 — سرویس‌هایِ داخلِ دستهٔ روبینو در فروشگاهِ مشتری و دکمهٔ پلتفرم."""
import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm, smm_api


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, smm):
        monkeypatch.setattr(mod, "Session", maker)

    async def _noop():
        return None
    monkeypatch.setattr(smm, "ensure_smm_columns", _noop)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.SmmCategory(id=1, bot_id=5, platform="rubino", name="فالوور روبینو"))
        # اسم هیچ نشانی از روبینو ندارد؛ پلتفرمِ ذخیره‌شده هم (از حدسِ پنل) اینستاگرام است
        s.add(models.SmmService(id=10, bot_id=5, service_id="77", name="فالوور ایرانی ویژه", category_pk=1,
                                panel_rate=1000, sell_rate=2000, min_qty=10, max_qty=1000, is_active=True,
                                platform_key="instagram", type_key="follower"))
        s.add(models.SmmService(id=11, bot_id=5, service_id="77", name="لایک پست", category_pk=1,
                                panel_rate=1000, sell_rate=2000, min_qty=10, max_qty=1000, is_active=True))
        s.add(models.SmmService(id=12, bot_id=5, service_id="88", name="telegram member", panel_rate=1,
                                sell_rate=2, min_qty=10, max_qty=1000, is_active=True))
        s.add(models.MainButton(id=50, bot_id=5, row=0, col=0, label="روبینو", action="smm_platform",
                                config=json.dumps({"platforms": ["rubino"]})))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_tree_uses_category_platform(db):
    tree, by_pk = await smm._user_active_tree(5)
    assert {it["pk"] for t in tree["rubino"].values() for it in t} == {10, 11}
    assert tree["rubino"]["follower"][0]["pk"] == 10
    assert tree["rubino"]["like"][0]["pk"] == 11
    assert "telegram" in tree and set(by_pk) == {10, 11, 12}
    async with db() as s:
        v = await s.get(models.SmmService, 11)
    assert (v.platform_key, v.type_key) == ("rubino", "like")   # ذخیره شد → همهٔ صفحه‌ها یکسان می‌بینند


@pytest.mark.asyncio
async def test_platform_button_shows_rubino(db):
    content = await smm._smm_platform_content(5, "rubino", home_cb="mbtn:home")
    assert content is not None
    text, kb = content
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "smmu:type:rubino:follower" in cbs and "smmu:type:rubino:like" in cbs


@pytest.mark.asyncio
async def test_admin_platform_picker_lists_rubino(db):
    kb = await tenant._thb_smmplat_kb(5, 50, ["rubino"])
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "thb:togglesmmplat:50:rubino" in cbs


@pytest.mark.asyncio
async def test_type_page_maps_by_pk_not_panel_id(db):
    class _Msg:
        def __init__(self):
            self.kb = None

        async def edit_text(self, text, reply_markup=None, parse_mode=None):
            self.kb = reply_markup

        async def answer(self, *a, **k):
            pass

    class _Cb:
        data = "smmu:type:rubino:like"
        message = _Msg()

        async def answer(self, *a, **k):
            pass

    async def _row(bot):
        return type("R", (), {"id": 5})()
    import tenant as _t
    orig = _t.get_bot_row
    _t.get_bot_row = _row
    try:
        cb = _Cb()
        await smm.smmu_pick_type(cb, None)
    finally:
        _t.get_bot_row = orig
    cbs = [b.callback_data for r in cb.message.kb.inline_keyboard for b in r]
    _ids = {c.split(":")[2] for c in cbs if c.startswith("smmu:svc:")}       # callback حالا پسوندِ لیست/صفحه هم دارد
    assert "11" in _ids and "10" not in _ids   # هر دو service_id=77 دارند


@pytest.mark.asyncio
async def test_category_button_still_works(db):
    async with db() as s:
        mb = models.MainButton(id=51, bot_id=5, row=1, col=0, label="r2", action="smm_direct_product",
                               config=json.dumps({"smm_category_ids": [1]}))
        s.add(mb)
        await s.commit()
    svcs = await smm._resolve_direct_product_services(5, mb)
    assert {v.id for v in svcs} == {10, 11}


def test_detect_platform_rubino_priority():
    assert smm_api.detect_platform("Rubika Rubino followers") == "rubino"
    assert smm_api.detect_platform("فالوور روبینو (اینستاگرامی)") == "rubino"
    assert smm_api.detect_platform("ممبر کانال روبیکا") == "rubika"
    assert smm_api.detect_platform("instagram likes") == "instagram"
