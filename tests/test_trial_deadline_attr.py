"""PATCH_TRIAL_DEADLINE_ATTR_V1 — اعلانِ فعال‌شدنِ تستِ پلن نباید به‌خاطرِ ثابتِ تعریف‌نشده بشکند."""
import plan_trials
import platform_bot as PB


def test_deadline_line_is_empty_when_constant_missing_and_filled_when_defined(monkeypatch):
    assert not hasattr(plan_trials, "TRIAL_START_DEADLINE_DAYS")
    assert PB._trial_start_deadline_line() == ""                     # قبلاً AttributeError (لاگ: has no attribute)
    monkeypatch.setattr(plan_trials, "TRIAL_START_DEADLINE_DAYS", 5, raising=False)
    line = PB._trial_start_deadline_line()
    assert "5 روز" in line and line.endswith("\n\n")
