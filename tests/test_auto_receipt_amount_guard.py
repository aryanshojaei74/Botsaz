"""
تست‌هایِ نگهبانِ مبلغ در تاییدِ خودکارِ رسید (PATCH_AI_APPROVE_AMOUNT_GUARD_V1).

باگِ واقعی: سفارش ۲۵۰٬۰۰۰ تومان، رسید ۱٬۸۰۰٬۰۰۰ ریال (= ۱۸۰٬۰۰۰ تومان)، و هوش مصنوعی در خروجی‌اش
هم عددِ درست را نوشته بود هم «تصمیم: تایید / دلیل: همه موارد مطابقت داشت». کد فقط به «تصمیم» اعتماد
می‌کرد و رسید تایید شد. این‌ها خروجی‌هایِ واقعی‌نما را به همان پارسرِ واقعی می‌دهند.
"""
import pytest

from tenant.gateways import auto_receipt as ar


def _resp(amount="1800000", unit="ریال", decision="تایید", reason="همه موارد مطابقت داشت",
          extra="", phrase="auto"):
    lines = []
    if amount is not None:
        lines.append(f"مبلغ_عدد: {amount}")
    if unit is not None:
        lines.append(f"مبلغ_واحد: {unit}")
    if phrase == "auto":                              # PATCH_RECEIPT_UNIT_PHRASE_V1: مدل عینِ عبارتِ رسید را کپی می‌کند
        phrase = f"{amount} {unit}" if (amount is not None and unit is not None) else None
    if phrase:
        lines.append(f"عبارت_مبلغ: {phrase}")
    lines += ["کارت_مقصد: 8940", "کارت_مبدأ: 7758", "تاریخ: 1405/07/06", "ساعت: 13:27",
              "شماره_پیگیری: 5099067599", f"تصمیم: {decision}", f"دلیل: {reason}"]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def parse(text, expected):
    return ar._parse_autonomous_response(text, "7758", "8940", expected_amount=expected)


# ───────────────────────── خودِ باگ ─────────────────────────

def test_the_reported_case_is_no_longer_approved():
    """۲۵۰٬۰۰۰ تومان سفارش، ۱٬۸۰۰٬۰۰۰ ریال رسید ⇒ باید ارجاع به بررسیِ دستی شود."""
    chk = parse(_resp(), 250_000)
    assert chk.ok is False
    assert chk.amount == 180_000                      # مبلغِ خوانده‌شده هنوز برایِ نمایش به ادمین می‌آید
    assert "180,000" in chk.reason and "250,000" in chk.reason
    assert "بررسی دستی" in chk.reason
    assert "تایید خودمختار توسط هوش مصنوعی" not in chk.reason      # پیامِ «همه موارد مطابقت داشت» نباید برسد


def test_reason_tells_the_admin_why_and_that_the_model_erred():
    chk = parse(_resp(), 250_000)
    assert "متوقف شد" in chk.reason and "اشتباهاً" in chk.reason


# ───────────────────────── تاییدِ درست همچنان کار می‌کند ─────────────────────────

@pytest.mark.parametrize("amount,unit,expected", [
    ("2500000", "ریال", 250_000),
    ("250000", "تومان", 250_000),
    ("250,000", "تومان", 250_000),
    ("۲۵۰۰۰۰۰", "ریال", 250_000),         # ارقامِ فارسی
    ("1800000", "ریال", 180_000),
])
def test_matching_amount_is_still_approved(amount, unit, expected):
    chk = parse(_resp(amount=amount, unit=unit), expected)
    assert chk.ok is True, chk.reason
    assert "تایید خودمختار توسط هوش مصنوعی" in chk.reason


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_one_toman_rounding_tolerance_matches_the_prompt(delta):
    # پرامپت صریحاً «رواداریِ حداکثر ۱ تومان برایِ گردکردن» می‌گوید
    chk = parse(_resp(amount=str((250_000 + delta) * 10)), 250_000)
    assert chk.ok is True


@pytest.mark.parametrize("delta", [-2, 2, -1000, 70_000, -70_000])
def test_more_than_one_toman_off_is_rejected_in_both_directions(delta):
    """هم کم‌پرداخت و هم بیش‌پرداخت — رسید باید دقیقاً برابرِ سفارش باشد."""
    chk = parse(_resp(amount=str((250_000 + delta) * 10)), 250_000)
    assert chk.ok is False
    assert "مطابقت ندارد" in chk.reason


# ───────────────────────── fail-safe ─────────────────────────

def test_approval_without_a_readable_amount_is_not_trusted():
    chk = parse(_resp(amount=None, unit=None), 250_000)
    assert chk.ok is False and chk.amount is None
    assert "خوانده نشد" in chk.reason


def test_unparseable_amount_is_not_trusted():
    chk = parse(_resp(amount="نامشخص"), 250_000)
    assert chk.ok is False


def test_model_referral_is_untouched():
    chk = parse(_resp(decision="ارجاع", reason="کارت مقصد مطابقت ندارد"), 250_000)
    assert chk.ok is False
    assert "کارت مقصد مطابقت ندارد" in chk.reason           # دلیلِ خودِ مدل حفظ می‌شود (نه پیامِ نگهبان)


def test_model_referral_with_matching_amount_still_stays_referred_for_other_reasons():
    chk = parse(_resp(amount="2500000", decision="ارجاع", reason="کارت مقصد مطابقت ندارد"), 250_000)
    assert chk.ok is False                                   # نگهبانِ قبلی فقط برایِ «فقط مبلغ» override می‌کند


# ───────────────────────── نگهبانِ قبلی (ردِ مدل → تاییدِ کد) سالم مانده ─────────────────────────

def test_old_override_still_fixes_model_unit_mistakes():
    """مدل به‌خاطرِ اشتباهِ ریال/تومان رد کرده ولی مبلغِ حسابِ کد دقیقاً برابر است ⇒ تایید."""
    chk = parse(_resp(amount="2500000", decision="ارجاع", reason="مبلغ مطابقت ندارد"), 250_000)
    assert chk.ok is True
    assert "اصلاح‌شده" in chk.reason


def test_old_override_does_not_fire_when_amount_really_differs():
    chk = parse(_resp(amount="1800000", decision="ارجاع", reason="مبلغ مطابقت ندارد"), 250_000)
    assert chk.ok is False


# ───────────────────────── بدونِ مبلغِ مورد انتظار ─────────────────────────

def test_without_expected_amount_behaviour_is_unchanged():
    chk = ar._parse_autonomous_response(_resp(), "7758", "8940", expected_amount=None)
    assert chk.ok is True


# ───────────────────────── رسیدهایِ متفاوت: کارت‌به‌کارت خودِ کاربر ─────────────────────────

def test_amount_line_inside_free_text_does_not_confuse_the_guard():
    txt = _resp(extra="توضیح: مبلغ 250000 تومان در متن آمده") 
    chk = parse(txt, 250_000)
    assert chk.ok is False and chk.amount == 180_000        # عددِ ساختاریافته ملاک است، نه هر عددی در متن


# ───────────────────────── PATCH_RECEIPT_UNIT_PHRASE_V1 (MidnightSoul: ریال به‌جایِ تومان شارژ شد) ─────────────────────────

def test_reported_case_rial_receipt_labelled_toman_is_NOT_approved():
    """رسیدِ «۲٬۰۰۰٬۰۰۰ ریال» برایِ سفارشِ ۲٬۰۰۰٬۰۰۰ تومان؛ مدل برچسبِ واحد را اشتباه «تومان» نوشته بود."""
    chk = parse(_resp(amount="2000000", unit="تومان", phrase="۲,۰۰۰,۰۰۰ ریال"), 2_000_000)
    assert chk.ok is False
    assert chk.amount is None                                     # تناقضِ برچسب و عبارتِ رسید ⇒ مبلغ قطعی نیست
    assert "قطعی نشد" in chk.reason and "۲,۰۰۰,۰۰۰ ریال" in chk.reason and "بررسیِ دستی" in chk.reason
    assert chk.amount_phrase == "۲,۰۰۰,۰۰۰ ریال"


def test_toman_claim_without_the_literal_phrase_is_not_trusted():
    chk = parse(_resp(amount="250000", unit="تومان", phrase=None), 250_000)
    assert chk.ok is False and chk.amount is None


def test_missing_unit_does_not_default_to_toman_even_if_the_models_text_says_toman():
    """مدل واحد را ننوشته ولی در دلیل «تومان» را بازتاب داده (از مبلغِ موردِ انتظار) ⇒ نباید تومان فرض شود."""
    t = _resp(amount="2000000", unit=None, phrase=None, reason="مبلغ ۲٬۰۰۰٬۰۰۰ تومان مطابقت دارد")
    chk = parse(t, 2_000_000)
    assert chk.ok is False and chk.amount == 200_000              # پیش‌فرضِ محافظه‌کارانه: ریال
    # و اگر واقعاً ریال بوده و سفارش ۲۰۰٬۰۰۰ تومان است، همچنان درست تایید می‌شود
    assert parse(_resp(amount="2000000", unit="ریال"), 200_000).ok is True


def test_phrase_number_must_match_the_raw_number():
    chk = parse(_resp(amount="2000000", unit="ریال", phrase="۲۰٬۰۰۰٬۰۰۰ ریال"), 200_000)
    assert chk.ok is False and chk.amount is None


def test_phrase_with_both_units_is_ambiguous():
    chk = parse(_resp(amount="2000000", unit="ریال", phrase="2,000,000 ریال (200,000 تومان)"), 200_000)
    assert chk.ok is False and chk.amount is None


def test_phrase_unit_wins_over_a_wrong_label_only_when_they_agree_otherwise_refuse():
    assert parse(_resp(amount="2000000", unit="ریال", phrase="2,000,000 ریال"), 200_000).ok is True
    assert parse(_resp(amount="200000", unit="تومان", phrase="200,000 تومان"), 200_000).ok is True
    assert parse(_resp(amount="200000", unit="ریال", phrase="200,000 تومان"), 200_000).ok is False   # تناقض


@pytest.mark.parametrize("phrase", ["۲,۰۰۰,۰۰۰ ریال", "2000000 ریال", "«۲٬۰۰۰٬۰۰۰ ریال»"])
def test_phrase_formats_are_parsed(phrase):
    assert parse(_resp(amount="2000000", unit="ریال", phrase=phrase), 200_000).ok is True


def test_prompt_asks_for_the_literal_phrase_and_forbids_copying_the_expected_unit():
    p = ar._receipt_ai_autonomous_prompt(200_000, "8940", None, None, "7758", "کارت", None)
    assert "عبارت_مبلغ:" in p and "کپی نکن" in p


# ───────────────────────── PATCH_AUTO_SKIP_REASON_V1 ─────────────────────────

def test_skip_reason_when_ai_approved_but_receipt_has_no_fingerprint():
    chk = ar.ReceiptCheck(ok=True, reason="🤖 تایید خودمختار توسط هوش مصنوعی -- همه موارد مطابقت داشت", amount=250_000)
    why = ar.auto_skip_reason(chk)
    assert "شمارهٔ پیگیری" in why and "تاییدِ دستی" in why
    assert "همه موارد مطابقت داشت" not in why                      # دیگر «دلیلِ تایید» زیرِ عنوانِ «تایید نشد» نمی‌آید


def test_skip_reason_when_fingerprint_exists_but_claim_failed():
    chk = ar.ReceiptCheck(ok=True, reason="x", amount=250_000, tracking_no="5099067599")
    why = ar.auto_skip_reason(chk)
    assert "هم‌زمان" in why or "قبلاً" in why


def test_wallet_and_purchase_flows_use_the_real_reason():
    import pathlib
    root = pathlib.Path(ar.__file__).parent
    for f in ("card_wallet.py", "card_purchase.py"):
        src = (root / f).read_text(encoding="utf-8")
        assert "auto_skip_reason(check) if check.ok else check.reason" in src, f
        assert "عینِ مبلغِ روی رسید" in src, f                      # ادمین عبارتِ روی رسید را در گزارشِ تاییدِ خودکار می‌بیند
