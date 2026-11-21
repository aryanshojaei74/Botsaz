"""PATCH_SVC_PAGE_OPTIONS_V1 (VikingVPN): روزهایِ باقی‌مانده، پنهان‌کردنِ افزایش حجم، تمدیدِ ساده."""
import datetime as dt
import time

import pytest

import tenant
from tenant.bots.vpn import vpn_user as VU


def test_remaining_days_text():
    now = dt.datetime.utcnow()
    assert VU._remaining_days_text(now + dt.timedelta(days=12, hours=3)) == "13 روز"          # رو به بالا
    assert VU._remaining_days_text(now + dt.timedelta(hours=5)) == "1 روز"
    assert VU._remaining_days_text(now - dt.timedelta(days=1)) == "منقضی شده"
    assert VU._remaining_days_text(None) == "بی‌انقضا"
    assert VU._remaining_days_text(None, int((time.time() + 5 * 86400 + 60) * 1000)) == "6 روز"


def test_usage_block_default_unchanged_and_remaining_mode():
    exp = dt.datetime.utcnow() + dt.timedelta(days=20, hours=2)
    usage = {"up": 0, "down": 0, "total": 10 * 1024 ** 3, "expiryTime": int(exp.timestamp() * 1000)}
    full = VU._usage_block(usage, exp)
    assert "⏳ انقضا:" in full and "باقی‌مانده:" not in full.split("⏳")[1]
    rem = VU._usage_block(usage, exp, remaining=True)
    assert "⏳ باقی‌مانده: <b>21 روز</b>" in rem and "انقضا" not in rem
    no_live = VU._usage_block(None, exp, remaining=True)
    assert "باقی‌مانده (طبق ثبت خرید)" in no_live and "21 روز" in no_live
    assert "انقضا (طبق ثبت خرید)" in VU._usage_block(None, exp)                          # پیش‌فرض مثلِ قبل


def test_on_hold_service_keeps_its_special_text_in_remaining_mode():
    usage = {"up": 0, "down": 0, "total": 5 * 1024 ** 3, "expiryTime": 0, "on_hold_seconds": 30 * 86400}
    assert "از اولین اتصال" in VU._usage_block(usage, None, remaining=True)


@pytest.mark.asyncio
async def test_titles_screen_has_the_three_service_page_toggles(monkeypatch):
    from tenant.bots.vpn import vpn_admin as VA
    import tenant.bots.vpn.vpn_titles as VT

    async def raw(bot_id, key):
        return ""
    monkeypatch.setattr(VT, "get_raw", raw)
    st = {}

    async def gs(bot_id, key, default=""):
        return st.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    _t, kb = await VA._titles_screen(1)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert {"vtl:sinfo", "vtl:saddv", "vtl:srens"} <= set(cbs)
    texts = " ".join(b.text for r in kb.inline_keyboard for b in r)
    assert "تاریخِ خرید و انقضا" in texts and "نمایش" in texts and "خاموش" in texts
    st.update(svc_info_mode="remaining", svc_addvol_btn="0", svc_renew_simple="1")
    _t, kb = await VA._titles_screen(1)
    texts = " ".join(b.text for r in kb.inline_keyboard for b in r)
    assert "روزهایِ باقی‌مانده" in texts and "پنهان" in texts and "روشن" in texts


@pytest.mark.asyncio
async def test_toggle_handler_flips_each_setting(monkeypatch):
    from types import SimpleNamespace as NS
    from tenant.bots.vpn import vpn_admin as VA
    st = {}

    async def gs(bot_id, key, default=""):
        return st.get(key, default)

    async def ss(bot_id, key, val):
        st[key] = val

    async def owner(cb, bot):
        return NS(id=1)

    async def menu(cb, bot, state):
        return None
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "set_setting", ss)
    monkeypatch.setattr(VA, "_owner", owner)
    monkeypatch.setattr(VA, "vpn_titles_menu", menu)
    for data, key, first, second in (("vtl:sinfo", "svc_info_mode", "remaining", "full"),
                                     ("vtl:saddv", "svc_addvol_btn", "0", "1"),
                                     ("vtl:srens", "svc_renew_simple", "1", "0")):
        await VA.vpn_svc_page_toggle(NS(data=data), NS(), NS())
        assert st[key] == first
        await VA.vpn_svc_page_toggle(NS(data=data), NS(), NS())
        assert st[key] == second
