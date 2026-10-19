"""scripts/diag_trial_button.py — جمع‌بندیِ تشخیصِ دکمهٔ تست رایگان (تیکت #849)."""
import importlib.util
import os
from collections import Counter

_SPEC = importlib.util.spec_from_file_location(
    "diag_trial_button", os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "diag_trial_button.py"))
D = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(D)


def test_button_value_differs_from_global_is_flagged():
    out = D.conclusions({"trial_mode": "auto", "trial_gb": "0.25", "trial_days": "3", "trial_panel_key": "sanai"},
                        "0.3", "3", ["sanai", "pasargad"], Counter())
    assert any("اختصاصیِ دکمه" in x and "0.25" in x for x in out) and any("فقط پنلِ sanai" in x for x in out)


def test_two_ticked_panels_means_random():
    out = D.conclusions({"trial_mode": "auto", "trial_panel_keys": "sanai,pasargad"}, "2", "3",
                        ["sanai", "pasargad"], Counter())
    assert any("رندوم" in x for x in out)


def test_button_without_mode_uses_legacy_first_panel():
    out = D.conclusions({}, "2", "3", ["sanai"], Counter())
    assert len(out) == 1 and "اولین پنلِ فعال" in out[0]


def test_unavailable_selected_panel_and_other_panel_usage():
    out = D.conclusions({"trial_mode": "auto", "trial_panel_key": "marzban"}, "2", "3", ["sanai"], Counter(sanai=4))
    assert any("در پنل‌هایِ فعال" in x for x in out) and any("پنلِ دیگر" in x and "sanai×4" in x for x in out)


def test_values_taken_from_global_raise_no_warning():
    out = D.conclusions({"trial_mode": "manual"}, "2", "3", [], Counter())
    assert out == []
