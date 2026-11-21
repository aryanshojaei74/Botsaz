"""لاگِ آخرِ شب: سه اصلاحِ کوچک."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def test_first_connect_loop_ignores_non_dict_clients():
    src = _src("main_tenant.py")
    i = src.index("PATCH_FIRSTCONN_BAD_LIST_V1")
    blk = src[i:i + 600]
    assert "isinstance(c, dict)" in blk and blk.index("isinstance(c, dict)") < blk.index("by_email =")
    clients = ["error", {"email": "a"}, None]          # همان منطق: رشته‌ها کنار گذاشته می‌شوند
    clients = [c for c in clients if isinstance(c, dict)] if isinstance(clients, (list, tuple)) else []
    assert {c.get("email"): c for c in clients if c.get("email")} == {"a": {"email": "a"}}


def test_panelyar_test_client_is_closed_in_finally():
    src = _src("tenant", "shared", "panel_clients", "panelyar.py")
    i = src.index("PATCH_PYAR_TEST_CLOSE_V1")
    assert "finally:" in src[i - 200:i] and "await client.close()" in src[i:i + 250]


def test_ozv_nobalance_log_is_throttled():
    src = _src("tenant", "bots", "virtual_service", "ozvinoo_vnum.py")
    i = src.index("PATCH_OZV_NOBALANCE_LOG_QUIET_V1")
    assert "> 600" in src[i - 120:i + 10]
