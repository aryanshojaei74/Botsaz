"""پنل SolidLayer (PATCH_SOLIDLAYER_PANEL_V1) — با یک سرورِ aiohttpِ محلی که قراردادِ openapi.json را شبیه‌سازی می‌کند.
واحدِ limit_expire در مستندات نیست؛ سرورِ تستی هر سه حالت (ثانیه/میلی‌ثانیه/روز) را شبیه‌سازی می‌کند."""
import datetime as _dt
import time

import pytest
import pytest_asyncio
from aiohttp import web

from tenant.shared.panel_clients import solidlayer_panel as SL
from tenant.shared.panel_clients.vpn_panel import PanelError
from tenant.shared.panel_clients.vpn_panel_factory import client_class_for, make_panel_client_from_fields

GB = 1024 ** 3


def _iso(ms: int) -> str:
    ms = min(ms, 253_402_000_000_000)                      # سالِ ۹۹۹۹ (واحدِ غلط ⇒ تاریخِ بسیار دور، نه خطا)
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakePanel:
    def __init__(self, unit="s", report_expired_at=True, strict=False):
        self.unit, self.report, self.strict = unit, report_expired_at, strict
        self.subs: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.logins = 0
        self.reject_next = False
        self.counter = 0

    def _expired_ms(self, v):
        if not v:
            return 0
        if self.unit == "s":
            return int(v) * 1000
        if self.unit == "ms":
            return int(v)
        return int(time.time() * 1000) + int(v) * 86_400_000          # days

    def _bad(self, v) -> bool:
        """strict: پنل مقدارِ خارج از بازهٔ واحدِ خودش را با ۴۰۰ رد می‌کند."""
        if not self.strict or not v:
            return False
        return not {"s": 1_000_000_000 <= v <= 5_000_000_000, "ms": v >= 100_000_000_000, "days": 0 < v <= 4000}[self.unit]

    def _view(self, u):
        s = dict(u)
        ex = self._expired_ms(u.get("limit_expire"))
        s["expired_at"] = _iso(ex) if (ex and self.report) else ""
        s["subscription_link"] = f"/sub/{u['access_key']}"
        s["enabled"] = u.get("enabled", True)
        return s

    def app(self):
        a = web.Application(middlewares=[self.mw])
        a.router.add_post("/api/admins/token", self.login)
        a.router.add_get("/api/admins/current", self.current)
        a.router.add_get("/api/services", self.services)
        a.router.add_get("/api/subscriptions", self.list_subs)
        a.router.add_post("/api/subscriptions", self.create)
        a.router.add_put("/api/subscriptions", self.update)
        a.router.add_delete("/api/subscriptions", self.delete)
        a.router.add_post("/api/subscriptions/enable", self.enable)
        a.router.add_post("/api/subscriptions/disable", self.disable)
        a.router.add_post("/api/subscriptions/reset", self.reset)
        a.router.add_post("/api/subscriptions/revoke", self.revoke)
        a.router.add_get("/api/subscriptions/{u}/links", self.links)
        return a

    @web.middleware
    async def mw(self, request, handler):
        self.calls.append((request.method, request.path))
        if request.path != "/api/admins/token":
            if self.reject_next:
                self.reject_next = False
                return web.json_response({"message": "expired"}, status=401)
            if request.headers.get("X-API-Key") != "KEY123" and request.headers.get("Authorization") != "Bearer JWT1":
                return web.json_response({"message": "unauthorized"}, status=401)
        return await handler(request)

    async def login(self, r):
        self.logins += 1
        b = await r.json()
        if b.get("password") != "secret":
            return web.json_response({"message": "bad"}, status=401)
        return web.json_response({"token": "JWT1", "type": "Bearer", "admin": {"username": b["username"], "api_key": "KEY123"}})

    async def current(self, r):
        return web.json_response({"username": "mehdi", "id": 1})

    async def services(self, r):
        return web.json_response({"items": [{"id": 3, "name": "A"}, {"id": 7, "name": "B"}], "total": 2})

    async def list_subs(self, r):
        q = r.query
        items = list(self.subs.values())
        if q.get("usernames"):
            items = [u for u in items if u["username"] in q["usernames"].split(",")]
        page, limit = int(q.get("page", 1)), int(q.get("limit", 10))
        part = items[(page - 1) * limit: page * limit]
        return web.json_response({"items": [self._view(u) for u in part], "total": len(items), "page": page, "limit": limit})

    async def create(self, r):
        body = await r.json()
        out = []
        for b in (body if isinstance(body, list) else [body]):
            if b["username"] in self.subs:
                return web.json_response({"message": "exists"}, status=400)
            if self._bad(b.get("limit_expire", 0)):
                return web.json_response({"message": "invalid limit_expire"}, status=400)
            self.counter += 1
            u = {"id": self.counter, "username": b["username"], "limit_usage": b.get("limit_usage", 0),
                 "limit_expire": b.get("limit_expire", 0), "services": b.get("services", []), "total_usage": 0,
                 "reset_usage": 0, "access_key": f"tok{self.counter}", "created_at": _iso(int(time.time() * 1000)),
                 "last_online_at": "", "enabled": True}
            self.subs[u["username"]] = u
            out.append(self._view(u))
        return web.json_response(out, status=201)

    async def update(self, r):
        b = await r.json()
        out = []
        for name in b["usernames"]:
            u = self.subs.get(name)
            if not u:
                return web.json_response({"message": "nf"}, status=404)
            if self._bad(b.get("limit_expire", 0)):
                return web.json_response({"message": "invalid limit_expire"}, status=400)
            for k in ("limit_usage", "limit_expire", "enabled"):
                if k in b:
                    u[k] = b[k]
            out.append(self._view(u))
        return web.json_response(out)

    async def delete(self, r):
        for n in (await r.json())["usernames"]:
            self.subs.pop(n, None)
        return web.json_response({"message": "ok"})

    async def _toggle(self, r, val):
        out = []
        for n in (await r.json())["usernames"]:
            if n in self.subs:
                self.subs[n]["enabled"] = val
                out.append(self._view(self.subs[n]))
        return web.json_response(out)

    async def enable(self, r):
        return await self._toggle(r, True)

    async def disable(self, r):
        return await self._toggle(r, False)

    async def reset(self, r):
        out = []
        for n in (await r.json())["usernames"]:
            u = self.subs[n]
            u["reset_usage"] = u["total_usage"]
            out.append(self._view(u))
        return web.json_response(out)

    async def revoke(self, r):
        out = []
        for n in (await r.json())["usernames"]:
            u = self.subs[n]
            u["access_key"] = u["access_key"] + "N"
            out.append(self._view(u))
        return web.json_response(out)

    async def links(self, r):
        return web.json_response({"username": r.match_info["u"], "links": [{"label": "a", "link": "vless://x", "link_name": "a"}]})


@pytest_asyncio.fixture
async def srv():
    SL._EXPIRE_FMT.clear()
    state = {}

    async def make(**kw):
        fp = FakePanel(**kw)
        runner = web.AppRunner(fp.app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        state.setdefault("runners", []).append(runner)
        return fp, f"http://127.0.0.1:{port}"
    yield make
    for r in state.get("runners", []):
        await r.cleanup()
    SL._EXPIRE_FMT.clear()


def _cl(url, **kw):
    return SL.SolidLayerPanelClient(url, kw.pop("username", "mehdi"), kw.pop("password", "secret"), verify_ssl=False, **kw)


def test_registered_in_factory():
    assert client_class_for("solidlayer") is SL.SolidLayerPanelClient and client_class_for("solidlayer#2") is SL.SolidLayerPanelClient
    c = make_panel_client_from_fields("solidlayer", "https://x.example/docs/", "u", "p", api_key="K")
    assert c.base_url == "https://x.example" and c.api_key == "K"           # /docs از آدرس حذف می‌شود


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("unit", ["s", "ms", "days"])
async def test_create_calibrates_expiry_unit_and_verifies_date(srv, unit, strict):
    fp, url = await srv(unit=unit, strict=strict)
    c = _cl(url)
    res = await c.create_account("ali_1", 5, 30)
    u = fp.subs["ali_1"]
    assert u["limit_usage"] == 5 * GB and u["services"] == [3, 7]                       # حجم بایت؛ همهٔ سرویس‌هایِ ادمین
    got = SL.parse_ms(fp._view(u)["expired_at"])
    assert abs(got - (time.time() * 1000 + 30 * 86_400_000)) < SL._TOLERANCE_MS         # تاریخِ واقعی ≈ ۳۰ روز بعد، با هر واحد
    assert SL._EXPIRE_FMT[url] == unit
    assert res["sub_link"] == f"{url}/sub/tok1" and res["sub_id"] == "tok1" and res["expiry_ms"] == got
    n = len(fp.calls)
    await c.create_account("ali_2", 0, 10)                                              # دومی دیگر آزمون‌وخطا ندارد
    assert fp.subs["ali_2"]["limit_usage"] == 0 and len([x for x in fp.calls[n:] if x[0] == "PUT"]) == 0
    await c.close()


@pytest.mark.asyncio
async def test_unknown_unit_rolls_back_account_with_clear_error(srv):
    fp, url = await srv(unit="s")
    c = _cl(url)
    real = c._update

    async def bad_update(username, **f):           # پنلی که هر فرمتی را اشتباه می‌خواند
        r = await real(username, **f)
        r["expired_at"] = _iso(int(time.time() * 1000) + 5 * 365 * 86_400_000)
        return r
    orig_post = c._request

    async def post_bad(method, path, **kw):
        res = await orig_post(method, path, **kw)
        if method == "POST" and path == "/api/subscriptions":
            res[0]["expired_at"] = _iso(int(time.time() * 1000) + 5 * 365 * 86_400_000)
        return res
    c._update = bad_update
    c._request = post_bad
    with pytest.raises(PanelError) as e:
        await c.create_account("bad_1", 1, 30)
    assert "limit_expire" in str(e.value) and "bad_1" not in fp.subs                       # اکانتِ نادرست نماند


@pytest.mark.asyncio
async def test_unverifiable_expiry_is_accepted_only_if_echoed(srv):
    fp, url = await srv(unit="s", report_expired_at=False)
    c = _cl(url)
    res = await c.create_account("echo_1", 2, 7)
    assert res["email"] == "echo_1" and fp.subs["echo_1"]["limit_expire"] > time.time()   # echo یکی بود ⇒ پذیرفته شد
    await c.close()


@pytest.mark.asyncio
async def test_usage_renew_traffic_toggle_reset_delete_flow(srv):
    fp, url = await srv(unit="s")
    c = _cl(url)
    await c.create_account("sara", 10, 30)
    fp.subs["sara"].update(total_usage=4 * GB, reset_usage=1 * GB, last_online_at=_iso(int(time.time() * 1000) - 86_400_000))
    u = await c.get_usage("sara")
    assert u["down"] == 3 * GB and u["total"] == 10 * GB and u["enable"] is True and u["last_online_ms"] > 0
    assert abs(u["expiryTime"] - (time.time() * 1000 + 30 * 86_400_000)) < SL._TOLERANCE_MS
    old = u["expiryTime"]
    await c.renew("sara", 30)                                                              # از انقضایِ فعلی ادامه می‌دهد
    assert abs((await c.get_usage("sara"))["expiryTime"] - (old + 30 * 86_400_000)) < SL._TOLERANCE_MS
    await c.add_traffic("sara", 5)
    assert fp.subs["sara"]["limit_usage"] == 15 * GB
    await c.set_enabled("sara", False)
    assert (await c.get_usage("sara"))["enable"] is False
    await c.renew("sara", 1, reset_usage=True)                                             # تمدید دوباره فعالش می‌کند + ریست
    assert fp.subs["sara"]["enabled"] is True and fp.subs["sara"]["reset_usage"] == fp.subs["sara"]["total_usage"]
    link = await c.reset_sub("sara")
    assert link.endswith("tok1N") and (await c.get_client("sara"))["subId"] == "tok1N"
    assert await c.get_client_links("sara") == ["vless://x"]
    assert await c.delete_account("sara") is True and await c.get_usage("sara") is None
    # نامحدود می‌ماند
    await c.create_account("unl", 0, 0)
    await c.add_traffic("unl", 5)
    await c.renew("unl", 30)
    assert fp.subs["unl"]["limit_usage"] == 0 and fp.subs["unl"]["limit_expire"] == 0
    with pytest.raises(PanelError):
        await c.rename_account("unl", "other")
    await c.close()


@pytest.mark.asyncio
async def test_list_clients_paginates_and_maps_for_idle_report(srv):
    fp, url = await srv(unit="s")
    c = _cl(url)
    for i in range(1, 8):
        await c.create_account(f"u{i}", 1, 5)
    fp.subs["u3"]["last_online_at"] = _iso(int(time.time() * 1000) - 5 * 86_400_000)
    fp.subs["u4"]["enabled"] = False
    orig = SL._PAGE
    SL._PAGE = 3                                                                              # سه صفحه
    try:
        rows = await c.list_clients()
    finally:
        SL._PAGE = orig
    assert [r["email"] for r in rows] == [f"u{i}" for i in range(1, 8)]
    by = {r["email"]: r for r in rows}
    assert by["u3"]["last_online_ms"] > 0 and by["u1"]["last_online_ms"] == 0 and by["u4"]["status"] == "disabled"
    assert by["u1"]["sub_id"] == "tok1" and by["u1"]["traffic"]["total"] == GB
    from tenant.shared.panel_clients import inactive_users as IU
    assert IU.has_last_online_field(rows)
    res = IU.classify(rows, int(time.time() * 1000), 2)
    assert [r["name"] for r in res["rows"]][-1] == "u3" and "u4" not in [r["name"] for r in res["rows"]]
    await c.close()


@pytest.mark.asyncio
async def test_auth_login_relogin_api_key_and_errors(srv):
    fp, url = await srv()
    c = _cl(url)
    info = await c.test_connection()
    assert info["ok"] and info["admin"] == "mehdi" and info["inbounds"] == [3, 7] and fp.logins == 1
    fp.reject_next = True                                                                     # توکن منقضی ⇒ یک‌بار دوباره لاگین
    assert (await c.get_usage("nobody")) is None and fp.logins == 2
    bad = _cl(url, password="wrong")
    with pytest.raises(PanelError) as e:
        await bad.login()
    assert "اشتباه" in str(e.value)
    ak = SL.SolidLayerPanelClient(url, "", "", api_key="KEY123")                              # فقط API Key
    assert (await ak.test_connection())["ok"] and fp.logins == 3                               # (۳ = ورودِ ناموفقِ bad هم شمرده شد)
    wrong = SL.SolidLayerPanelClient(url, "", "", api_key="nope")
    with pytest.raises(PanelError):
        await wrong.login()
    unreachable = _cl("http://127.0.0.1:1")
    with pytest.raises(PanelError):
        await unreachable.login()
    for x in (c, bad, ak, wrong, unreachable):
        await x.close()
