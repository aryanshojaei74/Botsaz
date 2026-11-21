"""باگِ #155: فروشِ خاموشِ یک پنل نباید «پنل وصل نشده» + راهنمایِ ادمین را به مشتری نشان بدهد."""
from types import SimpleNamespace

import pytest

import tenant


@pytest.mark.asyncio
async def test_sales_off_panel_says_sales_disabled_not_disconnected_for_customers():
    panel = SimpleNamespace(enabled=False, panel_key="pasarguard")
    t = await tenant._vpn_panel_off_text(1, "pasarguard", panel, staff=False)
    assert "فروشِ این بخش فعلاً غیرفعال است" in t
    assert "متصل نشده" not in t and "ادمین" not in t           # نه «وصل نشده»، نه راهنمایِ ادمین


@pytest.mark.asyncio
async def test_staff_gets_the_hint_how_to_turn_sales_on():
    panel = SimpleNamespace(enabled=False, panel_key="pasarguard")
    t = await tenant._vpn_panel_off_text(1, "pasarguard", panel, staff=True)
    assert "فروشِ این بخش فعلاً غیرفعال است" in t and "ادمین" in t and "فروش: غیرفعال" in t
    assert "متصل نشده" not in t


@pytest.mark.asyncio
async def test_really_disconnected_panel_has_plain_text_for_customer_and_hint_for_staff():
    cust = await tenant._vpn_panel_off_text(1, "sanai", None, staff=False)
    staff = await tenant._vpn_panel_off_text(1, "sanai", None, staff=True)
    assert "فعلاً در دسترس نیست" in cust and "ادمین" not in cust and "VPN خودکار" not in cust
    assert "متصل نشده" in staff and "VPN خودکار" in staff and cust in staff
