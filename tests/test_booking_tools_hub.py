"""PATCH_BOOKING_TOOLS_HUB_V1 — «ابزارهایِ عمومی» در پنلِ رزرو حتی در حالتِ منویِ کیبوردی خالی نیست و دسته‌بندی دارد."""
import sys

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.booking import bk_admin as A


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


@pytest.mark.asyncio
async def test_items_exist_in_reply_mode_and_are_categorized(db):
    from tenant import tools_inline_items
    async with db() as s:
        b = models.Bot(owner_id=5, token="t:1", field="booking", plan="pro")
        s.add(b)
        await s.commit()
        bid = b.id
    items = await tools_inline_items(bid, "pro", False)       # menu_mode پیش‌فرض = reply
    cbs = {c for _l, c in items}
    for need in ("set:card", "gwname:menu", "tadm:homebtns", "set:welcome", "tadm:backup:menu", "tadm:broadcast",
                 "tadm:users", "set:bot_toggle", "set:menu_mode", "tadm:discounts", "tadm:receiptchannel"):
        assert need in cbs, need
    cats = {A._gen_cat_of(l, c) for l, c in items}
    assert {"pay", "ui", "promo", "usr", "rep", "bot"} <= cats
    assert A._gen_cat_of("x", "set:card") == "pay" and A._gen_cat_of("x", "tadm:homebtns") == "ui"
    for l, c in items:
        print(A._gen_cat_of(l, c), c, l)
