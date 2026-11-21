"""PATCH_BALANCE_TRANSFER_V1 — انتقالِ موجودی بینِ کاربرانِ یک ربات."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.shared.balance_transfer as BT


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, BT):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=500, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=9, telegram_id=1001, full_name="Ali", username="ali", balance=100_000))
        s.add(models.BotUser(id=2, bot_id=9, telegram_id=1002, full_name="Sara", username="sara", balance=5_000))
        await s.commit()
    yield maker
    await engine.dispose()


async def _bal(maker, uid):
    async with maker() as s:
        return int((await s.get(models.BotUser, uid)).balance)


@pytest.mark.asyncio
async def test_auto_transfer_moves_money_and_logs(env):
    res, t = await BT.do_transfer(9, 1, 2, 30_000, "auto")
    assert res == "done" and await _bal(env, 1) == 70_000 and await _bal(env, 2) == 35_000
    async with env() as s:
        kinds = sorted(x.kind for x in (await s.scalars(select(models.WalletTxn))).all())
    assert kinds == ["transfer_in", "transfer_out"]


@pytest.mark.asyncio
async def test_overdraw_self_and_min_are_rejected(env):
    assert (await BT.do_transfer(9, 1, 2, 100_001, "auto"))[0] == "no_balance"
    assert (await BT.do_transfer(9, 1, 1, 5_000, "auto"))[0] == "invalid"
    assert (await BT.do_transfer(9, 1, 2, 500, "auto"))[0] == "invalid"
    assert await _bal(env, 1) == 100_000 and await _bal(env, 2) == 5_000
    # دو درخواستِ پشت‌سرهم نمی‌تواند موجودی را منفی کند
    assert (await BT.do_transfer(9, 1, 2, 80_000, "auto"))[0] == "done"
    assert (await BT.do_transfer(9, 1, 2, 80_000, "auto"))[0] == "no_balance"
    assert await _bal(env, 1) == 20_000


@pytest.mark.asyncio
async def test_approve_mode_holds_then_approves_or_refunds(env):
    res, t = await BT.do_transfer(9, 1, 2, 40_000, "approve")
    assert res == "pending" and await _bal(env, 1) == 60_000 and await _bal(env, 2) == 5_000
    st, _ = await BT.decide(9, t.id, True)
    assert st == "done" and await _bal(env, 2) == 45_000
    assert (await BT.decide(9, t.id, True))[0] == "already"          # دوبار تأیید = دوبار پرداخت نمی‌شود
    assert await _bal(env, 2) == 45_000
    res, t2 = await BT.do_transfer(9, 1, 2, 10_000, "approve")
    assert (await BT.decide(9, t2.id, False))[0] == "rejected"
    assert await _bal(env, 1) == 60_000 and await _bal(env, 2) == 45_000   # به فرستنده برگشت


@pytest.mark.asyncio
async def test_find_user_by_id_and_username_scoped_to_bot(env):
    assert (await BT.find_user(9, "@Sara")).id == 2
    assert (await BT.find_user(9, "۱۰۰۱")).id == 1
    assert await BT.find_user(9, "999") is None and await BT.find_user(9, "bad name!") is None
    assert BT.parse_amount("۲۵,۰۰۰") == 25_000 and BT.parse_amount("0") is None and BT.parse_amount("x") is None
