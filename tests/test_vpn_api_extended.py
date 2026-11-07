"""PATCH_VPN_API_EXT_V1 (Weirdnet) — API فروشِ VPN: حجمِ دلخواه، کد تخفیف، حذفِ سرویس، شارژ با آپی."""
import json

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models

TG = 555001


class FakeClient:
    fail_create = False
    fail_delete = False
    deleted: list = []

    async def create_account(self, email, **kw):
        if FakeClient.fail_create:
            raise RuntimeError("panel down")
        FakeClient.last = dict(email=email, **kw)
        return {"uuid": "u-1", "sub_id": "s-1", "sub_link": "https://sub/x", "expiry_ms": None}

    async def delete_account(self, email):
        if FakeClient.fail_delete:
            raise RuntimeError("boom")
        FakeClient.deleted.append(email)
        return True

    async def close(self):
        pass


@pytest_asyncio.fixture
async def env(monkeypatch):
    import sys
    import tenant
    from tenant.shared import vpn_api as A
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    _orig = config.Session
    for _m in list(sys.modules.values()):
        if getattr(_m, "Session", None) is _orig:
            monkeypatch.setattr(_m, "Session", maker, raising=False)
    monkeypatch.setattr(config, "Session", maker)
    tenant._SETTING_CACHE.clear()
    FakeClient.fail_create = FakeClient.fail_delete = False
    FakeClient.deleted = []

    async def _get_client(*a, **k):
        return FakeClient()

    async def _noop(*a, **k):
        return None

    from tenant.shared.panel_clients import panel_pool
    monkeypatch.setattr(panel_pool, "get_client", _get_client)
    monkeypatch.setattr(panel_pool, "drop", _noop)
    monkeypatch.setattr(A, "_notify_sale", _noop)
    monkeypatch.setattr("tenant.shared.trial_cleanup._make_client", lambda pf: FakeClient())
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:T", username="shop", field="vpn_sales", plan="pro", status="active")
        s.add(b)
        await s.flush()
        s.add(models.VpnPanel(bot_id=b.id, panel_key="sanai", base_url="http://p", username="u", password="p",
                              enabled=True))
        s.add(models.BotUser(bot_id=b.id, telegram_id=TG, full_name="Reseller", balance=1_000_000))
        s.add(models.VpnPlan(bot_id=b.id, title="پلن ۱۰", volume_gb=10, days=30, price=100000, panel_key="sanai",
                             active=True))
        s.add(models.DiscountCode(bot_id=b.id, code="OFF10", percent=10, disc_type="percent", is_active=True))
        s.add(models.DiscountCode(bot_id=b.id, code="OFF20", percent=20, disc_type="percent", is_active=True))
        await s.commit()
        bid = b.id
    for k, v in {"vc_enabled": "1", "vc_price_gb": "1000", "vc_price_day": "500",
                 "vc_min_gb": "5", "vc_max_gb": "100", "vc_min_days": "30", "vc_max_days": "90"}.items():
        await tenant.set_setting(bid, k, v)
    tenant._SETTING_CACHE.clear()
    from tenant.bots.vpn import vpn_user as V
    V._VC_CACHE.clear()
    from tenant.shared.public_api import AuthCtx
    async with maker() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == TG))
    ctx = AuthCtx(bid, None, 0, True)
    yield dict(A=A, ctx=ctx, bid=bid, maker=maker, user=u)
    await engine.dispose()


async def _call(env, action, **p):
    p.setdefault("telegram_id", str(TG))
    r = await env["A"].dispatch(env["ctx"], action, p)
    return json.loads(r.body)


async def _balance(env):
    async with env["maker"]() as s:
        return float(await s.scalar(select(models.BotUser.balance).where(models.BotUser.telegram_id == TG)))


@pytest.mark.asyncio
async def test_custom_info_has_live_prices_and_quote(env):
    r = await _call(env, "custom", gb="10", days="30")
    it = r["items"][0]
    assert it["panel"] == "sanai" and it["price_per_gb"] == 1000 and it["min_gb"] == 5 and it["max_days"] == 90
    assert it["quote"] == 10 * 1000 + 30 * 500          # 25000


@pytest.mark.asyncio
async def test_buy_custom_volume_charges_live_price_and_creates_account(env):
    r = await _call(env, "buy", gb="10", days="30", ref="o1")
    assert r["ok"] and r["charged"] == 25000 and r["service"]["volume_gb"] == 10 and r["service"]["days"] == 30
    assert await _balance(env) == 975000
    assert FakeClient.last["gb"] == 10 and FakeClient.last["days"] == 30
    r2 = await _call(env, "buy", gb="10", days="30", ref="o1")          # تکرارِ همان ref پولی نمی‌گیرد
    assert r2.get("duplicate") and await _balance(env) == 975000


@pytest.mark.asyncio
async def test_custom_out_of_range_and_missing_days_do_not_charge(env):
    r = await _call(env, "buy", gb="500", days="30")
    assert r == {"ok": False, "error": "gb_out_of_range", "min": 5, "max": 100}
    assert (await _call(env, "buy", gb="10"))["error"] == "days_required"
    assert (await _call(env, "buy", gb="10", days="400"))["error"] == "days_out_of_range"
    assert await _balance(env) == 1_000_000


@pytest.mark.asyncio
async def test_discount_code_on_plan_and_custom_and_usage_counted(env):
    q = await _call(env, "quote", plan_id="1", discount_code="off10")
    assert q["price"] == 100000 and q["discount"] == 10000 and q["final"] == 90000
    r = await _call(env, "buy", plan_id="1", discount_code="OFF10")
    assert r["ok"] and r["charged"] == 90000 and r["discount"] == 10000 and r["discount_code"] == "OFF10"
    r2 = await _call(env, "buy", gb="10", days="30", discount_code="OFF20")
    assert r2["charged"] == 20000
    again = await _call(env, "buy", plan_id="1", discount_code="OFF10")        # هر کاربر هر کد را یک‌بار
    assert again["error"] == "invalid_discount"
    async with env["maker"]() as s:
        assert sorted((await s.scalars(select(models.DiscountCode.used_count))).all()) == [1, 1]


@pytest.mark.asyncio
async def test_bad_code_rejected_and_panel_failure_refunds_money_and_code(env):
    r = await _call(env, "buy", plan_id="1", discount_code="NOPE")
    assert r["error"] == "invalid_discount" and await _balance(env) == 1_000_000
    FakeClient.fail_create = True
    r = await _call(env, "buy", plan_id="1", discount_code="OFF10")
    assert r["error"] == "panel_error" and r["refunded"] is True
    assert await _balance(env) == 1_000_000
    async with env["maker"]() as s:
        assert (await s.scalar(select(models.DiscountCode.used_count))) == 0


@pytest.mark.asyncio
async def test_delete_removes_from_panel_and_bot_and_failure_keeps_it(env):
    r = await _call(env, "buy", plan_id="1")
    code = r["service"]["code"]
    FakeClient.fail_delete = True
    bad = await _call(env, "delete", service=code)
    assert bad["error"] == "panel_error"
    assert (await _call(env, "services"))["count"] == 1
    FakeClient.fail_delete = False
    ok = await _call(env, "delete", service=code)
    assert ok["ok"] and ok["deleted"] and FakeClient.deleted
    assert (await _call(env, "services"))["count"] == 0
    assert (await _call(env, "delete", service=code))["error"] == "service_not_found"


@pytest.mark.asyncio
async def test_personal_key_cannot_delete_someone_elses_service(env):
    r = await _call(env, "buy", plan_id="1")
    code = r["service"]["code"]
    from tenant.shared.public_api import AuthCtx
    async with env["maker"]() as s:
        s.add(models.BotUser(bot_id=env["bid"], telegram_id=777, full_name="Other", balance=0))
        await s.commit()
        other = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 777))
    ctx = AuthCtx(env["bid"], other, 0, False)
    resp = await env["A"].dispatch(ctx, "delete", {"service": code})
    assert json.loads(resp.body)["error"] == "service_not_found"
    assert (await _call(env, "services"))["count"] == 1


@pytest.mark.asyncio
async def test_charge_via_apay_passthrough_and_status(env, monkeypatch):
    from tenant.gateways import apay as AP
    seen = {}

    async def fake_core(row, tg, amount):
        seen["args"] = (tg, amount)
        if amount == 1:
            return None, "open:ORD9"
        if amount == 2:
            return None, "حداقلِ مبلغ ۱۰٬۰۰۰ تومان است."
        return {"order_id": "ORD1", "pay_url": "https://pay/x", "pay_rial": 2000000, "card": "", "holder": "",
                "fee": 0, "credit": amount, "expires_in": 900}, ""

    async def fake_status(bot_id, tg, oid):
        return {"status": "paid", "credit": 200000, "balance": 1200000.0} if oid == "ORD1" else None

    monkeypatch.setattr(AP, "create_charge_core", fake_core)
    monkeypatch.setattr(AP, "mini_status", fake_status)
    r = await _call(env, "charge", amount="200000")
    assert r["ok"] and r["pay_url"] == "https://pay/x" and r["order_id"] == "ORD1" and seen["args"] == (TG, 200000)
    assert (await _call(env, "charge", amount="1")) == {"ok": False, "error": "open_invoice", "order_id": "ORD9"}
    assert (await _call(env, "charge", amount="2"))["error"] == "charge_failed"
    assert (await _call(env, "charge"))["error"] == "amount_required"
    assert (await _call(env, "charge_status", order_id="ORD1"))["status"] == "paid"
    assert (await _call(env, "charge_status", order_id="X"))["error"] == "order_not_found"
