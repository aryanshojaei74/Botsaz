"""PATCH_PSEL_XG_CUSTOM_V1 (Whale) — خریدِ حجمِ اضافه با مقدارِ دلخواه."""
import pytest

from tenant.bots.panel_seller import service as SV
from tests.test_panel_seller import FakeAPI, _Cb, _Msg, _St, _cbs, env, ui  # noqa: F401


async def _acct(ui):
    await ui.tenant.set_setting(7, "psel_extra_gb_price", "1000")
    ui.tenant._SETTING_CACHE.clear()
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    assert r["status"] == "ok"
    return r["account_id"]


@pytest.mark.asyncio
async def test_menu_has_custom_button_and_flow_ends_in_a_confirm_button(ui):
    UM = ui.UM
    aid = await _acct(ui)
    cb = _Cb(f"psel:xg:{aid}")
    await UM.extra_gb_menu(cb, None)
    assert f"psel:xc:{aid}" in _cbs(cb.message) and f"psel:xb:{aid}:10" in _cbs(cb.message)
    st = _St()
    await UM.extra_gb_custom_ask(_Cb(f"psel:xc:{aid}"), None, st)
    assert st.d["acc_id"] == aid and st.s is UM.PselAccFSM.extra_gb
    m = _Msg(text="۲۵")                                              # ارقامِ فارسی
    await UM.extra_gb_custom_save(m, None, st)
    assert f"psel:xb:{aid}:25" in _cbs(m) and "25" in m.out[-1][0].replace("۲۵", "25") and st.s is None
    # خریدِ همان دکمه ⇒ ۲۵ گیگ اضافه (مسیرِ موجودِ psel:xb)
    cb = _Cb(f"psel:xb:{aid}:25")
    await UM.extra_gb_buy(cb, None)
    assert "حجمِ اضافه ثبت شد" in cb.message.out[-1][0] and "125" in cb.message.out[-1][0].replace("۱۲۵", "125")


@pytest.mark.asyncio
async def test_invalid_amounts_are_rejected_and_state_kept(ui):
    UM = ui.UM
    aid = await _acct(ui)
    for bad in ("0", "abc", "-5", "1001", "2.5"):
        st = _St()
        st.d = {"acc_id": aid}
        st.s = UM.PselAccFSM.extra_gb
        m = _Msg(text=bad)
        await UM.extra_gb_custom_save(m, None, st)
        assert "❌" in m.out[-1][0] and st.s is not None, bad
    st = _St()
    st.d = {"acc_id": aid}
    st.s = UM.PselAccFSM.extra_gb
    m = _Msg(text="/cancel")
    await UM.extra_gb_custom_save(m, None, st)
    assert "لغو" in m.out[-1][0] and st.s is None


@pytest.mark.asyncio
async def test_other_buyer_cannot_use_someone_elses_panel(ui):
    UM = ui.UM
    aid = await _acct(ui)
    st = _St()
    st.d = {"acc_id": aid}
    st.s = UM.PselAccFSM.extra_gb
    m = _Msg(uid=999, text="10")
    await UM.extra_gb_custom_save(m, None, st)
    assert not any(f"psel:xb:{aid}" in str(o[1]) for o in m.out) and st.s is None
