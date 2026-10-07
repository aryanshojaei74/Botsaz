"""PATCH_APAY_GATEWAY_V1 — درگاهِ کارت‌به‌کارتِ آنیِ آپی."""
import hashlib
import hmac
import json
import time

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.gateways import apay as AP


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, AP):
        monkeypatch.setattr(mod, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="A", balance=0))
        s.add(models.ApayCharge(bot_id=5, user_telegram_id=777, order_id="bsabc", payment_id="pay_1",
                                amount_toman=50_000, pay_amount_rial=500_370, status="waiting"))
        await s.commit()
    await tenant.set_setting(5, "apay_token", "apay_testtoken")
    await tenant.set_setting(5, "apay_enabled", "1")
    notified = []

    async def _np(bot_id, tg, amount, bal):
        notified.append((tg, amount, bal))

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(AP, "_notify_paid", _np)
    import tenant.gateways.payment_page as pp
    monkeypatch.setattr(pp, "_tx_log_tenant", _noop)
    import tenant.shared.kyc as kyc
    monkeypatch.setattr(kyc, "after_charge", _noop)
    yield maker, notified
    await engine.dispose()


def _verify_resp(status="paid", amount=500_000, order_id="bsabc", verified=True):
    return 200, {"verified": verified, "payment": {
        "id": "pay_1", "order_id": order_id, "amount": amount, "pay_amount": amount + 370,
        "status": status, "late": False}}


async def _balance(maker):
    async with maker() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 777))
        ch = await s.scalar(select(models.ApayCharge))
        n = len((await s.scalars(select(models.WalletTxn))).all())
        return float(u.balance), ch.status, n


@pytest.mark.asyncio
async def test_settle_credits_once(db, monkeypatch):
    maker, notified = db

    async def _v(token, pid):
        assert token == "apay_testtoken" and pid == "pay_1"
        return _verify_resp()
    monkeypatch.setattr(AP, "verify_payment", _v)
    ok, _m, amt = await AP.settle_charge(5, "bsabc")
    assert ok and amt == 50_000
    assert await _balance(maker) == (50_000, "paid", 1)
    ok2, _m, _a = await AP.settle_charge(5, "bsabc")       # وب‌هوکِ تکراری
    assert not ok2
    assert await _balance(maker) == (50_000, "paid", 1)
    assert notified == [(777, 50_000, 50_000)]


@pytest.mark.asyncio
@pytest.mark.parametrize("resp", [
    _verify_resp(status="pending", verified=False),
    _verify_resp(status="expired", verified=False),
    _verify_resp(amount=5_000_000),            # مبلغِ ناجور
    _verify_resp(order_id="other"),            # سفارشِ دیگر
])
async def test_settle_rejects(db, monkeypatch, resp):
    maker, notified = db

    async def _v(token, pid):
        return resp
    monkeypatch.setattr(AP, "verify_payment", _v)
    ok, _m, _a = await AP.settle_charge(5, "bsabc")
    assert not ok
    assert await _balance(maker) == (0, "waiting", 0)
    assert notified == []


@pytest.mark.asyncio
async def test_cancelled_charge_still_settles(db, monkeypatch):
    maker, _n = db
    async with maker() as s:
        ch = await s.scalar(select(models.ApayCharge))
        ch.status = "cancelled"
        await s.commit()

    async def _v(token, pid):
        return _verify_resp()
    monkeypatch.setattr(AP, "verify_payment", _v)
    ok, _m, _a = await AP.settle_charge(5, "bsabc")
    assert ok
    assert (await _balance(maker))[:2] == (50_000, "paid")


def test_signature():
    raw = b'{"event":"payment.paid"}'
    ts = str(int(time.time()))
    sig = hmac.new(b"whsec", ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    assert AP.signature_ok("whsec", raw, ts, sig)
    assert not AP.signature_ok("other", raw, ts, sig)
    assert not AP.signature_ok("whsec", raw + b" ", ts, sig)
    old = str(int(time.time()) - 400)
    sig_old = hmac.new(b"whsec", old.encode() + b"." + raw, hashlib.sha256).hexdigest()
    assert not AP.signature_ok("whsec", raw, old, sig_old)


@pytest.mark.asyncio
async def test_webhook_verifies_with_api(db, monkeypatch):
    maker, _n = db
    await tenant.set_setting(5, "apay_secret", "whsec")
    calls = []

    async def _v(token, pid):
        calls.append(pid)
        return _verify_resp()
    monkeypatch.setattr(AP, "verify_payment", _v)
    raw = json.dumps({"event": "payment.paid", "payment": {"id": "pay_1", "order_id": "bsabc",
                                                           "amount": 500_000, "status": "paid"}}).encode()
    ts = str(int(time.time()))
    assert await AP.handle_webhook(5, raw, ts, "bad") == 401
    assert calls == []
    sig = hmac.new(b"whsec", ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    assert await AP.handle_webhook(5, raw, ts, sig) == 200
    assert calls == ["pay_1"]
    assert (await _balance(maker))[:2] == (50_000, "paid")
    assert await AP.handle_webhook(5, raw, ts, sig) == 200       # تکراری: بدونِ شارژِ دوباره
    assert calls == ["pay_1"]
    assert (await _balance(maker))[0] == 50_000


@pytest.mark.asyncio
async def test_create_payment_uses_rial(monkeypatch):
    seen = {}

    async def _api(token, method, path, data=None):
        seen.update(method=method, path=path, data=data)
        return 201, {"id": "pay_x", "pay_url": "https://apaygate.shop/pay/x"}
    monkeypatch.setattr(AP, "_api", _api)
    await AP.create_payment("t", amount_toman=12_345, order_id="bs1", callback_url="https://h/apay/webhook/5")
    assert seen["method"] == "POST" and seen["path"] == "/api/v1/payments"
    assert seen["data"]["amount"] == 123_450 and seen["data"]["order_id"] == "bs1"


@pytest.mark.asyncio
async def test_poll_expires_old(db, monkeypatch):
    from datetime import datetime, timedelta
    maker, _n = db
    async with maker() as s:
        ch = await s.scalar(select(models.ApayCharge))
        ch.created_at = datetime.utcnow() - timedelta(minutes=40)
        await s.commit()

    async def _v(token, pid):
        return _verify_resp(status="expired", verified=False)
    monkeypatch.setattr(AP, "verify_payment", _v)
    assert await AP.poll_pending() == 0
    assert (await _balance(maker))[1] == "expired"


def test_err_text():
    assert "کارت" in AP.err_text(409, {"error": {"code": "no_device", "message": "x"}})
    assert AP.err_text(500, {"error": {"code": "weird", "message": "boom"}}) == "boom"
