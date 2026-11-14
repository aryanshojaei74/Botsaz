"""SINA 7 Nov: دکمهٔ منوی اصلی زیرِ رسیدِ واریز، حذفِ جملهٔ اشتباه، دکمهٔ صفحهٔ پرداختِ بلوپال، تغییر کارت، ریپلایِ ادامهٔ AI."""
import inspect
import time
from types import SimpleNamespace

import pytest

import platform_group_guard as G
from tenant.gateways import wallet_receipt as WR


def test_wrong_sentence_removed_from_deposit_message():
    text, kb = WR.build_wallet_charge_message(100000, 200000, "card", virtual=True)
    assert "روی دکمهٔ «🛍 خرید محصول» کلیک کنید" not in text
    assert "استفاده کنید" in text


@pytest.mark.asyncio
async def test_home_button_under_receipt_is_optional(monkeypatch):
    import tenant
    store = {}

    async def gs(bot_id, key, default=""):
        return store.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    import tenant.gateways.wallet_receipt_buttons as B

    async def no_kb(bot_id):
        return None
    monkeypatch.setattr(B, "wallet_receipt_keyboard", no_kb)

    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a): return None
    monkeypatch.setattr("config.Session", lambda: _S())
    _t, kb = await WR.build_wallet_charge_message_for_bot(1, 1000, 2000, "card")
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "mbtn:home" not in datas
    store["wallet_receipt_home_btn"] = "1"
    _t, kb = await WR.build_wallet_charge_message_for_bot(1, 1000, 2000, "card")
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert datas[-1] == "mbtn:home"


def test_blupal_pay_button_toggle_wired():
    import tenant.gateways.blupal_admin as A
    src = inspect.getsource(A)
    assert "blupal_show_pay_btn" in src and 'bpl:pay_btn' in src and "_show_pay" in src


def test_cc_button_toggle_wired():
    import tenant.shared.kyc_user as KU
    import tenant.shared.kyc_admin as KA
    assert "card_change_btn_enabled" in inspect.getsource(KU)
    assert "kyc:ccbtn" in inspect.getsource(KA)


def test_ai_answer_followup_tracking():
    msg = SimpleNamespace(chat=SimpleNamespace(id=-100),
                          reply_to_message=SimpleNamespace(message_id=9, from_user=SimpleNamespace(id=42)))
    assert not G._is_ai_answer_reply(msg, 42)
    G._remember_ai_answer(-100, 9)
    assert G._is_ai_answer_reply(msg, 42)
    assert not G._is_ai_answer_reply(msg, 43)             # جوابِ ربات دیگری نیست
    G._ai_answer_ids[(-100, 9)] = time.monotonic() - G._AI_ANSWER_TTL_SEC - 5
    assert not G._is_ai_answer_reply(msg, 42)             # منقضی
