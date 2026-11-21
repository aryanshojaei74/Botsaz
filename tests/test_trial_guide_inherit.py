"""PATCH_TRIAL_GUIDE_INHERIT_V1 (MidnightSoul) — آموزش‌هایِ موجود زیرِ سرویس‌هایِ تست هم بیاید."""
import pytest

from tests.test_paused_text_e2e import cb_update, env  # noqa: F401


def _only_vpn(env):
    if env["field"] != "vpn_sales":
        pytest.skip("آموزشِ اتصالِ کانفیگ مخصوصِ رباتِ VPN است")


@pytest.mark.asyncio
async def test_trial_inherits_selected_source_and_own_text_wins(env):
    _only_vpn(env)
    t, bid = env["tenant"], env["bid"]
    for k, v in (("cfg_guide_enabled_manual", "1"), ("cfg_guide_text_manual", "آموزشِ دستی")):
        await t.set_setting(bid, k, v)
    t._SETTING_CACHE.clear()
    assert await t._cfg_guide_row(bid, t.TRIAL_GUIDE_SOURCE) is None                 # هنوز ارث‌بری نشده
    await t.set_setting(bid, "cfg_guide_trial_from", "manual")
    t._SETTING_CACHE.clear()
    row = await t._cfg_guide_row(bid, t.TRIAL_GUIDE_SOURCE)
    assert row and row[0].callback_data == "cfgguide:show:manual"                    # دکمهٔ آموزشِ بخشِ دستی
    for k, v in (("cfg_guide_enabled_trial", "1"), ("cfg_guide_text_trial", "آموزشِ اختصاصیِ تست")):
        await t.set_setting(bid, k, v)
    t._SETTING_CACHE.clear()
    row = await t._cfg_guide_row(bid, t.TRIAL_GUIDE_SOURCE)
    assert row[0].callback_data == "cfgguide:show:trial"                             # متنِ اختصاصی اولویت دارد


@pytest.mark.asyncio
async def test_other_sources_are_unaffected_by_trial_inheritance(env):
    _only_vpn(env)
    t, bid = env["tenant"], env["bid"]
    await t.set_setting(bid, "cfg_guide_trial_from", "manual")
    t._SETTING_CACHE.clear()
    assert await t._cfg_guide_row(bid, "unlimited") is None                           # ارث‌بری فقط برایِ تست است


@pytest.mark.asyncio
async def test_owner_ui_sets_and_clears_the_inheritance(env):
    _only_vpn(env)
    dp, bot, cap, t, bid = env["dp"], env["bot"], env["cap"], env["tenant"], env["bid"]
    await dp.feed_update(bot, cb_update(f"tadm:cfgguide:pick:{t.TRIAL_GUIDE_SOURCE}"))
    assert any("tadm:cfgguide:inh" in btns for _t, btns in cap.shown())
    cap.calls.clear()
    await dp.feed_update(bot, cb_update("tadm:cfgguide:inh"))
    assert any("tadm:cfgguide:inhset:manual" in btns for _t, btns in cap.shown())
    await dp.feed_update(bot, cb_update("tadm:cfgguide:inhset:manual"))
    t._SETTING_CACHE.clear()
    assert await t.get_setting(bid, "cfg_guide_trial_from", "") == "manual"
    await dp.feed_update(bot, cb_update("tadm:cfgguide:inhset:-"))
    t._SETTING_CACHE.clear()
    assert await t.get_setting(bid, "cfg_guide_trial_from", "") == ""
