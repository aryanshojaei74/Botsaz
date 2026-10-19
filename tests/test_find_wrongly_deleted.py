"""scripts/find_wrongly_deleted.py — پیدا کردنِ قربانیانِ باگِ حذفِ بعد از تمدید."""
import datetime as dt
import importlib.util
import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models

spec = importlib.util.spec_from_file_location(
    "fwd", os.path.join(os.path.dirname(__file__), "..", "scripts", "find_wrongly_deleted.py"))
FWD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(FWD)

pytestmark = pytest.mark.asyncio

LOG = ("Oct 19 11:19:21 BotSaz python3[27137]: 2026-10-19 11:19:21 [INFO] tenant.shared.traffic_cleanup: "
       "traffic_cleanup: سرویسِ 'taziyanteam7375002338h6m' (bot_id=313, panel=pasargad) "
       "به‌دلیلِ اتمامِ حجم پس از 5 ساعت حذف شد.")


def test_parse_line():
    ev = FWD.parse_lines([LOG, "garbage"])
    assert len(ev) == 1 and ev[0]["email"] == "taziyanteam7375002338h6m" and ev[0]["bot_id"] == 313
    assert ev[0]["hours"] == 5.0 and ev[0]["deleted_at"] == dt.datetime(2026, 10, 19, 11, 19, 21)


def test_candidate_ids_include_the_real_telegram_id_even_with_digit_suffix():
    assert 7375002338 in FWD.candidate_ids("taziyanteam7375002338h6m")
    assert 2683386048 in FWD.candidate_ids("novavpnir26833860482mjm")     # ۲ آخر، نویسهٔ تصادفیِ پسوند است


@pytest_asyncio.fixture
async def maker():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(eng, expire_on_commit=False)
    async with m() as s:
        b = models.Bot(id=313, owner_id=1, token="1:A", username="x", field="vpn_sales", plan="pro")
        s.add(b)
        await s.flush()
        u = models.BotUser(bot_id=313, telegram_id=7375002338, full_name="A", balance=0)
        u2 = models.BotUser(bot_id=313, telegram_id=1112223334, full_name="B", balance=0)
        s.add_all([u, u2])
        await s.flush()
        dele = dt.datetime(2026, 10, 19, 11, 19, 21)
        s.add(models.WalletTxn(bot_id=313, user_id=u.id, amount=-100000, kind="renew", note="تمدید VPN: +30 روز +4G",
                               created_at=dele - dt.timedelta(hours=2)))                  # داخلِ مهلت
        s.add(models.WalletTxn(bot_id=313, user_id=u2.id, amount=-50000, kind="renew", note="تمدید",
                               created_at=dele - dt.timedelta(days=2)))                   # خیلی قبل‌تر
        await s.commit()
    yield m
    await eng.dispose()


async def test_victim_found_only_when_payment_is_inside_the_grace_window(maker):
    ev = FWD.parse_lines([
        LOG,
        LOG.replace("taziyanteam7375002338h6m", "taziyanteam1112223334abc"),     # پرداختش دو روز پیش بود
    ])
    found = await FWD.analyze(ev, maker)
    assert [f["telegram_id"] for f in found] == [7375002338]
    assert found[0]["txns"][0][1] == "renew"


async def test_refund_credits_wallet_once_notifies_and_is_idempotent(maker):
    from sqlalchemy import select
    ev = FWD.parse_lines([LOG])
    found = await FWD.analyze(ev, maker)
    sent = []

    async def fake_send(token, chat_id, text):
        sent.append((chat_id, text))
        return True
    r1 = await FWD.apply_refunds(found, maker, send=fake_send)
    assert r1[0]["status"] == "refunded" and r1[0]["amount"] == 100000
    async with maker() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 7375002338))
        assert float(u.balance) == 100000
        refunds = (await s.scalars(select(models.WalletTxn).where(models.WalletTxn.kind == "refund"))).all()
        assert len(refunds) == 1 and float(refunds[0].amount) == 100000
    assert {c for c, _ in sent} == {7375002338, 1} and "100,000" in sent[0][1]     # کاربر + مالکِ ربات

    r2 = await FWD.apply_refunds(found, maker, send=fake_send)                      # اجرای دوباره
    assert r2[0]["status"] == "already"
    async with maker() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 7375002338))
        assert float(u.balance) == 100000
        assert len((await s.scalars(select(models.WalletTxn).where(models.WalletTxn.kind == "refund"))).all()) == 1
    assert len(sent) == 2                                                          # پیامِ تکراری نرفت
