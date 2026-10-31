"""PATCH_HOOSHPAY_GATEWAY_V1 — درگاهِ کارت‌به‌کارتِ آنیِ هوش‌پی (سرورِ aiohttpِ محلی که قراردادِ مستنداتِ رسمی را شبیه‌سازی می‌کند)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.gateways.hooshpay_admin as HA
from tenant.gateways import hooshpay as HP

KEY = "hp_live_testkey123456"


class FakeHooshPay:
    def __init__(self):
        self.invoices = {}
        self.calls = []
        self.n = 0
        self.fee_pct = 20

    def app(self):
        a = web.Application(middlewares=[self.mw])
        a.router.add_get("/api/v1/account", self.account)
        a.router.add_post("/api/v1/invoices", self.create)
        a.router.add_get("/api/v1/invoices/{uid}", self.get)
        a.router.add_post("/api/v1/invoices/{uid}/verify", self.verify)
        a.router.add_post("/api/v1/invoices/{uid}/cancel", self.cancel)
        return a

    @web.middleware
    async def mw(self, request, handler):
        self.calls.append((request.method, request.path))
        if request.headers.get("X-API-KEY") != KEY:
            return web.json_response({"success": False, "message": "invalid key"}, status=401)
        return await handler(request)

    async def account(self, r):
        return web.json_response({"success": True, "data": {"name": "shop"}})

    async def create(self, r):
        b = await r.json()
        if b["amount"] < 1000:
            return web.json_response({"success": False, "message": "min 1000"}, status=400)
        self.n += 1
        uid = f"inv_{self.n}"
        fee = b["amount"] * self.fee_pct // 100
        mode = b.get("fee_mode", "seller")
        payable = b["amount"] + (fee if mode == "buyer" else fee // 2 if mode == "split" else 0) + 17
        self.invoices[uid] = {"uid": uid, "amount": b["amount"], "fee_mode": mode, "order_id": b.get("order_id"),
                              "status": "pending", "payable_amount": payable, "fee_amount": fee}
        return web.json_response({"success": True, "data": {**self.invoices[uid], "payment_url": f"https://hooshpay.xyz/pay/{uid}"}})

    async def get(self, r):
        inv = self.invoices.get(r.match_info["uid"])
        if not inv:
            return web.json_response({"success": False, "message": "nf"}, status=404)
        return web.json_response({"success": True, "data": inv})

    async def verify(self, r):
        inv = self.invoices.get(r.match_info["uid"])
        if not inv:
            return web.json_response({"success": False, "message": "nf"}, status=404)
        return web.json_response({"success": True, "paid": inv["status"] == "paid", "status": inv["status"],
                                  "data": {**inv, "tracking_code": "556677" if inv["status"] == "paid" else None}})

    async def cancel(self, r):
        inv = self.invoices.get(r.match_info["uid"])
        if not inv or inv["status"] != "pending":
            return web.json_response({"success": False, "message": "not pending"}, status=400)
        inv["status"] = "cancelled"
        return web.json_response({"success": True})


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, HP, HA):
        monkeypatch.setattr(mod, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="A", balance=0))
        await s.commit()
    fp = FakeHooshPay()
    runner = web.AppRunner(fp.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(HP, "BASE_URL", f"http://127.0.0.1:{port}/api/v1")
    await tenant.set_setting(5, "hooshpay_api_key", KEY)
    await tenant.set_setting(5, "hooshpay_enabled", "1")
    notified = []

    async def _np(bot_id, tg, amount, bal, *a, **k):
        notified.append((tg, amount, bal))

    async def _noop(*a, **k):
        return None
    import tenant.gateways.uniquepay as UP
    monkeypatch.setattr(UP, "_notify_paid", _np)
    import tenant.gateways.payment_page as pp
    monkeypatch.setattr(pp, "_tx_log_tenant", _noop)
    import tenant.shared.kyc as kyc
    monkeypatch.setattr(kyc, "after_charge", _noop)
    yield maker, fp, notified
    await runner.cleanup()
    await engine.dispose()


async def _state(maker):
    async with maker() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 777))
        ch = (await s.scalars(select(models.HooshPayCharge).order_by(models.HooshPayCharge.id))).all()
        txns = (await s.scalars(select(models.WalletTxn))).all()
        return float(u.balance), [c.status for c in ch], [t.kind for t in txns]


class Msg:
    def __init__(self, text, uid=777):
        self.text = text
        self.from_user = SimpleNamespace(id=uid, full_name="A", username="a")
        self.out = []

    async def answer(self, text, **kw):
        self.out.append((text, kw.get("reply_markup")))


class St:
    async def clear(self): pass
    async def set_state(self, *a, **k): pass


@pytest.mark.asyncio
async def test_validate_key_and_errors(env):
    ok, why = await HP.validate_key(KEY)
    assert ok
    ok, why = await HP.validate_key("hp_live_wrong")
    assert not ok and "کلید" in why
    res = await HP.create_invoice(KEY, 500, "x", "buyer")
    assert not res["success"] and "ورودی" in HP._err(res)


@pytest.mark.asyncio
async def test_amount_flow_creates_invoice_with_buyer_fee_and_shows_payable(env, monkeypatch):
    maker, fp, notified = env
    import tenant as T

    async def fa(t):
        return int(str(t).replace(",", ""))

    async def row(bot):
        return SimpleNamespace(id=5, username="x", owner_id=1)
    monkeypatch.setattr(T, "fa_to_int", lambda t: int(str(t).replace(",", "")))
    monkeypatch.setattr(T, "get_bot_row", row)
    import tenant.shared.kyc as kyc

    async def okc(*a, **k):
        return True, ""
    monkeypatch.setattr(kyc, "check_charge", okc)
    m = Msg("50000")
    await HA.hou_amount(m, SimpleNamespace(), St())
    text, kb = m.out[-1]
    assert "مبلغِ قابلِ پرداخت: 60,017" in text and "50,000" in text and "10,017" in text      # buyer: +۲۰٪ +رقمِ یکتا
    assert kb.inline_keyboard[0][0].url == "https://hooshpay.xyz/pay/inv_1"
    inv = fp.invoices["inv_1"]
    assert inv["fee_mode"] == "buyer" and inv["order_id"].startswith("bs")
    assert await _state(maker) == (0.0, ["waiting"], [])
    async with maker() as s:
        ch = await s.scalar(select(models.HooshPayCharge))
    assert ch.invoice_uid == "inv_1" and ch.amount_toman == 50_000 and ch.payable_amount == 60_017
    # کمتر از حداقل
    m2 = Msg("5000")
    await HA.hou_amount(m2, SimpleNamespace(), St())
    assert "حداقل" in m2.out[-1][0] and len(fp.invoices) == 1


@pytest.mark.asyncio
async def test_settle_credits_requested_amount_once_not_payable(env):
    maker, fp, notified = env
    res = await HP.create_invoice(KEY, 50_000, "bsabc", "buyer")
    async with maker() as s:
        s.add(models.HooshPayCharge(bot_id=5, user_telegram_id=777, order_id="bsabc", invoice_uid="inv_1", amount_toman=50_000,
                                    payable_amount=60_017, fee_mode="buyer", status="waiting"))
        await s.commit()
    ok, msg, _ = await HP.settle_charge(5, "bsabc")
    assert not ok and "هنوز" in msg and await _state(maker) == (0.0, ["waiting"], [])
    fp.invoices["inv_1"]["status"] = "paid"
    ok, msg, credit = await HP.settle_charge(5, "bsabc")
    assert ok and credit == 50_000
    assert await _state(maker) == (50_000.0, ["paid"], ["hooshpay_charge"])               # مبلغِ شارژ، نه payable
    ok2, _m, _c = await HP.settle_charge(5, "bsabc")                                      # تکراری
    assert not ok2 and await _state(maker) == (50_000.0, ["paid"], ["hooshpay_charge"])
    assert notified == [(777, 50_000, 50_000.0)]
    async with maker() as s:
        ch = await s.scalar(select(models.HooshPayCharge))
    assert ch.tracking_code == "556677" and ch.paid_at is not None


@pytest.mark.asyncio
async def test_settle_rejects_amount_mismatch_and_marks_expired(env):
    maker, fp, notified = env
    await HP.create_invoice(KEY, 70_000, "bsx", "buyer")
    async with maker() as s:
        s.add(models.HooshPayCharge(bot_id=5, user_telegram_id=777, order_id="bsx", invoice_uid="inv_1", amount_toman=50_000,
                                    status="waiting"))                                    # رکوردِ ما ۵۰٬۰۰۰، فاکتور ۷۰٬۰۰۰
        await s.commit()
    fp.invoices["inv_1"]["status"] = "paid"
    ok, msg, _ = await HP.settle_charge(5, "bsx")
    assert not ok and "یکسان نیست" in msg and await _state(maker) == (0.0, ["waiting"], [])
    await HP.create_invoice(KEY, 50_000, "bsy", "buyer")
    async with maker() as s:
        s.add(models.HooshPayCharge(bot_id=5, user_telegram_id=777, order_id="bsy", invoice_uid="inv_2", amount_toman=50_000,
                                    status="waiting"))
        await s.commit()
    fp.invoices["inv_2"]["status"] = "expired"
    ok, msg, _ = await HP.settle_charge(5, "bsy")
    assert not ok and "تمام شده" in msg and (await _state(maker))[1] == ["waiting", "expired"]


@pytest.mark.asyncio
async def test_cancelled_record_still_settles_if_paid_later(env):
    maker, fp, notified = env
    await HP.create_invoice(KEY, 50_000, "bsz", "buyer")
    async with maker() as s:
        s.add(models.HooshPayCharge(bot_id=5, user_telegram_id=777, order_id="bsz", invoice_uid="inv_1", amount_toman=50_000,
                                    status="cancelled"))
        await s.commit()
    fp.invoices["inv_1"]["status"] = "paid"
    ok, _m, credit = await HP.settle_charge(5, "bsz")
    assert ok and credit == 50_000 and (await _state(maker))[:2] == (50_000.0, ["paid"])


@pytest.mark.asyncio
async def test_poll_pending_settles_and_closes_old_unpaid(env):
    maker, fp, notified = env
    for i, (oid, age) in enumerate((("bs1", 1), ("bs2", 1), ("bs3", 100)), start=1):
        await HP.create_invoice(KEY, 50_000, oid, "buyer")
        async with maker() as s:
            s.add(models.HooshPayCharge(bot_id=5, user_telegram_id=777, order_id=oid, invoice_uid=f"inv_{i}", amount_toman=50_000,
                                        status="waiting", created_at=datetime.utcnow() - timedelta(minutes=age)))
            await s.commit()
    fp.invoices["inv_1"]["status"] = "paid"
    assert await HP.poll_pending() == 1
    assert await _state(maker) == (50_000.0, ["paid", "waiting", "expired"], ["hooshpay_charge"])   # ۱۰۰ دقیقه‌ای بسته شد


@pytest.mark.asyncio
async def test_cancel_handler_credits_if_already_paid_else_cancels_both_sides(env, monkeypatch):
    maker, fp, notified = env
    import tenant as T

    async def row(bot):
        return SimpleNamespace(id=5, username="x", owner_id=1)

    async def back(bid):
        return None
    monkeypatch.setattr(T, "get_bot_row", row)
    monkeypatch.setattr(T, "inline_back_kb", back)
    for i, oid in enumerate(("bsp", "bsq"), start=1):
        await HP.create_invoice(KEY, 50_000, oid, "buyer")
        async with maker() as s:
            s.add(models.HooshPayCharge(bot_id=5, user_telegram_id=777, order_id=oid, invoice_uid=f"inv_{i}", amount_toman=50_000,
                                        status="waiting"))
            await s.commit()

    class CB:
        def __init__(self, data):
            self.data = data
            self.from_user = SimpleNamespace(id=777)
            self.answers = []
            self.message = SimpleNamespace(edit_reply_markup=self._e, answer=self._a)
            self.sent = []

        async def _e(self, **k): pass
        async def _a(self, t, **k): self.sent.append(t)
        async def answer(self, t="", show_alert=False): self.answers.append((t, show_alert))
    fp.invoices["inv_1"]["status"] = "paid"                                               # قبل از زدنِ لغو واریز شده بود
    c1 = CB("hou:cancel:bsp")
    await HA.hou_cancel(c1, SimpleNamespace())
    assert "اضافه شد" in c1.answers[-1][0] and (await _state(maker))[:2] == (50_000.0, ["paid", "waiting"])
    c2 = CB("hou:cancel:bsq")
    await HA.hou_cancel(c2, SimpleNamespace())
    assert fp.invoices["inv_2"]["status"] == "cancelled" and (await _state(maker))[1] == ["paid", "cancelled"] and c2.sent
    other = CB("hou:cancel:bsq")
    other.from_user = SimpleNamespace(id=999)
    await HA.hou_cancel(other, SimpleNamespace())
    assert "پیدا نشد" in other.answers[-1][0]


@pytest.mark.asyncio
async def test_availability_settings_and_registration(env):
    assert await HP.is_available(5) is True and await HP.get_fee_mode(5) == "buyer"
    await tenant.set_setting(5, "hooshpay_fee_mode", "bogus")
    assert await HP.get_fee_mode(5) == "buyer"
    await tenant.set_setting(5, "hooshpay_fee_mode", "split")
    assert await HP.get_fee_mode(5) == "split"
    assert await HP.get_min_amount(5) == 10_000 and await HP.get_max_amount(5) == 0
    await tenant.set_setting(5, "hooshpay_api_key", "")
    assert await HP.is_available(5) is False
    from plans import Feature, has_feature
    assert has_feature("pro", Feature.GATEWAY_HOOSHPAY) and has_feature("growth", Feature.GATEWAY_HOOSHPAY)
    assert not has_feature("free", Feature.GATEWAY_HOOSHPAY)
    assert tenant.GATEWAY_LABELS["hooshpay"] == ("هوش‌پی", "hooshpay_display_name")
    from tenant.gateways import gateway_layout as GL, wallet_limits as WL
    assert any(k == "hooshpay" and cb == "w:hooshpay" for k, cb, _t, _r in GL.ITEMS)
    assert "hooshpay" in WL.GATEWAYS
    from tenant.gateways.exact_amount_alert import is_target
    assert is_target("w:hooshpay")
    from tenant.shared import charge_bonus as CBN
    assert "hooshpay" in CBN.METHODS and "hooshpay_charge" in CBN.TOPUP_KINDS


@pytest.mark.asyncio
async def test_wallet_page_lists_hooshpay_only_when_available_and_visible(env):
    maker, fp, notified = env
    async with maker() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 777))

    class M:
        def __init__(self):
            self.sent = []
            self.chat = SimpleNamespace(id=777)
            self.from_user = SimpleNamespace(id=777)

        async def answer(self, text, **kw):
            self.sent.append(kw.get("reply_markup"))
            return SimpleNamespace(message_id=1)

        async def reply(self, text, **kw):
            return await self.answer(text, **kw)

    def has(mk):
        if mk is None:
            return False
        rows = getattr(mk, "inline_keyboard", None) or getattr(mk, "keyboard", [])
        return any("w:hooshpay" == getattr(b, "callback_data", None) or "هوش‌پی" in (getattr(b, "text", "") or "")
                   for r in rows for b in r)
    m = M()
    await tenant.show_wallet(m, 5, u)
    assert any(has(x) for x in m.sent)
    await tenant.set_setting(5, "hooshpay_show_wallet", "0")
    m = M()
    await tenant.show_wallet(m, 5, u)
    assert not any(has(x) for x in m.sent)
    await tenant.set_setting(5, "hooshpay_show_wallet", "1")
    await tenant.set_setting(5, "hooshpay_enabled", "0")
    m = M()
    await tenant.show_wallet(m, 5, u)
    assert not any(has(x) for x in m.sent)
