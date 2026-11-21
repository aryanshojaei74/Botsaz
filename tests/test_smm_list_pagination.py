"""PATCH_SMM_LIST_PAGINATION_V1 — «reply markup is too long»: فهرست‌هایِ سرویس ۳۰تایی صفحه‌بندی می‌شوند."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm


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

    async def _no_back(bot_id):
        return None
    monkeypatch.setattr(tenant, "inline_back_row", _no_back)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service",
                         plan="pro", status="active"))
        for i in range(70):
            s.add(models.SmmService(id=100 + i, bot_id=5, service_id=str(i),
                                    name=f"ممبر کانال تلگرام ایرانی با گارانتی شماره {i}",
                                    panel_rate=1, sell_rate=1000 + i, min_qty=10, max_qty=1000,
                                    is_active=True, platform_key="telegram", type_key="member"))
        await s.commit()
    yield maker
    await engine.dispose()


class _Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append(reply_markup)


def _svc_buttons(kb):
    return [b for r in kb.inline_keyboard for b in r if (b.callback_data or "").startswith("smmu:svc:")]


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_vs_type_list_is_paginated(db):
    m = _Msg()
    await smm.show_smm_type_to_user(None, m, 5, "vs_member")
    kb = m.sent[-1]
    assert len(_svc_buttons(kb)) == 30
    assert "smmu:vst:vs_member:2" in _cbs(kb)
    m2 = _Msg()
    await smm.show_smm_type_to_user(None, m2, 5, "vs_member", page=3)
    kb3 = m2.sent[-1]
    assert len(_svc_buttons(kb3)) == 10 and "smmu:vst:vs_member:2" in _cbs(kb3)
    # کلِ کیبوردِ یک صفحه زیرِ ۱۰ کیلوبایت می‌ماند
    import json
    assert len(json.dumps(kb.model_dump(exclude_none=True), ensure_ascii=False).encode()) < 10_000


def test_page_helpers():
    items, page, pages = smm._page_slice(list(range(61)), 5)
    assert (page, pages, items) == (3, 3, [60])
    assert smm._page_nav("x", 1, 1) is None
    nav = smm._page_nav("x:y", 2, 3)
    assert [b.callback_data for b in nav] == ["x:y:1", "noop", "x:y:3"]
