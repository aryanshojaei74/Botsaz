import pytest
from aiohttp import web

from tenant.gateways import gateway_redirect as GR


async def _serve(handler):
    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_auto_page_ok_detects_real_redirect_page_vs_foreign_site():
    GR._AUTO_OK_CACHE.clear()

    async def ours(req):
        return web.Response(text="<a href='https://www.zarinpal.com/pg/StartPay/TESTREF1234567890'>go</a>", content_type="text/html")

    async def landing(req):
        return web.Response(text="<html>NEXIVO SHOP landing</html>", content_type="text/html")
    r1, b1 = await _serve(ours)
    r2, b2 = await _serve(landing)
    try:
        assert await GR._auto_page_ok(b1, "zarinpal") is True
        assert await GR._auto_page_ok(b2, "zarinpal") is False
        assert await GR._auto_page_ok("http://127.0.0.1:1", "zarinpal") is False      # در دسترس نیست
    finally:
        await r1.cleanup()
        await r2.cleanup()


@pytest.mark.asyncio
async def test_startpay_for_auto_falls_back_to_direct_when_domain_is_not_ours(monkeypatch):
    async def link(bot_id, gw):
        return "auto"

    async def base(bot_id, gw):
        return "https://nexivopanel.shop"
    monkeypatch.setattr(GR, "get_link", link)
    monkeypatch.setattr(GR, "_callback_base", base)

    async def bad(b, g):
        return False
    monkeypatch.setattr(GR, "_auto_page_ok", bad)
    url = await GR.startpay_for(1, "zarinpal", "A0000000000000000000000000000000abcdef")
    assert url == GR.direct_startpay("zarinpal", "A0000000000000000000000000000000abcdef") and "nexivopanel" not in url

    async def good(b, g):
        return True
    monkeypatch.setattr(GR, "_auto_page_ok", good)
    url2 = await GR.startpay_for(1, "zarinpal", "A0000000000000000000000000000000abcdef")
    assert url2.startswith("https://nexivopanel.shop/gw/zarinpal/start/")


@pytest.mark.asyncio
async def test_status_line_warns_when_auto_domain_is_not_ours(monkeypatch):
    async def link(bot_id, gw):
        return "auto"

    async def base(bot_id, gw):
        return "https://nexivopanel.shop"

    async def bad(b, g):
        return False
    monkeypatch.setattr(GR, "get_link", link)
    monkeypatch.setattr(GR, "_callback_base", base)
    monkeypatch.setattr(GR, "_auto_page_ok", bad)
    assert "pay.html" in await GR.status_line(1, "zarinpal")

    async def good(b, g):
        return True
    monkeypatch.setattr(GR, "_auto_page_ok", good)
    assert "✅ خودکار" in await GR.status_line(1, "zarinpal")


def test_payhtml_is_found_and_button_shown():
    assert GR.payhtml_available()
    assert GR.PAY_HTML_PATH.name == "pay.html" and GR.PAY_HTML_PATH.stat().st_size > 1000
    kb = GR._menu_kb("zarinpal", True, "")
    assert "gwr:sendfile" in [b.callback_data for r in kb.inline_keyboard for b in r]
