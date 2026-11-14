"""PATCH_KYC_MISMATCH_CARDS_V1 — پیامِ مالک در «عدمِ تطابقِ کارت» هر دو کارت را نشان می‌دهد."""
from tenant.gateways.zibal_wallet import kyc_mismatch_admin_text as T


def test_shows_registered_and_paid_cards_masked_and_user():
    t = T(8872846175, "9101", "6037991111111111", "5022-29**-****-9101")
    assert "<code>8872846175</code>" in t
    assert "6037 •••• •••• 1111" in t                       # کارتِ احرازشده (نیمه‌پنهان)
    assert "…9101" in t or "9101" in t.split("کارتِ پرداخت")[1]
    assert "6037991111111111" not in t                      # شمارهٔ کامل در پیام نمی‌آید


def test_full_pan_paid_card_is_masked_and_missing_data_degrades():
    t = T(1, "9101", "6037991111111111", "5022291234569101")
    assert "5022 •••• •••• 9101" in t and "5022291234569101" not in t
    t2 = T(1, "9101", None, None)                           # کارتِ ثبت‌شده/پرداخت ناشناخته ⇒ خراب نشود
    assert "—" in t2 and "…9101" in t2


# ── ردّ لغو: پاسخِ خام در یادداشت می‌آید و متنِ آزادِ مثبت «ثبت شد» حساب می‌شود (SINA، #14410) ──
from tenant.bots.virtual_service.smm import _parse_cancel_response as PC


def test_cancel_free_text_success_counts_as_requested_but_negatives_do_not():
    assert PC([{"order": 5, "cancel": "Cancel request sent"}], 5) == (True, "requested")
    assert PC({"success": True}, 5) == (True, "requested")
    assert PC([{"order": 5, "cancel": "Cannot cancel this order"}], 5)[0] is False
    assert PC({"success": True, "message": "not allowed"}, 5)[0] is False
    assert PC([{"order": 5, "cancel": 0}], 5)[0] is False
    assert PC([{"order": 5, "cancel": {"status": "Canceled"}}], 5) == (True, "Canceled")      # رفتارِ قبلی دست‌نخورده


def test_cancel_unknown_shape_is_rejected_and_shows_raw_reply():
    ok, note = PC({"foo": "bar"}, 5)
    assert ok is False and '"foo": "bar"' in note and "پاسخِ خامِ پنل" in note
