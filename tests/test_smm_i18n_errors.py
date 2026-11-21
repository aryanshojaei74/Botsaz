"""WhaleSMM به‌جایِ متن، کلیدِ ترجمه (error.xxx) در پاسخِ ۲۰۰ می‌دهد؛ مشتری نباید چیزِ خام ببیند."""
from tenant.bots.virtual_service.smm import _parse_cancel_response, panel_err_friendly


def test_cancel_orders_shape_with_key():
    d = [{"order": 999999999999, "cancel": {"error": "error.incorrect_order_id"}}]
    ok, note = _parse_cancel_response(d, 999999999999)
    assert ok is False and note == "error.incorrect_order_id"
    msg = panel_err_friendly(note, "درخواستِ لغو")
    assert "error." not in msg and "پیدا نشد" in msg


def test_cancel_single_dict_shape_with_key():
    ok, note = _parse_cancel_response({"error": "error.incorrect_order_id"}, 1)
    assert ok is False
    assert "پیدا نشد" in panel_err_friendly(note, "درخواستِ لغو")


def test_refill_not_found_key():
    msg = panel_err_friendly("error.refill_not_found", "درخواستِ ریفیل")
    assert "error" not in msg and "ریفیل" in msg and "پیدا نشد" in msg


def test_human_text_still_works():
    assert "پیدا نشد" in panel_err_friendly("Incorrect order ID", "درخواستِ لغو")


def test_unknown_key_never_leaks_raw():
    msg = panel_err_friendly("error.something_weird", "درخواستِ لغو")
    assert "error." not in msg and "something_weird" not in msg
