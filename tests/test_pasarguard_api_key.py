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
