"""پنلِ HEIMDALL (فورکِ نسلِ جدیدِ ۳X-UI): ورود با twoFactorCode، CSRF از /csrf-token، RBAC، و کانفیگِ وایرگارد (PATCH_HEIMDALL_PANEL_V1)."""
import base64

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tenant.shared.panel_clients import heimdall_panel as H
from tenant.shared.panel_clients.vpn_panel_factory import client_class_for

pytestmark = pytest.mark.asyncio

SECRET = "/NfpSecretPath"
TOKEN = "csrf-after-login-123"


def _keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    k = X25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                           serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


SERVER_PRIV, SERVER_PUB = _keypair()
CLIENT_PRIV, CLIENT_PUB = _keypair()


def _app(state):
    async def home(request):
        return web.Response(text="<html><head></head><body>no csrf meta</body></html>", content_type="text/html")

    async def login(request):
        body = await request.json()
        state["login_body"] = body
        if body.get("username") != "u" or body.get("password") != "p" or "twoFactorCode" not in body:
            return web.json_response({"success": False, "msg": "Wrong username or password"}, status=400)
        resp = web.json_response({"success": True, "msg": "Logged in successfully"})
        resp.set_cookie("heimdall", "sess")
        return resp

    async def csrf(request):
        if request.cookies.get("heimdall") != "sess":
            return web.json_response({"success": False, "msg": "login required"}, status=401)
        return web.json_response({"success": True, "obj": TOKEN})

    def need_session(handler):
        async def w(request):
            if request.cookies.get("heimdall") != "sess":
                return web.json_response({"success": False, "msg": "login required"}, status=401)
            if request.method == "POST" and request.headers.get("X-CSRF-Token") != TOKEN:
                state["csrf_rejects"] = state.get("csrf_rejects", 0) + 1
                return web.json_response({"success": False, "msg": "invalid csrf token"}, status=403)
            return await handler(request)
        return w

    @need_session
    async def inb_list(request):
        if state.get("rbac_block"):
            return web.json_response({"success": False, "msg": "inbounds.view permission required"}, status=403)
        return web.json_response({"success": True, "obj": state["inbounds"]})

    @need_session
    async def inb_options(request):
        return web.json_response({"success": True, "obj": [
            {"id": i["id"], "remark": i["remark"], "protocol": i["protocol"], "port": i["port"],
             "listen": i.get("listen", ""), "shareAddr": i.get("shareAddr", ""), **state.get("opts_extra", {}).get(i["id"], {})}
            for i in state["inbounds"]]})

    @need_session
    async def inb_get(request):
        if state.get("get_block"):
            return web.json_response({"success": False, "msg": "inbounds.view permission required"}, status=403)
        iid = int(request.match_info["id"])
        ib = next(i for i in state["inbounds"] if i["id"] == iid)
        return web.json_response({"success": True, "obj": ib})

    @need_session
    async def add(request):
        body = await request.json()
        state["added"] = body
        state["clients"][body["client"]["email"]] = {"client": {**body["client"], **state.get("server_fill", {})},
                                                     "inboundIds": body["inboundIds"]}
        return web.json_response({"success": True, "obj": None})

    @need_session
    async def get_client(request):
        c = state["clients"].get(request.match_info["email"])
        return web.json_response({"success": bool(c), "obj": c})

    @need_session
    async def empty_list(request):
        return web.json_response({"success": True, "obj": []})

    @need_session
    async def clients_list(request):
        return web.json_response({"success": True, "obj": [v["client"] for v in state["clients"].values()]})

    app = web.Application()
    r = app.router
    r.add_get(SECRET + "/", home)
    r.add_post(SECRET + "/login", login)
    r.add_get(SECRET + "/csrf-token", csrf)
    r.add_get(SECRET + "/panel/api/inbounds/list", inb_list)
    r.add_get(SECRET + "/panel/api/inbounds/options", inb_options)
    r.add_get(SECRET + "/panel/api/inbounds/get/{id}", inb_get)
    r.add_post(SECRET + "/panel/api/clients/add", add)
    r.add_get(SECRET + "/panel/api/clients/get/{email}", get_client)
    r.add_get(SECRET + "/panel/api/clients/links/{email}", empty_list)
    r.add_get(SECRET + "/panel/api/clients/subLinks/{sub}", empty_list)
    r.add_get(SECRET + "/panel/api/clients/list", clients_list)
    return app


def _wg_inbound(iid=2):
    return {"id": iid, "enable": True, "protocol": "wireguard", "port": 51820, "remark": "WG-Germany",
            "listen": "", "shareAddr": "wg.example.net",
            "settings": {"mtu": 1380, "secretKey": SERVER_PRIV, "peers": []}}


def _vless_inbound(iid=1):
    return {"id": iid, "enable": True, "protocol": "vless", "port": 443, "remark": "VLESS", "settings": {}}


@pytest.fixture
async def panel():
    state = {"inbounds": [_vless_inbound(1), _wg_inbound(2)], "clients": {}}
    server = TestServer(_app(state))
    await server.start_server()
    c = H.HeimdallPanelClient(f"http://127.0.0.1:{server.port}{SECRET}", "u", "p")
    yield c, state
    await c.close()
    await server.close()


async def test_login_sends_two_factor_field_and_picks_csrf_from_endpoint(panel):
    c, state = panel
    await c.login()
    assert state["login_body"]["twoFactorCode"] == "" and c._csrf == TOKEN


async def test_create_account_passes_csrf_check_and_wrong_password_fails(panel):
    c, state = panel
    out = await c.create_account("alice", 10, 30, inbound_ids=[1])
    assert state["added"]["client"]["email"] == "alice" and state["added"]["inboundIds"] == [1]
    assert out["sub_id"] and state.get("csrf_rejects", 0) == 0
    bad = H.HeimdallPanelClient(c.base_url, "u", "wrong")
    try:
        with pytest.raises(H.PanelError):
            await bad.login()
    finally:
        await bad.close()


async def test_rbac_blocked_list_falls_back_to_options(panel):
    c, state = panel
    state["rbac_block"] = True
    ibs = await c.list_inbounds()
    assert [i["id"] for i in ibs] == [1, 2]


async def test_wireguard_conf_is_built_when_panel_returns_no_links(panel):
    c, state = panel
    state["server_fill"] = {"privateKey": CLIENT_PRIV, "publicKey": CLIENT_PUB,
                            "allowedIPs": ["10.8.0.7/32"], "keepAlive": 25, "preSharedKey": "PSK="}
    await c.create_account("bob", 5, 30, inbound_ids=[2])
    links = await c.get_client_links("bob")
    assert len(links) == 1
    conf = links[0]
    assert f"PrivateKey = {CLIENT_PRIV}" in conf and "Address = 10.8.0.7/32" in conf
    assert f"PublicKey = {SERVER_PUB}" in conf                      # از secretKeyِ سرور مشتق شد
    assert "Endpoint = wg.example.net:51820" in conf and "PersistentKeepalive = 25" in conf
    assert "PresharedKey = PSK=" in conf and "MTU = 1380" in conf and "# Name = WG-Germany" in conf
    # همین از مسیرِ sub_id هم به دست می‌آید
    sub = state["clients"]["bob"]["client"]["subId"]
    assert (await c.get_sub_links(sub)) == links


async def test_non_wireguard_client_without_links_returns_empty(panel):
    c, state = panel
    await c.create_account("carol", 5, 30, inbound_ids=[1])
    assert await c.get_client_links("carol") == []


def test_build_wg_conf_requires_keys_and_address():
    ib = _wg_inbound()
    assert H.build_wg_conf(ib, {"email": "x"}, "https://p.example:1/s") is None
    ok = H.build_wg_conf({**ib, "shareAddr": ""}, {"privateKey": CLIENT_PRIV, "allowedIPs": "10.0.0.2/32"},
                         "https://panel.example.org:8957/secret")
    assert "Endpoint = panel.example.org:51820" in ok          # بدونِ shareAddr: میزبانِ خودِ پنل
    assert H.wg_public_key(SERVER_PRIV) == SERVER_PUB and H.wg_public_key("bad") is None


def test_factory_and_registrations():
    assert client_class_for("heimdall") is H.HeimdallPanelClient
    assert client_class_for("heimdall#2") is H.HeimdallPanelClient     # پنلِ دوم
    from plans import Feature
    from tenant.bots.vpn import vpn_admin
    assert vpn_admin.PANEL_LABELS["heimdall"] and vpn_admin._PANEL_EXTRA_FEATURE_GATE["heimdall"] is Feature.HEIMDALL_PANEL
    assert "heimdall" in vpn_admin._IP_LIMIT_PANELS


async def test_wireguard_conf_works_for_admin_without_inbounds_view(panel):
    """ادمینِ واقعیِ مشتری: /inbounds/get و /list ۴۰۳ می‌دهند؛ فقط /options (با wgPublicKey/wgMtu/wgDns) در دسترس است."""
    c, state = panel
    state["get_block"] = True
    state["rbac_block"] = True
    wg = _wg_inbound(2)
    wg["settings"] = {}
    wg["shareAddr"] = ""
    state["inbounds"] = [_vless_inbound(1), wg]
    state["opts_extra"] = {2: {"wgPublicKey": SERVER_PUB, "wgMtu": 1280, "wgDns": "8.8.8.8"}}
    state["server_fill"] = {"privateKey": CLIENT_PRIV, "allowedIPs": ["10.9.0.3/32"], "keepAlive": 0}
    await c.create_account("dina", 5, 30, inbound_ids=[2])
    (conf,) = await c.get_client_links("dina")
    assert f"PublicKey = {SERVER_PUB}" in conf and "MTU = 1280" in conf and "DNS = 8.8.8.8" in conf
    assert "Address = 10.9.0.3/32" in conf and "PersistentKeepalive" not in conf
    assert "Endpoint = 127.0.0.1:51820" in conf               # بدونِ shareAddr: میزبانِ خودِ پنل


def test_wireguard_uri_with_camelcase_query_keys_is_parsed():
    from tenant.bots.vpn.vpn_user import _parse_wireguard_uri
    uri = ("wireguard://PRIVKEY%2Babc%3D@cim.example.net:35576?publicKey=SERVERPUB%3D&address=10.7.0.2%2F32"
           "&allowedIPs=0.0.0.0%2F0%2C%3A%3A%2F0&mtu=1420&dns=9.9.9.9&presharedKey=PSK%3D&keepAlive=25#AE_WG")
    conf = _parse_wireguard_uri(uri)
    assert conf and "PrivateKey = PRIVKEY+abc=" in conf and "PublicKey = SERVERPUB=" in conf
    assert "Address = 10.7.0.2/32" in conf and "Endpoint = cim.example.net:35576" in conf
    assert "PresharedKey = PSK=" in conf and "PersistentKeepalive = 25" in conf and "DNS = 9.9.9.9" in conf
    assert "# Name = AE_WG" in conf


def test_check_script_masks_link_secrets():
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "chk", os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "check_heimdall.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    out = m._mask_uri("wireguard://SECRETKEY%3D@h.example:1?publicKey=PUB&presharedKey=PSK#x")
    assert "SECRETKEY" not in out and "PSK" not in out and "publicKey=PUB" in out and "h.example:1" in out
    assert "UUID-1234" not in m._mask_uri("vless://UUID-1234@h.example:443?security=reality&pbk=PUBKEY&sid=ab#n")
