import pytest
from tenant.bots.vpn import vpn_invoice_tpl as it
from plans import Feature, has_feature


def test_render_vars_and_escape():
    out = it.render("سرور: {server} | {category} | {total} | {zzz}",
                    {"server": "A<b>", "category": "ویژه", "total": "400,000"})
    assert out == "سرور: A&lt;b&gt; | ویژه | 400,000 | {zzz}"


def test_warn_is_not_escaped_and_note_is_html():
    out = it.render("{warn}{note}", {"warn": "⚠️ x", "note": "<b>n</b>"})
    assert out == "⚠️ x<b>n</b>"


def test_pro_only():
    assert has_feature("pro", Feature.VPN_INVOICE_TPL)
    assert not has_feature("growth", Feature.VPN_INVOICE_TPL)
    assert not has_feature("free", Feature.VPN_INVOICE_TPL)


@pytest.mark.asyncio
async def test_get_template_gated_by_plan(monkeypatch):
    import tenant

    async def _gs(bot_id, key, default=""):
        return "T {server}"
    monkeypatch.setattr(tenant, "get_setting", _gs)
    assert await it.get_template(1, "pro") == "T {server}"
    assert await it.get_template(1, "growth") == ""
