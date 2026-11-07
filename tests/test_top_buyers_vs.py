"""«نفراتِ برتر» برایِ رباتِ خدمات مجازی: سفارش‌هایِ SMM/گیفت/شمارهٔ مجازی هم شمرده می‌شوند."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant.shared.top_customers as TC


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(TC, "Session", mk)
    yield mk
    await engine.dispose()


@pytest.mark.asyncio
async def test_virtual_service_purchases_are_ranked(maker):
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=11, full_name="A"))
        s.add(models.BotUser(id=2, bot_id=5, telegram_id=22, full_name="B"))
        s.add(models.SmmOrder(bot_id=5, user_telegram_id=11, service_id="1", link="l", quantity=10,
                              price_toman=100_000, status="completed", refunded_toman=20_000))
        s.add(models.SmmOrder(bot_id=5, user_telegram_id=11, service_id="1", link="l", quantity=10,
                              price_toman=50_000, status="failed"))            # شمرده نمی‌شود
        s.add(models.GiftOrder(bot_id=5, buyer_user_id=2, gift_id="g", star_count=50, price_toman=200_000,
                               dest_telegram_id=22, dest_label="me", status="sent", quantity=2, pay_method="card"))
        s.add(models.GiftOrder(bot_id=5, buyer_user_id=2, gift_id="g", star_count=50, price_toman=999_999,
                               dest_telegram_id=22, dest_label="me", status="pending"))   # شمرده نمی‌شود
        s.add(models.WalletTxn(bot_id=5, user_id=1, amount=-30_000, kind="smm_order"))
        await s.commit()
    res = await TC.top_buyers(5)
    got = {u.telegram_id: t for u, t in res}
    assert got == {22: 400_000, 11: 110_000}
    assert [u.telegram_id for u, _ in res] == [22, 11]


@pytest.mark.asyncio
async def test_non_vs_bot_unchanged(maker):
    async with maker() as s:
        s.add(models.Bot(id=6, owner_id=1, token="6:T", username="y", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=3, bot_id=6, telegram_id=33, full_name="C"))
        s.add(models.SmmOrder(bot_id=6, user_telegram_id=33, service_id="1", link="l", quantity=1,
                              price_toman=70_000, status="completed"))
        await s.commit()
    assert await TC.top_buyers(6) == []
