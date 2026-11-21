"""PATCH_METERED_PANEL_ERR_QUIET_V1: پنلِ خراب در حلقهٔ حجمی هر دور traceback نمی‌سازد؛ ساعتی یک خط هشدار."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_panel_errors_are_throttled_and_other_errors_still_logged():
    src = open(os.path.join(ROOT, "tenant", "bots", "vpn", "vpn_metered.py"), encoding="utf-8").read()
    blk = src[src.index("PATCH_METERED_PANEL_ERR_QUIET_V1"):][:900]
    assert "isinstance(e, PanelError)" in blk and ">= 3600" in blk and "logger.warning" in blk
    assert re.search(r"else:\s*\n\s*logger\.exception\(\"metered: پردازشِ سرویسِ", blk)
