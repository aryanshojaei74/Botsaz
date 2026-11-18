"""ایلیا: تستِ رایگانِ نماینده «خرید» نیست (کانالِ اعتماد نه، مالک/ادمین با لینکِ ساب بله) + قالبِ فاکتورِ آپی (پرو)."""
import types

import pytest

import tenant
from plans import Feature, has_feature
from tenant.gateways import apay as AP
from tenant.gateways import apay_invoice_tpl as IT


@pytest.mark.asyncio
async def test_purchase_alert_public_false_skips_trust_and_tx_log_but_notifies_admins_with_extra(monkeypatch):
    calls = {"trust": 0, "tx": 0, "admin": []}

    async def trust(*a, **k): calls["trust"] += 1

    async def txlog(*a, **k): calls["tx"] += 1

    async def notify(bot, row, text, kb=None): calls["admin"].append(text)

    async def gs(bid, key, default=""):
        return "1" if key == "purchase_notify" else default
    import tenant.shared.trust_report as TR
    import tenant.shared.tx_channel as TX
    monkeypatch.setattr(TR, "send_trust_channel_post", trust)
    monkeypatch.setattr(TX, "tx_log_purchase", txlog)
    monkeypatch.setattr(tenant, "_notify_admins", notify)
    monkeypatch.setattr(tenant, "get_setting", gs)
    row = types.SimpleNamespace(id=5, username="b", plan="pro")
    buyer = types.SimpleNamespace(telegram_id=9, username="u", full_name="N")
    await tenant._purchase_alert(None, row, buyer=buyer, section="s", detail="20 MB × 1", amount=0.0,
                                 public=False, extra="🔗 لینکِ ساب:\n<code>vless://x</code>")
    assert calls["trust"] == 0 and calls["tx"] == 0
    assert len(calls["admin"]) == 1 and "vless://x" in calls["admin"][0]
    # رفتارِ عادی (public=True) دست‌نخورده: کانالِ اعتماد و لاگ هر دو صدا زده می‌شوند
    await tenant._purchase_alert(None, row, buyer=buyer, section="s", detail="d", amount=1000.0)
    assert calls["trust"] == 1 and calls["tx"] == 1


def test_web_panel_trial_wiring():
    from pathlib import Path
    src = Path("tenant/shared/web_panel.py").read_text(encoding="utf-8")
    i = src.index("async def rs_trial_create")
    seg = src[i:src.index("def _rs_cfg_cards", i)]
    assert "public=False" in seg and "links_out=_trial_links" in seg and 'link="\\n".join(_trial_links)' in seg


def test_apay_template_render_and_feature_gate():
    v = IT.values_for(title="آپی", pay_rial=5000370, fee_toman=0, credit_toman=500000, card="6037991234567893",
                      holder="علی <رضایی>", order_id="AP1", buyer="Ali", username="ali", user_id=7, balance=1000)
    out = IT.render("واریز {pay_toman} تومان به {card} ({holder}) کد {code} {unknown}", v)
    assert "500,037" in out and "6037991234567893" in out and "علی &lt;رضایی&gt;" in out and "AP1" in out
    assert "{unknown}" in out                                   # متغیرِ ناشناس عیناً می‌ماند
    assert Feature.APAY_INVOICE_TPL
    assert has_feature("pro", Feature.APAY_INVOICE_TPL) and not has_feature("growth", Feature.APAY_INVOICE_TPL)
    assert not has_feature("free", Feature.APAY_INVOICE_TPL)


def test_invoice_view_uses_template_text_but_keeps_keyboard():
    kw = dict(title="آپی", pay_rial=5000370, card="6037", holder="h", order_id="AP1", pay_url="https://x.y",
              check_cb="c", cancel_cb="d", fee_toman=0, credit_toman=500000)
    t0, kb0 = AP.invoice_view(**kw)
    t1, kb1 = AP.invoice_view(**kw, tpl_text="متنِ من")
    assert t1 == "متنِ من" and t0 != t1
    assert [[b.callback_data or b.url for b in r] for r in kb0.inline_keyboard] == \
           [[b.callback_data or b.url for b in r] for r in kb1.inline_keyboard]
