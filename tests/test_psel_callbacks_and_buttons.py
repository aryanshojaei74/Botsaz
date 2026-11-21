"""PATCH_PSEL_ACCOUNT_BUTTONS_V1 / PATCH_PSEL_LOGIN_BUTTON_V1 — تداخلِ callback بینِ مدیر و مشتری + دکمه‌هایِ جدیدِ صفحهٔ پنل."""
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import tenant.bots.panel_seller.user as PU

ROOT = Path(__file__).resolve().parent.parent / "tenant" / "bots" / "panel_seller"


def _handlers(fname, router):
    src = (ROOT / fname).read_text(encoding="utf-8")
    exact = set(re.findall(r'@%s\.callback_query\(F\.data == "([^"]+)"\)' % router, src))
    regex = re.findall(r'@%s\.callback_query\(F\.data\.regexp\(r"([^"]+)"\)\)' % router, src)
    return exact, regex


def test_no_exact_callback_is_shared_between_admin_and_user_routers():
    """«خریدِ حجمِ دلخواه» برایِ مشتری «فقط مدیرِ ربات» می‌داد چون هر دو روتر psel:cv را می‌گرفتند و روترِ مدیر اول بود."""
    a_exact, _ = _handlers("admin.py", "psel_admin_router")
    u_exact, _ = _handlers("user.py", "psel_user_router")
    assert not (a_exact & u_exact), sorted(a_exact & u_exact)


def test_no_exact_user_callback_is_swallowed_by_an_admin_regex_and_vice_versa():
    a_exact, a_re = _handlers("admin.py", "psel_admin_router")
    u_exact, u_re = _handlers("user.py", "psel_user_router")
    for cb in u_exact:
        assert not any(re.match(rx, cb) for rx in a_re), cb
    for cb in a_exact:
        assert not any(re.match(rx, cb) for rx in u_re), cb


def test_custom_volume_button_uses_the_user_callback():
    src = (ROOT / "user.py").read_text(encoding="utf-8")
    assert '"psel:cvb"' in src and 'btn("📦 خریدِ حجمِ دلخواه", "psel:cv",' not in src


def test_login_rows_only_for_http_urls():
    assert PU._login_rows("https://whale.tazvek.ir/dashboard")[0][0].url == "https://whale.tazvek.ir/dashboard"
    assert PU._login_rows("") == [] and PU._login_rows(None) == [] and PU._login_rows("javascript:alert(1)") == []


def test_delete_permission_exists_and_defaults_to_off():
    from tenant.bots.panel_seller.admin import PERMS
    assert PERMS["psel_allow_delete"][1] == "0"
