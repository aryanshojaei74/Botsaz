from tenant.bots.vpn.vpn_user import _with_icon


def test_with_icon():
    assert _with_icon(True, "🛰", "vip") == "🛰 vip"
    assert _with_icon(False, "🛰", "vip") == "vip"
    assert _with_icon(True, "", "vip") == "vip"
