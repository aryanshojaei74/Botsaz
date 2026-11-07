"""PATCH_TRIAL_STATUS_V1 — دکمهٔ «🔍 وضعیتِ پاک‌سازیِ تست‌ها» (باگِ #191) از روترِ واقعی."""
import pytest

from tests.test_paused_text_e2e import cb_update, env  # noqa: F401  (fixtureِ سرتاسریِ مشترک)


@pytest.mark.asyncio
async def test_trial_menu_has_the_status_button_and_it_opens(env):
    if env["field"] != "vpn_sales":
        pytest.skip("تستِ رایگانِ VPN فقط برایِ رباتِ فروشِ VPN است")
    dp, bot, cap = env["dp"], env["bot"], env["cap"]
    await dp.feed_update(bot, cb_update("set:trial"))
    assert any("set:trial_status" in btns for _t, btns in cap.shown())
    cap.calls.clear()
    await dp.feed_update(bot, cb_update("set:trial_status"))
    t = " ".join(x for x, _b in cap.shown())
    assert "وضعیتِ" in t and "تست" in t
    assert any("set:trial_clean" in btns for _t, btns in cap.shown())
    cap.calls.clear()
    await dp.feed_update(bot, cb_update("set:trial_clean"))             # بدونِ پنل هم بی‌خطا
    assert any("وضعیتِ" in (x or "") for x, _b in cap.shown())
