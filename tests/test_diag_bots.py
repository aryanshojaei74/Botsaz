"""PATCH_BOT_DIAG_V1: دسته‌بندیِ «چرا رباتِ دارایِ مهلت کار نمی‌کند»."""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("diag_bots", os.path.join(ROOT, "scripts", "diag_bots.py"))
DB = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DB)

EXP = "https://x.example/webhook/T"
ALL = ["message", "callback_query", "chat_member"]
GOOD = {"url": EXP, "allowed_updates": ALL, "pending_update_count": 0}
NOW = 1_000_000.0


def C(db_status="active", getme="ok", wh=None, expected=EXP):
    return DB.classify(db_status, getme, GOOD if wh is None else wh, expected, NOW, ALL)[0]


def test_classes():
    assert C() == "OK"
    assert C(getme="unauthorized") == "DEAD_TOKEN" and C("paused", "unauthorized") == "DEAD_TOKEN"      # توکنِ باطل همیشه اول
    for st in ("paused", "locked", "admin_paused", "disabled_bad_token"):
        assert C(st) == "STATUS_BLOCKED", st
    assert C(getme="error") == "DELIVERY_ERROR"
    assert C(wh={**GOOD, "url": ""}) == "WRONG_WEBHOOK" and C(wh={**GOOD, "url": "https://old/webhook/T"}) == "WRONG_WEBHOOK"
    assert C(wh={**GOOD, "allowed_updates": []}) == "MISSING_UPDATES"                                    # پیش‌فرضِ تلگرام: بدونِ chat_member
    assert C(wh={**GOOD, "allowed_updates": ["message"]}) == "MISSING_UPDATES"
    assert C(wh={**GOOD, "allowed_updates": ["message", "callback_query"]}) == "MISSING_UPDATES"          # chat_member کم است
    assert C(wh={**GOOD, "pending_update_count": 7}) == "DELIVERY_ERROR"
    assert C(wh={**GOOD, "last_error_date": NOW - 300, "last_error_message": "Read timeout expired"}) == "DELIVERY_ERROR"
    assert C(wh={**GOOD, "last_error_date": NOW - 7200, "last_error_message": "old"}) == "OK"            # خطای قدیمی مهم نیست
    assert C(expected=None) == "OK"                                                                         # WEBHOOK_BASE خالی ⇒ آدرس چک نمی‌شود
