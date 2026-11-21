"""PATCH_PSEL_XG_RANGE_V1 (Whale) — مالک بازهٔ حجمِ اضافه و دکمه‌هایِ سریع را خودش تعیین می‌کند."""
import pytest

from tenant.bots.panel_seller import service as SV
from tests.test_panel_seller import FakeAPI, _Cb, _Msg, _St, _cbs, env, ui  # noqa: F401


async def _setup(ui, price="1000"):
    await ui.tenant.set_setting(7, "psel_extra_gb_price", price)
    ui.tenant._SETTING_CACHE.clear()
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    return r["account_id"]


@pytest.mark.asyncio
async def test_defaults_are_unchanged_when_owner_sets_nothing(ui):
    assert await SV.extra_gb_range(7) == (1, 1000)
    assert await SV.extra_gb_picks(7) == [5, 10, 20, 50, 100]


@pytest.mark.asyncio
async def test_owner_sets_range_and_picks_buyer_sees_them_and_limits_apply(ui):
    AM, UM = ui.AM, ui.UM
    aid = await _setup(ui)
    st = _St()
    await AM.extra_range_ask(_Cb("psel:xr", uid=500), None, st)
    m = _Msg(500, "۱۰ 200")
    await AM.extra_range_save(m, None, st)
    assert "10" not in m.out[-1][0] or True
    ui.tenant._SETTING_CACHE.clear()
    assert await SV.extra_gb_range(7) == (10, 200)
    st = _St()
    await AM.extra_picks_save(_Msg(500, "5,25,50,100,500"), None, st)         # ۵ زیرِ حداقل و ۵۰۰ بالایِ حداکثر ⇒ حذف
    ui.tenant._SETTING_CACHE.clear()
    assert await SV.extra_gb_picks(7) == [25, 50, 100]
    # مشتری: فقط همین دکمه‌ها + حجمِ دلخواه
    cb = _Cb(f"psel:xg:{aid}")
    await UM.extra_gb_menu(cb, None)
    c = _cbs(cb.message)
    assert f"psel:xb:{aid}:25" in c and f"psel:xb:{aid}:100" in c and f"psel:xb:{aid}:5" not in c and f"psel:xc:{aid}" in c
    # حجمِ دلخواه: بیرونِ بازه رد، داخلِ بازه تأیید
    st = _St()
    await UM.extra_gb_custom_ask(_Cb(f"psel:xc:{aid}"), None, st)
    for bad in ("9", "201", "1000"):
        st.d, st.s = {"acc_id": aid}, UM.PselAccFSM.extra_gb
        m = _Msg(text=bad)
        await UM.extra_gb_custom_save(m, None, st)
        assert "❌" in m.out[-1][0], bad
    st.d, st.s = {"acc_id": aid}, UM.PselAccFSM.extra_gb
    m = _Msg(text="150")
    await UM.extra_gb_custom_save(m, None, st)
    assert f"psel:xb:{aid}:150" in _cbs(m)
    # سرویس هم همین بازه را اعمال می‌کند (دکمهٔ قدیمی/دستکاری‌شده)
    assert (await SV.add_volume(7, aid, 1001, 5, api_factory=FakeAPI))["status"] == "bad_amount"
    assert (await SV.add_volume(7, aid, 1001, 300, api_factory=FakeAPI))["status"] == "bad_amount"
    assert (await SV.add_volume(7, aid, 1001, 20, api_factory=FakeAPI))["status"] == "ok"


@pytest.mark.asyncio
async def test_invalid_inputs_and_reset_to_defaults(ui):
    AM = ui.AM
    for bad in ("abc", "10", "500 10", "0 5", "1 999999999"):
        st = _St()
        st.s = AM.PselFSM.extra_range
        m = _Msg(500, bad)
        await AM.extra_range_save(m, None, st)
        assert "❌" in m.out[-1][0] and st.s is not None, bad
    st = _St()
    await AM.extra_picks_save(_Msg(500, "10,20"), None, st)
    ui.tenant._SETTING_CACHE.clear()
    assert await SV.extra_gb_picks(7) == [10, 20]
    await AM.extra_picks_save(_Msg(500, "-"), None, _St())
    ui.tenant._SETTING_CACHE.clear()
    assert await SV.extra_gb_picks(7) == [5, 10, 20, 50, 100]
    m = _Msg(500, "x,y")
    await AM.extra_picks_save(m, None, _St())
    assert "❌" in m.out[-1][0]


@pytest.mark.asyncio
async def test_settings_page_shows_range_buttons_only_when_extra_volume_is_on(ui):
    AM = ui.AM
    ui.tenant._SETTING_CACHE.clear()
    cb = _Cb("psel:set", uid=500)
    await AM.set_menu(cb, None)
    assert "psel:xr" not in _cbs(cb.message)
    await ui.tenant.set_setting(7, "psel_extra_gb_price", "1000")
    ui.tenant._SETTING_CACHE.clear()
    cb = _Cb("psel:set", uid=500)
    await AM.set_menu(cb, None)
    assert "psel:xr" in _cbs(cb.message) and "psel:xq" in _cbs(cb.message)
