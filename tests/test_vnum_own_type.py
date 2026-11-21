from types import SimpleNamespace as NS
from tenant.bots.virtual_service import smm, smm_api


def _v(**k):
    d = dict(service_id="ozv:1", category="", type_key="", platform_key="", name="🇺🇸 آمریکا")
    d.update(k)
    return NS(**d)


def test_vnum_rows_share_one_type():
    a = smm._svc_platform_type(_v(type_key="other", platform_key="telegram"))
    b = smm._svc_platform_type(_v(type_key="virtual_number", platform_key="telegram"))
    c = smm._svc_platform_type(_v(platform_key=""))
    assert a == b == ("telegram", "virtual_number") and c[1] == "virtual_number"
    assert smm_api.service_type_label("virtual_number") != "📦 سایر"


def test_normal_service_unchanged():
    v = _v(service_id="55", type_key="channel_member", platform_key="telegram", name="x")
    assert smm._svc_platform_type(v) == ("telegram", "channel_member")
