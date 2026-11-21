"""RickPanel (مرزبان‌سازگار، بدونِ inbound) — PATCH_RICKPANEL_PANEL_V1."""
import json
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from plans import Feature, has_feature
from tenant.shared.panel_clients.marzban_panel import PanelError, _LAST_LOGIN_ERR, _LAST_LOGIN_TS, _TOKEN_CACHE
from tenant.shared.panel_clients.rickpanel_panel import RickPanelClient
from tenant.shared.panel_clients.vpn_panel_factory import client_class_for, make_panel_client_from_fields

pytestmark = pytest.mark.asyncio

SERVICE = {"min_days": 7, "min_data_gb": 1, "require_unlimited": False, "allow_on_hold": True}


def _app(state):
    users = state.setdefault("users", {})

    def auth(handler):
        async def w(request):
            if request.headers.get("Authorization") != "Bearer tok123":
                return web.json_response({"detail": "Not authenticated"}, status=401)
            state.setdefault("calls", []).append(f"{request.method} {request.path}")
            return await handler(request)
        return w

    async def token(request):
        form = await request.post()
        if form.get("username") != "u" or form.get("password") != "p":
            return web.json_response({"detail": "Incorrect username or password"}, status=401)
        return web.json_response({"access_token": "tok123", "token_type": "bearer"})

    @auth
    async def admin(request):
        return web.json_response({"username": "u", "service": SERVICE})

    @auth
    async def create(request):
        body = await request.json()
        state["create_body"] = body
        if body["username"] in users:
            return web.json_response({"detail": "User already exists"}, status=409)
        if body.get("status") == "on_hold" and not SERVICE["allow_on_hold"]:
            return web.json_response({"detail": "service does not allow on hold"}, status=400)
        if state.get("reject"):
            return web.json_response({"detail": state["reject"]}, status=400)
        users[body["username"]] = {**body, "used_traffic": 0, "subscription_url": f"/sub/{body['username']}/TOKEN",
                                   "links": ["vless://x"]}
        return web.json_response(users[body["username"]])

    @auth
    async def get_user(request):
        u = users.get(request.match_info["u"])
        return web.json_response(u) if u else web.json_response({"detail": "User not found"}, status=404)

    @auth
    async def put_user(request):
        name = request.match_info["u"]
        if name not in users:
            return web.json_response({"detail": "User not found"}, status=404)
        state["put_body"] = await request.json()
        users[name].update(state["put_body"])
        return web.json_response(users[name])

    @auth
    async def reset(request):
        if state.get("reset_403"):
            return web.json_response({"detail": "wait 3 days"}, status=403)
        return web.json_response({"ok": True})

    @auth
    async def revoke(request):
        return web.json_response({"subscription_url": "/sub/new/TOKEN2"})

    @auth
    async def lst(request):
        return web.json_response({"users": [{"username": n, "used_traffic": 5, "data_limit": 10, "expire": 0}
                                             for n in users], "total": len(users)})

    app = web.Application()
    app.router.add_post("/api/admin/token", token)
    app.router.add_get("/api/admin", admin)
    app.router.add_post("/api/user", create)
    app.router.add_get("/api/user/{u}", get_user)
    app.router.add_put("/api/user/{u}", put_user)
    app.router.add_post("/api/user/{u}/reset", reset)
    app.router.add_post("/api/user/{u}/revoke_sub", revoke)
    app.router.add_get("/api/users", lst)
    return app


@pytest.fixture
async def srv():
    for d in (_TOKEN_CACHE, _LAST_LOGIN_ERR, _LAST_LOGIN_TS):
        d.clear()
    state = {}
    server = TestServer(_app(state))
    await server.start_server()
    c = RickPanelClient(str(server.make_url("")).rstrip("/"), "u", "p", sub_base="")
    yield state, c
    await c.close()
    await server.close()


async def test_factory_and_plan_gate():
    assert client_class_for("rickpanel") is RickPanelClient and client_class_for("rickpanel#2") is RickPanelClient
    c = make_panel_client_from_fields("rickpanel", "https://x", "u", "p", "", "-")
    assert isinstance(c, RickPanelClient) and not c.inbound_tags            # «-» اینباندی نمی‌شود
    assert has_feature("pro", Feature.RICKPANEL_PANEL) and not has_feature("free", Feature.RICKPANEL_PANEL)


async def test_connection_shows_service_rules_and_needs_no_inbounds(srv):
    state, c = srv
    res = await c.test_connection()
    assert res["ok"] and res["inbounds"] == [] and res["service"] == SERVICE and res["admin"] == "u"
    assert "GET /api/inbounds" not in state["calls"]


async def test_create_account_body_has_no_inbounds_and_correct_units(srv):
    state, c = srv
    out = await c.create_account("Ali.X", 5, 30)
    b = state["create_body"]
    assert "inbounds" not in b and b["username"] == "ali_x" and b["data_limit"] == 5 * 1024 ** 3
    assert abs(b["expire"] - (int(time.time()) + 30 * 86400)) < 5 and b["status"] == "active"
    assert out["email"] == "ali_x" and out["sub_link"].endswith("/sub/ali_x/TOKEN")
    assert "GET /api/inbounds" not in state["calls"]


async def test_unlimited_and_trial_on_hold(srv):
    state, c = srv
    await c.create_account("u1", 0, 0)
    assert state["create_body"]["data_limit"] == 0 and state["create_body"]["expire"] == 0
    await c.create_account("trial", 0, 0, mb=300, hours=2, on_hold=True)
    b = state["create_body"]
    assert b["status"] == "on_hold" and b["on_hold_expire_duration"] == 7200 and b["expire"] == 0
    assert b["data_limit"] == 300 * 1024 * 1024


async def test_service_rule_rejection_has_persian_reason(srv):
    state, c = srv
    state["reject"] = "service requires unlimited data"
    with pytest.raises(PanelError) as e:
        await c.create_account("x", 5, 30)
    assert "نامحدود" in str(e.value)
    state["reject"] = None
    await c.create_account("dup", 1, 30)
    with pytest.raises(PanelError) as e2:
        await c.create_account("dup", 1, 30)
    assert "از قبل" in str(e2.value)


async def test_usage_renew_traffic_reset_revoke_delete_flow(srv):
    state, c = srv
    await c.create_account("bob", 2, 10)
    u = await c.get_usage("bob")
    assert u["total"] == 2 * 1024 ** 3 and u["enable"] and u["expiryTime"] > 0
    await c.add_traffic("bob", 3)
    assert state["put_body"]["data_limit"] == 5 * 1024 ** 3
    await c.renew("bob", 30)
    assert state["put_body"]["status"] == "active" and state["put_body"]["expire"] > time.time() + 35 * 86400
    await c.set_enabled("bob", False)
    assert state["put_body"] == {"status": "disabled"}
    assert await c.reset_usage("bob") is True
    assert (await c.reset_sub("bob")).endswith("/sub/new/TOKEN2")
    assert [x["email"] for x in await c.list_clients()] == ["bob"]


async def test_reset_too_soon_is_explained(srv):
    state, c = srv
    await c.create_account("carl", 1, 10)
    state["reset_403"] = True
    with pytest.raises(PanelError) as e:
        await c.reset_usage("carl")
    assert "چند روز" in str(e.value)


async def test_bad_password_fails_cleanly():
    for d in (_TOKEN_CACHE, _LAST_LOGIN_ERR, _LAST_LOGIN_TS):
        d.clear()
    server = TestServer(_app({}))
    await server.start_server()
    c = RickPanelClient(str(server.make_url("")).rstrip("/"), "u", "WRONG")
    try:
        with pytest.raises(PanelError):
            await c.test_connection()
    finally:
        await c.close()
        await server.close()
