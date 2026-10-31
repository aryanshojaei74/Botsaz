"""PATCH_PSEL_STRONG_PASSWORD_V1 / PATCH_PSEL_PASARGUARD_ROLE_V1 — خطای واقعیِ Whale: پاسارگاردِ جدید ۴۲۲ می‌داد
(رمزِ ضعیف + role_id اجباری)."""
import re

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.panel_seller import adapter as AD
from tenant.bots.panel_seller import service as SV
from tenant.shared.panel_clients.vpn_panel import PanelError


def test_generated_password_satisfies_pasarguard_rules():
    for _ in range(200):
        pw = SV.gen_password()
        assert len(pw) >= 12 and len(pw.encode()) <= 72
        assert len(re.findall(r"\d", pw)) >= 2 and len(re.findall(r"[A-Z]", pw)) >= 2 and len(re.findall(r"[a-z]", pw)) >= 2
        assert re.search(r"[!@#$%^&*()\-_=+\[\]{}|;:,.<>?/~`]", pw) and '"' not in pw


class NewPG:
    """شبیه‌سازِ پاسارگاردِ جدید: ایجادِ ادمین بدونِ role_id ⇒ ۴۲۲؛ رمزِ ضعیف ⇒ ۴۲۲."""
    roles = [{"id": 1, "name": "owner", "is_owner": True}, {"id": 4, "name": "seller", "is_owner": False}]
    created: list = []

    async def _request(self, method, path, json=None, params=None):
        if path == "/api/admin-roles/simple":
            return {"roles": NewPG.roles, "total": len(NewPG.roles)}
        if method == "POST" and path == "/api/admin":
            errs = {}
            pw = json.get("password", "")
            if len(re.findall(r"\d", pw)) < 2 or not re.search(r"[!@#$%^&*\-_=+]", pw):
                errs["password"] = "Value error, Password must contain at least 2 digits"
            if "role_id" not in json:
                errs["role_id"] = "Field required"
            if errs:
                raise PanelError(f"POST /api/admin HTTP 422: {{\"detail\": {errs}}}")
            NewPG.created.append(dict(json))
            return {"username": json["username"]}
        if method == "PUT":
            if "is_sudo" in (json or {}):
                raise PanelError("PUT HTTP 422: extra field is_sudo")
            NewPG.created.append(("pw", path, dict(json)))
            return {}
        return {}

    async def modify_admin_password(self, username, pw):
        return await self._request("PUT", f"/api/admin/{username}", json={"password": pw, "is_sudo": False})

    async def close(self): pass


@pytest.mark.asyncio
async def test_create_retries_with_role_and_strong_password():
    NewPG.created = []
    api = AD.PanelAdminAPI(NewPG())
    panel = type("P", (), {"id": 3, "bot_id": 7})()

    async def _gs(bid, k, d=""): return d
    import tenant as T
    old = T.get_setting
    T.get_setting = _gs
    try:
        await SV.create_admin_on(api, panel, "psabc", SV.gen_password())
    finally:
        T.get_setting = old
    assert len(NewPG.created) == 1 and NewPG.created[0]["role_id"] == 4                  # تنها نقشِ غیرِمالک


@pytest.mark.asyncio
async def test_multiple_roles_are_not_guessed_but_owner_choice_is_used(monkeypatch):
    NewPG.created = []
    NewPG.roles = [{"id": 1, "name": "owner", "is_owner": True}, {"id": 4, "name": "seller", "is_owner": False},
                   {"id": 5, "name": "full-admin", "is_owner": False}]
    api = AD.PanelAdminAPI(NewPG())
    panel = type("P", (), {"id": 3, "bot_id": 7})()
    store = {}

    async def _gs(bid, k, d=""): return store.get(k, d)
    import tenant as T
    monkeypatch.setattr(T, "get_setting", _gs)
    with pytest.raises(PanelError) as ei:
        await SV.create_admin_on(api, panel, "psabc", SV.gen_password())
    assert "seller#4" in str(ei.value) and "full-admin#5" in str(ei.value) and not NewPG.created      # حدس نزد؛ علت برایِ مالک
    store["psel_role_3"] = "4"                                                                           # مالک انتخاب کرد
    await SV.create_admin_on(api, panel, "psabc", SV.gen_password())
    assert NewPG.created[0]["role_id"] == 4
    store["psel_role_3"] = "99"                                                                          # نقشِ حذف‌شده ⇒ دوباره انتخاب لازم
    with pytest.raises(PanelError):
        await SV.create_admin_on(api, panel, "psabc2", SV.gen_password())
    NewPG.roles = [{"id": 1, "name": "owner", "is_owner": True}, {"id": 4, "name": "seller", "is_owner": False}]


@pytest.mark.asyncio
async def test_old_marzban_style_panel_unchanged_and_password_set_falls_back():
    class Old:
        calls = []
        async def _request(self, method, path, json=None, params=None):
            Old.calls.append((method, path, dict(json or {})))
            return {}
        async def close(self): pass
    api = AD.PanelAdminAPI(Old())
    await SV.create_admin_on(api, None, "u1", "Aa11!!bbCCdd")
    assert Old.calls[0][2].get("role_id") is None and Old.calls[0][2]["is_sudo"] is False
    # رمز: روی پنلِ جدید با is_sudo رد می‌شود ⇒ یک‌بار فقط با رمز
    NewPG.created = []
    api2 = AD.PanelAdminAPI(NewPG())
    await api2.set_password("psabc", "Aa11!!bbCCdd")
    assert NewPG.created[-1][2] == {"password": "Aa11!!bbCCdd"}


# ─────────── نامِ‌کاربریِ انتخابیِ مشتری + فروشِ حجمیِ دلخواه (Whale Support) ───────────
from tests.test_panel_seller import FakeAPI


@pytest_asyncio.fixture
async def env2(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, SV, tenant):
        monkeypatch.setattr(mod, "Session", m)
    tenant._SETTING_CACHE.clear()
    FakeAPI.log, FakeAPI.fail_create = [], False
    async with m() as s:
        s.add(models.Bot(id=7, owner_id=500, token="7:T", username="pbot", field="panel_seller", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=1001, full_name="Ali", balance=1_000_000))
        s.add(models.VpnPanel(id=3, bot_id=7, panel_key="marzban", base_url="https://p", username="s", password="x", enabled=True))
        s.add(models.PanelSellerPackage(id=1, bot_id=7, panel_id=3, title="۱۰۰ گیگ", data_gb=100, max_users=10, days=30,
                                        price_toman=300_000, active=True))
        await s.commit()
    yield m
    await engine.dispose()


def test_valid_username_rules():
    assert SV.valid_username("shop_ali") and SV.valid_username("Ali9") and SV.valid_username("a" * 32)
    for bad in ("", "ab", "9ali", "_ali", "ali ali", "ali-x", "علی", "a" * 33, "ali@x"):
        assert not SV.valid_username(bad), bad


@pytest.mark.asyncio
async def test_buy_with_chosen_username_and_duplicates_rejected_before_charge(env2):
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI, username="shop_ali")
    assert r["status"] == "ok" and r["username"] == "shop_ali"
    assert ("create", "shop_ali", r["password"]) in FakeAPI.log
    async with env2() as s:
        from sqlalchemy import select as _sel
        bal = (await s.scalar(_sel(models.BotUser).where(models.BotUser.telegram_id == 1001))).balance
    assert bal == 700_000
    again = await SV.buy(7, 1001, 1, api_factory=FakeAPI, username="SHOP_ALI")           # بدونِ حساسیت به حروف
    assert again["status"] == "username_taken"
    assert (await SV.buy(7, 1001, 1, api_factory=FakeAPI, username="9bad"))["status"] == "bad_username"
    async with env2() as s:
        bal2 = (await s.scalar(_sel(models.BotUser).where(models.BotUser.telegram_id == 1001))).balance
    assert bal2 == 700_000                                                              # رد شد ⇒ پولی کم نشد


@pytest.mark.asyncio
async def test_panel_side_duplicate_refunds_and_does_not_create_account(env2):
    class Dup(FakeAPI):
        async def create_admin(self, u, p): raise PanelError("POST /api/admin HTTP 409: Admin already exists")
    r = await SV.buy(7, 1001, 1, api_factory=Dup, username="taken1")
    assert r["status"] == "panel_failed" and "already exists" in r["error"]
    async with env2() as s:
        from sqlalchemy import select as _sel
        bal = (await s.scalar(_sel(models.BotUser).where(models.BotUser.telegram_id == 1001))).balance
        n = len((await s.scalars(_sel(models.PanelSellerAccount))).all())
    assert bal == 1_000_000 and n == 0


async def _custom(env, **kw):
    base = {"price_gb": 5000, "min_gb": 10, "max_gb": 500, "users": 0, "days": 0}
    base.update(kw)
    for k, v in base.items():
        await tenant.set_setting(7, f"psel_custom_{k}", str(v))
    tenant._SETTING_CACHE.clear()


@pytest.mark.asyncio
async def test_custom_volume_unlimited_time_min_and_charge(env2):
    assert (await SV.buy_custom(7, 1001, 20, api_factory=FakeAPI))["status"] == "disabled"      # پیش‌فرض خاموش
    await _custom(env2)
    low = await SV.buy_custom(7, 1001, 5, api_factory=FakeAPI)
    assert low["status"] == "bad_amount" and low["min_gb"] == 10                                  # زیرِ حداقل
    assert (await SV.buy_custom(7, 1001, 501, api_factory=FakeAPI))["status"] == "bad_amount"
    r = await SV.buy_custom(7, 1001, 20, api_factory=FakeAPI, username="vol_user")
    assert r["status"] == "ok" and r["username"] == "vol_user"
    async with env2() as s:
        from sqlalchemy import select as _sel
        a = await s.get(models.PanelSellerAccount, r["account_id"])
        bal = (await s.scalar(_sel(models.BotUser).where(models.BotUser.telegram_id == 1001))).balance
    assert a.data_gb == 20 and a.expires_at is None and a.package_id is None and a.price_toman == 100_000
    assert a.max_users == 0 and bal == 900_000 and "20" in a.title


@pytest.mark.asyncio
async def test_custom_volume_insufficient_balance_and_timed_option(env2):
    await _custom(env2, days=30, users=5)
    r = await SV.buy_custom(7, 1001, 500, api_factory=FakeAPI)                                   # ۲٬۵۰۰٬۰۰۰ > موجودی
    assert r["status"] == "insufficient" and r["need"] == 2_500_000
    ok = await SV.buy_custom(7, 1001, 10, api_factory=FakeAPI)
    async with env2() as s:
        a = await s.get(models.PanelSellerAccount, ok["account_id"])
    assert a.expires_at is not None and a.max_users == 5


# ─────────── مصرف/تعدادِ کاربر در پاسارگاردِ جدید (PATCH_PSEL_PASARGUARD_USAGE_V1) ───────────
class NewPGStats:
    admins = [{"username": "other", "used_traffic": 111, "total_users": 9},
              {"username": "PsAbc", "used_traffic": 5_000_000_000, "total_users": 7}]
    calls: list = []

    async def _request(self, method, path, json=None, params=None):
        NewPGStats.calls.append((method, path))
        if path == "/api/admins":
            return {"admins": NewPGStats.admins, "total": len(NewPGStats.admins), "active": 2, "disabled": 0, "limited": 0}
        raise PanelError(f"{method} {path} HTTP 404")

    async def close(self): pass


@pytest.mark.asyncio
async def test_new_pasarguard_reads_usage_and_user_count_from_admin_list():
    NewPGStats.calls = []
    api = AD.PanelAdminAPI(NewPGStats())
    assert await api.usage_bytes("psabc") == 5_000_000_000             # فقط خودِ ادمین (نه «other»)؛ بدونِ حساسیت به حروف
    assert await api.user_count("psabc") == 7                          # total_users؛ فیلترِ /api/users نامعتبر را صدا نمی‌زند
    assert all(p == "/api/admins" for _m, p in NewPGStats.calls)


@pytest.mark.asyncio
async def test_new_pasarguard_missing_admin_raises_instead_of_reporting_zero():
    api = AD.PanelAdminAPI(NewPGStats())
    with pytest.raises(PanelError):
        await api.usage_bytes("ghost")                                  # صفر نمی‌دهد (وگرنه محدودیتِ حجم هرگز اعمال نمی‌شد)
    with pytest.raises(PanelError):
        await api.user_count("ghost")


@pytest.mark.asyncio
async def test_old_marzban_style_keeps_old_endpoints():
    class OldM:
        calls = []

        async def _request(self, method, path, json=None, params=None):
            OldM.calls.append((method, path))
            if path == "/api/admins":
                return [{"username": "u1", "is_sudo": False}]                   # لیستِ ساده ⇒ سبکِ قدیمی
            if path.startswith("/api/admin/usage/"):
                return 2048
            if path == "/api/users":
                return {"users": [], "total": 4}
            raise PanelError("404")

        async def close(self): pass
    api = AD.PanelAdminAPI(OldM())
    assert await api.usage_bytes("u1") == 2048 and await api.user_count("u1") == 4

    class NoAdminsEndpoint(OldM):
        async def _request(self, method, path, json=None, params=None):
            if path == "/api/admins":
                raise PanelError("GET /api/admins HTTP 404")
            return await OldM._request(self, method, path, json=json, params=params)
    api2 = AD.PanelAdminAPI(NoAdminsEndpoint())
    assert await api2.usage_bytes("u1") == 2048                                  # /api/admins نبود ⇒ همان رفتارِ قبلی


# ─────────── سقفِ حجمِ بومی + آدرسِ پشتیبانی/عنوانِ پروفایل + صفحهٔ خریدِ بدونِ بسته (Whale) ───────────
class PGWrite(NewPG):
    """پاسارگاردِ جدید با ثبتِ همهٔ درخواست‌ها."""
    log: list = []

    async def _request(self, method, path, json=None, params=None):
        PGWrite.log.append((method, path, dict(json or {})))
        if path == "/api/admins":
            return {"admins": [{"username": "psabc", "used_traffic": 0, "total_users": 0}], "total": 1}
        return await NewPG._request(self, method, path, json=json, params=params)


@pytest.mark.asyncio
async def test_native_data_limit_is_sent_on_create_and_updates(monkeypatch):
    PGWrite.log = []
    NewPG.created = []
    api = AD.PanelAdminAPI(PGWrite())
    panel = type("P", (), {"id": 3, "bot_id": 7})()

    async def _gs(bid, k, d=""): return d
    import tenant as T
    monkeypatch.setattr(T, "get_setting", _gs)
    await SV.create_admin_on(api, panel, "psabc", SV.gen_password(), data_gb=100)
    assert NewPG.created[0]["data_limit"] == 100 * 1024 ** 3                       # «۱۰۰ گیگ» روی خودِ پنل (قبلاً نامحدود می‌ماند)
    PGWrite.log = []
    await SV.apply_native_limit(api, "psabc", 120)
    assert ("PUT", "/api/admin/psabc", {"data_limit": 120 * 1024 ** 3}) in PGWrite.log
    PGWrite.log = []
    await SV.apply_native_limit(api, "psabc", 0)                                   # نامحدود ⇒ ۰ (طبقِ کدِ رسمی)
    assert ("PUT", "/api/admin/psabc", {"data_limit": 0}) in PGWrite.log
    await api.reset_usage("psabc")
    assert ("POST", "/api/admin/psabc/reset", {}) in PGWrite.log                    # مسیرِ ریستِ پاسارگاردِ جدید


@pytest.mark.asyncio
async def test_native_limit_failure_never_breaks_and_old_panels_untouched():
    class Boom(PGWrite):
        async def _request(self, method, path, json=None, params=None):
            if method == "PUT":
                raise PanelError("PUT 500")
            return await PGWrite._request(self, method, path, json=json, params=params)
    await SV.apply_native_limit(AD.PanelAdminAPI(Boom()), "psabc", 10)           # استثنا نمی‌دهد
    class Old:
        calls = []
        async def _request(self, method, path, json=None, params=None):
            Old.calls.append((method, path))
            return [{"username": "u1"}] if path == "/api/admins" else {}
        async def close(self): pass
    assert await AD.PanelAdminAPI(Old()).apply_data_limit("u1", 10) is False
    assert not any(m == "PUT" for m, _p in Old.calls)                              # پنلِ قدیمی: هیچ PUT ای نمی‌رود


def test_support_url_normalisation():
    from tenant.bots.panel_seller.user import _support_url
    assert _support_url("@my_support") == "https://t.me/my_support"
    assert _support_url("my_support") == "https://t.me/my_support"
    assert _support_url("t.me/my_support") == "https://t.me/my_support"
    assert _support_url("https://t.me/x_yz1") == "https://t.me/x_yz1"
    for bad in ("", "a b", "@ab", "javascript:alert(1)", "<x>", "x" * 300):
        assert _support_url(bad) is None, bad


@pytest.mark.asyncio
async def test_snapshot_single_request_for_new_style_and_fallback_for_old():
    NewPGStats.calls = []
    snap = await AD.PanelAdminAPI(NewPGStats()).snapshot("psabc")
    assert snap == {"used_bytes": 5_000_000_000, "users": 7, "new_style": True} and len(NewPGStats.calls) == 1
