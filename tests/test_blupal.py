"""PATCH_BLUPAL_TENANT_V1 — درگاهِ کارت‌به‌کارتِ خودکارِ بلوپال برایِ ربات‌های مشتری (سرورِ aiohttpِ محلی که قراردادِ مستنداتِ blupal.top را شبیه می‌سازد)."""
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
import tenant.gateways.blupal_admin as BA
from tenant.gateways import blupal as BP

KEY = "blu_live_testkey123456"


class FakeBlu:
    def __init__(self):
        self.invoices = {}
        self.calls = []
        self.n = 100

    def app(self):
        a = web.Application(middlewares=[self.mw])
        a.router.add_post("/api/v1/invoices/create", self.create)
        a.router.add_get("/api/v1/invoices/{iid}", self.get)
        return a

    @web.middleware
    async def mw(self, request, handler):
        self.calls.append((request.method, request.path))
        if request.headers.get("X-API-Key") != KEY:
            return web.json_response({"success": False, "error": "unauthorized"}, status=401)
        return await handler(request)

    async def create(self, r):
        b = await r.json()
        amt = b.get("amount")
        if not amt:
            return web.json_response({"success": False, "error": "amount_required"}, status=400)
        if amt < 100_000:
            return web.json_response({"success": False, "error": "amount_too_low", "message": "x"}, status=400)
        if amt > 500_000_000:
            return web.json_response({"success": False, "error": "amount_too_high"}, status=400)
        self.n += 1
        inv = {"success": True, "invoice_id": self.n, "amount": amt, "final_amount": amt + 123, "status": "PENDING",
               "payment_link": f"https://blupal.top/payment/T{self.n}", "card_number": "6219861012345678",
               "mode": "live", "expires_at": None}
        self.invoices[self.n] = inv
        return web.json_response(inv)

    async def get(self, r):
        try:
            inv = self.invoices.get(int(r.match_info["iid"]))
        except ValueError:
            return web.json_response({"success": False, "error": "invalid_invoice_id"}, status=400)
        if not inv:
            return web.json_response({"success": False, "error": "not_found"}, status=404)
        out = dict(inv)
        if inv["status"] == "PAID":
            out.update(transaction_id=456, payer_name="علی رضایی", payer_card="6037991234567890")
        return web.json_response(out)


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, BP, BA):
        monkeypatch.setattr(mod, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="A", balance=0))
        await s.commit()
    fb = FakeBlu()
    runner = web.AppRunner(fb.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(BP, "BASE_URL", f"http://127.0.0.1:{port}/api")
    await tenant.set_setting(5, "blupal_api_key", KEY)
    await tenant.set_setting(5, "blupal_enabled", "1")
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
    yield maker, fb, notified
    await runner.cleanup()
    await engine.dispose()


async def _state(maker):
    async with maker() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 777))
        ch = (await s.scalars(select(models.BluPalCharge).order_by(models.BluPalCharge.id))).all()
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


async def _add_charge(maker, oid, inv_id, toman=100_000, status="waiting", age_min=1, final=None):
    async with maker() as s:
        s.add(models.BluPalCharge(bot_id=5, user_telegram_id=777, order_id=oid, invoice_id=inv_id, amount_toman=toman,
                                  amount_rial=toman * 10, final_amount_rial=final if final is not None else toman * 10 + 123,
                                  status=status, created_at=datetime.utcnow() - timedelta(minutes=age_min)))
        await s.commit()


@pytest.mark.asyncio
async def test_validate_key_and_errors(env):
    ok, why = await BP.validate_key(KEY)
    assert ok and "اصلی" in why                                       # 404 روی فاکتورِ ناموجود ⇒ کلید درست
    ok, why = await BP.validate_key("blu_live_wrong")
    assert not ok and "کلید" in why
    assert BP.key_mode("blu_test_abc") == "sandbox" and BP.key_mode("zzz") == "unknown"
    res = await BP.create_invoice(KEY, 5_000)
    assert not res["success"] and "حداقل" in BP._err(res)
    res = await BP.create_invoice(KEY, 600_000_000)
    assert "سقف" in BP._err(res)


@pytest.mark.asyncio
async def test_amount_flow_converts_toman_to_rial_and_shows_exact_amount(env, monkeypatch):
    maker, fb, notified = env
    import tenant as T

    async def row(bot):
        return SimpleNamespace(id=5, username="x", owner_id=1)
    monkeypatch.setattr(T, "fa_to_int", lambda t: int(str(t).replace(",", "")))
    monkeypatch.setattr(T, "get_bot_row", row)
    import tenant.shared.kyc as kyc

    async def okc(*a, **k):
        return True, ""
    monkeypatch.setattr(kyc, "check_charge", okc)
    m = Msg("100000")
    await BA.bpu_amount(m, SimpleNamespace(), St())
    text, kb = m.out[-1]
    assert "1,000,123 ریال" in text and "100,000" in text and "6219861012345678" in text
    assert kb.inline_keyboard[0][0].url == "https://blupal.top/payment/T101"
    assert fb.invoices[101]["amount"] == 1_000_000                       # تومان×۱۰ = ریال
    async with maker() as s:
        ch = await s.scalar(select(models.BluPalCharge))
    assert ch.invoice_id == 101 and ch.amount_toman == 100_000 and ch.amount_rial == 1_000_000 and ch.final_amount_rial == 1_000_123
    assert await _state(maker) == (0.0, ["waiting"], [])
    m2 = Msg("5000")                                                      # کمتر از حداقلِ ۱۰٬۰۰۰ تومان
    await BA.bpu_amount(m2, SimpleNamespace(), St())
    assert "حداقل" in m2.out[-1][0] and len(fb.invoices) == 1
    m3 = Msg("60000000")                                                  # بیشتر از ۵۰ میلیون تومان
    await BA.bpu_amount(m3, SimpleNamespace(), St())
    assert "حداکثر" in m3.out[-1][0] and len(fb.invoices) == 1


@pytest.mark.asyncio
async def test_settle_credits_requested_amount_once(env):
    maker, fb, notified = env
    await BP.create_invoice(KEY, 1_000_000)
    await _add_charge(maker, "bpabc", 101)
    ok, msg, _ = await BP.settle_charge(5, "bpabc")
    assert not ok and "هنوز" in msg and await _state(maker) == (0.0, ["waiting"], [])
    fb.invoices[101]["status"] = "PAID"
    ok, msg, credit = await BP.settle_charge(5, "bpabc")
    assert ok and credit == 100_000
    assert await _state(maker) == (100_000.0, ["paid"], ["blupal_charge"])           # مبلغِ شارژ، نه ریال/final
    ok2, _m, _c = await BP.settle_charge(5, "bpabc")                                  # تکراری
    assert not ok2 and await _state(maker) == (100_000.0, ["paid"], ["blupal_charge"])
    assert notified == [(777, 100_000, 100_000.0)]
    async with maker() as s:
        ch = await s.scalar(select(models.BluPalCharge))
    assert ch.tracking_code == "456" and ch.payer_name == "علی رضایی" and ch.paid_at is not None


@pytest.mark.asyncio
async def test_settle_rejects_amount_mismatch_and_marks_expired_or_cancelled(env):
    maker, fb, notified = env
    await BP.create_invoice(KEY, 700_000)                                            # inv 101: ۷۰٬۰۰۰ تومان
    await _add_charge(maker, "bpx", 101, toman=100_000)                              # رکوردِ ما ۱۰۰٬۰۰۰ تومان
    fb.invoices[101]["status"] = "PAID"
    ok, msg, _ = await BP.settle_charge(5, "bpx")
    assert not ok and "یکسان نیست" in msg and await _state(maker) == (0.0, ["waiting"], [])
    await BP.create_invoice(KEY, 1_000_000)                                          # inv 102
    await _add_charge(maker, "bpy", 102, final=999)                                  # final_amount با رکورد نمی‌خواند
    fb.invoices[102]["status"] = "PAID"
    ok, msg, _ = await BP.settle_charge(5, "bpy")
    assert not ok and "یکسان نیست" in msg
    await BP.create_invoice(KEY, 1_000_000)                                          # inv 103
    await _add_charge(maker, "bpz", 103)
    fb.invoices[103]["status"] = "EXPIRED"
    ok, msg, _ = await BP.settle_charge(5, "bpz")
    assert not ok and "تمام شده" in msg and (await _state(maker))[1] == ["waiting", "waiting", "expired"]
    await BP.create_invoice(KEY, 1_000_000)                                          # inv 104
    await _add_charge(maker, "bpc", 104)
    fb.invoices[104]["status"] = "CANCELED"
    await BP.settle_charge(5, "bpc")
    assert (await _state(maker))[1][-1] == "cancelled"


@pytest.mark.asyncio
async def test_late_payment_after_expire_or_cancel_still_credits(env):
    maker, fb, notified = env
    await BP.create_invoice(KEY, 1_000_000)
    await _add_charge(maker, "bpl", 101, status="expired")
    fb.invoices[101]["status"] = "PAID"
    ok, _m, credit = await BP.settle_charge(5, "bpl")
    assert ok and credit == 100_000 and (await _state(maker))[:2] == (100_000.0, ["paid"])
    await BP.create_invoice(KEY, 1_000_000)
    await _add_charge(maker, "bpk", 102, status="cancelled")                          # کاربر لغو زد ولی بعد واریز کرد
    fb.invoices[102]["status"] = "PAID"
    ok, _m, credit = await BP.settle_charge(5, "bpk")
    assert ok and (await _state(maker))[0] == 200_000.0


@pytest.mark.asyncio
async def test_poll_pending_settles_expires_and_rechecks_closed_rarely(env):
    maker, fb, notified = env
    for i, (oid, age, st) in enumerate((("bp1", 1, "waiting"), ("bp2", 1, "waiting"), ("bp3", 100, "waiting"),
                                        ("bp4", 200, "expired")), start=101):
        await BP.create_invoice(KEY, 1_000_000)
        await _add_charge(maker, oid, i, status=st, age_min=age)
    fb.invoices[101]["status"] = "PAID"
    fb.invoices[104]["status"] = "PAID"                                               # منقضی‌شدهٔ محلی که بعداً واریز شد
    fb.calls.clear()
    assert await BP.poll_pending(include_closed=False) == 1                           # دورِ عادی: فقط بازها
    assert not any(p.endswith("/104") for _m, p in fb.calls)
    assert await _state(maker) == (100_000.0, ["paid", "waiting", "expired", "expired"], ["blupal_charge"])   # ۱۰۰ دقیقه‌ای بسته شد
    assert await BP.poll_pending(include_closed=True) == 1                            # دورِ عمیق: منقضی‌ها هم
    assert (await _state(maker))[0] == 200_000.0


@pytest.mark.asyncio
async def test_cancel_handler_credits_if_already_paid_else_cancels(env, monkeypatch):
    maker, fb, notified = env
    import tenant as T

    async def row(bot):
        return SimpleNamespace(id=5, username="x", owner_id=1)

    async def back(bid):
        return None
    monkeypatch.setattr(T, "get_bot_row", row)
    monkeypatch.setattr(T, "inline_back_kb", back)
    for i, oid in enumerate(("bpp", "bpq"), start=101):
        await BP.create_invoice(KEY, 1_000_000)
        await _add_charge(maker, oid, i)

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
    fb.invoices[101]["status"] = "PAID"
    c1 = CB("bpu:cancel:bpp")
    await BA.bpu_cancel(c1, SimpleNamespace())
    assert "اضافه شد" in c1.answers[-1][0] and (await _state(maker))[:2] == (100_000.0, ["paid", "waiting"])
    c2 = CB("bpu:cancel:bpq")
    await BA.bpu_cancel(c2, SimpleNamespace())
    assert (await _state(maker))[1] == ["paid", "cancelled"] and c2.sent
    other = CB("bpu:cancel:bpq")
    other.from_user = SimpleNamespace(id=999)
    await BA.bpu_cancel(other, SimpleNamespace())
    assert "پیدا نشد" in other.answers[-1][0]


@pytest.mark.asyncio
async def test_availability_settings_and_registration(env):
    assert await BP.is_available(5) is True
    assert await BP.get_min_amount(5) == 10_000 and await BP.get_max_amount(5) == 0 and await BP.effective_max(5) == 50_000_000
    await tenant.set_setting(5, "blupal_min_amount", "100")                          # زیرِ کفِ درگاه ⇒ نادیده
    assert await BP.get_min_amount(5) == 10_000
    await tenant.set_setting(5, "blupal_max_amount", "999999999")                    # بالاتر از سقفِ درگاه ⇒ سقفِ درگاه
    assert await BP.get_max_amount(5) == 50_000_000
    await tenant.set_setting(5, "blupal_api_key", "")
    assert await BP.is_available(5) is False
    from plans import Feature, has_feature
    assert has_feature("pro", Feature.GATEWAY_BLUPAL) and has_feature("growth", Feature.GATEWAY_BLUPAL)
    assert not has_feature("free", Feature.GATEWAY_BLUPAL)
    assert tenant.GATEWAY_LABELS["blupal"] == ("بلوپال", "blupal_display_name")
    from tenant.gateways import gateway_layout as GL, wallet_limits as WL
    assert any(k == "blupal" and cb == "w:blupal" for k, cb, _t, _r in GL.ITEMS)
    assert "blupal" in WL.GATEWAYS and WL.GATEWAYS["blupal"].floor_min == 10_000
    from tenant.gateways.exact_amount_alert import is_target
    assert is_target("w:blupal")
    from tenant.shared import charge_bonus as CBN, kyc as K, period_stats as PS
    assert "blupal" in CBN.METHODS and "blupal_charge" in CBN.TOPUP_KINDS
    assert "blupal_charge" in K.DEPOSIT_GATEWAY_KINDS and "blupal_charge" in PS.CHARGE_KINDS


@pytest.mark.asyncio
async def test_wallet_page_lists_blupal_only_when_available_and_visible(env):
    maker, fb, notified = env
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
        return any("w:blupal" == getattr(b, "callback_data", None) or "بلوپال" in (getattr(b, "text", "") or "")
                   for r in rows for b in r)
    m = M()
    await tenant.show_wallet(m, 5, u)
    assert any(has(x) for x in m.sent)
    await tenant.set_setting(5, "blupal_show_wallet", "0")
    m = M()
    await tenant.show_wallet(m, 5, u)
    assert not any(has(x) for x in m.sent)
    await tenant.set_setting(5, "blupal_show_wallet", "1")
    await tenant.set_setting(5, "blupal_enabled", "0")
    m = M()
    await tenant.show_wallet(m, 5, u)
    assert not any(has(x) for x in m.sent)
