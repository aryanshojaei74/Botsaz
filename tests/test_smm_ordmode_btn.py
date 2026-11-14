import pytest
from types import SimpleNamespace as NS
from tenant.bots.virtual_service import smm


class _Sess:
    def __init__(self, p): self.p = p
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, model, pk): return self.p
    async def scalars(self, *a, **k): return NS(all=lambda: [self.p])


@pytest.mark.asyncio
async def test_detail_shows_order_mode_and_toggle(monkeypatch):
    p = NS(id=5, bot_id=1, name="پنل", api_url="u", api_key="k" * 12, last_balance=None,
           last_currency="", last_check_at=None, last_check_ok=True, is_active=True,
           auto_check_enabled=False, order_mode="manual")
    monkeypatch.setattr(smm, "Session", lambda: _Sess(p))
    state = {"mode": "manual"}

    async def _om(b, pk): return state["mode"]
    async def _set(b, pk, attr, val):
        assert attr == "order_mode"; state["mode"] = val
    async def _z(*a, **k): return 0
    async def _s(*a, **k): return "margin"
    async def _t(*a, **k): return True
    monkeypatch.setattr(smm, "_panel_order_mode", _om)
    monkeypatch.setattr(smm, "_panel_set_setting", _set)
    monkeypatch.setattr(smm, "_panel_margin_pct", _z)
    monkeypatch.setattr(smm, "_panel_price_mode", _s)
    monkeypatch.setattr(smm, "_get_browsing_panel", _z)
    monkeypatch.setattr(smm, "_panel_bulk_enabled", _t)
    from tenant.bots.virtual_service import smm_price_sync as PS
    monkeypatch.setattr(PS, "panel_auto_on", _t)

    text, kb = await smm._panel_detail_content(1, 5)
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert "🧾 حالتِ ثبتِ سفارش" in labels and "⏳ تأییدِ ادمین" in labels
    btn = [b for r in kb.inline_keyboard for b in r if b.text == "⏳ تأییدِ ادمین"][0]
    assert btn.callback_data == "smm:panel:ordmode:5"

    # toggle handler
    async def _owner(cb, bot): return NS(id=1)
    monkeypatch.setattr(smm, "_owner", _owner)
    rendered = []
    async def _rend(target, bid, pid, prefix=""): rendered.append(pid)
    monkeypatch.setattr(smm, "_render_panel_detail", _rend)
    answers = []
    class CB:
        data = "smm:panel:ordmode:5"; message = object()
        async def answer(self, t=None, **k): answers.append(t)
    await smm.smm_panel_ordmode(CB(), None)
    assert state["mode"] == "auto" and rendered == [5] and "خودکار" in answers[0]
    await smm.smm_panel_ordmode(CB(), None)
    assert state["mode"] == "manual"
