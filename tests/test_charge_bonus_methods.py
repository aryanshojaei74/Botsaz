"""PATCH_CHARGE_BONUS_METHODS_V1 — بونوسِ شارژ فقط برایِ روش‌هایِ انتخاب‌شده."""
from types import SimpleNamespace

from tenant.shared import charge_bonus as CB


def T(kind, note=""):
    return SimpleNamespace(kind=kind, note=note, amount=100000)


def test_method_of_maps_kinds_and_notes():
    assert CB.method_of(T("zarinpal_charge")) == "zarinpal" and CB.method_of(T("zibal_charge")) == "zibal"
    assert CB.method_of(T("crypto_charge")) == "crypto" and CB.method_of(T("tonpays_charge")) == "crypto"
    assert CB.method_of(T("charge", "AUTO:شارژ خودکار کارت")) == "card" and CB.method_of(T("charge", "شارژ سفارش #12")) == "card"
    assert CB.method_of(T("charge", "شارژ با استارز")) == "stars"
    assert CB.method_of(T("charge", "بازگشت وجه")) is None and CB.method_of(T("refund")) is None


def test_parse_methods_and_allowed():
    assert CB.parse_methods(None) == set() and CB.parse_methods("card, zibal,bogus") == {"card", "zibal"}
    assert CB.method_allowed(T("zarinpal_charge"), set())                       # بدونِ محدودیت ⇒ همه
    en = {"card", "zibal"}
    assert CB.method_allowed(T("zibal_charge"), en) and CB.method_allowed(T("charge", "شارژ سفارش #1"), en)
    assert not CB.method_allowed(T("zarinpal_charge"), en)
    assert not CB.method_allowed(T("charge", "شارژ با استارز"), en) and not CB.method_allowed(T("charge", "ناشناخته"), en)
    # با محدودیت، روشِ ناشناخته بونوس نمی‌گیرد، بدونِ محدودیت می‌گیرد
    assert CB.method_allowed(T("charge", "ناشناخته"), set())


def test_every_topup_kind_has_a_method():
    for k in CB.TOPUP_KINDS - {"charge"}:
        assert CB.method_of(T(k)) is not None, k


def test_cancel_parser_handles_doc_shapes():
    """قالب‌هایِ مستندِ استانداردِ cancel (WhaleSMM و پنل‌هایِ مشابه): لیستِ [{order, cancel: 1|{error}}]."""
    from tenant.bots.virtual_service.smm import _parse_cancel_response as P
    assert P([{"order": 5, "cancel": 1}], 5) == (True, "requested")
    ok, note = P([{"order": 5, "cancel": {"error": "Incorrect order ID"}}], 5)
    assert not ok and "Incorrect" in note
    ok, note = P({"error": "Neither"}, 5)
    assert not ok and note == "Neither"
    assert P([{"order": 5, "cancel": {"error": "Cancel already requested"}}], 5) == (True, "already")
    assert P(None, 5)[0] is False and P([], 5)[0] is False
