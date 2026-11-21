import time, datetime
from tenant.bots.vpn.vpn_user import _svc_time_expired as E


def test_expired_by_panel_ms():
    assert E({"expiryTime": int((time.time() - 3600) * 1000)}) is True
    assert E({"expiryTime": int((time.time() + 3600) * 1000)}) is False


def test_unlimited_or_unknown_not_expired():
    assert E({"expiryTime": 0}) is False and E(None) is False and E({}) is False


def test_fallback_date():
    assert E(None, datetime.datetime.utcnow() - datetime.timedelta(days=1)) is True
    assert E({"expiryTime": 0}, datetime.datetime.utcnow() + datetime.timedelta(days=1)) is False
