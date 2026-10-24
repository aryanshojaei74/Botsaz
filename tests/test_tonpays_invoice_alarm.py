"""آلارمِ «دقیقاً مبلغ فاکتور را واریز کنید» لحظهٔ «🧾 ساخت فاکتور» (درخواستِ دیار)."""
from types import SimpleNamespace

import pytest

import tenant
from tenant.gateways import tonpays as TP
from tenant.gateways.exact_amount_alert import ALERT_TEXT

pytestmark = pytest.mark.asyncio


class _St:
    def __init__(self, d=None):
        self.d, self.s = dict(d or {}), None

    async def get_data(self):
        return dict(self.d)

    async def update_data(self, **k):
        self.d.update(k)

    async def set_state(self, s):
        self.s = s

    async def clear(self):
        self.d, self.s = {}, None


def _cb():
    calls = []

    async def ans(text=None, show_alert=None, **k):
        calls.append(("answer", text, show_alert))

    async def edit(*a, **k):
        calls.append(("edit",))
    msg = SimpleNamespace(answer=None, edit_reply_markup=edit, edit_text=edit)
    return SimpleNamespace(answer=ans, message=msg, from_user=SimpleNamespace(id=77)), calls


async def test_make_invoice_click_shows_alarm_then_creates(monkeypatch):
    created = []

    async def gbr(bot):
        return SimpleNamespace(id=1)

    async def fake_create(bot, row, msg, user_id, amount):
        created.append((user_id, amount))
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(TP, "_create_invoice", fake_create)
    cb, calls = _cb()
    st = _St({"tonp_amount": 50000})
    await TP.tonp_make_invoice(cb, object(), st)
    assert calls[0] == ("answer", ALERT_TEXT, True)             # اولین چیز: آلارم
    assert created == [(77, 50000)]                              # بعد: فاکتور، برایِ کاربرِ واقعی نه رباتِ پیام
    assert st.d == {}


async def test_make_invoice_without_amount_is_safe(monkeypatch):
    async def gbr(bot):
        return SimpleNamespace(id=1)
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    cb, calls = _cb()
    await TP.tonp_make_invoice(cb, object(), _St())
    assert calls[0][0] == "answer" and calls[0][2] is True and calls[0][1] != ALERT_TEXT


async def _amount(monkeypatch, setting):
    from tenant.gateways import wallet_limits as WL
    created, sent = [], []

    async def gbr(bot):
        return SimpleNamespace(id=1)

    async def gs(bid, key, default=""):
        return setting if key == "pay_exact_alert" else default

    async def chk(*a, **k):
        return None

    async def fake_create(bot, row, msg, user_id, amount):
        created.append((user_id, amount))
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(WL, "check", chk)
    monkeypatch.setattr(TP, "_create_invoice", fake_create)

    async def answer(text, **k):
        sent.append((text, k.get("reply_markup")))
    msg = SimpleNamespace(text="50000", from_user=SimpleNamespace(id=77), answer=answer)
    st = _St()
    await TP.tonpays_amount(msg, object(), st)
    return created, sent, st


async def test_amount_step_asks_to_press_make_invoice_when_alarm_on(monkeypatch):
    created, sent, st = await _amount(monkeypatch, "1")
    assert created == [] and st.s == TP.TonPaysFSM.confirm and st.d["tonp_amount"] == 50000
    kb = sent[0][1]
    assert [b.callback_data for r in kb.inline_keyboard for b in r] == ["tonp:mk", "tonp:mkcancel"]


async def test_amount_step_creates_directly_when_alarm_off(monkeypatch):
    created, sent, st = await _amount(monkeypatch, "0")
    assert created == [(77, 50000)] and sent == []
