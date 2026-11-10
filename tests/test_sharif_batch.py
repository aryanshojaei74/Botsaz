from types import SimpleNamespace as NS
from tenant.bots.virtual_service import smm
from tenant.gateways import apay as AP


def test_unit_label():
    assert smm.unit_label(NS(unit_mode="auto", max_qty=1, unit_name=None)) == "هر عدد"
    assert smm.unit_label(NS(unit_mode="auto", max_qty=1, unit_name="اکانت")) == "هر اکانت"
    assert smm.unit_label(NS(unit_mode="per_1000", max_qty=1, unit_name="کد")) == "هر ۱۰۰۰ کد"
    assert smm.unit_label(NS(unit_mode="auto", max_qty=100, unit_name=None)) == "هر ۱۰۰۰ عدد"


def _btns(**kw):
    t, kb = AP.invoice_view(title="x", pay_rial=300890, card="6219861965232381", holder="h", order_id="o",
                            pay_url="https://x.y", check_cb="c", cancel_cb="d", **kw)
    return t, [b.text for r in kb.inline_keyboard for b in r]


def test_apay_hide_pay_btn():
    t, b = _btns()
    assert "💳 پرداخت" in b and "«💳 پرداخت» را بزن" in t
    t, b = _btns(show_pay_btn=False)
    assert "💳 پرداخت" not in b and "«💳 پرداخت»" not in t
    # بدونِ کارت دکمه تنها راه است، پنهان نمی‌شود
    t2, kb = AP.invoice_view(title="x", pay_rial=1, card="", holder="", order_id="o", pay_url="https://x.y",
                             check_cb="c", cancel_cb="d", show_pay_btn=False)
    assert "💳 پرداخت" in [b.text for r in kb.inline_keyboard for b in r]
