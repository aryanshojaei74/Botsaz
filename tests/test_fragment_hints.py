"""PATCH_FRAGMENT_HINTS_V1 — علتِ فارسیِ خطایِ فرگمنت (هش/کوکی)."""
import pyfragment as pf
import pytest

from tenant.bots.virtual_service import fragment_stars as FS
from tenant.bots.virtual_service import smm


@pytest.mark.parametrize("exc,word", [
    (pf.CookieError("x"), "کوکی‌ها ناقص یا منقضی"),
    (pf.FragmentPageError("Could not extract the API hash"), "«هش»"),
    (pf.UserNotFoundError("x"), "یوزرنیم"),
    (pf.WalletError("x"), "موجودی"),
    (pf.VerificationError("x"), "KYC"),
    (pf.FragmentAPIError("Bad request"), "هشِ کهنه"),
])
def test_hint_mapping(exc, word):
    assert word in FS.fragment_hint(exc)


def test_unknown_error_has_no_hint_and_page_error_stays_a_clean_pre_payment_failure():
    assert FS.fragment_hint(RuntimeError("boom")) is None
    msg = smm._fg_clean_failure(pf.FragmentPageError("x"))
    assert msg and "کوکی" in msg and "خروج" in msg                  # قبل از پرداخت بوده ⇒ سفارش به دستی برمی‌گردد
    assert smm._fg_clean_failure(RuntimeError("x")) is None         # نامشخص ⇒ هشدارِ «شاید پول خرج شده»
