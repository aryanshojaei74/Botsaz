"""TonPays: فاکتورِ باز ⇒ «لغو و پرداخت جدید» (PATCH_TONPAYS_PENDING_V1)."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.gateways import tonpays as TP

pytestmark = pytest.mark.asyncio

USER = 555


class Msg:
    def __init__(self):
        self.sent = []
        self.chat = SimpleNamespace(id=USER)

    async def answer(self, text="", **kw):
        self.sent.append((text, kw))

    async def edit_reply_markup(self, **kw):
        pass


class Cb:
    def __init__(self, data):
        self.data, self.message = data, Msg()
        self.from_user = SimpleNamespace(id=USER)
        self.alerts = []

    async def answer(self, text="", show_alert=False, **kw):
        self.alerts.append(text)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(TP, "Session", maker)
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:A", username="x", field="vpn_sales", plan="pro")
        s.add(b)
        await s.flush()
        s.add(models.TonPaysCharge(bot_id=b.id, user_telegram_id=USER, order_id="tpOLD", invoice_id="TP-OLD",
                                   request_amount=50000, final_amount=50037, status="waiting"))
        await s.commit()
        bid = b.id

    async def gbr(_b):
        return SimpleNamespace(id=bid)

    async def gk(bot_id, custom):
        return "KEY"
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(TP, "get_key", gk)
    yield maker, bid
    await engine.dispose()


def _err(code="PENDING_INVOICE_EXISTS"):
    return {"detail": {"code": code, "message": "buyer has a pending invoice"}}


async def test_pending_error_is_detected_and_other_errors_are_not(monkeypatch):
    async def req(method, path, key, body=None):
        return 409, _err()
    monkeypatch.setattr(TP, "_request", req)
    with pytest.raises(TP.TonPaysError) as e:
        await TP.create_invoice("K", 50000, "o1", buyer_chat_id=USER)
    assert e.value.code == "PENDING_INVOICE_EXISTS"

    async def req2(method, path, key, body=None):
        return 429, {"detail": {"code": "RATE_LIMIT_EXCEEDED", "message": "slow down"}}
    monkeypatch.setattr(TP, "_request", req2)
    with pytest.raises(TP.TonPaysError) as e2:
        await TP.create_invoice("K", 50000, "o1", buyer_chat_id=USER)
    assert e2.value.code == "RATE_LIMIT_EXCEEDED"


async def test_cancel_invoice_tries_next_path_on_route_not_found(monkeypatch):
    calls = []

    async def req(method, path, key, body=None):
        calls.append((path, body))
        if path.endswith("/TP-X/cancel"):
            return 404, {"detail": "Not Found"}
        return 200, {"status": "canceled"}
    monkeypatch.setattr(TP, "_request", req)
    r = await TP.cancel_invoice("K", "TP-X")
    assert r["status"] == "canceled"
    assert calls[1] == ("/api/v1/invoices/cancel", {"invoice_id": "TP-X"})


async def test_cancel_unsupported_when_no_route_exists(monkeypatch):
    async def req(method, path, key, body=None):
        return 404, {"detail": "Not Found"}
    monkeypatch.setattr(TP, "_request", req)
    assert (await TP.cancel_at_gateway("K", "TP-X"))[0] == "unsupported"


async def test_cancel_at_gateway_never_cancels_a_paid_invoice(monkeypatch):
    async def chk(key, inv, custom=False):
        return {"status": "completed", "paid": True}

    async def boom(*a, **k):
        raise AssertionError("نباید لغو شود")
    monkeypatch.setattr(TP, "check_invoice", chk)
    monkeypatch.setattr(TP, "cancel_invoice", boom)
    assert (await TP.cancel_at_gateway("K", "TP-X"))[0] == "paid"


async def test_create_with_pending_offers_cancel_button(db, monkeypatch):
    maker, bid = db

    async def create(*a, **k):
        raise TP.TonPaysError("x", "PENDING_INVOICE_EXISTS", 409)
    monkeypatch.setattr(TP, "create_invoice", create)

    async def cm(_bid):
        return False
    monkeypatch.setattr(TP, "is_custom_mode", cm)
    msg = Msg()
    await TP._create_invoice(object(), SimpleNamespace(id=bid), msg, USER, 70000)
    text, kw = msg.sent[-1]
    assert "50,000" in text and "لغو" in text
    data = [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r]
    assert data == ["tonp:verify:tpOLD", "tonp:cnew:tpOLD:70000"]


async def test_cancel_and_new_cancels_then_creates(db, monkeypatch):
    maker, bid = db
    created = []

    async def cag(key, inv, custom=False):
        assert inv == "TP-OLD"
        return "cancelled", ""

    async def fake_create(bot, row, msg, user_id, amount, after_cancel=False):
        created.append((user_id, amount, after_cancel))
    monkeypatch.setattr(TP, "cancel_at_gateway", cag)
    monkeypatch.setattr(TP, "_create_invoice", fake_create)
    cb = Cb("tonp:cnew:tpOLD:70000")
    await TP.tonp_cancel_and_new(cb, object())
    assert created == [(USER, 70000, True)]
    async with maker() as s:
        assert (await s.scalar(select(models.TonPaysCharge.status))) == "cancelled"


async def test_cancel_and_new_settles_instead_when_it_was_paid(db, monkeypatch):
    maker, bid = db

    async def cag(key, inv, custom=False):
        return "paid", ""

    async def settle(b, o):
        return True, "پرداخت تایید و کیف پول شارژ شد.", 50000

    async def boom(*a, **k):
        raise AssertionError("فاکتورِ جدید نباید ساخته شود")
    monkeypatch.setattr(TP, "cancel_at_gateway", cag)
    monkeypatch.setattr(TP, "settle_charge", settle)
    monkeypatch.setattr(TP, "_create_invoice", boom)
    cb = Cb("tonp:cnew:tpOLD:70000")
    await TP.tonp_cancel_and_new(cb, object())
    assert "شارژ شد" in cb.message.sent[-1][0]
    async with maker() as s:
        assert (await s.scalar(select(models.TonPaysCharge.status))) == "waiting"


async def test_pending_after_cancel_does_not_loop(db, monkeypatch):
    maker, bid = db

    async def create(*a, **k):
        raise TP.TonPaysError("x", "PENDING_INVOICE_EXISTS", 409)
    monkeypatch.setattr(TP, "create_invoice", create)

    async def cm(_bid):
        return False
    monkeypatch.setattr(TP, "is_custom_mode", cm)
    msg = Msg()
    await TP._create_invoice(object(), SimpleNamespace(id=bid), msg, USER, 70000, after_cancel=True)
    assert "reply_markup" not in msg.sent[-1][1]


async def test_need_action_hint_shows_remaining_and_excess():
    assert "12,000" in TP.need_action_hint({"remaining_amount": 12000})
    assert "عودت" in TP.need_action_hint({"excess_amount": 5000})
    assert TP.need_action_hint({}) == ""
