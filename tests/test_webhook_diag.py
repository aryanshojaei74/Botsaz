"""PATCH_WEBHOOK_DIAG_V1 / PATCH_LATE_DELIVERY_LOG_V1: ابزارِ تشخیصِ «آپدیت دیر می‌رسد؟»."""
import importlib.util
import os

import late_delivery as MT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("diag_webhooks", os.path.join(ROOT, "scripts", "diag_webhooks.py"))
DW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DW)


def test_delivery_lag_for_messages_only():
    assert MT.delivery_lag({"message": {"date": 1000}}, now=1010.5) == 10.5
    assert MT.delivery_lag({"callback_query": {"id": "1"}}, now=1010) is None            # دکمه تاریخِ ارسال ندارد
    assert MT.delivery_lag({"edited_message": {"date": 5}}, now=1010) is None           # date همان پیامِ اصلی است
    assert MT.delivery_lag({"message": {}}, now=1010) is None


def test_late_delivery_warns_only_above_threshold(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        MT.warn({"message": {"date": __import__("time").time() - 1}}, "tok123456")
        assert "late update" not in caplog.text
        MT.warn({"message": {"date": __import__("time").time() - 30}}, "tok123456")
    assert "late update delivery" in caplog.text and "tok123456"[-6:] in caplog.text


def test_summarize_flags_pending_and_recent_errors():
    now = 100_000.0
    ok = DW.summarize({"result": {"url": "https://x/webhook/t", "pending_update_count": 0, "max_connections": 40,
                                  "ip_address": "1.2.3.4"}}, now)
    assert ok["problem"] is False and ok["pending"] == 0 and ok["err_age_min"] is None
    pend = DW.summarize({"result": {"pending_update_count": 12}}, now)
    assert pend["problem"] and pend["pending"] == 12
    fresh = DW.summarize({"result": {"last_error_date": now - 120, "last_error_message": "Read timeout expired"}}, now)
    assert fresh["problem"] and fresh["err_age_min"] == 2.0 and "timeout" in fresh["err"]
    old = DW.summarize({"result": {"last_error_date": now - 7200, "last_error_message": "old"}}, now)
    assert old["problem"] is False                                                     # خطای دو ساعت پیش مشکلِ فعلی نیست
