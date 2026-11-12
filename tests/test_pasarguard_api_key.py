"""PATCH_PASARGUARD_APIKEY_HEADER_V1 — اتصال با API Key: PasarGuard کلید را فقط از X-Api-Key / «Authorization: ApiKey» می‌پذیرد."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tenant.shared.panel_clients.pasargad_panel import PasarGuardPanelClient
from tenant.shared.panel_clients.marzban_panel import MarzbanPanelClient

KEY = "pgk_testkey_123"
seen = []


def _app():
    def authed(request):
        seen.append(dict(request.headers))
        # همان منطقِ app/routers/authentication.py در PasarGuard: Bearer ⇒ JWT (نامعتبر)، فقط X-Api-Key / ApiKey معتبر است
        if request.headers.get("X-Api-Key", "").strip() == KEY:
            return True
        scheme, _, cred = request.headers.get("Authorization", "").partition(" ")
        return scheme.lower() == "apikey" and cred.strip() == KEY

    async def groups(request):
        if not authed(request):
            raise web.HTTPUnauthorized(text='{"detail":"Could not validate credentials"}')
        return web.json_response({"groups": [{"id": 1, "name": "g1", "is_disabled": False}], "total": 1})

    async def simple(request):
        return await groups(request)
    app = web.Application()
    app.router.add_get("/api/groups", groups)
    app.router.add_get("/api/groups/simple", simple)
    return app


@pytest.mark.asyncio
async def test_pasarguard_api_key_is_sent_in_the_headers_the_panel_accepts():
    seen.clear()
    server = TestServer(_app())
    await server.start_server()
    try:
        cl = PasarGuardPanelClient(f"http://{server.host}:{server.port}", "", "", api_key=KEY)
        res = await cl.test_connection()
        assert res["ok"] is True and res["inbounds"] == [1]
        h = seen[-1]
        assert h.get("X-Api-Key") == KEY
        assert h.get("Authorization") == f"ApiKey {KEY}"              # Bearer نیست (پنل آن را JWT می‌خواند)
        await cl.close()
    finally:
        await server.close()


def test_username_password_mode_still_uses_bearer():
    cl = PasarGuardPanelClient("http://x", "u", "p")
    cl._token = "jwt123"
    assert cl._auth_headers() == {"Authorization": "Bearer jwt123"}


def test_other_marzban_family_panels_keep_bearer_and_also_send_x_api_key():
    cl = MarzbanPanelClient("http://x", "", "", api_key="k")
    h = cl._auth_headers()
    assert h["Authorization"] == "Bearer k" and h["X-Api-Key"] == "k"


@pytest.mark.asyncio
async def test_slow_create_logs_a_timing_breakdown(caplog, monkeypatch):
    """PATCH_PASARGUARD_CREATE_TIMING_V1: ساختِ کند باید در لاگ بگوید وقت در گروه‌ها رفته یا در POSTِ پنل."""
    import asyncio
    import logging
    from tenant.shared.panel_clients import pasargad_panel as PM

    async def slow_user(request):
        await asyncio.sleep(0.15)
        b = await request.json()
        return web.json_response({"username": b["username"], "subscription_url": "/sub/x"})

    async def groups(request):
        return web.json_response({"groups": [{"id": 1, "name": "g"}], "total": 1})
    app = web.Application()
    app.router.add_get("/api/groups", groups)
    app.router.add_get("/api/groups/simple", groups)
    app.router.add_post("/api/user", slow_user)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(PM, "_SLOW_CREATE_LOG_SEC", 0.05)
    try:
        cl = PM.PasarGuardPanelClient(f"http://{server.host}:{server.port}", "", "", api_key="k")
        with caplog.at_level(logging.WARNING):
            await cl.create_account("slowuser", 1, 1)
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "pasarguard create slow" in msgs and "auth=api_key" in msgs and "post=" in msgs
        await cl.close()
    finally:
        await server.close()
