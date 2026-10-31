from tenant.shared import buyer_ident as bi


def test_modes_cycle_and_normalize():
    assert bi.next_mode("both") == "id" and bi.next_mode("id") == "username" and bi.next_mode("username") == "both"
    assert bi.normalize("???") == "both" and bi.normalize(None) == "both"


def test_trust_ident_text():
    assert bi.ident_text("both", "ali", 12) == "@ali — <code>12</code>"
    assert bi.ident_text("id", "ali", 12) == "<code>12</code>"
    assert bi.ident_text("username", "ali", 12) == "@ali"
    assert bi.ident_text("username", None, 12) == "<code>12</code>"      # بدونِ یوزرنیم ⇒ آیدی، تا بی‌هویت نماند


def test_tx_lines():
    assert bi.tx_lines("id", "Ali", "ali", 12) == ["🆔 آیدی عددی: <code>12</code>"]
    assert len(bi.tx_lines("both", "Ali", "ali", 12)) == 3
    assert bi.tx_lines("username", "Ali", "ali", 12) == ["🔗 یوزرنیم: @ali"]


def test_trial_report_respects_ident_mode():
    from tenant.shared.trial_report import build_report
    kw = dict(user_id=77, username="ali", full_name="Ali R", link="", volume="1", duration="1", panel=None, source="panel")
    both = build_report(**kw)
    assert "<code>77</code>" in both and "@ali" in both and "Ali R" in both
    only_id = build_report(ident_mode="id", **kw)
    assert "<code>77</code>" in only_id and "@ali" not in only_id and "Ali R" not in only_id
    only_user = build_report(ident_mode="username", **kw)
    assert "@ali" in only_user and "<code>77</code>" not in only_user
    no_user = build_report(ident_mode="username", **{**kw, "username": None})
    assert "<code>77</code>" in no_user                      # یوزرنیم نداشت ⇒ آیدی
