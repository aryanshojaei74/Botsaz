import pytest
from types import SimpleNamespace as NS
from tenant.bots.virtual_service import smm


@pytest.mark.asyncio
async def test_header_merged_into_plan_message(monkeypatch):
    async def _s(b): return (1.0, 0)
    async def _t(b): return [(3, 100)]
    async def _p(*a, **k): return (3550000, False)
    async def _g(*a, **k): return "1"
    async def _br(b): return None
    monkeypatch.setattr(smm, "_premium_settings", _s)
    monkeypatch.setattr(smm, "_premium_tiers_for", _t)
    monkeypatch.setattr(smm, "_premium_effective_price", _p)
    monkeypatch.setattr(smm, "_get_setting", _g)
    import tenant
    monkeypatch.setattr(tenant, "inline_back_row", _br)
    sent = []

    class T:
        async def answer(self, text, **k): sent.append(text)
    await smm.show_premium_shop_to_user(NS(), T(), 1, header="خرید پریمیوم <b>x</b>")
    assert len(sent) == 1 and "خرید پریمیوم &lt;b&gt;x" in sent[0] and "پلن" in sent[0]
