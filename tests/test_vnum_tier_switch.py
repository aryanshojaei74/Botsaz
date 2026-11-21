import pytest
from types import SimpleNamespace as NS
from tenant.bots.virtual_service import smm


@pytest.mark.asyncio
async def test_tier_list_has_switch_button(monkeypatch):
    items = [NS(id=1, name="A", is_active=True, is_manual=False, service_id="ozv:p:1"),
             NS(id=2, name="B", is_active=True, is_manual=False, service_id="ozv:p:2:c")]

    async def _l(b): return items
    monkeypatch.setattr(smm, "_vnum_list", _l)
    monkeypatch.setattr(smm, "_vnum_shop_price", lambda v: 100)
    import tenant

    async def _br(b): return None
    monkeypatch.setattr(tenant, "inline_back_row", _br)
    sent = []

    class T:
        async def answer(self, text, reply_markup=None, **k): sent.append(reply_markup)
    await smm.show_vnum_shop_to_user(NS(), T(), 1, 0, only=None, tier="nr")
    cbs = [b.callback_data for r in sent[0].inline_keyboard for b in r]
    assert "vnum:shop:0:all:ch" in cbs
    sent.clear()
    await smm.show_vnum_shop_to_user(NS(), T(), 1, 0, only=None, tier="ch")
    cbs = [b.callback_data for r in sent[0].inline_keyboard for b in r]
    assert "vnum:shop:0:all:nr" in cbs
