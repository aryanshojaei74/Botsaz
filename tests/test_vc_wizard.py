"""PATCH_VC_WIZARD_V1 (تیکت #936): خریدِ مرحله‌ایِ پیام‌به‌پیام — حجم ← مدت ← تعداد ← نام ← پیش‌فاکتور."""
from types import SimpleNamespace as NS

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from tenant.bots.vpn import vpn_admin as VA
from tenant.bots.vpn import vpn_user as VU

CFG = {"vc_enabled": "1", "vc_min_gb": "10", "vc_max_gb": "100", "vc_step_gb": "10", "vc_min_days": "30",
       "vc_max_days": "90", "vc_step_days": "30", "vc_max_count": "5", "vc_price_gb": "1000", "vc_price_day": "500",
       "vc_wizard": "1", "vc_simple": "0"}


class _Msg:
    def __init__(self, text, uid=9):
        self.text, self.from_user = text, NS(id=uid, is_bot=False)
        self.out = []

    async def answer(self, text, **k):
        self.out.append(text)


@pytest.fixture
def env(monkeypatch):
    cfg = dict(CFG)

    async def vc_all(bot_id, tg, pk="", days=None, gb=None, **k):
        return cfg
    monkeypatch.setattr(VU, "_vc_all_for_user", vc_all)
    asked = []

    async def ask_name(target, plan_id, kind="plan", bot_id=0):
        asked.append(kind)
    monkeypatch.setattr(VU, "_ask_custom_name", ask_name)

    async def wrow(bot):
        return NS(id=1)
    monkeypatch.setattr(VU, "_wiz_row", wrow)
    st = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=9, user_id=9))
    return cfg, st, asked


@pytest.mark.asyncio
async def test_full_wizard_flow_asks_each_field_in_separate_messages_then_the_name(env):
    cfg, st, asked = env
    m0 = _Msg("")
    await VU._wiz_begin(m0, 1, 9, cfg, "sanai", st)
    assert "حجمِ بسته" in m0.out[0] and await st.get_state() == VU._VcWizardFSM.gb.state
    m1 = _Msg("۵۰")
    await VU.vc_wiz_gb(m1, NS(token="1:T"), st)
    assert "حجم: 50 گیگ" in m1.out[0] and "مدتِ بسته" in m1.out[0] and await st.get_state() == VU._VcWizardFSM.days.state
    m2 = _Msg("60")
    await VU.vc_wiz_days(m2, NS(), st)
    assert "مدت: 60 روز" in m2.out[0] and "تعدادِ سفارش" in m2.out[0] and await st.get_state() == VU._VcWizardFSM.count.state
    m3 = _Msg("2")
    await VU.vc_wiz_count(m3, NS(), st)
    assert m3.out == ["✅ تعداد: 2"] and asked == ["vc"]
    d = await st.get_data()
    assert (d["vc_gb"], d["vc_days"], d["vc_count"]) == (50, 60, 2) and d["vcustom_label"] is None
    assert await st.get_state() == VU._VpnNameFSM.naming_vc.state          # همان مرحلهٔ نامِ موجود ← پیش‌فاکتور


@pytest.mark.asyncio
async def test_validation_snapping_cancel_and_single_count_skip(env):
    cfg, st, asked = env
    await VU._wiz_begin(_Msg(""), 1, 9, cfg, "sanai", st)
    bad = _Msg("500")
    await VU.vc_wiz_gb(bad, NS(), st)
    assert "10 تا 100" in bad.out[0] and await st.get_state() == VU._VcWizardFSM.gb.state
    txt = _Msg("abc")
    await VU.vc_wiz_gb(txt, NS(), st)
    assert "عددِ معتبر" in txt.out[0]
    snap = _Msg("47")                                           # به نزدیک‌ترین مضربِ گام (۱۰) گرد می‌شود
    await VU.vc_wiz_gb(snap, NS(), st)
    assert (await st.get_data())["vc_gb"] == 50
    cfg["vc_max_count"] = "1"                                   # تعدادِ مجاز ۱ ⇒ مرحلهٔ تعداد رد می‌شود
    await VU.vc_wiz_days(_Msg("30"), NS(), st)
    assert asked == ["vc"] and (await st.get_data())["vc_count"] == 1
    st2 = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=8, user_id=8))
    await VU._wiz_begin(_Msg("", 8), 1, 8, cfg, "sanai", st2)
    c = _Msg("لغو", 8)
    await VU.vc_wiz_gb(c, NS(), st2)
    assert await st2.get_state() is None and "لغو" in c.out[0]


def test_wizard_setting_is_registered_in_both_key_tables_and_has_an_admin_toggle():
    assert "vc_wizard" in VU._VC_DEF and "vc_wizard" in VA._VC_DEFAULTS
    assert "vc_wizard" in VU._VC_PANEL_KEYS_U and "vc_wizard" in VA._VC_PANEL_KEYS
    import inspect
    assert "tvpn:custom_wizard:" in inspect.getsource(VA)
