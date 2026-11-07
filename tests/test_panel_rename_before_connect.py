import asyncio

import tenant
from tenant.bots.vpn import vpn_admin as VA


def test_rename_button_is_available_before_the_panel_is_connected(monkeypatch):
    async def gs(bot_id, key, default=""):
        return default

    async def ibs(bot_id, key):
        return None

    async def plans(bot_id, key):
        return (0, 0)
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "ibtn_style", ibs)
    monkeypatch.setattr(VA, "_plans_count", plans)
    kb = asyncio.run(VA.panel_manage_kb(None, "pasargad#4", bot_id=1, plan="pro"))
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "tvpn:rename:pasargad#4" in cbs and "tvpn:setup:pasargad#4" in cbs
