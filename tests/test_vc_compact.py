"""YOUxsell: صفحهٔ +/− فشردهٔ «حجم/روز دلخواه» — بدونِ تیترِ دکمه‌ها، بدونِ دکمهٔ «مجموع»، دکمهٔ «تایید»، بدونِ «بازگشت به خانه»ِ تکراری."""
from tenant.bots.vpn import vpn_admin as VA
from tenant.bots.vpn import vpn_user as VU


def _cfg(compact):
    c = dict(VA._VC_DEFAULTS)
    c.update(vc_min_gb="1", vc_max_gb="100", vc_step_gb="1", vc_min_days="30", vc_max_days="90",
             vc_max_count="5", vc_price_gb="9000", vc_price_day="0", vc_compact=compact)
    return c


def _texts(kb):
    return [b.text for r in kb.inline_keyboard for b in r]


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


def test_keys_registered_on_both_sides_off_by_default():
    assert VA._VC_DEFAULTS["vc_compact"] == "0" and VU._VC_DEF["vc_compact"] == "0"
    assert "vc_compact" in VA._VC_PANEL_KEYS and "vc_compact" in VU._VC_PANEL_KEYS_U
    assert VA._pvc_key("pasargad", "vc_compact") == VU._pvc_key_u("pasargad", "vc_compact")


def test_default_layout_unchanged():
    kb = VU._vc_cfg_kb(_cfg("0"), 1, 30, 1, no_plans=True)
    t = _texts(kb)
    assert "🎯 انتخاب حجم دلخواه" in t and "🎯 انتخاب روز دلخواه" in t and "🎯 انتخاب تعداد دلخواه" in t
    assert any(x.startswith("💵 مجموع") for x in t) and "💳 پرداخت" in t and "🏠 بازگشت به خانه" in t


def test_compact_layout_removes_headers_total_and_duplicate_home():
    kb = VU._vc_cfg_kb(_cfg("1"), 1, 30, 1, no_plans=True)
    t, c = _texts(kb), _cbs(kb)
    assert not any(x.startswith("🎯 انتخاب") for x in t)                 # تیترِ دکمه‌ها نیست
    assert not any(x.startswith("💵 مجموع") for x in t)                   # دکمهٔ مجموع نیست
    assert "🏠 بازگشت به خانه" not in t and "vc:home" not in c            # «بازگشت به خانه»ِ تکراری نیست
    assert "💳 پرداخت" not in t and "✅ تایید" in t
    # فقط −/مقدار/+ برایِ حجم، روز و تعداد می‌ماند
    for step in ("vc:gb-", "vc:gb+", "vc:d-", "vc:d+", "vc:c-", "vc:c+"):
        assert step in c
    assert any(x.startswith("vc:checkout:1:30:1") for x in c)             # تایید همان مسیرِ قبلیِ پرداخت است


def test_compact_with_plans_keeps_back_to_previous_menu_and_simple_mode_wins():
    kb = VU._vc_cfg_kb(_cfg("1"), 1, 30, 1, no_plans=False)
    assert "vc:back" in _cbs(kb)
    c = dict(_cfg("1"), vc_simple="1")
    kb2 = VU._vc_cfg_kb(c, 1, 30, 1)
    assert "vc:gb:manual" in _cbs(kb2) and "💳 پرداخت" in _texts(kb2)   # حالتِ ساده اولویت دارد؛ فشرده اثر ندارد


def test_admin_has_the_toggle():
    import asyncio
    kb = asyncio.run(VA._vc_kb(_cfg("1"), "pasargad", 1))
    assert "tvpn:custom_compact:pasargad" in _cbs(kb)
