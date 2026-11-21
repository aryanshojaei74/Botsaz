"""PATCH_REFPOINT_REQUIRE_ORDER_V1 / PATCH_REFPOINT_RESET_ALL_V1"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import ref_points as RP


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, RP):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add_all([models.BotUser(id=1, bot_id=9, telegram_id=1001, full_name="Inv", balance=0),
                   models.BotUser(id=2, bot_id=9, telegram_id=1002, full_name="J1", balance=0),
                   models.BotUser(id=3, bot_id=9, telegram_id=1003, full_name="J2", balance=0)])
        await s.flush()
        s.add(models.ReferralLink(id=1, bot_id=9, user_id=1, code="c1"))
        await s.flush()
        for tg in (1002, 1003):
            s.add(models.ReferralJoin(bot_id=9, referral_link_id=1, inviter_user_id=1, referred_telegram_id=tg))
        await s.commit()
    await RP._set(9, "refpoint_enabled", "1")
    await RP._set(9, "refpoint_per_join", "2")
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_without_requirement_points_are_immediate(db):
    assert await RP.award_for_join(9, 1, 1002) == 2
    assert await RP.get_points(9, 1) == 2


@pytest.mark.asyncio
async def test_requirement_holds_points_until_first_order_then_releases_once(db):
    await RP._set(9, "refpoint_require_order", "1")
    assert await RP.award_for_join(9, 1, 1002) == 0
    assert await RP.award_for_join(9, 1, 1003) == 0
    assert await RP.get_points(9, 1) == 0 and await RP.pending_points(9, 1) == 4
    assert not await RP.user_has_order(9, 1)
    async with db() as s:                      # سفارشِ در انتظار هنوز شرط را برآورده نمی‌کند
        s.add(models.Order(bot_id=9, user_id=1, volume_gb=1, days=1, amount=1000, pay_method="wallet", status="pending"))
        await s.commit()
    assert await RP.get_points(9, 1) == 0
    async with db() as s:
        s.add(models.Order(bot_id=9, user_id=1, volume_gb=1, days=1, amount=1000, pay_method="wallet", status="paid"))
        await s.commit()
    assert await RP.user_has_order(9, 1)
    assert await RP.get_points(9, 1) == 4          # آزاد شد
    assert await RP.get_points(9, 1) == 4          # دوباره‌شماری نمی‌شود
    assert await RP.pending_points(9, 1) == 0
    async with db() as s:
        w = await s.scalar(select(models.RefPointWallet).where(models.RefPointWallet.user_id == 1))
    assert w.total_earned == 4


@pytest.mark.asyncio
async def test_requirement_inviter_with_order_gets_points_immediately(db):
    await RP._set(9, "refpoint_require_order", "1")
    async with db() as s:
        s.add(models.Order(bot_id=9, user_id=1, volume_gb=1, days=1, amount=1, pay_method="wallet", status="paid"))
        await s.commit()
    assert await RP.award_for_join(9, 1, 1002) == 2


@pytest.mark.asyncio
async def test_refund_award_is_not_blocked_by_requirement(db):
    await RP._set(9, "refpoint_require_order", "1")
    assert await RP.award_points(9, 1, 3) == 3       # بازگشتِ امتیازِ کسرشده مستقل از شرط است


@pytest.mark.asyncio
async def test_reset_all_points(db):
    await RP.award_points(9, 1, 5)
    await RP.award_points(9, 2, 7)
    await RP._set(9, "refpoint_require_order", "1")
    await RP.award_for_join(9, 3, 1002)             # در انتظار برایِ کاربرِ بدونِ سفارش
    n, tot = await RP.reset_all_points(9)
    assert (n, tot) == (2, 12)
    assert await RP.get_points(9, 1) == 0 and await RP.get_points(9, 2) == 0
    assert await RP.pending_points(9, 1) == 0
