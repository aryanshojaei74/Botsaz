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
