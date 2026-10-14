"""PATCH_APAY_DIRECT_PURCHASE_V1 — پرداختِ مستقیمِ سفارش/پلن/خریدِ دلخواه با آپی (مثلِ آبان‌گیت‌وی)."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.gateways.apay_admin as AA
from tenant.gateways import apay as AP
from tenant.gateways.abangateway_admin import vc_pack


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, AP, AA):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="A", balance=30_000))
        s.add(models.VpnPlan(id=3, bot_id=5, panel_key="sanai", title="p", volume_gb=10, days=30, price=100_000, active=True))
        s.add(models.Order(id=8, bot_id=5, user_id=1, volume_gb=10, days=30, pay_method="wallet", amount=120_000, status="pending"))
        await s.commit()
    await tenant.set_setting(5, "apay_token", "apay_testtoken")
    await tenant.set_setting(5, "apay_enabled", "1")
    sent = []

    async def avail(bot_id):
        return True

    async def create(token, **kw):
        sent.append(kw)
        return 200, {"id": "pay_9", "pay_url": "https://pay/x", "pay_amount": kw["amount_toman"] * 10}

    async def card(token, res):
        return "6037", "holder"

    async def minmax(bot_id):
        return 10_000

    async def maxv(bot_id):
        return 0
    monkeypatch.setattr(AP, "is_available", avail)
    monkeypatch.setattr(AP, "create_payment", create)
    monkeypatch.setattr(AP, "card_for", card)
    monkeypatch.setattr(AP, "get_min_amount", minmax)
    monkeypatch.setattr(AP, "get_max_amount", maxv)
    monkeypatch.setattr(AP, "watch_tenant_charge", lambda *a, **k: None)
    monkeypatch.setattr(AP, "webhook_url", lambda bid: _aw("https://hook"))
    yield maker, sent
    await engine.dispose()


async def _aw(v):
    return v


class Msg:
    def __init__(self):
        self.out = []
        self.chat = SimpleNamespace(id=1)
        self.from_user = SimpleNamespace(id=777)

    async def answer(self, text, **k):
        self.out.append(text)
        return SimpleNamespace(chat=self.chat, message_id=len(self.out), delete=self._d)

    async def _d(self):
        pass


class Cb:
    def __init__(self, data):
        self.data, self.from_user, self.message, self.alerts = data, SimpleNamespace(id=777), Msg(), []

    async def answer(self, text="", show_alert=False, **k):
        self.alerts.append(text)


class St:
    async def clear(self):
        pass

    async def update_data(self, **k):
        pass


BOT = SimpleNamespace(token="5:T")


async def _charge(maker):
    async with maker() as s:
        return (await s.scalars(select(models.ApayCharge))).all()


@pytest.mark.asyncio
async def test_direct_plan_charges_only_the_shortfall_and_remembers_the_plan(env):
    maker, sent = env
    await AA.apu_start_for_vpn_plan(Cb("w:apay:vpn:3:100000"), BOT, St())
    ch = await _charge(maker)
    assert len(ch) == 1 and ch[0].amount_toman == 70_000 and ch[0].for_order_id == -3     # ۱۰۰٬۰۰۰ − موجودی ۳۰٬۰۰۰


@pytest.mark.asyncio
async def test_direct_vc_and_order(env):
    maker, _s = env
    await AA.apu_start_for_vc(Cb("w:apay:vc:10:30:1:50000"), BOT, St())
    await AA.apu_start_for_order(Cb("w:apay:8"), BOT, St())
    ch = sorted(await _charge(maker), key=lambda c: c.id)
    assert ch[0].amount_toman == 20_000 and ch[0].for_order_id == vc_pack(10, 30, 1)
    assert ch[1].amount_toman == 90_000 and ch[1].for_order_id == 8


@pytest.mark.asyncio
async def test_direct_refuses_when_balance_enough_or_foreign_order(env):
    maker, _s = env
    cb = Cb("w:apay:vpn:3:20000")                                   # موجودی ۳۰٬۰۰۰ کافی است
    await AA.apu_start_for_vpn_plan(cb, BOT, St())
    assert not await _charge(maker) and any("کافی" in a for a in cb.alerts)
    async with maker() as s:
        s.add(models.BotUser(id=2, bot_id=5, telegram_id=999, full_name="B", balance=0))
        s.add(models.Order(id=9, bot_id=5, user_id=2, volume_gb=10, days=30, pay_method="wallet", amount=50_000, status="pending"))
        await s.commit()
    cb2 = Cb("w:apay:9")
    await AA.apu_start_for_order(cb2, BOT, St())
    assert not await _charge(maker) and any("مالِ تو نیست" in a for a in cb2.alerts)


def test_resume_button_mapping():
    assert AP.resume_button(None) is None
    assert AP.resume_button(8) == ("🛒 تکمیلِ سفارش و دریافتِ سرویس", "pay:wallet:8")
    assert AP.resume_button(-3)[1] == "vpay:3"
    assert AP.resume_button(vc_pack(10, 30, 2))[1] == "vc:pay:10:30:2"


@pytest.mark.asyncio
async def test_settle_passes_for_order_to_notification(env, monkeypatch):
    maker, _s = env
    await AA.apu_start_for_vpn_plan(Cb("w:apay:vpn:3:100000"), BOT, St())
    got = []

    async def np(bot_id, tg, amount, bal, *a, **k):
        got.append(k.get("for_order"))
    monkeypatch.setattr(AP, "_notify_paid", np)

    async def noop(*a, **k):
        return None
    import tenant.gateways.payment_page as pp
    import tenant.shared.kyc as kyc
    monkeypatch.setattr(pp, "_tx_log_tenant", noop)
    monkeypatch.setattr(kyc, "after_charge", noop)
    ch = (await _charge(maker))[0]

    async def v(token, pid):
        return 200, {"verified": True, "payment": {"id": "pay_9", "order_id": ch.order_id,
                     "amount": (ch.amount_toman + ch.fee_toman) * 10, "status": "paid", "late": False}}
    monkeypatch.setattr(AP, "verify_payment", v)
    ok, *_ = await AP.settle_charge(5, ch.order_id)
    assert ok and got == [-3]
