"""بونوسِ شارژِ کیف پول (PATCH_CHARGE_BONUS_V1)."""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
from tenant.shared import charge_bonus as CB

pytestmark = pytest.mark.asyncio


async def test_parse_tiers_variants_and_bad_lines():
    assert CB.parse_tiers("300000=20\n1000000=25") == [(300000, 20.0), (1000000, 25.0)]
    assert CB.parse_tiers("۳۰۰,۰۰۰ = ۲۰٪ ; 1000000: 25") == [(300000, 20.0), (1000000, 25.0)]
    assert CB.parse_tiers("abc\n0=10\n5000=0\n5000=7") == [(5000, 7.0)]
    assert CB.parse_tiers("") == []


async def test_bonus_for_picks_highest_reached_tier_and_cap():
    tiers = [(300000, 20.0), (1000000, 25.0)]
    assert CB.bonus_for(299999, tiers) == (0, 0.0)
    assert CB.bonus_for(300000, tiers) == (60000, 20.0)
    assert CB.bonus_for(1000000, tiers) == (250000, 25.0)
    assert CB.bonus_for(1000000, tiers, cap=100000) == (100000, 25.0)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:A", username="x", field="vpn_sales", plan="pro")
        s.add(b)
        await s.flush()
        u = models.BotUser(bot_id=b.id, telegram_id=555, full_name="A", balance=0)
        s.add(u)
        await s.commit()
        ids = (b.id, u.id)
    sent = []

    async def fake_notify(items):
        sent.extend(items)
    monkeypatch.setattr(CB, "_notify", fake_notify)
    yield maker, ids, sent
    await engine.dispose()


async def _set(maker, bot_id, **kv):
    async with maker() as s:
        for k, v in kv.items():
            s.add(models.Setting(bot_id=bot_id, key=k, value=v))
        await s.commit()


async def _charge(maker, ids, amount, kind="zarinpal_charge", note="x"):
    bot_id, uid = ids
    async with maker() as s:
        u = await s.get(models.BotUser, uid)
        u.balance = float(u.balance or 0) + amount
        s.add(models.WalletTxn(bot_id=bot_id, user_id=uid, amount=amount, kind=kind, note=note))
        await s.commit()
        mem = float(u.balance)
    await asyncio.sleep(0)
    async with maker() as s:
        db_bal = float((await s.get(models.BotUser, uid)).balance)
        kinds = [(t.kind, float(t.amount)) for t in (await s.scalars(
            select(models.WalletTxn).order_by(models.WalletTxn.id))).all()]
    return mem, db_bal, kinds


async def test_bonus_added_atomically_in_db_and_memory(db):
    maker, ids, sent = db
    await _set(maker, ids[0], chg_bonus_on="1", chg_bonus_tiers="300000=20")
    mem, db_bal, kinds = await _charge(maker, ids, 300000)
    assert db_bal == 360000 and mem == 360000
    assert ("charge_bonus", 60000.0) in kinds
    assert sent and sent[0]["bonus"] == 60000 and sent[0]["tg"] == 555 and sent[0]["balance"] == 360000


async def test_below_tier_off_and_non_topup_get_no_bonus(db):
    maker, ids, sent = db
    await _set(maker, ids[0], chg_bonus_on="1", chg_bonus_tiers="300000=20")
    _, bal, kinds = await _charge(maker, ids, 100000)
    assert bal == 100000 and all(k != "charge_bonus" for k, _ in kinds)
    # بازگشتِ وجه (kind=charge با یادداشتِ دیگر) و جایزه بونوس نمی‌گیرند
    _, bal, kinds = await _charge(maker, ids, 400000, kind="charge", note="بازگشتِ وجه — x")
    assert bal == 500000 and all(k != "charge_bonus" for k, _ in kinds)
    # شارژِ دستیِ کارت‌به‌کارت (note مشخص) بونوس می‌گیرد
    _, bal, kinds = await _charge(maker, ids, 300000, kind="charge", note="شارژ سفارش #12")
    assert bal == 860000 and ("charge_bonus", 60000.0) in kinds
    assert len(sent) == 1


async def test_disabled_by_default_and_cap(db):
    maker, ids, sent = db
    _, bal, _k = await _charge(maker, ids, 500000)
    assert bal == 500000 and not sent
    await _set(maker, ids[0], chg_bonus_on="1", chg_bonus_tiers="100000=50", chg_bonus_cap="30000")
    _, bal, kinds = await _charge(maker, ids, 100000)
    assert bal == 630000 and ("charge_bonus", 30000.0) in kinds
