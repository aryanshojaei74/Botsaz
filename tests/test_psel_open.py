import pytest
from tenant.bots.panel_seller import gate


@pytest.mark.asyncio
async def test_open_by_default_for_everyone():
    assert gate.OPEN is True
    assert await gate.allowed(123456789)
    import platform_bot as PB
    assert "تست" not in PB.FIELDS["panel_seller"] and "تست" not in PB.PANEL_SELLER_TEMPLATE_PITCH
