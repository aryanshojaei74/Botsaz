from tenant.shared.trust_report import strip_panel_tag as S


def test_strips_short_panel_marker_in_renewal_and_purchase():
    assert S("🥇طلایی (A) — +50 گیگ / +365 روز") == "🥇طلایی — +50 گیگ / +365 روز"
    assert S("🥇طلایی (A)") == "🥇طلایی"
    assert S("🥇طلایی (B) — 50 گیگ · 365 روز") == "🥇طلایی — 50 گیگ · 365 روز"


def test_keeps_meaningful_parentheses_and_plain_titles():
    assert S("👑 رویال — نامحدود · 30 روز") == "👑 رویال — نامحدود · 30 روز"
    assert S("پلن (ویژه) — 10 گیگ") == "پلن (ویژه) — 10 گیگ"
    assert S("مولتی (Germany) — 5 گیگ", "Germany") == "مولتی — 5 گیگ"
    assert S(None) == "" and S("") == ""


def test_strips_panel_name_words_like_upanel_but_keeps_real_notes():
    # PATCH_TRUST_STRIP_PANEL_TAG_V2 (ایلیا: خریدِ نامحدود و تمدیدِ پنلِ یوپنل)
    pn = "🟡 یوپنل (مرزبان)"
    assert S("👑 رویال (یوپنل) — نامحدود · 30 روز", pn) == "👑 رویال — نامحدود · 30 روز"
    assert S("👑 رویال (مرزبان) — +نامحدود / +30 روز", pn) == "👑 رویال — +نامحدود / +30 روز"
    assert S("🥇طلایی (UPanel)", "UPanel") == "🥇طلایی"
    assert S("پلن (سنایی) — 10 گیگ") == "پلن — 10 گیگ"          # حتی وقتی panel_name پاس نشده (از فهرستِ پنل‌هایِ ربات)
    assert S("پلن (ویژه) — 10 گیگ", pn) == "پلن (ویژه) — 10 گیگ"    # توضیحِ واقعی می‌ماند
    assert S("پلن (۲ ماهه) — 10 گیگ", pn) == "پلن (۲ ماهه) — 10 گیگ"
