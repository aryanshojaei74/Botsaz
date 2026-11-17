"""سینا: «تراکنش کل» و «نفرات برتر» در خدماتِ مجازی — هر خرید (با هر روشِ شارژِ کیف پول، از جمله بلوپال) دقیقاً یک بار شمرده شود."""
import types

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant.shared.top_customers as TC
from tenant.shared import profile_stats as PS


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(TC, "Session", mk)
    import config
    import tenant
    monkeypatch.setattr(config, "Session", mk)
    monkeypatch.setattr(tenant, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="x", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=500, full_name="Ali", username="ali"))
        # شارژ با بلوپال، بعد سفارشِ SMM ۱۰هزار (SmmOrder + کسر از کیف پول) و خریدِ شمارهٔ مجازی ۴هزار (فقط کسر از کیف پول)
        s.add(models.WalletTxn(bot_id=7, user_id=1, amount=50_000, kind="blupal_charge", note="شارژ"))
        s.add(models.SmmOrder(bot_id=7, user_telegram_id=500, service_id="1", link="l", quantity=1000,
                              price_toman=10_000, status="completed"))
        s.add(models.WalletTxn(bot_id=7, user_id=1, amount=-10_000, kind="smm_order", note="سفارش SMM: لایک × 1000"))
        s.add(models.WalletTxn(bot_id=7, user_id=1, amount=-4_000, kind="smm_order", note="خریدِ شمارهٔ مجازی: ترکیه"))
        await s.commit()
    yield mk
    await engine.dispose()


@pytest.mark.asyncio
async def test_top_buyers_counts_each_purchase_once(maker):
    (u, total), = await TC.top_buyers(7)
    assert u.telegram_id == 500 and total == 14_000          # ۱۰هزار SMM یک‌بار + ۴هزار شماره (نه ۲۴هزار)


@pytest.mark.asyncio
async def test_profile_total_spent_includes_virtual_service_purchases(maker):
    u = types.SimpleNamespace(id=1, telegram_id=500)
    st = await PS.compute(7, u, is_virtual_service=True)
    assert st.total_spent == 14_000
    st2 = await PS.compute(7, u, is_virtual_service=False)    # غیرِ مجازی: رفتارِ قبلی (فقط Order)
    assert st2.total_spent == 0
