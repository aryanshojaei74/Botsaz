import pytest
from types import SimpleNamespace as NS
from tenant.bots.virtual_service import smm


@pytest.mark.asyncio
async def test_failure_alert_names_the_panel(monkeypatch):
    async def _gs(*a, **k): return "1"
    async def _pc(bot_id, pk): return ("u", "k", "پنل ممبر۲")
    monkeypatch.setattr(smm, "_get_setting", _gs)
    monkeypatch.setattr(smm, "_panel_creds", _pc)
    sent = []

    class B:
        async def send_message(self, cid, text, **k): sent.append(text)
    await smm._notify_smm_order_failed(B(), NS(id=1, owner_id=9), NS(username="u", telegram_id=5),
                                       NS(name="فیک", panel_pk=3), 10, 100, "HTTP 400: اعتبار کافی نیست")
    assert "پنل ممبر۲" in sent[0] and "«پنل ممبر۲» تموم شده" in sent[0]
