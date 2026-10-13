"""PATCH_VC_SIMPLE_MODE_V1 — حالتِ ساده‌یِ «حجم دلخواه»: فقط عددِ حجم، مدتِ ثابتِ ادمین."""
from tenant.bots.vpn import vpn_admin as VA
from tenant.bots.vpn import vpn_user as VU


def _cfg(simple):
    c = dict(VA._VC_DEFAULTS)
    c.update(vc_min_gb="5", vc_max_gb="100", vc_step_gb="5", vc_min_days="30", vc_max_days="90",
             vc_max_count="5", vc_price_gb="2500", vc_price_day="0", vc_simple=simple)
    return c


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


def test_defaults_and_panel_keys_registered():
    assert VA._VC_DEFAULTS["vc_simple"] == "0" and VA._VC_DEFAULTS["vc_simple_days"] == "30"
    assert "vc_simple" in VA._VC_PANEL_KEYS and "vc_simple_days" in VA._VC_PANEL_KEYS


def test_simple_keyboard_hides_days_and_count_rows():
    full = _cbs(VU._vc_cfg_kb(_cfg("0"), 10, 30, 1))
    simple = _cbs(VU._vc_cfg_kb(_cfg("1"), 10, 30, 1))
    assert "vc:d+" in full and "vc:c+" in full and "vc:gb+" in full
    assert "vc:d+" not in simple and "vc:c+" not in simple and "vc:gb+" not in simple
    assert "vc:gb:manual" in simple and any(c.startswith("vc:checkout:10:30:1") for c in simple)
