"""PATCH_GATEWAY_INPLACE_V1 (فدایی): نتیجهٔ «بررسیِ پرداخت»/«لغو» در فاکتورِ درگاه‌ها پیامِ جدید نمی‌سازد."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.gateways.abangateway_admin as ABA
import tenant.gateways.apay_admin as APA
import tenant.gateways.hooshpay_admin as HPA
import tenant.gateways.uniquepay_admin as UPA
from tenant.gateways import abangateway as AG, apay as AP, hooshpay as HP, uniquepay as UP


class CB:
    def __init__(self, data, uid=777):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.answers, self.edited, self.sent, self.stripped = [], [], [], []
        self.message = SimpleNamespace(chat=SimpleNamespace(id=777), message_id=5,
                                       edit_text=self._edit, answer=self._send, edit_reply_markup=self._strip)

    async def answer(self, t="", show_alert=False):
        self.answers.append((t, show_alert))

    async def _edit(self, text, **k):
        self.edited.append(text)

    async def _send(self, text, **k):
        self.sent.append(text)

    async def _strip(self, **k):
        self.stripped.append(1)


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, HPA, UPA, ABA, APA):
        monkeypatch.setattr(mod, "Session", maker)

    async def row(bot):
        return SimpleNamespace(id=5, username="x", owner_id=1)

    async def back(bid):
        return None
    monkeypatch.setattr(tenant, "get_bot_row", row)
    monkeypatch.setattr(tenant, "inline_back_kb", back)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="A", balance=100_000))
        s.add(models.HooshPayCharge(bot_id=5, user_telegram_id=777, order_id="hp1", invoice_uid="u1", amount_toman=100_000,
                                    payable_amount=100_000, fee_mode="merchant", status="waiting"))
        s.add(models.UniquePayCharge(bot_id=5, user_telegram_id=777, hash_id="up1", amount_toman=100_000, status="waiting"))
        s.add(models.AbanGatewayCharge(bot_id=5, user_telegram_id=777, order_id="ab1", invoice_id=1, amount_toman=100_000,
                                       payable_toman=100_000, status="waiting"))
        s.add(models.ApayCharge(bot_id=5, user_telegram_id=777, order_id="ap1", payment_id="p1", amount_toman=100_000,
                                status="waiting"))
        await s.commit()
    yield maker
    await engine.dispose()


def _settle(monkeypatch, mod, result):
    async def f(*a, **k):
        return result
    monkeypatch.setattr(mod, "settle_charge", f)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,mod,handler,data", [
    ("hooshpay", HP, HPA.hou_check, "hou:check:hp1"),
    ("uniquepay", UP, UPA.upu_check, "upu:check:up1"),
    ("aban", AG, ABA.abu_check, "abu:check:ab1"),
    ("apay", AP, APA.apu_check, "apu:check:ap1"),
])
async def test_not_paid_is_a_toast_not_a_new_message(env, monkeypatch, name, mod, handler, data):
    _settle(monkeypatch, mod, (False, "هنوز پرداختی ثبت نشده است.", 0))

    async def noop(*a, **k):
        return None
    if name == "aban":
        monkeypatch.setattr(AG, "mark_claimed", noop)
    c = CB(data)
    await handler(c, SimpleNamespace())
    assert c.answers == [("ℹ️ هنوز پرداختی ثبت نشده است.", False)]
    assert not c.sent and not c.edited


@pytest.mark.asyncio
@pytest.mark.parametrize("mod,handler,data", [
    (HP, HPA.hou_check, "hou:check:hp1"),
    (UP, UPA.upu_check, "upu:check:up1"),
])
async def test_paid_edits_the_invoice_message_and_sends_nothing_else(env, monkeypatch, mod, handler, data):
    seen = {}

    async def f(bot_id, oid, notify=True):
        seen["notify"] = notify
        return True, "ok", 100_000
    monkeypatch.setattr(mod, "settle_charge", f)
    import tenant.gateways.wallet_receipt as WR

    async def msg(bot_id, amount, bal, method):
        return f"✅ {int(amount):,} تومان شارژ شد", None
    monkeypatch.setattr(WR, "build_wallet_charge_message_for_bot", msg)
    c = CB(data)
    await handler(c, SimpleNamespace())
    assert seen["notify"] is False                                    # settle پیامِ جدید نمی‌فرستد؛ همان فاکتور ویرایش می‌شود
    assert c.edited == ["✅ 100,000 تومان شارژ شد"] and not c.sent
    assert c.answers and "شارژ شد" in c.answers[0][0]


@pytest.mark.asyncio
async def test_aban_paid_only_toasts_and_strips_dead_buttons(env, monkeypatch):
    _settle(monkeypatch, AG, (True, "ok", 100_000))                   # settle_charge خودش پیامِ شارژ/ادامهٔ خرید را می‌فرستد
    c = CB("abu:check:ab1")
    await ABA.abu_check(c, SimpleNamespace())
    assert c.stripped and not c.sent and not c.edited and "شارژ شد" in c.answers[0][0]


@pytest.mark.asyncio
async def test_poll_path_still_notifies_when_notify_default(env, monkeypatch):
    import inspect
    assert inspect.signature(HP.settle_charge).parameters["notify"].default is True
    assert inspect.signature(UP.settle_charge).parameters["notify"].default is True


@pytest.mark.asyncio
async def test_cancel_edits_the_invoice_in_place(env, monkeypatch):
    async def noop(*a, **k):
        return None

    async def nopaid(*a, **k):
        return (False, "", 0)
    for mod in (HP, UP, AG):
        monkeypatch.setattr(mod, "settle_charge", nopaid)
    monkeypatch.setattr(HP, "cancel_invoice", noop)
    monkeypatch.setattr(AG, "cancel_invoice", noop)
    for handler, data in ((HPA.hou_cancel, "hou:cancel:hp1"), (UPA.upu_cancel, "upu:cancel:up1"), (ABA.abu_cancel, "abu:cancel:ab1")):
        c = CB(data)
        await handler(c, SimpleNamespace())
        assert c.edited and "لغو شد" in c.edited[0] and not c.sent, data


@pytest.mark.asyncio
async def test_apay_cancel_edits_in_place(env, monkeypatch):
    async def remote(*a, **k):
        return "cancelled"

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(AP, "cancel_remote", remote)
    monkeypatch.setattr(AP, "_drop_admin_ask", noop)
    import tenant.gateways.apay_log as AL
    monkeypatch.setattr(AL, "log_event", noop)
    c = CB("apu:cancel:ap1")
    await APA.apu_cancel(c, SimpleNamespace(edit_message_reply_markup=noop))
    assert c.edited and "لغو شد" in c.edited[0] and not c.sent
