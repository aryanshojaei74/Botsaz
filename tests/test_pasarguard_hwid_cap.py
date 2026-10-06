"""«HWID limit cannot exceed 7» (گزارشِ ɪᴍ mehdi): پنل سقفِ hwid_limit دارد؛ ساخت باید با سقفِ پنل دوباره امتحان شود، نه خطا به مشتری."""
import asyncio
import base64
import json
import time

import pytest
from aiohttp import web

import tenant.shared.panel_clients.marzban_panel as M
import tenant.shared.panel_clients.pasargad_panel as PG


def _jwt():
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{b({'a': 1})}.{b({'exp': int(time.time()) + 3600})}.sig"


def _app(posts, cap, msg="HWID limit cannot exceed {cap}"):
    async def token(_):
        return web.json_response({"access_token": _jwt()})

    async def simple(_):
        return web.json_response({"groups": [{"id": 1, "name": "g"}], "total": 1})

    async def user(request):
        body = await request.json()
        posts.append(body["hwid_limit"])
        if body["hwid_limit"] > cap:
            return web.json_response({"detail": msg.format(cap=cap)}, status=400)
        return web.json_response({"username": body["username"], "subscription_url": "/sub/x"})

    app = web.Application()
    app.router.add_post("/api/admin/token", token)
    app.router.add_get("/api/groups/simple", simple)
    app.router.add_post("/api/user", user)
    return app


async def _run(app, port, fn):
    r = web.AppRunner(app)
    await r.setup()
    await web.TCPSite(r, "127.0.0.1", port).start()
    try:
        return await fn()
    finally:
        await r.cleanup()


def _reset():
    PG._GROUPS_CACHE.clear()
    PG._HWID_MAX.clear()
    M._TOKEN_CACHE.clear()
    M._LAST_LOGIN_TS.clear()


def test_error_message_parsing():
    assert PG._hwid_cap_from_error('POST /api/user HTTP 400: {"detail":"HWID limit cannot exceed 7"}') == 7
    assert PG._hwid_cap_from_error("HWID limit cannot be unlimited; maximum is 2") == 2
    assert PG._hwid_cap_from_error("something else") is None


def test_unlimited_default_is_retried_with_panel_cap_and_remembered():
    _reset()
    posts = []

    async def go():
        c = PG.PasarGuardPanelClient("http://127.0.0.1:8870", "u", "p", inbound_ids=["1"])
        try:
            a1 = await c.create_account("u1", gb=1, days=1)                 # limit_ip=0 ⇒ پیش‌فرضِ 999
            n_first = len(posts)
            a2 = await c.create_account("u2", gb=1, days=1)
            return a1, a2, n_first, len(posts) - n_first
        finally:
            await c.close()

    a1, a2, n_first, n_second = asyncio.run(_run(_app(posts, 7), 8870, go))
    assert a1["sub_link"].endswith("/sub/x") and a2["sub_link"].endswith("/sub/x")
    assert posts[:2] == [999, 7]            # اولی رد شد ⇒ با سقفِ پنل دوباره
    assert n_first == 2
    assert n_second == 1 and posts[-1] == 7  # دفعهٔ بعد سقف یادش بود؛ بدونِ تلاشِ اضافه


def test_seller_limit_above_cap_is_clamped_and_below_cap_untouched():
    _reset()
    posts = []

    async def go():
        c = PG.PasarGuardPanelClient("http://127.0.0.1:8871", "u", "p", inbound_ids=["1"])
        try:
            await c.create_account("u1", gb=1, days=1, limit_ip=10)   # بالاتر از سقف
            await c.create_account("u2", gb=1, days=1, limit_ip=2)    # پایین‌تر: دست نخورد
        finally:
            await c.close()

    asyncio.run(_run(_app(posts, 7), 8871, go))
    assert posts == [10, 7, 2]


def test_unrelated_400_is_not_retried():
    _reset()
    posts = []

    async def go():
        c = PG.PasarGuardPanelClient("http://127.0.0.1:8872", "u", "p", inbound_ids=["1"])
        try:
            return await c.create_account("u1", gb=1, days=1)
        finally:
            await c.close()

    app = _app(posts, 0, msg="Username already exists")      # هر hwid_limit>0 ⇒ خطایِ نامرتبط
    with pytest.raises(M.PanelError) as ei:
        asyncio.run(_run(app, 8872, go))
    assert "Username already exists" in str(ei.value)
    assert len(posts) == 1
