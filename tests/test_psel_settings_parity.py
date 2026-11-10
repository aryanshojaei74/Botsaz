"""PATCH_PSEL_PARITY_V2 — مالکِ رباتِ پنل‌فروشی برایِ «انتقال موجودی بینِ کاربران» دکمه دارد."""
import tenant  # noqa: F401
from tenant.settings_registry import section_key, visible_items


def test_panel_seller_finance_has_balance_transfer_admin_button():
    items = visible_items(section_key("finance", "panel_seller"), "pro")
    assert [i.callback for i in items] == ["btr:admin", "wd:admin"]
    assert items[0].handler is not None            # حالتِ کیبوردی هم کار کند


def test_other_kinds_unchanged():
    assert "btr:admin" in [i.callback for i in visible_items(section_key("finance", "vpn_sales"), "pro")]
