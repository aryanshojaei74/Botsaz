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

    async def _np(bot_id, tg, amount, bal, *a, **k):
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


@pytest.mark.asyncio
async def test_guide_pages(db, monkeypatch):
    import tenant.gateways.custom_domain as cd
    monkeypatch.setattr(cd, "Session", db[0])
    from tenant.gateways import apay_admin as AA
    _t, kb = await AA._menu_content(5)
    flat = [b for r in kb.inline_keyboard for b in r]
    assert any(b.url == "https://t.me/APayGateBot" for b in flat)
    assert any(b.callback_data == "ap:guide" for b in flat)
    _t, kb = await AA._guide_content(5, "")
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "ap:guide:android" in cbs and "ap:guide:ios" in cbs
    t, kb = await AA._guide_content(5, "android")
    assert any(b.url == AA.APK_URL for r in kb.inline_keyboard for b in r)
    t, _kb = await AA._guide_content(5, "ios")
    assert "hook/sms/KEY" in t and "Shortcuts" in t


@pytest.mark.asyncio
async def test_guide_uses_site_links(db, monkeypatch):
    import tenant.gateways.custom_domain as cd
    monkeypatch.setattr(cd, "Session", db[0])
    from tenant.gateways import apay_admin as AA

    async def links():
        return {"ios": "https://www.icloud.com/shortcuts/abc", "apk": "https://apaygate.shop/download/x.apk"}
    monkeypatch.setattr(AP, "site_links", links)
    t, kb = await AA._guide_content(5, "ios")
    urls = [b.url for r in kb.inline_keyboard for b in r]
    assert "https://www.icloud.com/shortcuts/abc" in urls and "Add Shortcut" in t
    t, kb = await AA._guide_content(5, "android")
    assert "https://apaygate.shop/download/x.apk" in [b.url for r in kb.inline_keyboard for b in r]
    assert "Install anyway" in t


@pytest.mark.asyncio
async def test_site_links_parse_and_cache(monkeypatch):
    body = 'window.APAY={"bot":"https://t.me/APayGateBot","ios":"https://www.icloud.com/shortcuts/z","apk":"/download/apay-forwarder.apk"};'
    calls = []

    class R:
        status = 200
        async def text(self): return body
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class S:
        def __init__(self, *a, **k): pass
        def get(self, url):
            calls.append(url)
            return R()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(AP.aiohttp, "ClientSession", S)
    monkeypatch.setattr(AP, "_SITE_CACHE", {"at": 0.0, "data": {}})
    d = await AP.site_links()
    assert d == {"ios": "https://www.icloud.com/shortcuts/z",
                 "apk": "https://apaygate.shop/download/apay-forwarder.apk"}
    await AP.site_links()
    assert calls == ["https://apaygate.shop/config.js"]       # کش


def test_card_info_shapes():
    assert AP.card_info({"card_number": "5047-0611-3505-6063", "card_holder": "آریان شجاعی"}) == \
        ("5047061135056063", "آریان شجاعی")
    assert AP.card_info({"card": {"number": "5047061135056063", "name": "علی"}, "name": "shop"}) == \
        ("5047061135056063", "علی")
    assert AP.card_info({"card": "5047 0611 3505 6063"}) == ("5047061135056063", "")
    assert AP.card_info({"id": "pay_1", "name": "MyShop"}) == ("", "")   # «name» ِ سطحِ اول نامِ فروشگاه است


def test_invoice_view_layout():
    t, kb = AP.invoice_view(title="آپی", pay_rial=100_610, card="5047061135056063", holder="آریان شجاعی",
                            order_id="bs1", pay_url="https://apaygate.shop/p/x",
                            check_cb="apu:check:bs1", cancel_cb="apu:cancel:bs1")
    assert "مبلغِ شارژ" not in t and "<code>100610</code>" in t and "10,061 تومان" in t
    assert "<code>5047061135056063</code>" in t and "آریان شجاعی" in t
    copies = [b.copy_text.text for r in kb.inline_keyboard for b in r if b.copy_text]
    assert copies == ["5047061135056063", "100610"]
    t2, kb2 = AP.invoice_view(title="آپی", pay_rial=100_610, card="", holder="", order_id="bs1",
                              pay_url="https://apaygate.shop/p/x", check_cb="c", cancel_cb="x")
    assert [b.copy_text.text for r in kb2.inline_keyboard for b in r if b.copy_text] == ["100610"]
    assert "«💳 پرداخت» را بزن" in t2


@pytest.mark.asyncio
async def test_card_for_reads_public_endpoint(monkeypatch):
    """PATCH_APAY_PUBLIC_CARD_V1 — شکلِ واقعیِ پاسخِ /api/v1/public/payments/:id روی سرور."""
    pub = {"id": "pay_x", "pay_amount": 100590, "fee": 15000, "status": "pending",
           "card_number": "5047 0611 3505 6063", "card_holder": "آریان شجاعی", "merchant": "BotSaz"}
    calls = []

    class R:
        status = 200
        async def json(self, content_type=None): return pub
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class S:
        def __init__(self, *a, **k): pass
        def get(self, url):
            calls.append(url)
            return R()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(AP.aiohttp, "ClientSession", S)
    api_res = {"id": "pay_x", "amount": 100000, "pay_amount": 100590, "pay_url": "u", "status": "pending"}
    assert await AP.card_for("tok", api_res) == ("5047061135056063", "آریان شجاعی")
    assert calls == ["https://apaygate.shop/api/v1/public/payments/pay_x"]


@pytest.mark.asyncio
async def test_fast_watch_settles_when_public_status_paid(db, monkeypatch):
    """PATCH_APAY_FAST_WATCH_V1 — پایشِ ۵ثانیه‌ای: با «paid» تسویه و توقف؛ با بسته‌شدنِ فاکتور توقف."""
    import asyncio
    maker, notified = db
    monkeypatch.setattr(AP, "WATCH_EVERY", 0.01)
    seq = iter(["pending", "pending", "paid"])

    async def ps(pid):
        return next(seq, "paid")

    async def v(token, pid):
        return _verify_resp()
    monkeypatch.setattr(AP, "public_status", ps)
    monkeypatch.setattr(AP, "verify_payment", v)
    AP.watch_tenant_charge(5, "bsabc", "pay_1")
    for _ in range(100):
        await asyncio.sleep(0.02)
        if not AP._WATCH_TASKS:
            break
    assert (await _balance(maker))[:2] == (50_000, "paid")
    assert notified == [(777, 50_000, 50_000)]


@pytest.mark.asyncio
async def test_buyer_pays_fee_settles_on_total_credits_amount(db, monkeypatch):
    """PATCH_APAY_FEE_PAYER_V1 — کارمزد با خریدار: آپی مبلغ+کارمزد، کیف پول فقط مبلغ."""
    maker, notified = db
    async with maker() as s:
        ch = await s.scalar(select(models.ApayCharge))
        ch.fee_toman = 1_500
        await s.commit()

    async def v_wrong(token, pid):
        return _verify_resp(amount=500_000)              # بدونِ کارمزد → رد
    monkeypatch.setattr(AP, "verify_payment", v_wrong)
    assert not (await AP.settle_charge(5, "bsabc"))[0]

    async def v(token, pid):
        return _verify_resp(amount=515_000)              # (50,000 + 1,500) × 10
    monkeypatch.setattr(AP, "verify_payment", v)
    ok, _m, amt = await AP.settle_charge(5, "bsabc")
    assert ok and amt == 50_000
    assert (await _balance(maker))[:2] == (50_000, "paid")


@pytest.mark.asyncio
async def test_fee_cfg_and_invoice_line(db):
    assert await AP.get_fee_cfg(5) == ("seller", AP.FEE_TOMAN)
    await tenant.set_setting(5, "apay_fee_payer", "buyer")
    await tenant.set_setting(5, "apay_fee_amount", "2000")
    assert await AP.get_fee_cfg(5) == ("buyer", 2000)
    assert AP.fee_for_buyer("buyer", 2000) == 2000 and AP.fee_for_buyer("seller", 2000) == 0
    t, _kb = AP.invoice_view(title="آپی", pay_rial=520_340, card="", holder="", order_id="x",
                             pay_url="https://p", check_cb="c", cancel_cb="x",
                             fee_toman=2000, credit_toman=50_000)
    assert "2,000" in t and "50,000" in t and "کارمزدِ درگاه" in t


# === PATCH_APAY_ADMIN_ASK_V1 ===
@pytest.mark.asyncio
async def test_tenant_admin_ask_and_manual_settle(db, monkeypatch):
    from datetime import datetime, timedelta
    maker, notified = db
    sent = []

    async def _so(brow, text, kb):
        sent.append((brow.owner_id, text, kb))
    monkeypatch.setattr(AP, "_send_owner", _so)
    assert await AP.ask_admin_pending() == 0
    async with maker() as s:
        ch = await s.scalar(select(models.ApayCharge))
        ch.created_at = datetime.utcnow() - timedelta(minutes=6)
        ch.status = "cancelled"
        cid = ch.id
        await s.commit()
    assert await AP.ask_admin_pending() == 0               # لغوِ خودِ کاربر → از ادمین نمی‌پرسد
    async with maker() as s:
        ch = await s.get(models.ApayCharge, cid)
        ch.status = "waiting"
        await s.commit()
    assert await AP.ask_admin_pending() == 1 and await AP.ask_admin_pending() == 0
    assert sent[0][0] == 1 and "50,000" in sent[0][1] and "500370" in sent[0][1]
    assert sent[0][2].inline_keyboard[0][0].callback_data == f"apadm:ok:{cid}"
    assert not (await AP.manual_settle(6, cid, 1))[0]          # ربات دیگر
    assert (await AP.manual_settle(5, cid, 1))[0]
    assert not (await AP.manual_settle(5, cid, 1))[0]
    assert await _balance(maker) == (50_000, "paid", 1) and notified == [(777, 50_000, 50_000)]


# === PATCH_APAY_ASK_CLEANUP_V1 ===
@pytest.mark.asyncio
async def test_tenant_auto_settle_deletes_owner_ask_message(db, monkeypatch):
    maker, _n = db
    dropped = []

    class Sent:
        message_id = 555

    async def _so(brow, text, kb):
        return Sent()

    async def _drop(bot_id, chat, msg):
        dropped.append((bot_id, chat, msg))
    monkeypatch.setattr(AP, "_send_owner", _so)
    monkeypatch.setattr(AP, "_drop_admin_ask", _drop)
    from datetime import datetime, timedelta
    async with maker() as s:
        ch = await s.scalar(select(models.ApayCharge))
        ch.created_at = datetime.utcnow() - timedelta(minutes=6)
        await s.commit()
    assert await AP.ask_admin_pending() == 1

    async def _v(token, pid):
        return _verify_resp()
    monkeypatch.setattr(AP, "verify_payment", _v)
    assert (await AP.settle_charge(5, "bsabc"))[0]
    assert dropped == [(5, 1, 555)]


# === PATCH_APAY_NO_WEBHOOK_UI_V1 — مشتری مستأجر دیگر «وب‌هوک» نمی‌بیند ===
@pytest.mark.asyncio
async def test_tenant_apay_settings_and_guide_have_no_webhook_setup():
    from tenant.gateways import apay_admin as AA
    import tenant.gateways.apay as AP2
    guide_t, _ = await AA.guide_content("", "https://x/apay/webhook/5", "ap")
    guide_p, _ = await AA.guide_content("", "https://x/apay/platform/webhook", "papay")
    assert "نیازی به ثبت وب‌هوک" in guide_t and "کلید وب‌هوک" not in guide_t and "https://x/apay/webhook/5" not in guide_t
    assert "کلید وب‌هوک" in guide_p and "https://x/apay/platform/webhook" in guide_p      # صفحهٔ خود مدیر بدون تغییر
    import inspect
    src = inspect.getsource(AA._menu_content)
    assert "ap:setsecret" not in src and "آدرس وب‌هوک" not in src and "نیازی به ثبت وب‌هوک" in src
    assert AP2.webhook_url is not None                                                   # سازوکار فنی (callback) سرجایش است
