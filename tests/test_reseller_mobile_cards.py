import datetime
from tenant.shared import web_panel as WP

ROW = (7, "995752281", "10 گیگ / ۳۰ روز", 10, 30, "active", datetime.datetime(2026, 12, 1),
       "https://sub.example.ir/sub/abc?x=1&y=<2>")


def test_card_has_copyable_link_and_actions():
    h = WP._rs_cfg_cards([ROW])
    assert 'class="cfg"' in h and "995752281" in h and "10 گیگ" in h and "📋 کپی" in h
    assert 'readonly' in h and 'value="https://sub.example.ir/sub/abc?x=1&amp;y=&lt;2&gt;"' in h      # escape شده
    assert 'href="/panel/r/renew/7"' in h and 'href="/panel/r/delete/7"' in h
    assert "<table" not in h


def test_card_without_link_or_limits():
    h = WP._rs_cfg_cards([(1, None, None, 0, 0, None, None, None)])
    assert "نامحدود" in h and "بی‌انقضا" in h and "کپی" not in h
    assert WP._rs_cfg_cards([]) == ""


def test_shell_ships_copy_script_and_css():
    page = WP._shell("t", "<p>x</p>", "")
    assert "function cpy(" in page and ".cfg{" in page and "viewport" in page
