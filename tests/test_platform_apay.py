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


# === PATCH_APAY_ASK_CLEANUP_V1 ===
@pytest.mark.asyncio
async def test_auto_settle_deletes_admin_ask_message(db, monkeypatch):
    maker, _st, paid = db
    deleted = []

    async def drop(chat, msg):
        deleted.append((chat, msg))
    monkeypatch.setattr(PA, "_drop_admin_ask", drop)
    async with maker() as s:
        ch = await s.scalar(select(models.PlatformApayCharge))
        ch.admin_ask_chat, ch.admin_ask_msg = 777, 4321
        await s.commit()

    async def v(tok, pid):
        return _vr()
    monkeypatch.setattr(AP, "verify_payment", v)
    assert (await PA.settle_charge("pbx1"))[0]
    assert deleted == [(777, 4321)]


@pytest.mark.asyncio
async def test_partner_manual_confirm_approves_order_not_wallet(partner, monkeypatch):
    maker, _sent = partner
    ok, _m = await PA.manual_settle(2, 777)         # id=2: شارژِ همکار (اولی کیف پول است)
    async with maker() as s:
        o = await s.get(models.PartnerStockOrder, 7)
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == 42))
    assert ok and o.status == "approved" and float(u.balance) == 0
    assert not (await PA.manual_settle(2, 777))[0]


@pytest.mark.asyncio
async def test_no_admin_ask_when_user_cancelled(db, monkeypatch):
    """PATCH_APAY_NO_ASK_ON_CANCEL_V1 — کاربر خودش لغو کرده؛ دیگر «واریز شد؟» به ادمین نمی‌رود."""
    maker, _st, _p = db
    import sys, types
    sent = []

    class FakeBot:
        async def send_message(self, chat, text, reply_markup=None):
            sent.append(chat)
    monkeypatch.setitem(sys.modules, "main_platform", types.SimpleNamespace(platform_bot=FakeBot()))
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 777)
    async with maker() as s:
        ch = await s.scalar(select(models.PlatformApayCharge))
        ch.created_at = datetime.utcnow() - timedelta(minutes=6)
        ch.status = "cancelled"
        await s.commit()
    assert await PA.ask_admin_pending() == 0 and not sent


# ═══════════ PATCH_APAY_ONE_OPEN_INVOICE_V1 — یک فاکتور باز؛ پیام «واریز شد؟»ی مدیر بعد از لغو/انقضا پاک می‌شود ═══════════

def _fake_msg(sent):
    async def answer(text, reply_markup=None, **kw):
        sent.append((text, reply_markup))
        return types_ns(message_id=900 + len(sent), chat=types_ns(id=42))
    return types_ns(answer=answer)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


@pytest.mark.asyncio
async def test_open_invoice_detection(db):
    maker, _st, _p = db
    assert (await PA.open_invoice(42)).order_id == "pbx1"                      # waiting و تازه
    assert await PA.open_invoice(43) is None                                   # کاربر دیگر
    async with maker() as s:
        ch = await s.scalar(select(models.PlatformApayCharge))
        ch.created_at = datetime.utcnow() - timedelta(minutes=PA.OPEN_INVOICE_MIN + 1)
        await s.commit()
    assert await PA.open_invoice(42) is None                                   # از مهلت اعتبار گذشته
    for status in ("paid", "cancelled", "expired"):
        async with maker() as s:
            ch = await s.scalar(select(models.PlatformApayCharge))
            ch.created_at, ch.status = datetime.utcnow(), status
            await s.commit()
        assert await PA.open_invoice(42) is None, status
    async with maker() as s:                                                   # خرید عمدهٔ همکار (ربات دیگر) مانع نیست
        ch = await s.scalar(select(models.PlatformApayCharge))
        ch.status, ch.purpose = "waiting", "partner_order"
        await s.commit()
    assert await PA.open_invoice(42) is None


@pytest.mark.asyncio
async def test_second_invoice_is_blocked_with_pay_or_cancel_message(db, monkeypatch):
    maker, _st, _p = db
    calls = []

    async def create(*a, **k):
        calls.append(1)
        return 201, {"id": "pay_n", "pay_url": "https://pay/x", "pay_amount": 1_000_000}
    monkeypatch.setattr(AP, "create_payment", create)
    sent = []
    await PA._make_invoice(_fake_msg(sent), 42, 50_000)
    assert calls == [] and len(sent) == 1                                      # فاکتور جدیدی ساخته نشد
    text, kb = sent[0]
    assert "فاکتور باز" in text and "100,000" in text and "پرداخت کن" in text and "لغوش کن" in text
    datas = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert datas == ["papay:check:pbx1", "papay:cancel:pbx1"]


@pytest.mark.asyncio
async def test_new_invoice_allowed_after_cancel_or_pay_and_concurrent_taps_make_one(db, monkeypatch):
    import asyncio
    maker, _st, _p = db
    calls = []

    async def create(*a, **k):
        calls.append(1)
        await asyncio.sleep(0.05)
        return 201, {"id": f"pay_{len(calls)}", "pay_url": "https://pay/x", "pay_amount": 1_000_000}

    async def card_for(tok, res):
        return "6037", "holder"
    monkeypatch.setattr(AP, "create_payment", create)
    monkeypatch.setattr(AP, "card_for", card_for)
    monkeypatch.setattr(AP, "invoice_view", lambda **k: ("invoice", None))
    monkeypatch.setattr(AP, "spawn_watch", lambda *a, **k: None)
    async with maker() as s:                                                   # فاکتور قبلی لغو شده
        (await s.scalar(select(models.PlatformApayCharge))).status = "cancelled"
        await s.commit()
    sent = []
    await asyncio.gather(PA._make_invoice(_fake_msg(sent), 42, 50_000), PA._make_invoice(_fake_msg(sent), 42, 50_000))
    assert len(calls) == 1                                                     # دو کلیک هم‌زمان = فقط یک فاکتور
    texts = [t for t, _k in sent]
    assert any("فاکتور باز" in t for t in texts) and any(t == "invoice" for t in texts)
    async with maker() as s:
        n = len((await s.scalars(select(models.PlatformApayCharge).where(
            models.PlatformApayCharge.status == "waiting"))).all())
    assert n == 1


@pytest.mark.asyncio
async def test_charge_entry_point_blocks_before_asking_amount(db, monkeypatch):
    sent = []
    cb = types_ns(data="charge_papay", from_user=types_ns(id=42), message=_fake_msg(sent), answer=_noop_answer)
    state = types_ns(clear=_noop_answer)
    await PA.charge_papay(cb, state)
    assert len(sent) == 1 and "فاکتور باز" in sent[0][0] and "مبلغ شارژ را انتخاب کن" not in sent[0][0]


async def _noop_answer(*a, **k):
    return None


@pytest.mark.asyncio
async def test_cancel_removes_admins_pending_ask_and_buttons_of_original_invoice(db, monkeypatch):
    maker, _st, _p = db
    dropped, edited = [], []

    async def drop(chat, msg):
        dropped.append((chat, msg))
    monkeypatch.setattr(PA, "_drop_admin_ask", drop)
    async with maker() as s:
        ch = await s.scalar(select(models.PlatformApayCharge))
        ch.admin_ask_chat, ch.admin_ask_msg, ch.chat_id, ch.msg_id = 777, 4321, 42, 500
        await s.commit()

    class B:
        async def edit_message_reply_markup(self, **k):
            edited.append(k)
    answers = []

    async def answer(t="", **k):
        answers.append(t)

    async def mdl_answer(t, **k):
        answers.append(t)

    async def edit_rm(reply_markup=None):
        answers.append("edit_rm")
    cb = types_ns(data="papay:cancel:pbx1", from_user=types_ns(id=42), answer=answer, bot=B(),
                  message=types_ns(message_id=999, answer=mdl_answer, edit_reply_markup=edit_rm))   # لغو از پیام «فاکتور باز دارید»
    await PA.papay_cancel(cb)
    assert dropped == [(777, 4321)]
    assert edited and edited[0]["chat_id"] == 42 and edited[0]["message_id"] == 500
    async with maker() as s:
        assert (await s.scalar(select(models.PlatformApayCharge))).status == "cancelled"


@pytest.mark.asyncio
async def test_expiry_removes_unanswered_ask_but_answered_ask_is_kept(db, monkeypatch):
    maker, _st, _p = db
    dropped = []

    async def notify(oid, chat, msg):                       # PATCH_APAY_EXPIRY_NOTICE_V1: دیگر پاک نمی‌شود؛ «منقضی شد» اعلام می‌شود
        dropped.append((oid, chat, msg))
    monkeypatch.setattr(PA, "notify_expired", notify)
    async with maker() as s:
        s.add(models.PlatformApayCharge(user_telegram_id=42, order_id="pbold", payment_id="pay_o", amount_toman=50_000,
                                        status="waiting", created_at=datetime.utcnow() - timedelta(minutes=45),
                                        admin_ask_chat=777, admin_ask_msg=111))
        ch = await s.scalar(select(models.PlatformApayCharge).where(models.PlatformApayCharge.order_id == "pbx1"))
        ch.status = "cancelled"
        await s.commit()

    async def v(tok, pid):
        return 200, {"verified": False, "payment": {"status": "expired"}}
    monkeypatch.setattr(AP, "verify_payment", v)
    await PA.poll_pending_once()
    assert dropped == [("pbold", 777, 111)]                                   # منقضی‌شده و پرداخت‌نشده ⇒ به کاربر/مدیر اعلام می‌شود

    # مدیر جواب بدهد ⇒ شناسه‌ها فراموش می‌شوند و پیام نتیجه هرگز پاک نمی‌شود
    async with maker() as s:
        s.add(models.PlatformApayCharge(user_telegram_id=42, order_id="pbans", payment_id="pay_a", amount_toman=1000,
                                        status="waiting", admin_ask_chat=777, admin_ask_msg=222))
        await s.commit()
        cid = (await s.scalar(select(models.PlatformApayCharge).where(models.PlatformApayCharge.order_id == "pbans"))).id

    async def has_perm(*a, **k):
        return True
    monkeypatch.setattr(PB, "has_perm", has_perm)
    edits = []

    async def edit_text(t, reply_markup=None):
        edits.append(t)

    async def answer(*a, **k):
        return None
    cb = types_ns(data=f"papadm:no:{cid}", from_user=types_ns(id=777), answer=answer,
                  message=types_ns(html_text="پرسش", edit_text=edit_text))
    await PA.papay_admin_answer(cb)
    async with maker() as s:
        ch = await s.get(models.PlatformApayCharge, cid)
    assert ch.admin_ask_chat is None and ch.admin_ask_msg is None and "واریز نشده" in edits[0]


# ───────────── تأییدِ دستیِ آپی / هشدارِ «paid ولی شارژ نشد» / اعلامِ انقضا ─────────────
@pytest.mark.asyncio
async def test_manual_seller_confirmation_without_sms_is_credited(db, monkeypatch):
    """مدیر در رباتِ آپی «تأییدِ دستی (بدونِ پیامک)» کرد: status=paid ولی verified=false ⇒ باید شارژ شود."""
    maker, _st, paid = db

    async def v(tok, pid):
        return _vr(verified=False)
    monkeypatch.setattr(AP, "verify_payment", v)
    ok, _m, credit = await PA.settle_charge("pbx1")
    assert ok and credit == 100_000
    assert await _state(maker) == (100_000, "paid", 1) and paid == [(42, 100_000, 100_000)]


@pytest.mark.asyncio
async def test_paid_but_mismatch_alerts_admin_once_with_charge_button(db, monkeypatch):
    maker, _st, paid = db
    import config
    import sys
    import types
    sent = []

    class Bot:
        async def send_message(self, chat, text, reply_markup=None, **k):
            sent.append((chat, text, reply_markup))
    monkeypatch.setitem(sys.modules, "main_platform", types.SimpleNamespace(platform_bot=Bot()))
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 777)
    PA._BLOCK_ALERTED.clear()

    async def v(tok, pid):
        return _vr(amount=9_999_990)
    monkeypatch.setattr(AP, "verify_payment", v)
    assert not (await PA.settle_charge("pbx1"))[0]
    assert not (await PA.settle_charge("pbx1"))[0]                      # دوباره ⇒ پیامِ تکراری نمی‌رود
    assert len(sent) == 1 and sent[0][0] == 777
    assert "paid" in sent[0][1] and "نمی‌خواند" in sent[0][1] and "pbx1" in sent[0][1]
    cbs = [b.callback_data for r in sent[0][2].inline_keyboard for b in r]
    assert any(c.startswith("papadm:ok:") for c in cbs) and await _state(maker) == (0, "waiting", 0)


@pytest.mark.asyncio
async def test_expiry_tells_user_and_turns_admin_question_into_expired_notice(db, monkeypatch):
    maker, _st, _p = db
    import config
    import sys
    import types
    sent, edits, markups = [], [], []

    class Bot:
        async def send_message(self, chat, text, reply_markup=None, **k):
            sent.append((chat, text, reply_markup))

        async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None, **k):
            edits.append((chat_id, message_id, text, reply_markup))

        async def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None, **k):
            markups.append((chat_id, message_id))
    monkeypatch.setitem(sys.modules, "main_platform", types.SimpleNamespace(platform_bot=Bot()))
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 777)
    async with maker() as s:
        ch = await s.scalar(select(models.PlatformApayCharge))
        ch.created_at = datetime.utcnow() - timedelta(minutes=45)
        ch.chat_id, ch.msg_id, ch.admin_ask_chat, ch.admin_ask_msg = 42, 9, 777, 555
        await s.commit()

    async def v(tok, pid):
        return 200, {"verified": False, "payment": {"status": "expired"}}
    monkeypatch.setattr(AP, "verify_payment", v)
    await PA.poll_pending_once()
    assert (await _state(maker))[1] == "expired"
    user_msg = [t for c, t, _k in sent if c == 42]
    assert user_msg and "منقضی شد" in user_msg[0] and "pbx1" in user_msg[0]       # به کاربر گفته شد
    assert markups == [(42, 9)]                                                    # دکمه‌هایِ فاکتورِ مرده برداشته شد
    assert edits and edits[0][:2] == (777, 555) and "منقضی شد" in edits[0][2]     # پرسشِ مدیر «منقضی شد» شد، پاک نشد
    assert any(b.callback_data.startswith("papadm:ok:") for r in edits[0][3].inline_keyboard for b in r)


@pytest.mark.asyncio
async def test_admin_who_pays_is_also_asked(db, monkeypatch):
    """مدیر با حسابِ خودش تست می‌کند: قبلاً «واریز شد؟» برایش نمی‌آمد."""
    maker, _st, _p = db
    import config
    import sys
    import types
    sent = []

    class Bot:
        async def send_message(self, chat, text, reply_markup=None, **k):
            sent.append(chat)
            return types_ns(message_id=5)
    monkeypatch.setitem(sys.modules, "main_platform", types.SimpleNamespace(platform_bot=Bot()))
    monkeypatch.setattr(config, "PLATFORM_ADMIN_ID", 42)               # پرداخت‌کننده = مدیر
    async with maker() as s:
        ch = await s.scalar(select(models.PlatformApayCharge))
        ch.created_at = datetime.utcnow() - timedelta(minutes=AP.ASK_AFTER_MIN + 1)
        await s.commit()
    assert await PA.ask_admin_pending() == 1 and sent == [42]
