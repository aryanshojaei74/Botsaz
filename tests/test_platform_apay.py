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
    monkeypatch.setattr(PA, "_g", gp)      # PATCH_PARTNER_APAY_V1: _g دیگر از platform_bot نمی‌خواند
    monkeypatch.setattr(PA, "_s", sp)
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


# === PATCH_APAY_ADMIN_ASK_V1 ===
@pytest.mark.asyncio
async def test_admin_ask_after_5min_and_manual_settle(db, monkeypatch):
    maker, _st, _p = db
    import sys, types
    sent = []

    class FakeBot:
        async def send_message(self, chat, text, reply_markup=None):
            sent.append((chat, text, reply_markup))

        async def delete_message(self, **k):
            pass
    monkeypatch.setitem(sys.modules, "main_platform", types.SimpleNamespace(platform_bot=FakeBot()))
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 777)
    assert await PA.ask_admin_pending() == 0                   # هنوز ۵ دقیقه نشده
    async with maker() as s:
        ch = await s.scalar(select(models.PlatformApayCharge))
        ch.created_at = datetime.utcnow() - timedelta(minutes=6)
        cid = ch.id
        await s.commit()
    assert await PA.ask_admin_pending() == 1
    assert await PA.ask_admin_pending() == 0                   # فقط یک‌بار
    assert sent[0][0] == 777 and "100,000" in sent[0][1] and "ثبتِ فاکتور" in sent[0][1]
    assert sent[0][2].inline_keyboard[0][0].callback_data == f"papadm:ok:{cid}"
    assert (await PA.manual_settle(cid, 777))[0]
    assert not (await PA.manual_settle(cid, 777))[0]           # دوبار شارژ نشود
    assert await _state(maker) == (100_000, "paid", 1)


# === PATCH_PARTNER_APAY_V1 ===
@pytest_asyncio.fixture
async def partner(db, monkeypatch):
    maker, store, paid = db
    import platform_partner as PP
    monkeypatch.setattr(PP, "Session", maker)
    sent = []

    async def fake_send(chat, text):
        sent.append((chat, text))
    monkeypatch.setattr(PP, "_pp_send_platform", fake_send)

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(PA, "_delete_partner_invoice", noop)
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=42, token="9:T", username="pb", field="vpn_sales",
                         plan="partner", status="active"))
        await s.flush()
        s.add(models.PartnerBot(id=3, bot_id=9, owner_id=42, stock_growth=0, stock_pro=1))
        await s.flush()
        s.add(models.PartnerStockOrder(id=7, partner_bot_id=3, qty_growth=2, qty_pro=1,
                                       amount=320_600, method="apay", status="pending"))
        s.add(models.PlatformApayCharge(user_telegram_id=42, order_id="ppord7", payment_id="pay_p7",
                                        amount_toman=320_600, status="waiting",
                                        purpose="partner_order", ref_id=7))
        await s.commit()
    yield maker, sent


@pytest.mark.asyncio
async def test_partner_order_paid_with_apay_adds_stock_not_wallet(partner, monkeypatch):
    maker, sent = partner

    async def v(tok, pid):
        return 200, {"verified": True, "payment": {"id": "pay_p7", "order_id": "ppord7",
                                                   "amount": 3_206_000, "status": "paid"}}
    monkeypatch.setattr(AP, "verify_payment", v)
    assert (await PA.settle_charge("ppord7"))[0]
    assert not (await PA.settle_charge("ppord7"))[0]            # دوباره تسویه نمی‌شود
    async with maker() as s:
        o = await s.get(models.PartnerStockOrder, 7)
        pb = await s.get(models.PartnerBot, 3)
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 42))
    assert o.status == "approved" and pb.stock_growth == 2 and pb.stock_pro == 2
    assert float(u.balance) == 0                                 # کیف پول شارژ نشد
    assert sent and "روش" in sent[0][1] and "کارت" in sent[0][1]


@pytest.mark.asyncio
async def test_partner_invoice_created_and_expired_order_rejected(partner, monkeypatch):
    maker, _sent = partner
    msgs = []

    class FakeMsg:
        class bot:
            @staticmethod
            async def get_me():
                class M:
                    username = "pb"
                return M()

        class chat:
            id = 5

        async def answer(self, text, reply_markup=None, **k):
            msgs.append(text)

            class S:
                class chat:
                    id = 5
                message_id = 99
            return S()

    async def cp(token, **kw):
        assert kw["amount_toman"] == 320_600 + 1500 * 0 or kw["amount_toman"] == 320_600 + 1500
        return 200, {"id": "pay_n", "pay_url": "https://x/p", "pay_amount": 3_206_070}

    async def cf(token, res):
        return "6219861900000000", "علی"
    monkeypatch.setattr(AP, "create_payment", cp)
    monkeypatch.setattr(AP, "card_for", cf)
    monkeypatch.setattr(AP, "spawn_watch", lambda *a, **k: None)
    assert await PA.create_partner_invoice(FakeMsg(), 42, 7, 320_600)
    async with maker() as s:
        rows = (await s.scalars(select(models.PlatformApayCharge).where(
            models.PlatformApayCharge.payment_id == "pay_n"))).all()
    assert len(rows) == 1 and rows[0].purpose == "partner_order" and rows[0].ref_id == 7
    assert rows[0].msg_id == 99 and any("6219861900000000" in m for m in msgs)

    # فاکتورِ قدیمی و پرداخت‌نشده → منقضی؛ سفارشِ pending رد می‌شود
    async with maker() as s:
        ch = await s.scalar(select(models.PlatformApayCharge).where(
            models.PlatformApayCharge.order_id == "ppord7"))
        ch.created_at = datetime.utcnow() - timedelta(minutes=45)
        await s.commit()

    async def v(tok, pid):
        return 200, {"verified": False, "payment": {"status": "expired"}}
    monkeypatch.setattr(AP, "verify_payment", v)
    await PA.poll_pending_once()
    async with maker() as s:
        o = await s.get(models.PartnerStockOrder, 7)
    assert o.status == "rejected"
