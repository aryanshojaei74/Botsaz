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
