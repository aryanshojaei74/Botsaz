"""PATCH_SMM_CANCEL_ID_REPLY_V1 (SINA، سفارش #14493) — پاسخِ لغوِ پنل‌هایی که شناسهٔ درخواستِ لغو برمی‌گردانند."""
from tenant.bots.virtual_service.smm import _parse_cancel_response as P


def test_panel_returning_cancel_request_id_means_requested():
    assert P([{"order": 7653721, "cancel": 67252}], 7653721) == (True, "requested")
    assert P([{"order": 7, "cancel": "67252"}], 7) == (True, "requested")
    assert P([{"order": 7, "cancel": 1}], 7) == (True, "requested")


def test_zero_false_and_errors_are_still_rejected():
    assert P([{"order": 7, "cancel": 0}], 7)[0] is False
    assert P([{"order": 7, "cancel": False}], 7)[0] is False
    assert P([{"order": 7, "cancel": {"error": "Incorrect order ID"}}], 7) == (False, "Incorrect order ID")
    assert P({"error": "Incorrect order ID"}, 7) == (False, "Incorrect order ID")


def test_already_requested_still_ok():
    assert P({"error": "Cancel already requested"}, 7) == (True, "already")
