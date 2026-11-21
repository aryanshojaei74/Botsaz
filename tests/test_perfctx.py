import asyncio
import os
import tempfile

from aiohttp import web


def test_breakdown_counts_db_http_tg():
    db = tempfile.mktemp(suffix=".db")
    import perfctx
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    import aiohttp

    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")   # موتورِ مستقل؛ config را reload نمی‌کنیم
    perfctx.install_db(engine)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def hello(_):
        await asyncio.sleep(0.05)
        return web.json_response({"ok": 1})

    async def go():
        app = web.Application()
        app.router.add_get("/x", hello)
        r = web.AppRunner(app)
        await r.setup()
        await web.TCPSite(r, "127.0.0.1", 8896).start()
        acc = perfctx.begin()
        async with Session() as s:
            await s.execute(text("select 1"))
            await s.execute(text("select 2"))
        async with aiohttp.ClientSession() as cs:
            async with cs.get("http://127.0.0.1:8896/x") as resp:
                await resp.read()
        perfctx.add("tg", 1.5, "SendMessage")
        await r.cleanup()
        return acc

    acc = asyncio.run(go())
    assert acc["db"][0] >= 2
    assert acc["http"][0] == 1 and acc["http"][1] >= 0.05 and "127.0.0.1" in acc["http"][3]
    out = perfctx.fmt(acc)
    assert "tg 1×1.5s(max 1.5s SendMessage)" in out and "db " in out and "http 1×" in out


def test_no_context_is_silent_and_cheap():
    import perfctx
    perfctx._acc.set(None)
    perfctx.add("db", 1.0, "x")  # نباید خطا بدهد
    assert perfctx.fmt(None) == ""


def test_login_throttle_wait_is_reported(monkeypatch):
    import perfctx
    import tenant.shared.panel_clients.marzban_panel as M

    monkeypatch.setattr(M, "_MIN_LOGIN_INTERVAL", 1.2)
    monkeypatch.setattr(M, "_MAX_LOGIN_WAIT", 5.0)
    M._LAST_LOGIN_TS.clear()
    M._TOKEN_CACHE.clear()

    async def token(_):
        return web.json_response({"access_token": "no-exp-token"})  # JWT نیست ⇒ کش نمی‌شود

    async def go():
        app = web.Application()
        app.router.add_post("/api/admin/token", token)
        r = web.AppRunner(app)
        await r.setup()
        await web.TCPSite(r, "127.0.0.1", 8893).start()
        acc = perfctx.begin()
        c1 = M.MarzbanPanelClient("http://127.0.0.1:8893", "u", "p")
        await c1.login()
        c2 = M.MarzbanPanelClient("http://127.0.0.1:8893", "u", "p")
        await c2.login()      # باید ≈۱٫۲ث بخوابد و در wait ثبت شود
        await c1.close()
        await c2.close()
        await r.cleanup()
        return acc

    acc = asyncio.run(go())
    assert acc["wait"][0] >= 1 and acc["wait"][1] >= 0.8
    assert "login-throttle" in perfctx.fmt(acc)


def test_slow_body_is_counted_as_http():
    import perfctx
    import aiohttp

    async def slow_body(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/plain"})
        resp.content_length = 6
        await resp.prepare(request)
        await resp.write(b"abc")
        await asyncio.sleep(0.6)
        await resp.write(b"def")
        await resp.write_eof()
        return resp

    async def go():
        app = web.Application()
        app.router.add_get("/b", slow_body)
        r = web.AppRunner(app)
        await r.setup()
        await web.TCPSite(r, "127.0.0.1", 8891).start()
        acc = perfctx.begin()
        async with aiohttp.ClientSession() as cs:
            async with cs.get("http://127.0.0.1:8891/b") as resp:
                await resp.text()
        await r.cleanup()
        return acc

    acc = asyncio.run(go())
    assert acc["http"][0] == 1            # یک تماس
    assert acc["http"][1] >= 0.55         # ولی زمانِ بدنه هم حساب شد
    assert "body" in acc["http"][3]
