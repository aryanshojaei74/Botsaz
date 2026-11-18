"""لاگ‌های پرتکرارِ هر آپدیت نباید INFO باشند (فشارِ I/O روی journald)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_raw_update_not_info():
    src = (ROOT / "main_tenant.py").read_text()
    i = src.index("[RAW_UPDATE] token")
    assert "logger.debug(" in src[i - 80:i]


def test_smm_group_filter_not_info():
    src = (ROOT / "tenant/bots/virtual_service/smm_ai.py").read_text()
    a = src.index("async def _smm_ai_group_match")
    b = src.index("remainder = text[len(group_name):]", a)
    assert "logger.info(" not in src[a:b]


def test_forcejoin_timeout_short():
    import re
    src = (ROOT / "tenant/__init__.py").read_text()
    m = re.search(r"^_FORCEJOIN_CHECK_TIMEOUT = ([0-9.]+)", src, re.M)
    assert m and float(m.group(1)) <= 3.0
    assert "get_chat_member(fj.chat_id, user_id), timeout=_FORCEJOIN_CHECK_TIMEOUT" in src
