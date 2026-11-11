import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tenant.shared.panel_clients.hmpanel_panel import HMPanelClient, _hm_name
from tenant.shared.panel_clients.vpn_panel import PanelError
from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client_from_fields, client_class_for

KEY = "hmp_testkey"


def _app(state):
    def auth(request):
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            raise web.HTTPUnauthorized(text='{"message":"bad"}')

    async def account(request):
        auth(request)
        return web.json_response({"id": "a1", "username": "ali", "role": "RESELLER", "balance": 5, "clientCount": 1, "maxClients": 10})

    async def inbounds(request):
        auth(request)
        return web.json_response({"data": [{"id": "ib1", "tag": "inbound-443"}, {"id": "ib2", "tag": "inbound-80"}]})

    def view(c):
        used = c["up"] + c["down"]
        return {"id": c["id"], "email": c["email"], "enable": c["enable"], "status": "active" if c["enable"] else "disabled",
                "upBytes": c["up"], "downBytes": c["down"], "usedBytes": used, "totalBytes": c["total"],
                "expiryTime": c["expiryTime"], "subscriptionUrl": f"https://p.io/s/{c['id']}"}

    async def create(request):
        auth(request)
        b = await request.json()
        state["create_body"] = b
        cid = f"c{len(state['clients']) + 1}"
        state["clients"][b["email"]] = {"id": cid, "email": b["email"], "enable": True, "up": 0, "down": 0,
                                        "total": b["total"], "expiryTime": b["expiryTime"]}
        return web.json_response({"ok": True})

    async def lookup(request):
        auth(request)
        c = state["clients"].get(request.query.get("key"))
        if not c:
            raise web.HTTPNotFound(text='{"message":"nf"}')
        return web.json_response(view(c))

    def by_id(cid):
        return next(c for c in state["clients"].values() if c["id"] == cid)

    async def patch(request):
        auth(request)
        c = by_id(request.match_info["cid"])
        b = await request.json()
        state["patch"] = b
        if "enable" in b:
            c["enable"] = b["enable"]
        if "total" in b:
            c["total"] = b["total"]
        return web.json_response(view(c))

    async def renew(request):
        auth(request)
        c = by_id(request.match_info["cid"])
        b = await request.json()
        state["renew"] = b
        base = max(int(time.time() * 1000), c["expiryTime"])
        c["expiryTime"] = base + b["days"] * 86400000
        if b.get("resetUsage"):
            c["up"] = c["down"] = 0
        return web.json_response(view(c))

    async def reset(request):
        auth(request)
        c = by_id(request.match_info["cid"])
        c["up"] = c["down"] = 0
        return web.json_response(view(c))

    async def delete(request):
        auth(request)
        c = by_id(request.match_info["cid"])
        state["clients"].pop(c["email"])
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/api/v1/account", account)
    app.router.add_get("/api/v1/inbounds", inbounds)
    app.router.add_post("/api/v1/clients", create)
    app.router.add_get("/api/v1/clients/lookup", lookup)
    app.router.add_patch("/api/v1/clients/{cid}", patch)
    app.router.add_delete("/api/v1/clients/{cid}", delete)
    app.router.add_post("/api/v1/clients/{cid}/renew", renew)
    app.router.add_post("/api/v1/clients/{cid}/reset-usage", reset)
    return app


@pytest.mark.asyncio
async def test_full_lifecycle():
    state = {"clients": {}}
    server = TestServer(_app(state))
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}/api"
        cl = HMPanelClient(base, "-", KEY, inbound_ids=None)
        info = await cl.test_connection()
        assert info["username"] == "ali" and info["inbounds"] == ["inbound-443", "inbound-80"]

        res = await cl.create_account("user_1", 10, 30)
        assert res["sub_link"] == "https://p.io/s/c1"
        assert state["create_body"]["inboundIds"] == ["ib1", "ib2"]
        assert state["create_body"]["total"] == 10 * 1024 ** 3
        assert state["create_body"]["expiryTime"] > time.time() * 1000

        cur = await cl.get_client("user_1")
        assert cur["data_limit"] == 10 * 1024 ** 3 and cur["status"] == "active" and cur["sub_link"].endswith("/s/c1")
        assert (await cl.get_client("nobody")) is None

        await cl.add_traffic("user_1", 5)
        assert state["patch"] == {"total": 15 * 1024 ** 3}
        await cl.renew("user_1", 30, reset_usage=True)
        assert state["renew"] == {"days": 30, "resetUsage": True}
        await cl.set_enabled("user_1", False)
        assert (await cl.get_client("user_1"))["status"] == "disabled"
        await cl.set_ip_limit("user_1", 2)
        assert state["patch"] == {"limitIp": 2}
        assert await cl.reset_usage("user_1") is True
        assert await cl.delete_account("user_1") is True
        assert await cl.delete_account("user_1") is False
        await cl.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_bad_key_and_tag_filter():
    state = {"clients": {}}
    server = TestServer(_app(state))
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        bad = HMPanelClient(base, "-", "hmp_wrong")
        with pytest.raises(PanelError):
            await bad.test_connection()
        await bad.close()
        cl = HMPanelClient(base, "-", KEY, inbound_ids=["inbound-80"])
        await cl.create_account("abc", 0, 0)
        assert state["create_body"]["inboundIds"] == ["ib2"]
        assert state["create_body"]["total"] == 0 and state["create_body"]["expiryTime"] == 0
        await cl.close()
    finally:
        await server.close()


def test_factory_and_name():
    assert client_class_for("hmpanel#2") is HMPanelClient
    c = make_panel_client_from_fields("hmpanel", "https://x.io/api", "-", KEY, inbound_ids_str="inbound-1,inbound-2")
    assert isinstance(c, HMPanelClient) and c.inbound_ids == ["inbound-1", "inbound-2"] and c.base_url == "https://x.io"
    assert _hm_name("user_12") == "user_12"
    n = _hm_name("کاربر ۱")
    assert n == _hm_name("کاربر ۱") and n.replace("-", "").replace("_", "").isalnum()
