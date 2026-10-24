import asyncio
import base64
import json
import time

from aiohttp import web

import tenant.shared.panel_clients.pasargad_panel as PG
import tenant.shared.panel_clients.marzban_panel as M


def _jwt():
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{b({'a': 1})}.{b({'exp': int(time.time()) + 3600})}.sig"


def _server(calls, *, full="403", simple="200", simple_groups=None, full_groups=None):
    simple_groups = simple_groups if simple_groups is not None else [{"id": 1, "name": "g"}]
    full_groups = full_groups if full_groups is not None else simple_groups

    async def token(_):
        calls.append("login")
        return web.json_response({"access_token": _jwt()})

    async def groups(_):
        calls.append("full")
        if full == "403":
            return web.json_response({"detail": "forbidden"}, status=403)
        return web.json_response({"groups": full_groups, "total": len(full_groups)})

    async def simple_h(_):
        calls.append("simple")
        if simple == "404":
            return web.json_response({"detail": "nf"}, status=404)
        return web.json_response({"groups": simple_groups, "total": len(simple_groups)})

    app = web.Application()
    app.router.add_post("/api/admin/token", token)
    app.router.add_get("/api/groups", groups)
    app.router.add_get("/api/groups/simple", simple_h)
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
    M._TOKEN_CACHE.clear()
    M._LAST_LOGIN_TS.clear()


def test_operator_panel_never_hits_forbidden_full_list_or_relogin():
    _reset()
    calls = []

    async def go():
        c = PG.PasarGuardPanelClient("http://127.0.0.1:8890", "u", "p")
        try:
            return await c.list_groups()
        finally:
            await c.close()

    res = asyncio.run(_run(_server(calls, full="403"), 8890, go))
    assert res == [{"id": 1, "name": "g"}]
    assert "full" not in calls            # مسیرِ ۴۰۳ اصلاً زده نشد
    assert calls.count("login") == 1      # لاگینِ اجباریِ اضافه نداشتیم


def test_old_panel_without_simple_falls_back_to_full():
    _reset()
    calls = []

    async def go():
        c = PG.PasarGuardPanelClient("http://127.0.0.1:8889", "u", "p")
        try:
            return await c.list_groups()
        finally:
            await c.close()

    res = asyncio.run(_run(_server(calls, full="200", simple="404"), 8889, go))
    assert res == [{"id": 1, "name": "g"}]
    assert calls.index("simple") < calls.index("full")


def test_result_is_cached_so_second_call_makes_no_request():
    _reset()
    calls = []

    async def go():
        c = PG.PasarGuardPanelClient("http://127.0.0.1:8888", "u", "p")
        try:
            await c.list_groups()
            n = len(calls)
            await c.list_groups()
            return n
        finally:
            await c.close()

    n = asyncio.run(_run(_server(calls, full="403"), 8888, go))
    assert len(calls) == n


def test_trial_uses_pooled_client_whose_close_is_noop():
    from tenant import _trial_panel_client

    class P:
        panel_key = "pasarguard"
        base_url = "http://127.0.0.1:1"
        username = "u"
        password = "p"
        sub_base = ""
        inbound_ids = "1"
        api_key = None

    c1 = _trial_panel_client(P())
    c2 = _trial_panel_client(P())
    assert c1.raw is c2.raw                       # همان کلاینتِ مشترک
    assert asyncio.run(c1.close()) is None        # close بی‌اثر


def test_group_missing_in_light_list_is_rechecked_against_full_list():
    """لیستِ سبک ممکن است ناقص باشد؛ گروهِ ۲ فقط در لیستِ کامل هست ⇒ ساخت نباید رد شود."""
    _reset()
    calls = []
    app = _server(calls, full="200", simple_groups=[{"id": 1, "name": "a"}],
                  full_groups=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
    posted = []

    async def user(request):
        posted.append(await request.json())
        return web.json_response({"username": "u1", "subscription_url": "/sub/abc"})

    app.router.add_post("/api/user", user)

    async def go():
        c = PG.PasarGuardPanelClient("http://127.0.0.1:8887", "u", "p", inbound_ids=["2"])
        try:
            return await c.create_account("u1", gb=1, days=1)
        finally:
            await c.close()

    acc = asyncio.run(_run(app, 8887, go))
    assert posted and posted[0].get("group_ids") == [2]
    assert acc["sub_link"].endswith("/sub/abc")
    assert "full" in calls


def test_403_does_not_sleep_on_login_throttle():
    """۴۰۳ (نداشتنِ اجازه) نباید پشتِ محدودکنندهٔ لاگین ۱۲ثانیه بخوابد؛ خطا فوری و کشِ توکن سالم می‌ماند."""
    _reset()
    calls = []
    app = _server(calls, full="403")

    async def go():
        c = PG.PasarGuardPanelClient("http://127.0.0.1:8886", "u", "p")
        try:
            await c.login()                       # لاگینِ واقعی ⇒ پنجرهٔ ۱۲ثانیه‌ای باز می‌شود
            t = time.monotonic()
            try:
                await c._request("GET", "/api/groups")
            except M.PanelError as e:
                return time.monotonic() - t, str(e)
            return time.monotonic() - t, "no-error"
        finally:
            await c.close()

    dt, msg = asyncio.run(_run(app, 8886, go))
    assert "HTTP 403" in msg
    assert dt < 1.5                                # قبلاً ≈۱۲ثانیه
    assert calls.count("login") == 1               # لاگینِ اجباریِ اضافه نزد
    assert (("http://127.0.0.1:8886", "u")) in M._TOKEN_CACHE   # توکنِ کش پاک نشد


def test_failed_login_fails_fast_inside_throttle_window(monkeypatch):
    """پنلی که لاگینش خطا می‌دهد: تلاشِ بعدی در پنجرهٔ ۱۲ثانیه همان خطا را فوراً بدهد، نه بعد از خواب."""
    _reset()
    M._LAST_LOGIN_ERR.clear()
    monkeypatch.setattr(M, "_MIN_LOGIN_INTERVAL", 4.0)
    monkeypatch.setattr(M, "_MAX_LOGIN_WAIT", 6.0)
    hits = {"n": 0}

    async def bad_token(_):
        hits["n"] += 1
        return web.json_response({"detail": "Incorrect username or password"}, status=401)

    app = web.Application()
    app.router.add_post("/api/admin/token", bad_token)

    async def go():
        c1 = PG.PasarGuardPanelClient("http://127.0.0.1:8885", "u", "bad")
        c2 = PG.PasarGuardPanelClient("http://127.0.0.1:8885", "u", "bad")
        errs = []
        try:
            for c in (c1, c2):
                t = time.monotonic()
                try:
                    await c.login()
                except M.PanelError as e:
                    errs.append((time.monotonic() - t, str(e)))
            return errs
        finally:
            await c1.close()
            await c2.close()

    errs = asyncio.run(_run(app, 8885, go))
    assert len(errs) == 2
    assert errs[1][0] < 1.0                 # بدونِ خوابِ ۴ثانیه‌ای
    assert errs[0][1] == errs[1][1]         # همان پیامِ واقعیِ پنل
    assert hits["n"] == 1                   # فشارِ اضافه به پنل نیامد


def test_successful_relogin_still_waits_but_clears_error(monkeypatch):
    _reset()
    M._LAST_LOGIN_ERR.clear()
    M._LAST_LOGIN_ERR[("http://127.0.0.1:8884", "u")] = (0.0, "قدیمی")   # خطای خیلی قدیمی نباید فوراً شکست بدهد
    app = web.Application()

    async def token(_):
        return web.json_response({"access_token": "no-exp"})

    app.router.add_post("/api/admin/token", token)

    async def go():
        c = PG.PasarGuardPanelClient("http://127.0.0.1:8884", "u", "p")
        try:
            return await c.login()
        finally:
            await c.close()

    assert asyncio.run(_run(app, 8884, go)) is True
    assert ("http://127.0.0.1:8884", "u") not in M._LAST_LOGIN_ERR
