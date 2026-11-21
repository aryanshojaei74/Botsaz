"""scripts/diag_bots_off.py — طبقه‌بندیِ وضعیتِ وبهوک (PATCH_DIAG_BOTS_OFF_V1)."""
import datetime as dt
import importlib.util
import os

_SPEC = importlib.util.spec_from_file_location(
    "diag_bots_off", os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "diag_bots_off.py"))
diag = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(diag)

NOW = dt.datetime(2026, 10, 19, 12, 0, 0)
EXP = "https://x.example/webhook/TOKEN"


def test_healthy_webhook_has_no_problems():
    info = {"url": EXP, "pending": 0, "last_error_date": None, "last_error_message": None}
    assert diag.classify(info, EXP, NOW) == []


def test_missing_and_wrong_webhook_are_reported():
    assert "وبهوک ست نیست" in diag.classify({"url": "", "pending": 0}, EXP, NOW)[0]
    wrong = diag.classify({"url": "https://other/webhook/TOKEN", "pending": 0}, EXP, NOW)
    assert wrong and "آدرسِ دیگری" in wrong[0]


def test_recent_delivery_error_vs_old_error():
    recent = {"url": EXP, "pending": 0, "last_error_date": NOW - dt.timedelta(minutes=5),
              "last_error_message": "Wrong response (502) from webhook"}
    old = {"url": EXP, "pending": 0, "last_error_date": NOW - dt.timedelta(hours=5),
           "last_error_message": "Wrong response (502) from webhook"}
    assert any("502" in p for p in diag.classify(recent, EXP, NOW))
    assert diag.classify(old, EXP, NOW) == []


def test_stuck_pending_updates_are_reported():
    info = {"url": EXP, "pending": 400, "last_error_date": None}
    assert any("400" in p for p in diag.classify(info, EXP, NOW))


def test_unset_webhook_base_skips_url_comparison():
    assert diag.classify({"url": "https://anything/webhook/T", "pending": 0}, None, NOW) == []
