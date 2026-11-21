import pytest
from tenant.shared import menu_knowledge as MK
from tenant.bots.virtual_service import smm_ai


def test_providers_hidden():
    t = MK.describe_action("vnum_shop_auto", None)
    from tenant import _SIMPLE_ACTIONS
    d = MK.describe_action("vnum_shop_auto", _SIMPLE_ACTIONS)
    assert "OzVinoo" not in d and "SMSPool" not in d and d


def test_prompt_forbids_leaks_and_reads_rules():
    p = smm_ai._SYSTEM_PROMPT
    assert "قوانین" in p and "ارائه‌دهنده" in p
