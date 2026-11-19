"""PATCH_FJ_LOG_ARGS_V1: پیامِ لاگِ «کانال پاسخ نداد» آرگومان‌هایش برعکس بود (%.0f ← chat_id رشته) ⇒ TypeError در لاگ (۵۰ بار در ۶ ساعت)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_force_join_timeout_log_formats_cleanly():
    src = open(os.path.join(ROOT, "tenant", "__init__.py"), encoding="utf-8").read()
    i = src.index('"force-join: کانال %s ظرفِ %.0f ثانیه پاسخ نداد')
    call = src[i:i + 400]
    fmt = "force-join: کانال %s ظرفِ %.0f ثانیه پاسخ نداد"
    args = re.search(r"\.\",\s*(.+?)\)\s", call).group(1)
    assert args.split(",")[0].strip() == "fj.chat_id"
    assert fmt % ("@mychannel", float(8)) == "force-join: کانال @mychannel ظرفِ 8 ثانیه پاسخ نداد"
