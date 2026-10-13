"""PATCH_TGTOOLS_LOOKUP_V2 — فقط ردِ صریحِ lookup مانعِ خرید می‌شود و علتش نشان داده می‌شود."""
from tenant.bots.virtual_service import tgtools_api as tg


def test_empty_or_unknown_shape_is_allowed():
    assert tg.lookup_verdict(None) == (True, "")
    assert tg.lookup_verdict({}) == (True, "")
    assert tg.lookup_verdict({"name": "Dariush", "recipient": "EQ..."}) == (True, "")


def test_explicit_found_false_blocks_with_reason():
    ok, why = tg.lookup_verdict({"found": False, "message": "No Telegram users found."})
    assert ok is False and "No Telegram users" in why


def test_explicit_eligible_false_blocks():
    ok, why = tg.lookup_verdict({"found": True, "eligible": False, "reason": "restricted"})
    assert ok is False and why == "restricted"


def test_found_true_allowed():
    assert tg.lookup_verdict({"found": True, "eligible": True}) == (True, "")


def test_username_extraction_keeps_digits_case():
    assert tg.extract_username("🆔 آیدی: 7363212943\n👤 یوزرنیم: @Ejei09") == "Ejei09"
