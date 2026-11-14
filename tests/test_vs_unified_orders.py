"""PATCH_VS_UNIFIED_ORDERS_V1 — «لیست سفارشات» همهٔ انواعِ سفارش (SMM + استارز/گیفت/پریمیوم/TON + NFT) را نشان می‌دهد."""
from datetime import datetime, timedelta

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
    for mod in (config, tenant, smm, models):
        if hasattr(mod, "Session"):
            monkeypatch.setattr(mod, "Session", maker)
    t0 = datetime(2026, 10, 11, 12, 0, 0)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=111, username="buyer"))
        await s.commit()
        s.add(models.SmmOrder(bot_id=5, user_telegram_id=111, service_id="1", link="https://t.me/abc",
                              quantity=100, price_toman=1000, status="sent", created_at=t0))
        s.add(models.GiftOrder(bot_id=5, buyer_user_id=1, gift_id="starsell_500", gift_emoji="⭐",
                               star_count=500, price_toman=90000, dest_telegram_id=111, dest_label="@buyer",
                               status="awaiting_admin", created_at=t0 + timedelta(minutes=1)))
        s.add(models.GiftOrder(bot_id=5, buyer_user_id=1, gift_id="premium_3", gift_emoji="🌟",
                               star_count=1000, price_toman=500000, dest_telegram_id=111, dest_label="@buyer",
                               status="failed", created_at=t0 + timedelta(minutes=2)))
        await s.commit()
    yield maker
    await engine.dispose()


def _texts(kb):
    return [b.text for r in kb.inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_list_contains_stars_and_premium_orders(db):
    text, kb = await smm._smm_vs_paginated_content(5, 1, only_failed=False, kind="list")
    t = _texts(kb)
    assert any("500" in x and "استارز" in x for x in t)
    assert any("پریمیوم 3" in x for x in t)
    assert any("https://t.me/abc" in x for x in t)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert any(c.startswith("smm:vs:gview:list:1:") for c in cbs)
    assert any(c.startswith("smm:vs:view:list:1:") for c in cbs)


@pytest.mark.asyncio
async def test_failed_filter_includes_failed_gift_orders(db):
    _, kb = await smm._smm_vs_paginated_content(5, 1, only_failed=True, kind="failed")
    t = _texts(kb)
    assert any("پریمیوم 3" in x for x in t)
    assert not any("استارز" in x for x in t)


@pytest.mark.asyncio
async def test_gift_detail_has_action_buttons(db):
    async with db() as s:
        oid = (await s.scalar(models.select(models.GiftOrder.id).where(
            models.GiftOrder.gift_id == "starsell_500")))
    text, kb = await smm._vs_gift_detail_content(5, oid, "smm:vs:list:1")
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert f"star:done:{oid}" in cbs and f"star:reject:{oid}" in cbs
    assert "استارز" in text


@pytest.mark.asyncio
async def test_other_bot_order_not_visible(db):
    text, _ = await smm._vs_gift_detail_content(999, 1, "smm:vs:menu")
    assert "پیدا نشد" in text
