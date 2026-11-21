"""VikingVPN: خطِ «یکی را انتخاب کن:» قابلِ حذف + نامِ دکمهٔ «بدون نام (ادامه)» قابلِ تغییر."""
from types import SimpleNamespace as NS

import pytest

import tenant
from tenant.bots.vpn import vpn_user as VU


@pytest.fixture
def store(monkeypatch):
    st = {}

    async def gs(bot_id, key, default=""):
        return st.get(key, default)

    async def ss(bot_id, key, val):
        st[key] = val
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "set_setting", ss)
    return st


@pytest.mark.asyncio
async def test_pick_line_default_shown_and_can_be_hidden(store):
    assert await VU._pick_suffix(1, "یکی را انتخاب کن:") == "\n\nیکی را انتخاب کن:"
    store["vpn_pick_line"] = "0"
    assert await VU._pick_suffix(1, "یکی را انتخاب کن:") == ""
    store["vpn_pick_line"] = "1"
    assert (await VU._pick_suffix(1, "x")).endswith("x")


@pytest.mark.asyncio
async def test_skip_label_default_custom_and_reset(store):
    assert await VU._cfg_name_skip_label(1) == "⏭ بدون نام (ادامه)"
    store["cfg_name_skip_label"] = "خودکار انتخاب کن!"
    assert await VU._cfg_name_skip_label(1) == "خودکار انتخاب کن!"
    store["cfg_name_skip_label"] = ""
    assert await VU._cfg_name_skip_label(1) == "⏭ بدون نام (ادامه)"


@pytest.mark.asyncio
async def test_name_prompt_uses_custom_skip_label_and_same_callbacks(store, monkeypatch):
    store["cfg_name_skip_label"] = "خودکار انتخاب کن!"
    out = []

    class T:
        async def answer(self, text, reply_markup=None, **k):
            out.append(reply_markup)
    await VU._ask_custom_name(T(), 7, "plan", 1)
    btn = out[0].inline_keyboard[0][0]
    assert btn.text == "خودکار انتخاب کن!" and btn.callback_data == "vname:skip:7"


@pytest.mark.asyncio
async def test_titles_screen_has_pick_and_service_name_toggles(store, monkeypatch):
    from tenant.bots.vpn import vpn_admin as VA
    from tenant.bots.vpn import vpn_titles as VT

    async def raw(bot_id, key):
        return ""
    import tenant.bots.vpn.vpn_titles as _vt
    monkeypatch.setattr(_vt, "get_raw", raw)
    text, kb = await VA._titles_screen(1)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "vtl:pick" in cbs and "vtl:svcn" in cbs
