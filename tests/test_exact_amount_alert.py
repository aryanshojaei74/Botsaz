import pytest
from types import SimpleNamespace
from tenant.gateways import exact_amount_alert as ea




def test_targets():
    for d in ("w:apay", "w:apay:vpn:3:1000", "w:abangateway:vc:1:2:3:4", "w:uniquepay"):
        assert ea.is_target(d)
    for d in ("w:charge", "w:zibal", "w:tonpays", None, ""):
        assert not ea.is_target(d)


class _Ev:
    def __init__(self, data, calls):
        self.data = data
        self._calls = calls

    async def answer(self, text=None, show_alert=None, *a, **kw):
        self._calls.append((text, show_alert))

    def model_copy(self, update=None):
        return _Ev(self.data, self._calls)


async def _run(monkeypatch, data, setting, handler_text=None, second=False):
    import tenant
    calls = []

    async def gbr(bot):
        return SimpleNamespace(id=1)

    async def gs(bid, key, default=""):
        return setting
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "get_setting", gs)

    async def handler(ev, d):
        await ev.answer(handler_text) if handler_text else await ev.answer()
        if second:
            await ev.answer()
    await ea.exact_amount_alert_mw(handler, _Ev(data, calls), {"bot": object()})
    return calls


@pytest.mark.asyncio
async def test_first_empty_answer_becomes_alert(monkeypatch):
    assert await _run(monkeypatch, "w:uniquepay", "1") == [(ea.ALERT_TEXT, True)]


@pytest.mark.asyncio
async def test_handler_own_text_untouched_and_off_switch(monkeypatch):
    assert await _run(monkeypatch, "w:apay", "1", handler_text="درگاه در دسترس نیست") == [("درگاه در دسترس نیست", None)]
    assert await _run(monkeypatch, "w:apay", "0") == [(None, None)]
    assert await _run(monkeypatch, "w:charge", "1") == [(None, None)]


@pytest.mark.asyncio
async def test_only_first_answer_is_alert(monkeypatch):
    calls = await _run(monkeypatch, "w:abangateway", "1", second=True)
    assert calls[0] == (ea.ALERT_TEXT, True) and calls[1] == (None, None)
