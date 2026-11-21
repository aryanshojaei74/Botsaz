"""PATCH_TRUST_GIFT_TOTAL_V1 — گزارشِ خریدِ گیفتِ چندتایی مبلغِ کل را نشان دهد، نه قیمتِ یک عدد."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.virtual_service import smm


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(smm, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=3, owner_id=1, token="3:T", username="g", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.BotUser(id=10, bot_id=3, telegram_id=81, balance=0))
        s.add(models.GiftOrder(id=1, bot_id=3, buyer_user_id=10, gift_id="5170145012310081615",
                               gift_emoji="💝", star_count=15, price_toman=34672, quantity=2,
                               dest_telegram_id=81, dest_label="me", status="sent"))
        s.add(models.GiftOrder(id=2, bot_id=3, buyer_user_id=10, gift_id="starsell_100",
                               gift_emoji="⭐", star_count=100, price_toman=250000,
                               dest_telegram_id=81, dest_label="me", status="sent"))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_trust_report_uses_total(db, monkeypatch):
    seen = []

    async def fake_alert(bot, row, **kw):
        seen.append((kw["amount"], kw.get("count")))
    monkeypatch.setattr(tenant, "_purchase_alert", fake_alert)
    row = SimpleNamespace(id=3)
    await smm._notify_gift_order_trust_success(object(), row, 1)
    await smm._notify_gift_order_trust_success(object(), row, 2)
    assert seen[0][0] == 69344          # دو گیفت × ۳۴٬۶۷۲
    assert seen[1][0] == 250000         # استارز: price_toman خودش مبلغِ کل است
