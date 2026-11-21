"""اتصال‌هایِ keep-aliveِ تلگرام بینِ Botها/دورها مشترک است (کمتر دست‌دهیِ TCP/TLS ⇒ کمتر تایم‌اوتِ _create_connection)."""
import pytest
from aiohttp import web
from aiogram.client.session.aiohttp import AiohttpSession

import config  # noqa: F401  (پچ‌ها را نصب می‌کند)

pytestmark = pytest.mark.asyncio


async def _server():
    conns = {"n": 0}

    @web.middleware
    async def mw(req, handler):
        return await handler(req)

    async def ok(req): return web.Response(text="ok")
    app = web.Application(middlewares=[mw])
    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    orig = runner.server.connection_made

    def cm(proto, transport):
        conns["n"] += 1
        return orig(proto, transport)
    runner.server.connection_made = cm
    return runner, port, conns


async def test_sessions_share_one_connector_and_connections(monkeypatch):
    monkeypatch.delenv("TG_SHARED_CONNECTOR", raising=False)
    runner, port, conns = await _server()
    s1, s2 = AiohttpSession(), AiohttpSession()
    a, b = await s1.create_session(), await s2.create_session()
    shared = a.connector
    assert a is not b and shared is b.connector
    for sess in (a, b, a):
        async with sess.get(f"http://127.0.0.1:{port}/") as r:
            assert await r.text() == "ok"
    assert conns["n"] == 1                          # سه درخواست از دو session، فقط یک اتصالِ TCP
    await s1.close()
    assert not shared.closed and not b.closed  # بستنِ یک Bot connectorِ مشترک را نمی‌بندد
    async with b.get(f"http://127.0.0.1:{port}/") as r:
        assert r.status == 200
    await s2.close()
    await shared.close()
    await runner.cleanup()


async def test_switch_off_restores_separate_connectors(monkeypatch):
    monkeypatch.setenv("TG_SHARED_CONNECTOR", "0")
    s1, s2 = AiohttpSession(), AiohttpSession()
    a, b = await s1.create_session(), await s2.create_session()
    assert a.connector is not b.connector
    await s1.close()
    await s2.close()
