"""TonPays: فاکتورِ باز ⇒ «لغو و پرداخت جدید»، مبلغِ کمتر/بیشتر، عودت (PATCH_TONPAYS_PENDING_V1)."""
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


def _err(code="BUYER_HAS_PENDING_INVOICE"):
    return {"detail": {"code": code, "message": "buyer has a pending invoice"}}


async def test_pending_error_is_detected_and_other_errors_are_not(monkeypatch):
    async def req(method, path, key, body=None):
        return 409, _err()
    monkeypatch.setattr(TP, "_request", req)
    with pytest.raises(TP.TonPaysError) as e:
        await TP.create_invoice("K", 50000, "o1", buyer_chat_id=USER)
    assert e.value.code == "BUYER_HAS_PENDING_INVOICE"

    async def req2(method, path, key, body=None):
        return 429, {"detail": {"code": "RATE_LIMIT_EXCEEDED", "message": "slow down"}}
    monkeypatch.setattr(TP, "_request", req2)
    with pytest.raises(TP.TonPaysError) as e2:
        await TP.create_invoice("K", 50000, "o1", buyer_chat_id=USER)
    assert e2.value.code == "RATE_LIMIT_EXCEEDED"


async def test_cancel_uses_documented_paths(monkeypatch):
    calls = []

    async def req(method, path, key, body=None):
        calls.append((method, path, body))
        return 200, {"status": "canceled"}
    monkeypatch.setattr(TP, "_request", req)
    await TP.cancel_invoice("K", "TP-X")
    await TP.cancel_invoice("K", "TP-C", custom=True)
    assert calls == [("POST", "/api/v1/invoices/TP-X/cancel", None),
                     ("POST", "/api/custom/v1/invoices/TP-C/cancel", None)]


async def test_cancel_blocked_for_processing_and_action_required(monkeypatch):
    async def chk(key, inv, custom=False):
        return {"status": "action_required_less", "paid": False}

    async def boom(*a, **k):
        raise AssertionError("نباید لغو شود")
    monkeypatch.setattr(TP, "check_invoice", chk)
    monkeypatch.setattr(TP, "cancel_invoice", boom)
    res, why = await TP.cancel_at_gateway("K", "TP-X")
    assert res == "blocked" and "مبلغِ کمتر" in why


async def test_cancel_already_canceled_counts_as_cancelled(monkeypatch):
    async def chk(key, inv, custom=False):
        raise TP.TonPaysError("x", "RATE_LIMIT_EXCEEDED", 429)

    async def cancel(key, inv, custom=False):
        raise TP.TonPaysError("x", "INVOICE_ALREADY_CANCELED", 409)
    monkeypatch.setattr(TP, "check_invoice", chk)
    monkeypatch.setattr(TP, "cancel_invoice", cancel)
    assert (await TP.cancel_at_gateway("K", "TP-X"))[0] == "cancelled"


async def test_other_buyer_block_codes_are_raised_with_their_code(monkeypatch):
    for code in ("BUYER_HAS_PROCESSING_INVOICE", "BUYER_HAS_NEED_ACTION_LESS",
                 "BUYER_HAS_NEED_ACTION_MORE", "BUYER_INVOICE_BUSY"):
        async def req(method, path, key, body=None, _c=code):
            return 409, _err(_c)
        monkeypatch.setattr(TP, "_request", req)
        with pytest.raises(TP.TonPaysError) as e:
            await TP.create_invoice("K", 50000, "o1", buyer_chat_id=USER, custom=True)
        assert e.value.code == code


async def test_refund_posts_card_without_amount(monkeypatch):
    calls = []

    async def req(method, path, key, body=None):
        calls.append((path, body))
        return 200, {"status": "completed", "paid": True}
    monkeypatch.setattr(TP, "_request", req)
    await TP.refund_card("K", "TP-X", "6037991234567890", "Ali Rezaei")
    assert calls == [("/api/v1/invoices/TP-X/refund",
                      {"invoice_id": "TP-X", "card_number": "6037991234567890", "card_holder": "Ali Rezaei"})]
    assert "amount" not in calls[0][1]


async def test_norm_card_accepts_persian_digits_and_rejects_short():
    assert TP.norm_card("۶۰۳۷-۹۹۱۲ ۳۴۵۶ ۷۸۹۰") == "6037991234567890"
    assert TP.norm_card("6037") == ""


async def test_action_notice_less_shows_remainder_card_and_receipt_button():
    info = {"status": "action_required_less", "remaining_amount": 12000,
            "remaining_card_number": "6219861012345678", "remaining_card_holder": "Shop Card"}
    text, kb = TP.action_notice("tonp", "tpOLD", info, False)
    assert "120,000 ریال" in text and "6219861012345678" in text and "Shop Card" in text
    data = [b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data]
    assert data == ["tonp:rcpt:tpOLD", "tonp:verify:tpOLD"]


async def test_action_notice_more_offers_refund_card():
    text, kb = TP.action_notice("tonp", "tpOLD", {"status": "action_required_more", "refund_amount": 15000}, False)
    assert "15,000" in text
    assert [b.callback_data for r in kb.inline_keyboard for b in r] == ["tonp:rfnd:tpOLD"]


async def test_refund_flow_registers_card_then_settles(db, monkeypatch):
    maker, bid = db
    got = []

    async def rc(key, inv, card, holder, custom=False):
        got.append((inv, card, holder))

    async def settle(b, o):
        return True, "پرداخت تایید و کیف پول شارژ شد.", 50000
    monkeypatch.setattr(TP, "refund_card", rc)
    monkeypatch.setattr(TP, "settle_charge", settle)
    msg = Msg()
    msg.text = "Ali Rezaei"
    msg.from_user = SimpleNamespace(id=USER)
    st = SimpleNamespace(data={"tonp_rfnd_order": "tpOLD", "tonp_rfnd_card": "6037991234567890"})

    async def get_data():
        return dict(st.data)

    async def clear():
        st.data = {}
    st.get_data, st.clear = get_data, clear
    await TP.tonp_refund_holder(msg, object(), st)
    assert got == [("TP-OLD", "6037991234567890", "Ali Rezaei")]
    assert "کارتِ عودت ثبت شد" in msg.sent[-1][0] and "شارژ شد" in msg.sent[-1][0]


async def test_need_action_hint_for_less_more_and_rejected():
    assert "12,000" in TP.need_action_hint({"status": "action_required_less", "remaining_amount": 12000})
    assert "15,000" in TP.need_action_hint({"status": "action_required_more", "refund_amount": 15000})
    assert "دلیل" in TP.need_action_hint({"status": "rejected", "reject_reasons": ["x"]})
    assert TP.need_action_hint({"status": "pending"}) == ""


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
        raise TP.TonPaysError("x", "BUYER_HAS_PENDING_INVOICE", 409)
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
        raise AssertionError("فاکتور جدید نباید ساخته شود")
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
        raise TP.TonPaysError("x", "BUYER_HAS_PENDING_INVOICE", 409)
    monkeypatch.setattr(TP, "create_invoice", create)

    async def cm(_bid):
        return False
    monkeypatch.setattr(TP, "is_custom_mode", cm)
    msg = Msg()
    await TP._create_invoice(object(), SimpleNamespace(id=bid), msg, USER, 70000, after_cancel=True)
    assert msg.sent[-1][1].get("reply_markup") is None


# ═══════════ PATCH_TONPAYS_DOCS_AUDIT_V1 — تطبیق با مستندات رسمی ═══════════

async def test_cancel_at_gateway_result_classes(monkeypatch):
    async def pend(key, inv, custom=False):
        return {"status": "pending", "paid": False}
    monkeypatch.setattr(TP, "check_invoice", pend)
    cases = [(TP.TonPaysError("x", "INVOICE_NOT_FOUND", 404), "cancelled"),
             (TP.TonPaysError("x", "RATE_LIMIT_EXCEEDED", 429), "retry"),
             (TP.TonPaysError("x", "", 502), "retry"),
             (TP.TonPaysError("x", "BUYER_INVOICE_BUSY", 409), "retry"),
             (TP.TonPaysError("x", "INVALID_API_KEY", 401), "error"),
             (OSError("network down"), "retry")]
    for exc, want in cases:
        async def cancel(key, inv, custom=False, _e=exc):
            raise _e
        monkeypatch.setattr(TP, "cancel_invoice", cancel)
        assert (await TP.cancel_at_gateway("K", "TP-X"))[0] == want, (exc, want)

    async def nf(key, inv, custom=False):
        raise TP.TonPaysError("x", "INVOICE_NOT_FOUND", 404)
    monkeypatch.setattr(TP, "check_invoice", nf)                   # فاکتور در درگاه نیست ⇒ چیزی باز نمانده
    assert (await TP.cancel_at_gateway("K", "TP-X"))[0] == "cancelled"


async def test_cancel_and_new_keeps_invoice_open_when_gateway_cancel_failed(db, monkeypatch):
    maker, bid = db

    async def cag(key, inv, custom=False):
        return "retry", "timeout"

    async def boom(*a, **k):
        raise AssertionError("فاکتور جدید نباید ساخته شود")
    monkeypatch.setattr(TP, "cancel_at_gateway", cag)
    monkeypatch.setattr(TP, "_create_invoice", boom)
    cb = Cb("tonp:cnew:tpOLD:70000")
    await TP.tonp_cancel_and_new(cb, object())
    text, kw = cb.message.sent[-1]
    assert "درگاه" in text and "دوباره" in text
    assert [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r] == ["tonp:verify:tpOLD", "tonp:cnew:tpOLD:70000"]
    async with maker() as s:
        assert (await s.scalar(select(models.TonPaysCharge.status))) == "waiting"      # محلی «لغوشده» نشد


async def test_plain_cancel_keeps_invoice_open_when_gateway_cancel_failed(db, monkeypatch):
    maker, bid = db

    async def cag(key, inv, custom=False):
        return "retry", "502"
    monkeypatch.setattr(TP, "cancel_at_gateway", cag)
    cb = Cb("tonp:cancel:tpOLD")
    await TP.tonp_cancel(cb, object())
    assert cb.alerts and "درگاه" in cb.alerts[-1]
    async with maker() as s:
        assert (await s.scalar(select(models.TonPaysCharge.status))) == "waiting"


def test_remainder_notice_hides_receipt_button_after_receipt_was_sent():
    info = {"status": "action_required_less", "need_action_kind": "less", "remaining_amount": 12000,
            "remaining_card_number": "6219861012345678", "remaining_card_holder": "Shop Card",
            "remaining_receipt_received": False}
    text, kb = TP.action_notice("tonp", "tpX", info, False)
    datas = [b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data]
    assert "tonp:rcpt:tpX" in datas and "tonp:verify:tpX" in datas and "۱۲" not in text and "120,000 ریال" in text
    text2, kb2 = TP.action_notice("tonp", "tpX", {**info, "remaining_receipt_received": True}, False)
    datas2 = [b.callback_data for r in kb2.inline_keyboard for b in r if b.callback_data]
    assert "tonp:rcpt:tpX" not in datas2 and "tonp:verify:tpX" in datas2 and "فرستاده‌ای" in text2


def test_documented_error_codes_have_farsi_text():
    for code in ("INVOICE_ID_MISMATCH", "BUYER_HAS_PENDING_INVOICE", "BUYER_HAS_PROCESSING_INVOICE",
                 "BUYER_HAS_NEED_ACTION_LESS", "BUYER_HAS_NEED_ACTION_MORE", "BUYER_INVOICE_BUSY",
                 "CANCEL_NOT_ALLOWED_PROCESSING", "CANCEL_NOT_ALLOWED_NEED_ACTION_LESS",
                 "CANCEL_NOT_ALLOWED_NEED_ACTION_MORE", "INVOICE_ALREADY_CANCELED", "INVOICE_EXPIRED",
                 "REFUND_NOT_ALLOWED", "REFUND_AMOUNT_UNKNOWN", "INVALID_REFUND_CARD", "INVALID_REFUND_HOLDER",
                 "REFUND_ALREADY_OPEN", "NOT_PENDING", "REMAINDER_INVOICE_MISSING", "EMPTY_RECEIPT",
                 "INVALID_RECEIPT_TYPE", "RECEIPT_TOO_LARGE", "RATE_LIMIT_EXCEEDED", "INVOICE_NOT_FOUND"):
        assert TP.ERROR_FA.get(code), code


async def test_platform_bot_keeps_invoice_open_when_gateway_cancel_failed(monkeypatch):
    import platform_tonpays as PT
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(PT, "Session", maker)
    async with maker() as s:
        s.add(models.PlatformTonPaysCharge(user_telegram_id=USER, order_id="ptOLD", invoice_id="TP-OLD",
                                           request_amount=50000, final_amount=50037, status="waiting"))
        await s.commit()

    async def cag(key, inv, custom=False):
        return "retry", "502"

    async def gk(custom):
        return "KEY"

    async def boom(*a, **k):
        raise AssertionError("فاکتور جدید نباید ساخته شود")
    monkeypatch.setattr(PT, "cancel_at_gateway", cag)
    monkeypatch.setattr(PT, "_get_key", gk)
    monkeypatch.setattr(PT, "_create_invoice", boom)
    cb = Cb("ptonp:cnew:ptOLD:70000")
    await PT.ptonp_cancel_and_new(cb, object())
    text, kw = cb.message.sent[-1]
    assert "درگاه" in text and "ptonp:cnew:ptOLD:70000" in str(kw["reply_markup"])
    cb2 = Cb("ptonp:cancel:ptOLD")
    await PT.ptonp_cancel(cb2)
    assert cb2.alerts and "درگاه" in cb2.alerts[-1]
    async with maker() as s:
        assert (await s.scalar(select(models.PlatformTonPaysCharge.status))) == "waiting"
    await engine.dispose()
