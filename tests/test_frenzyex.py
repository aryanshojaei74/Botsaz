"""PATCH_FRENZYEX_GATEWAY_V1 — درگاهِ فرنزی‌اکس (سرورِ aiohttpِ محلی که قراردادِ مستنداتِ v5 را شبیه‌سازی می‌کند)."""
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
import tenant.gateways.frenzyex_admin as FA
from tenant.gateways import frenzyex as FX

KEY = "frx_live_testkey123456"


class FakeFrenzy:
    def __init__(self):
        self.reqs = {}
        self.calls = []
        self.n = 0
        self.cancel_status = 200

    def app(self):
        a = web.Application(middlewares=[self.mw])
        a.router.add_post("/api/v1/payment-requests", self.create)
        a.router.add_get("/api/v1/payment-requests/{rid}", self.get)
        a.router.add_post("/api/v1/payment-requests/{rid}/cancel", self.cancel)
        return a

    @web.middleware
    async def mw(self, request, handler):
        self.calls.append((request.method, request.path))
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return web.json_response({"detail": "invalid key"}, status=401)
        return await handler(request)

    async def create(self, r):
        b = await r.json()
        if not b.get("amount") or b["amount"] <= 0:
            return web.json_response({"detail": [{"msg": "amount > 0"}]}, status=422)
        self.n += 1
        rid = f"Req{self.n}"
        self.reqs[rid] = {"request_id": rid, "status": "pending", "amount": b["amount"], "amount_ccy": b["amount_ccy"],
                          "order_ref": b.get("order_ref"), "payment_method": b.get("payment_method"), "tx_hash": None}
        return web.json_response({**self.reqs[rid], "checkout_url": f"https://t.me/FrenzyExBot?start=pay_{rid}",
                                  "bot_pay_url": f"https://t.me/FrenzyExBot?start=paybot_{rid}"})

    async def get(self, r):
        q = self.reqs.get(r.match_info["rid"])
        if not q:
            return web.json_response({"detail": "not found"}, status=404)
        return web.json_response(q)

    async def cancel(self, r):
        q = self.reqs.get(r.match_info["rid"])
        if not q:
            return web.json_response({"detail": "nf"}, status=404)
        if self.cancel_status == 409:
            return web.json_response({"detail": "payment_received"}, status=409)
        q["status"] = "canceled"
        return web.json_response(q)


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, FX, FA):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="A", balance=0))
        await s.commit()
    fp = FakeFrenzy()
    runner = web.AppRunner(fp.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(FX, "BASE_URL", f"http://127.0.0.1:{port}")
    await tenant.set_setting(5, "frenzyex_api_key", KEY)
    await tenant.set_setting(5, "frenzyex_enabled", "1")
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


async def _make(maker, fp, amount=150_000, oid="fx1", rid=None):
    res = await FX.create_request(KEY, amount, oid, "auto", "t")
    assert res["success"], res
    async with maker() as s:
        s.add(models.FrenzyExCharge(bot_id=5, user_telegram_id=777, order_id=oid, request_id=res["request_id"],
                                    amount_toman=amount, method="auto", status="waiting"))
        await s.commit()
    return res["request_id"]


async def _bal(maker):
    async with maker() as s:
        return float((await s.scalar(select(models.BotUser).where(models.BotUser.id == 1))).balance)


@pytest.mark.asyncio
async def test_validate_key_ok_bad_and_no_invoice_created(env):
    _m, fp, _n = env
    assert (await FX.validate_key(KEY))[0] is True
    ok, why = await FX.validate_key("frx_live_wrongkey000000")
    assert ok is False and "نامعتبر" in why
    assert not any(m == "POST" for m, _p in fp.calls)             # تستِ کلید فاکتور نمی‌سازد


@pytest.mark.asyncio
async def test_create_sends_documented_body_and_returns_links(env):
    _m, fp, _n = env
    res = await FX.create_request(KEY, 150000, "fxA", "standard", "شارژ")
    assert res["success"] and res["checkout_url"].startswith("https://t.me/") and res["amount_ccy"] == "TMN"
    assert fp.reqs[res["request_id"]]["order_ref"] == "fxA" and fp.reqs[res["request_id"]]["payment_method"] == "standard"
    bad = await FX.create_request(KEY, 0, "fxB", "auto")
    assert bad["success"] is False and bad["http"] == 422 and "۰" in FX._err(bad)


@pytest.mark.asyncio
async def test_unpaid_does_not_credit_then_paid_credits_exactly_once(env):
    maker, fp, notified = env
    rid = await _make(maker, fp)
    ok, msg, amt = await FX.settle_charge(5, "fx1")
    assert not ok and amt == 0 and await _bal(maker) == 0
    fp.reqs[rid].update(status="paid", tx_hash="custody:99")
    ok, _m, amt = await FX.settle_charge(5, "fx1")
    assert ok and amt == 150_000 and await _bal(maker) == 150_000.0 and notified == [(777, 150_000, 150_000.0)]
    ok2, msg2, amt2 = await FX.settle_charge(5, "fx1")
    assert not ok2 and amt2 == 0 and await _bal(maker) == 150_000.0                 # دوباره شارژ نمی‌شود
    async with maker() as s:
        ch = await s.scalar(select(models.FrenzyExCharge))
        txn = await s.scalar(select(models.WalletTxn).where(models.WalletTxn.kind == "frenzyex_charge"))
    assert ch.status == "paid" and ch.tracking_code == "custody:99" and txn and txn.amount == 150_000


@pytest.mark.asyncio
async def test_amount_or_order_ref_mismatch_is_rejected(env):
    maker, fp, _n = env
    rid = await _make(maker, fp)
    fp.reqs[rid].update(status="paid", amount=999)
    ok, msg, amt = await FX.settle_charge(5, "fx1")
    assert not ok and amt == 0 and await _bal(maker) == 0
    fp.reqs[rid].update(amount=150_000, order_ref="OTHER")
    ok, msg, amt = await FX.settle_charge(5, "fx1")
    assert not ok and await _bal(maker) == 0
    fp.reqs[rid].update(order_ref="fx1", amount_ccy="TRX")
    ok, msg, amt = await FX.settle_charge(5, "fx1")
    assert not ok and await _bal(maker) == 0


@pytest.mark.asyncio
async def test_late_payment_after_expiry_or_cancel_is_still_credited(env):
    maker, fp, _n = env
    rid = await _make(maker, fp)
    async with maker() as s:
        ch = await s.scalar(select(models.FrenzyExCharge))
        ch.status = "cancelled"
        ch.created_at = datetime.utcnow() - timedelta(minutes=100)
        await s.commit()
    fp.reqs[rid]["status"] = "paid"
    assert await FX.poll_pending() == 1
    assert await _bal(maker) == 150_000.0


@pytest.mark.asyncio
async def test_poll_ignores_old_records_and_marks_waiting_expired(env):
    maker, fp, _n = env
    rid = await _make(maker, fp)
    async with maker() as s:
        ch = await s.scalar(select(models.FrenzyExCharge))
        ch.created_at = datetime.utcnow() - timedelta(minutes=80)
        await s.commit()
    assert await FX.poll_pending() == 0
    async with maker() as s:
        assert (await s.scalar(select(models.FrenzyExCharge))).status == "expired"
    async with maker() as s:                                   # قدیمی‌تر از ۳ ساعت ⇒ دیگر استعلام نمی‌شود
        ch = await s.scalar(select(models.FrenzyExCharge))
        ch.created_at = datetime.utcnow() - timedelta(hours=4)
        await s.commit()
    fp.reqs[rid]["status"] = "paid"
    assert await FX.poll_pending() == 0 and await _bal(maker) == 0


class _Msg:
    def __init__(self, text, uid=777):
        self.text, self.from_user = text, SimpleNamespace(id=uid, full_name="A")
        self.out = []

    async def answer(self, text, **k):
        self.out.append((text, k.get("reply_markup")))

    async def delete(self):
        pass


class _State:
    async def clear(self): ...
    async def set_state(self, *a): ...


@pytest.mark.asyncio
async def test_user_flow_creates_invoice_with_pay_button(env, monkeypatch):
    maker, fp, _n = env
    row = SimpleNamespace(id=5, owner_id=1, plan="pro")

    async def gbr(bot):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    import tenant.shared.kyc as kyc

    async def chk(*a, **k):
        return True, ""
    monkeypatch.setattr(kyc, "check_charge", chk)
    m = _Msg("150000")
    await FA.fxu_amount(m, object(), _State())
    kb = m.out[-1][1]
    urls = [b.url for r in kb.inline_keyboard for b in r if b.url]
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data]
    assert urls and urls[0].startswith("https://t.me/FrenzyExBot")
    assert any(c.startswith("fxu:check:") for c in cbs) and any(c.startswith("fxu:cancel:") for c in cbs)
    async with maker() as s:
        ch = await s.scalar(select(models.FrenzyExCharge))
    assert ch.amount_toman == 150_000 and ch.status == "waiting" and ch.request_id.startswith("Req")
    m2 = _Msg("500")                                           # زیرِ حداقل
    await FA.fxu_amount(m2, object(), _State())
    assert "حداقل" in m2.out[-1][0] and len([c for c in fp.calls if c[0] == "POST"]) == 1


@pytest.mark.asyncio
async def test_create_failure_creates_no_record_and_does_not_retry(env, monkeypatch):
    maker, fp, _n = env
    monkeypatch.setattr(FX, "BASE_URL", "http://127.0.0.1:1")          # اتصال رد می‌شود
    row = SimpleNamespace(id=5, owner_id=1, plan="pro")

    async def gbr(bot):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    import tenant.shared.kyc as kyc

    async def chk(*a, **k):
        return True, ""
    monkeypatch.setattr(kyc, "check_charge", chk)
    m = _Msg("150000")
    await FA.fxu_amount(m, object(), _State())
    assert "ناموفق" in m.out[-1][0]
    async with maker() as s:
        assert await s.scalar(select(models.FrenzyExCharge)) is None


@pytest.mark.asyncio
async def test_cancel_409_keeps_invoice_open(env, monkeypatch):
    maker, fp, _n = env
    await _make(maker, fp)
    fp.cancel_status = 409
    row = SimpleNamespace(id=5, owner_id=1, plan="pro")

    async def gbr(bot):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    alerts = []

    class CB:
        data = "fxu:cancel:fx1"
        from_user = SimpleNamespace(id=777)
        message = SimpleNamespace()

        async def answer(self, text="", **k):
            alerts.append(text)
    await FA.fxu_cancel(CB(), object())
    assert alerts and "بررسی" in alerts[-1]
    async with maker() as s:
        assert (await s.scalar(select(models.FrenzyExCharge))).status == "waiting"


@pytest.mark.asyncio
async def test_only_pro_can_use_and_see_the_gateway(env):
    maker, _fp, _n = env
    assert await FX.is_available(5) is True
    async with maker() as s:
        b = await s.get(models.Bot, 5)
        b.plan = "growth"
        await s.commit()
    tenant._BOT_ROW_CACHE.clear()
    assert await FX.is_available(5) is False
    from plans import Feature, has_feature
    assert has_feature("pro", Feature.GATEWAY_FRENZYEX) and not has_feature("growth", Feature.GATEWAY_FRENZYEX)


def test_gateway_is_registered_everywhere():
    from tenant.gateways import gateway_layout, wallet_limits, exact_amount_alert
    from tenant.shared import charge_bonus, kyc, period_stats, vpn_api
    assert "frenzyex" in tenant.GATEWAY_LABELS and "frenzyex" in tenant.WALLET_FULL_LABELS
    assert any(i[0] == "frenzyex" and i[1] == "w:frenzyex" for i in gateway_layout.ITEMS)
    assert "frenzyex" in wallet_limits.GATEWAYS
    assert exact_amount_alert.is_target("w:frenzyex")
    assert "frenzyex_charge" in charge_bonus.TOPUP_KINDS and "frenzyex_charge" in kyc.DEPOSIT_GATEWAY_KINDS
    assert "frenzyex_charge" in period_stats.CHARGE_KINDS and "frenzyex_charge" in vpn_api._PAYMENT_KINDS
