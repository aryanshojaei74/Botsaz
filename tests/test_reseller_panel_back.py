"""صفحهٔ «پنل نمایندگی» (هر دو حالت) باید دکمهٔ «بازگشت به منو» داشته باشد."""
import pytest
import tenant.shared.reseller_signup as RS


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_panel", [False, True])
async def test_render_has_back_to_menu(monkeypatch, has_panel):
    async def _p(b, t):
        class P:
            web_username = "u"
        return P() if has_panel else None
    async def _m(b): return 350000
    async def _bal(b, t): return 0
    async def _url(b): return ""
    monkeypatch.setattr(RS, "_partner", _p)
    monkeypatch.setattr(RS, "min_balance", _m)
    monkeypatch.setattr(RS, "_balance", _bal)
    monkeypatch.setattr(RS, "_panel_url", _url)
    monkeypatch.setattr(RS, "_has_panel", lambda p: bool(p))
    _t, kb = await RS.render(1, 2)
    assert "mbtn:home" in _cbs(kb)
