import pytest
from types import SimpleNamespace as NS
import models
import reactivation_link as RL


class FakeSession:
    def __init__(self, sale, pbot, prow): self.sale, self.pbot, self.prow = sale, pbot, prow
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def scalar(self, *a, **k): return self.sale
    async def get(self, model, pk):
        return self.pbot if model is models.PartnerBot else self.prow


@pytest.mark.asyncio
async def test_partner_sold_bot_links_to_partner_bot(monkeypatch):
    fs = FakeSession(NS(partner_bot_id=4), NS(bot_id=9), NS(username="NexivoPartner_bot"))
    assert await RL.partner_bot_link(NS(id=123), lambda: fs) == "https://t.me/NexivoPartner_bot?start=pubot_123"


@pytest.mark.asyncio
async def test_direct_bot_has_no_partner_link(monkeypatch):
    fs = FakeSession(None, None, None)
    assert await RL.partner_bot_link(NS(id=55), lambda: fs) is None
    fs2 = FakeSession(NS(partner_bot_id=4), NS(bot_id=9), NS(username=None))        # ربات همکار یوزرنیم ندارد
    assert await RL.partner_bot_link(NS(id=55), lambda: fs2) is None


@pytest.mark.asyncio
async def test_db_error_returns_none_so_caller_falls_back(monkeypatch):
    class Boom:
        async def __aenter__(self): raise RuntimeError("db")
        async def __aexit__(self, *a): return False
    assert await RL.partner_bot_link(NS(id=7), lambda: Boom()) is None


def test_main_tenant_uses_it_for_the_archive_button():
    src = open("main_tenant.py", encoding="utf-8").read()
    assert "await _reactivation_link(brow)" in src and 'link = _platform_deep_link(f"mbup_{brow.id}")' not in src
