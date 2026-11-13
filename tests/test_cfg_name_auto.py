import pytest
import tenant
from tenant.bots.vpn import vpn_user as vu


@pytest.mark.asyncio
async def test_auto_name_off_by_default(monkeypatch):
    async def _gs(b, k, d=""): return d
    monkeypatch.setattr(tenant, "get_setting", _gs)
    assert await vu._auto_cfg_name(1, "x") is None


@pytest.mark.asyncio
async def test_auto_name_uses_server_label(monkeypatch):
    async def _gs(b, k, d=""): return "1" if k == "cfg_name_auto" else d
    monkeypatch.setattr(tenant, "get_setting", _gs)
    from tenant.bots.vpn import vpn_admin

    async def _pl(b, k): return "آلمان"
    monkeypatch.setattr(vpn_admin, "panel_label", _pl)
    n = await vu._auto_cfg_name(1, "x")
    assert n.startswith("آلمان-") and n.split("-")[1].isdigit()
    assert (await vu._auto_cfg_name(1, None)).startswith("VPN-")
