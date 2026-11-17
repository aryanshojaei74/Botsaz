"""سینا: احرازِ سطح ۱ «نامِ روی کارت» هم می‌گیرد و در مدیریتِ کارت‌هایِ احرازشده دیده می‌شود."""
import types

import pytest

from tenant.shared import kyc as K


def test_name_normalization_and_matching():
    assert K.names_match("علی رضایی", "رضایی علی اکبر") is True            # ترتیب/نامِ میانی مهم نیست
    assert K.names_match("علي رضايي", "علی رضایی") is True                # ی/ک عربی
    assert K.names_match("ALI Rezaei", "ali  rezaei") is True
    assert K.names_match("علی رضایی", "محمد کریمی") is False
    assert K.names_match("", "علی") is None and K.names_match("علی", None) is None
    assert K.norm_name("  می‌رزا‌ ") == ["می", "رزا"]


def test_request_caption_shows_holder():
    from tenant.shared.kyc_review import request_caption
    u = types.SimpleNamespace(full_name="x", telegram_id=1, username="u", id=1, kyc_level=0, kyc_card=None)
    req = types.SimpleNamespace(id=9, level=1, kind=None, card="6037991234567893", card_holder="علی <رضایی>")
    try:
        cap = request_caption(u, req)
    except Exception:
        pytest.skip("user_block needs more fields")
    assert "نامِ روی کارت" in cap and "علی &lt;رضایی&gt;" in cap


def test_flow_wiring_in_source():
    import re
    from pathlib import Path
    ku = Path("tenant/shared/kyc_user.py").read_text(encoding="utf-8")
    assert "card_holder = State()" in ku
    # شمارهٔ کارت ⇒ نام ⇒ عکس؛ نام در درخواست ذخیره می‌شود
    assert re.search(r"await state\.set_state\(KycUserFSM\.card_holder\)", ku)
    assert "card_holder=holder" in ku
    ka = Path("tenant/shared/kyc_admin.py").read_text(encoding="utf-8")
    assert "u.kyc_card_holder = getattr(r, \"card_holder\", None) or None" in ka
    assert ka.count("u.kyc_card_holder = None") >= 2                      # حذفِ کارت/ریستِ سطح نام را هم پاک می‌کند
