"""PATCH_PLATFORM_APAY_V1 — درگاهِ آپی در کیف پولِ خودِ بات‌ساز."""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_apay as PA
import platform_bot as PB
from tenant.gateways import apay as AP


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, PA, PB):
        monkeypatch.setattr(mod, "Session", maker)
    store = {"papay_token": "apay_x", "papay_enabled": "1"}

    async def gp(k, d=""):
        return store.get(k, d)

    async def sp(k, v):
        store[k] = v
    monkeypatch.setattr(PB, "get_pset", gp)
    monkeypatch.setattr(PB, "set_pset", sp)
    paid = []

    async def after(tg, credit, bal, *a):
        paid.append((tg, credit, bal))
    monkeypatch.setattr(PA, "_after_paid", after)
    async with maker() as s:
        s.add(models.PlatformUser(telegram_id=42, full_name="U", balance=0))
        s.add(models.PlatformApayCharge(user_telegram_id=42, order_id="pbx1", payment_id="pay_9",
                                        amount_toman=100_000, status="waiting"))
        await s.commit()
    yield maker, store, paid
    await engine.dispose()


def _vr(amount=1_000_000, status="paid", order="pbx1", verified=True):
    return 200, {"verified": verified, "payment": {"id": "pay_9", "order_id": order,
                                                   "amount": amount, "status": status}}


async def _state(maker):
    async with maker() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 42))
        ch = await s.scalar(select(models.PlatformApayCharge))
        n = len((await s.scalars(select(models.PlatformTxn))).all())
        return float(u.balance), ch.status, n


@pytest.mark.asyncio
async def test_settle_once(db, monkeypatch):
    maker, _st, paid = db

    async def v(tok, pid):
        return _vr()
    monkeypatch.setattr(AP, "verify_payment", v)
    assert (await PA.settle_charge("pbx1"))[0]
    assert not (await PA.settle_charge("pbx1"))[0]
    assert await _state(maker) == (100_000, "paid", 1)
    assert paid == [(42, 100_000, 100_000)]


@pytest.mark.asyncio
@pytest.mark.parametrize("resp", [_vr(status="pending", verified=False), _vr(amount=9_999_990),
                                  _vr(order="other")])
async def test_settle_rejects(db, monkeypatch, resp):
    maker, _st, paid = db

    async def v(tok, pid):
        return resp
    monkeypatch.setattr(AP, "verify_payment", v)
    assert not (await PA.settle_charge("pbx1"))[0]
    assert await _state(maker) == (0, "waiting", 0) and paid == []


@pytest.mark.asyncio
async def test_webhook_signature(db, monkeypatch):
    maker, store, _p = db
    store["papay_secret"] = "whsec"

    async def v(tok, pid):
        return _vr()
    monkeypatch.setattr(AP, "verify_payment", v)
    raw = json.dumps({"event": "payment.paid", "payment": {"id": "pay_9", "order_id": "pbx1"}}).encode()
    ts = str(int(time.time()))
    assert await PA.handle_webhook(raw, ts, "bad") == 401
    sig = hmac.new(b"whsec", ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    assert await PA.handle_webhook(raw, ts, sig) == 200
    assert (await _state(maker))[:2] == (100_000, "paid")


@pytest.mark.asyncio
async def test_poll_checks_recent_cancelled_and_expires_waiting(db, monkeypatch):
    maker, _st, _p = db
    async with maker() as s:
        s.add(models.PlatformApayCharge(user_telegram_id=42, order_id="pbold", payment_id="pay_o",
                                        amount_toman=50_000, status="waiting",
                                        created_at=datetime.utcnow() - timedelta(minutes=45)))
        ch = await s.scalar(select(models.PlatformApayCharge).where(
            models.PlatformApayCharge.order_id == "pbx1"))
        ch.status = "cancelled"
        await s.commit()

    async def v(tok, pid):
        return _vr() if pid == "pay_9" else (200, {"verified": False, "payment": {"status": "expired"}})
    monkeypatch.setattr(AP, "verify_payment", v)
    assert await PA.poll_pending_once() == 1           # لغوشده ولی پرداخت‌شده → شارژ
    async with maker() as s:
        old = await s.scalar(select(models.PlatformApayCharge).where(
            models.PlatformApayCharge.order_id == "pbold"))
    assert old.status == "expired"


@pytest.mark.asyncio
async def test_availability_and_name(db):
    _m, store, _p = db
    assert await PA.is_available("wallet") and await PA.is_available("purchase")
    store["papay_show_purchase"] = "0"
    assert not await PA.is_available("purchase")
    assert await PA.display_name() == PA.DEFAULT_LABEL
    store["papay_display_name"] = "💳 کارت به کارت سریع"
    assert await PA.display_name() == "💳 کارت به کارت سریع"
    kb = await PB._platform_wallet_kb()
    assert any(b.callback_data == "charge_papay" and "سریع" in b.text
               for r in kb.inline_keyboard for b in r)
    store["papay_enabled"] = "0"
    kb = await PB._platform_wallet_kb()
    assert not any(b.callback_data == "charge_papay" for r in kb.inline_keyboard for b in r)


@pytest.mark.asyncio
async def test_platform_buyer_fee(db, monkeypatch):
    maker, store, paid = db
    async with maker() as s:
        ch = await s.scalar(select(models.PlatformApayCharge))
        ch.fee_toman = 1_500
        await s.commit()

    async def v(tok, pid):
        return _vr(amount=1_015_000)
    monkeypatch.setattr(AP, "verify_payment", v)
    assert (await PA.settle_charge("pbx1"))[0]
    assert await _state(maker) == (100_000, "paid", 1)
    assert await PA.fee_cfg() == ("seller", AP.FEE_TOMAN)
    store["papay_fee_payer"] = "buyer"
    assert (await PA.fee_cfg())[0] == "buyer"
