"""PATCH_PSEL_RESUBS_V1 (ABOL) — RickPanel «API زیرنماینده»: ساختِ پنل برایِ مشتری بدونِ admins.create."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.panel_seller import adapter as AD
from tenant.bots.panel_seller import gate as GT
from tenant.bots.panel_seller import resubs as RS
from tenant.bots.panel_seller import service as SV
from tenant.shared.panel_clients.rickpanel_panel import RickPanelClient
from tenant.shared.panel_clients.vpn_panel import PanelError

OWNER = "abol"


class FakeRick(RickPanelClient):
    """RickPanelClient بدونِ شبکه: _request را با یک «پنلِ ساختگی» جواب می‌دهد."""

    def __init__(self, enabled=True, rows=None, legacy_403=True, detail=None, can_allocate=True, remaining=800 * 1024 ** 3):
        self.base_url, self.username = "https://p.test", OWNER
        self.enabled, self.rows, self.legacy_403, self.detail = enabled, list(rows or []), legacy_403, detail or {}
        self.can_allocate, self.remaining = can_allocate, remaining
        self.calls: list = []
        self.next_id = 100

    async def close(self):
        pass

    async def _request(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        if path == "/accounting/api/resubs/enabled":
            return {"enabled": True, "prefix": OWNER} if self.enabled else None
        if path == "/accounting/api/resubs" and method == "GET":
            # شکلِ واقعیِ پنلِ زنده (ابول): rep = رکوردِ خودِ مالک (id/username دارد ولی زیرنماینده نیست)
            return {"rep": {"id": 1, "username": OWNER, "status": "active", "true_limit": 10 ** 12, "remaining": self.remaining,
                            "min_remaining": 500 * 1024 ** 3, "can_allocate": self.can_allocate},
                    "rules": {"min_sub": 1, "max_subs": 50, "prefix": OWNER, "suffix_pattern": "^[a-z0-9]{2,16}$"},
                    "totals": {"subs": len(self.rows)}, "subs": self.rows,
                    "history": [{"at": 1, "sub_username": f"{OWNER}_x", "action": "create", "delta_bytes": 0, "ok": 1}]}
        if path == "/accounting/api/resubs" and method == "POST":
            b = kw["json"]
            self.next_id += 1
            row = {"id": self.next_id, "username": f"{OWNER}_{b['suffix']}", "status": "active", "data_limit": int(b["gb"] * 1024 ** 3),
                   "used_traffic": 0, "users_total": 0, "managed": True, "cap": int(b["gb"] * 1024 ** 3), "stopped": False}
            self.rows.append(row)
            return {"ok": True, "sub": row}
        if path.endswith("/detail"):
            return self.detail
        if path.startswith("/accounting/api/resubs/"):
            return {"ok": True}
        if path == "/api/admin" and method == "POST":
            if self.legacy_403:
                raise PanelError('POST /api/admin HTTP 403: {"detail":"Permission denied: admins.create"}')
            return {"username": kw["json"]["username"]}
        return {}


def _api(c):
    return AD.PanelAdminAPI(c)


@pytest.fixture(autouse=True)
def _clear():
    RS.clear_cache()
    yield
    RS.clear_cache()


@pytest.mark.asyncio
async def test_create_uses_resubs_and_returns_real_prefixed_username():
    c = FakeRick()
    name = await _api(c).create_resub("ps12ab34cd", "Ab12!xyz-Qw9", 100)
    assert name == f"{OWNER}_ps12ab34cd"
    post = [x for x in c.calls if x[0] == "POST" and x[1] == "/accounting/api/resubs"][0]
    assert post[2] == {"suffix": "ps12ab34cd", "password": "Ab12!xyz-Qw9", "gb": 100.0}
    assert not any(p == "/api/admin" for _m, p, _j in c.calls)        # مسیرِ ممنوع (admins.create) اصلاً صدا زده نشد


@pytest.mark.asyncio
async def test_unlimited_and_tiny_caps_are_clamped_to_api_limits():
    c = FakeRick()
    await _api(c).create_resub("psaaaaaaaa", "Ab12!xyz-Qw9", 0)
    post = [x for x in c.calls if x[0] == "POST" and x[1] == "/accounting/api/resubs"][0]
    assert post[2]["gb"] == 100000.0                                  # «نامحدود» (۰) ⇒ سقفِ مجازِ API
    await _api(c).apply_data_limit(f"{OWNER}_psaaaaaaaa", 0)
    assert ("/accounting/api/resubs/101/limit", {"gb": 100000.0}) in [(p, j) for _m, p, j in c.calls]
    assert RS.make_suffix("Ab-C_d!!") == "abcd" and RS.make_suffix("x") == "xps" and len(RS.make_suffix("a" * 40)) == 16


@pytest.mark.asyncio
async def test_not_enabled_returns_none_and_legacy_path_is_used_with_clear_403_hint():
    c = FakeRick(enabled=False)
    assert await _api(c).create_resub("psaaaaaaaa", "Ab12!xyz-Qw9", 10) is None
    with pytest.raises(PanelError) as ei:
        await SV.create_admin_on(_api(c), None, "psaaaaaaaa", "Ab12!xyz-Qw9", 10)
    assert "admins.create" in str(ei.value) and "زیرنماینده" in str(ei.value)      # پیامِ قابلِ اقدام، نه خطای خام


@pytest.mark.asyncio
async def test_pasarguard_family_uses_resubs_too_but_only_when_panel_says_enabled():
    """ابول: پنلش پاسارگاد بود نه RickPanel؛ تشخیص باید برایِ کلِ خانوادهٔ مرزبان کار کند."""
    from tenant.shared.panel_clients.pasargad_panel import PasarGuardPanelClient

    class FakePG(PasarGuardPanelClient):
        def __init__(self, enabled):
            # کشِ «فعال‌بودن» به‌ازایِ (آدرسِ پنل، نامِ کاربری) است؛ دو پنلِ جدا ⇒ دو آدرس
            self.base_url, self.username, self.en, self.calls = f"https://pg-{enabled}.test", OWNER, enabled, []

        async def close(self):
            pass

        async def _request(self, method, path, **kw):
            self.calls.append((method, path))
            if path == "/accounting/api/resubs/enabled":
                return {"prefix": OWNER} if self.en else None
            if path == "/accounting/api/resubs" and method == "POST":
                return {"sub": {"id": 5, "username": f"{OWNER}_{kw['json']['suffix']}"}}
            return {}
    on, off = FakePG(True), FakePG(False)
    name = await AD.PanelAdminAPI(on, "pasargad").create_resub("psabcdefgh", "Ab12!xyz-Qw9", 10)
    assert name == f"{OWNER}_psabcdefgh"
    assert await AD.PanelAdminAPI(off, "pasargad").create_resub("psabcdefgh", "Ab12!xyz-Qw9", 10) is None
    assert ("POST", "/accounting/api/resubs") not in off.calls            # فعال نبود ⇒ مسیرِ قدیمی، هیچ نوشتنی روی resubs


@pytest.mark.asyncio
async def test_html_or_empty_answers_never_count_as_enabled_and_unsupported_panels_are_skipped():
    class Spa:
        calls = []

        async def _request(self, method, path, **kw):
            Spa.calls.append(path)
            return {"raw": "<html>dashboard</html>"}       # SPA برایِ هر مسیرِ ناشناخته ۲۰۰ HTML می‌دهد
    assert await AD.PanelAdminAPI(Spa(), "marzban")._rs(None) is None
    RS.clear_cache()

    class Empty:
        async def _request(self, *a, **k):
            return {}
    assert await AD.PanelAdminAPI(Empty(), "marzban")._rs(None) is None
    RS.clear_cache()
    Spa.calls.clear()
    assert await AD.PanelAdminAPI(Spa(), "sanai")._rs(None) is None and not Spa.calls   # پنلِ غیرِ خانوادهٔ مرزبان: بدونِ هیچ درخواست


@pytest.mark.asyncio
async def test_operations_on_existing_sub_use_ids_and_status_values():
    c = FakeRick(rows=[{"id": 7, "username": f"{OWNER}_ps1", "cap": 1, "used_traffic": 5, "managed": True}])
    a = _api(c)
    await a.set_password(f"{OWNER}_ps1", "Zz99!hello-Q1")
    await a.disable_users(f"{OWNER}_ps1")
    await a.activate_users(f"{OWNER}_ps1")
    assert await a.set_admin_disabled(f"{OWNER}_ps1", True) is True
    await a.apply_data_limit(f"{OWNER}_ps1", 50)
    await a.reset_usage(f"{OWNER}_ps1")
    await a.delete_admin(f"{OWNER}_ps1")                               # حذف در API نیست ⇒ غیرفعال
    posts = [(p, j) for m, p, j in c.calls if m == "POST" and p.startswith("/accounting/api/resubs/7/")]
    assert ("/accounting/api/resubs/7/password", {"password": "Zz99!hello-Q1"}) in posts
    assert posts.count(("/accounting/api/resubs/7/status", {"status": "disabled"})) == 3
    assert ("/accounting/api/resubs/7/status", {"status": "active"}) in posts
    assert ("/accounting/api/resubs/7/limit", {"gb": 50.0}) in posts
    assert ("/accounting/api/resubs/7/reset", {}) in posts
    assert not any(m == "DELETE" for m, _p, _j in c.calls)


@pytest.mark.asyncio
async def test_unknown_username_falls_back_to_legacy_admin_ops():
    """ادمینِ قدیمی (قبل از فعال‌شدنِ زیرنماینده) در فهرستِ زیرنماینده‌ها نیست ⇒ مسیرِ قبلی، نه خطا."""
    c = FakeRick(rows=[{"id": 7, "username": f"{OWNER}_ps1", "managed": True}])
    a = _api(c)
    assert await a._rs("psOLDADMIN") is None                          # در فهرست نیست ⇒ مسیرِ قدیمی
    assert await a._rs(f"{OWNER}_ps1") is not None                   # زیرنماینده ⇒ resubs


@pytest.mark.asyncio
async def test_usage_and_user_count_read_defensively_and_never_guess():
    c = FakeRick(rows=[{"id": 7, "username": f"{OWNER}_ps1", "cap": 10, "used_traffic": 1234, "users_total": 3, "managed": True}])
    a = _api(c)
    assert await a.usage_bytes(f"{OWNER}_ps1") == 1234 and await a.user_count(f"{OWNER}_ps1") == 3
    snap = await a.snapshot(f"{OWNER}_ps1")
    assert snap == {"used_bytes": 1234, "users": 3, "new_style": False}
    c2 = FakeRick(rows=[{"id": 7, "username": f"{OWNER}_ps1", "managed": True}], detail={"weird": 1})
    with pytest.raises(PanelError) as ei:
        await _api(c2).usage_bytes(f"{OWNER}_ps1")
    assert "weird" in str(ei.value)                                    # کلیدهایِ پاسخ در خطا می‌آید تا قابلِ اصلاح باشد


def test_find_rows_handles_nested_shapes_and_error_mapping():
    obj = {"rep": {"x": 1}, "resubs": [{"sub_id": 3, "name": "a_b", "usage": 2}], "totals": {}}
    rows = RS.find_rows(obj)
    assert len(rows) == 1 and rows[0]["name"] == "a_b"
    assert "۵۰۰ گیگ" in str(RS.map_error(PanelError("POST x HTTP 409: min_remaining"), "ساخت"))
    assert "تکراری" in str(RS.map_error(PanelError("POST x HTTP 409: username exists"), "ساخت"))
    assert "۱۰ ساخت" in str(RS.map_error(PanelError("POST x HTTP 429: too many"), "ساخت"))
    assert "رمز" in str(RS.map_error(PanelError("POST x HTTP 422: bad"), "ساخت"))


# ───────── سرتاسری: خریدِ پنل باید نامِ واقعیِ زیرنماینده را ذخیره کند ─────────
@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    import tenant as _tenant
    for mod in (config, GT, SV, _tenant):
        monkeypatch.setattr(mod, "Session", maker)
    getattr(_tenant, "_SETTING_CACHE", {}).clear()
    from tenant.bots.panel_seller import gate as _g
    monkeypatch.setattr(_g, "OPEN", True)
    async with maker() as s:
        s.add(models.Bot(id=7, owner_id=500, token="7:T", username="pbot", field="panel_seller", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=1001, full_name="Ali", username="ali", balance=1_000_000))
        s.add(models.VpnPanel(id=3, bot_id=7, panel_key="rickpanel", base_url="https://p.test",
                              username=OWNER, password="x", enabled=True))
        s.add(models.PanelSellerPackage(id=1, bot_id=7, panel_id=3, title="۱۰۰ گیگ", data_gb=100, max_users=10, days=30,
                                        price_toman=300_000, active=True))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_buy_on_rickpanel_stores_real_prefixed_admin_username(env):
    c = FakeRick()
    r = await SV.buy(7, 1001, 1, api_factory=lambda panel: AD.PanelAdminAPI(c))
    assert r["status"] == "ok" and r["username"].startswith(f"{OWNER}_ps")        # نامِ واقعیِ پنل، نه فقط «ps…»
    async with env() as s:
        a = await s.scalar(select(models.PanelSellerAccount))
    assert a.admin_username == r["username"] and a.data_gb == 100
    assert not any(p == "/api/admin" for _m, p, _j in c.calls)


@pytest.mark.asyncio
async def test_buy_failure_on_rule_refund_and_message(env):
    class Refuse(FakeRick):
        async def _request(self, method, path, **kw):
            if path == "/accounting/api/resubs" and method == "POST":
                raise PanelError("POST /accounting/api/resubs HTTP 409: {'detail':'min_remaining'}")
            return await super()._request(method, path, **kw)
    r = await SV.buy(7, 1001, 1, api_factory=lambda panel: AD.PanelAdminAPI(Refuse()))
    assert r["status"] == "panel_failed" and "۵۰۰ گیگ" in r["error"]
    async with env() as s:
        u = await s.scalar(select(models.BotUser))
    assert int(u.balance) == 1_000_000                                    # پول برگشت


# ───────── شکلِ واقعیِ پنلِ زنده (خروجیِ ابول) ─────────
@pytest.mark.asyncio
async def test_rep_record_is_not_a_sub_and_readonly_subs_fall_back_to_legacy():
    c = FakeRick(rows=[{"id": 7, "username": f"{OWNER}_mine", "managed": True, "used_traffic": 9, "users_total": 2},
                       {"id": 8, "username": f"{OWNER}_botmade", "managed": False, "used_traffic": 1, "users_total": 1}])
    a = _api(c)
    assert await a._rs(OWNER) is None                                   # «rep» (خودِ مالک) زیرنماینده نیست
    assert await a._rs(f"{OWNER}_mine") is not None
    assert await a._rs(f"{OWNER}_botmade") is None                      # managed=false ⇒ فقط‌خواندنی ⇒ مسیرِ قدیمی
    rows = await RS.ResubAPI(c).list_rows()
    assert [r["username"] for r in rows] == [f"{OWNER}_mine", f"{OWNER}_botmade"]


@pytest.mark.asyncio
async def test_enabled_false_in_a_200_answer_means_not_enabled():
    class C(FakeRick):
        async def _request(self, method, path, **kw):
            if path.endswith("/enabled"):
                return {"enabled": False, "prefix": OWNER}
            return await super()._request(method, path, **kw)
    assert await _api(C())._rs(None) is None


@pytest.mark.asyncio
async def test_usage_and_users_come_from_the_list_row_in_one_request():
    c = FakeRick(rows=[{"id": 7, "username": f"{OWNER}_ps1", "managed": True, "used_traffic": 5_000, "users_total": 4, "users_active": 1}])
    snap = await _api(c).snapshot(f"{OWNER}_ps1")
    assert snap == {"used_bytes": 5_000, "users": 4, "new_style": False}    # users_total (نه users_active)
    assert not any(p.endswith("/detail") for _m, p, _j in c.calls)         # لازم نبود به «detail» برود


@pytest.mark.asyncio
async def test_create_refuses_clearly_when_main_quota_cannot_allocate():
    c = FakeRick(can_allocate=False, remaining=100 * 1024 ** 3)
    with pytest.raises(PanelError) as ei:
        await _api(c).create_resub("psabcdefgh", "Ab12!xyz-Qw9", 10)
    assert "100 گیگ" in str(ei.value) and "500 گیگ" in str(ei.value)
    assert not any(m == "POST" and p == "/accounting/api/resubs" for m, p, _j in c.calls)    # POSTِ بیهوده زده نشد
