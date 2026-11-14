"""PATCH_ZIBAL_FEE_V1 — کارمزدِ زیبال با خریدار (درصدی/ثابت) + قابلِ تغییرِ نام بودنِ زیبال در چیدمانِ درگاه‌ها."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select

import config
import models
from tenant.gateways import zibal_wallet as ZW
from tenant.gateways import gateway_layout as GL


def test_calc_fee_modes():
    assert ZW.calc_fee("off", 5, 100_000) == 0
    assert ZW.calc_fee("pct", 2, 100_000) == 2_000
    assert ZW.calc_fee("pct", 1.5, 10_001) == 151          # گردشدن به بالا
    assert ZW.calc_fee("fixed", 2000, 100_000) == 2_000
    assert ZW.calc_fee("pct", 0, 100_000) == 0


def test_receipt_parse_with_and_without_fee():
    assert ZW._zb_parse_receipt_fee("zibal_track=123") == ("123", 0)
    assert ZW._zb_parse_receipt_fee("zibal_track=123|fee=2000") == ("123", 2000)
    assert ZW._zb_parse_receipt("zibal_track=123|fee=2000") == "123"


def test_zibal_is_renamable_in_layout():
    assert {k: r for k, _c, _t, r in GL.ITEMS}["zibal"] is True


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, ZW):
        monkeypatch.setattr(mod, "Session", m)
    async with m() as s:
        s.add(models.Bot(id=1, owner_id=1, token="1:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=5, bot_id=1, telegram_id=77, balance=0))
        s.add(models.Setting(bot_id=1, key="zibal_merchant_id", value="merchant123"))
        s.add(models.Order(id=9, bot_id=1, user_id=5, volume_gb=0, days=0, count=1, amount=100_000,
                           pay_method="zibal_wallet", status="pending", receipt_text="zibal_track=T1|fee=2000"))
        await s.commit()
    yield m
    await engine.dispose()


@pytest.mark.asyncio
async def test_settle_credits_base_amount_not_fee(maker, monkeypatch):
    async def _ver(merchant, track):
        return True, "REF", 102_000                        # خریدار با کارمزد پرداخت کرده
    monkeypatch.setattr(ZW.platform_zibal, "verify_payment", _ver)
    st, _m, amount, bot_id, tg, bal, _ref = await ZW._settle_zibal_order(9)
    assert st == "credited" and amount == 100_000 and bal == 100_000
    async with maker() as s:
        assert (await s.get(models.BotUser, 5)).balance == 100_000
