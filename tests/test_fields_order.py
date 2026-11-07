import platform_bot as PB


def test_funstat_locked_and_active_first():
    assert "funstat" not in PB.ACTIVE_FIELDS and "funstat" in PB.FIELDS
    keys = [k for k, _ in PB._fields_ordered()]
    flags = [k in PB.ACTIVE_FIELDS for k in keys]
    assert flags == sorted(flags, reverse=True)      # فعال‌ها اول، قرمزها آخر
    assert keys[-1] != "vpn_sales" and keys.index("vpn_sales") < keys.index("funstat")
