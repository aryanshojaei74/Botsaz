"""PATCH_PANEL_SELLER_V1 — ربات پنل‌فروشی: گیت/مرحلهٔ تست، خرید/تمدید/تعلیق/اعمالِ سقف‌ها، ثبتِ نوعِ ربات."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.panel_seller import adapter as AD
from tenant.bots.panel_seller import common as PC
from tenant.bots.panel_seller import gate as GT
from tenant.bots.panel_seller import service as SV


class FakeAPI:
    log: list = []
    usage = 0
    users = 0
    fail_create = False

    def __init__(self, panel):
        self.c = self

    async def create_admin(self, u, p):
        if FakeAPI.fail_create:
            raise RuntimeError("boom")
        FakeAPI.log.append(("create", u, p))

    async def set_password(self, u, p):
        FakeAPI.log.append(("pw", u, p))

    async def delete_admin(self, u):
        FakeAPI.log.append(("del", u))

    async def disable_users(self, u):
        FakeAPI.log.append(("off", u))

    async def activate_users(self, u):
        FakeAPI.log.append(("on", u))

    new_style = False                      # PATCH_PSEL_NATIVE_LIMIT_V1: شبیه‌سازِ پاسارگاردِ جدید

    async def is_new_style(self, u):
        return FakeAPI.new_style

    async def put_admin(self, u, **fields):
        FakeAPI.log.append(("put", u, dict(fields)))

    async def apply_data_limit(self, u, gb):
        if not FakeAPI.new_style:
            return False
        FakeAPI.log.append(("limit", u, int(gb)))
        return True

    async def reset_usage(self, u):
        FakeAPI.log.append(("reset", u))

    async def snapshot(self, u):
        return {"used_bytes": FakeAPI.usage, "users": FakeAPI.users, "new_style": FakeAPI.new_style}

    async def usage_bytes(self, u):
        return FakeAPI.usage

    async def user_count(self, u):
        return FakeAPI.users

    async def _request(self, *a, **k):
        FakeAPI.log.append(("req",) + a)

    async def restart_core(self):
        FakeAPI.log.append(("req", "POST", "/api/core/restart"))

    async def close(self):
        pass


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    import tenant as _tenant
    for mod in (config, GT, SV, _tenant):               # tenant.Session: تنظیماتِ ربات (psel_keep_login و ...) از آن خوانده می‌شود
        monkeypatch.setattr(mod, "Session", maker)
    getattr(_tenant, "_SETTING_CACHE", {}).clear()
    FakeAPI.log, FakeAPI.usage, FakeAPI.users, FakeAPI.fail_create, FakeAPI.new_style = [], 0, 0, False, False
    async with maker() as s:
        s.add(models.Bot(id=7, owner_id=500, token="7:T", username="pbot", field="panel_seller", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=1001, full_name="Ali", username="ali", balance=1_000_000))
        s.add(models.VpnPanel(id=3, bot_id=7, panel_key="marzban", base_url="https://p.example.com:8000",
                              username="sudo", password="x", enabled=True))
        s.add(models.PanelSellerPackage(id=1, bot_id=7, panel_id=3, title="۱۰۰ گیگ", data_gb=100, max_users=10, days=30,
                                        price_toman=300_000, active=True))
        await s.commit()
    yield maker
    await engine.dispose()


async def _bal(maker):
    async with maker() as s:
        return int((await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 1001))).balance)


def test_adapter_supports_only_marzban_family():
    for k in ("marzban", "rebecca", "pasargad", "rickpanel", "marzban#2"):
        assert AD.supports(k)
    for k in ("sanai", "hiddify", "alireza", "pasarguard", "marzneshin", None):
        assert not AD.supports(k)


@pytest.mark.asyncio
async def test_beta_gate_grant_expire_revoke(env):
    now = datetime(2026, 1, 1)
    assert await GT.status(5) == (False, "none")
    await GT.grant(5, 7, now=now)
    assert (await GT.status(5, now=now + timedelta(days=6)))[0] is True
    assert await GT.status(5, now=now + timedelta(days=8)) == (False, "expired")
    await GT.grant(6, 0)                                   # دائمی
    assert (await GT.status(6, now=now + timedelta(days=9999)))[0] is True
    assert await GT.revoke(6) is True and await GT.revoke(6) is False


@pytest.mark.asyncio
async def test_sales_gate_needs_pro_and_beta_access(env):
    maker = env
    row = NS(owner_id=500, plan="pro")
    ok, why = await PC.sales_gate(row)
    assert not ok and "تست" in why                         # دسترسی نداده‌اند
    await GT.grant(500, 5)
    assert (await PC.sales_gate(row))[0] is True
    ok, why = await PC.sales_gate(NS(owner_id=500, plan="growth"))
    assert not ok and "حرفه‌ای" in why


@pytest.mark.asyncio
async def test_buy_success_creates_admin_and_debits_wallet(env):
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    assert r["status"] == "ok" and r["username"].startswith("ps") and len(r["password"]) >= 12
    assert ("create", r["username"], r["password"]) in FakeAPI.log
    assert await _bal(env) == 700_000
    async with env() as s:
        a = await s.scalar(select(models.PanelSellerAccount))
    assert a.status == "active" and a.data_gb == 100 and a.max_users == 10 and a.buyer_tg == 1001
    assert 29 <= (a.expires_at - datetime.utcnow()).days <= 30 and a.tracking_code


@pytest.mark.asyncio
async def test_buy_insufficient_and_panel_failure_refund(env):
    async with env() as s:
        u = await s.scalar(select(models.BotUser))
        u.balance = 1000
        await s.commit()
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    assert r == {"status": "insufficient", "need": 300_000, "balance": 1000}
    assert await _bal(env) == 1000 and not FakeAPI.log
    async with env() as s:
        u = await s.scalar(select(models.BotUser))
        u.balance = 500_000
        await s.commit()
    FakeAPI.fail_create = True
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    assert r["status"] == "panel_failed" and await _bal(env) == 500_000          # پول برگشت
    async with env() as s:
        assert await s.scalar(select(models.PanelSellerAccount)) is None
    # پنلِ غیرِ مرزبانی ⇒ فروش نمی‌شود
    async with env() as s:
        p = await s.get(models.VpnPanel, 3)
        p.panel_key = "sanai"
        await s.commit()
    assert (await SV.buy(7, 1001, 1, api_factory=FakeAPI))["status"] == "unsupported"


async def _sell(env):
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    assert r["status"] == "ok"
    return r["account_id"], r["username"]


@pytest.mark.asyncio
async def test_enforce_expiry_traffic_and_warnings(env):
    aid, uname = await _sell(env)
    now = datetime.utcnow()
    # هشدارِ نزدیکیِ پایان فقط یک‌بار
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, aid)
        a.expires_at = now + timedelta(days=2)
        await s.commit()
    ev = await SV.enforce_once(7, FakeAPI, now)
    assert len(ev) == 1 and ev[0][0] == 1001 and "روز" in ev[0][1]
    assert await SV.enforce_once(7, FakeAPI, now) == []
    # ۸۰٪ حجم ⇒ هشدار؛ تمام‌شدن ⇒ تعلیق
    FakeAPI.usage = 85 * SV.GB
    ev = await SV.enforce_once(7, FakeAPI, now)
    assert any("۸۰" in t for _tg, t in ev)
    FakeAPI.usage = 100 * SV.GB
    ev = await SV.enforce_once(7, FakeAPI, now)
    assert ("off", uname) in FakeAPI.log and any(tg == 0 for tg, _ in ev) and any(tg == 1001 for tg, _ in ev)
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, aid)
    assert a.status == "suspended" and a.suspend_reason == "traffic"
    assert not any(x[0] == "pw" and x[1] == uname for x in FakeAPI.log)         # PATCH_PSEL_KEEP_LOGIN_V1: پایانِ حجم ⇒ ورودِ مشتری باز می‌ماند (فقط کاربران خاموش)
    # انقضا
    aid2, uname2 = await _sell(env)
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, aid2)
        a.expires_at = now - timedelta(hours=1)
        await s.commit()
    FakeAPI.usage = 0
    await SV.enforce_once(7, FakeAPI, now)
    async with env() as s:
        assert (await s.get(models.PanelSellerAccount, aid2)).suspend_reason == "expired"
    assert any(x[0] == "pw" and x[1] == uname2 for x in FakeAPI.log)             # پایانِ مهلت: مثلِ قبل رمز تصادفی ⇒ ورود بسته


@pytest.mark.asyncio
async def test_renew_reactivates_with_new_password_and_charges(env):
    aid, uname = await _sell(env)
    assert await SV.suspend(aid, "expired", FakeAPI) is True
    FakeAPI.log.clear()
    r = await SV.renew(7, aid, 1001, api_factory=FakeAPI)
    assert r["status"] == "ok" and r["new_password"] and ("on", uname) in FakeAPI.log
    assert await _bal(env) == 400_000                                              # ۱۰۰۰۰۰۰ − ۳۰۰۰۰۰ − ۳۰۰۰۰۰
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, aid)
    assert a.status == "active" and a.flags is None
    # خریدارِ دیگر نمی‌تواند تمدید کند
    assert (await SV.renew(7, aid, 999, api_factory=FakeAPI))["status"] == "not_found"


@pytest.mark.asyncio
async def test_password_toggle_delete_are_scoped_to_owner_of_account(env):
    aid, uname = await _sell(env)
    r = await SV.change_password(7, aid, 1001, api_factory=FakeAPI)
    assert r["status"] == "ok" and ("pw", uname, r["password"]) in FakeAPI.log
    assert (await SV.change_password(7, aid, 555, api_factory=FakeAPI))["status"] == "not_found"
    assert (await SV.set_users_enabled(7, aid, 1001, False, api_factory=FakeAPI))["status"] == "ok"
    assert (await SV.set_users_enabled(7, aid, 555, False, api_factory=FakeAPI))["status"] == "not_found"
    assert await SV.delete_account(7, aid, FakeAPI) is True and ("del", uname) in FakeAPI.log
    assert (await SV.change_password(7, aid, 1001, api_factory=FakeAPI))["status"] == "not_found"


def test_kind_registered_on_platform_and_tenant():
    import platform_bot as PB
    assert "panel_seller" in PB.FIELDS and "panel_seller" in PB.ACTIVE_FIELDS and "panel_seller" in PB.FIELD_PITCH
    assert "در حال تست" in PB.FIELDS["panel_seller"]
    kb = PB._action_kb_base("panel_seller")
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "act:psel_buy" in cbs and "act:psel_my" in cbs
    from tenant.settings_registry import _PRODUCTS
    assert "panel_seller" in _PRODUCTS


# ───────────────────────── هندلرها (UI) ─────────────────────────
class _Msg:
    def __init__(self, uid=1001, text=""):
        self.text, self.from_user, self.out, self.chat = text, NS(id=uid, full_name="Ali", username="ali"), [], NS(id=uid)
        self.message_id = 1

    async def answer(self, text, **k):
        self.out.append((text, k.get("reply_markup")))
        return self

    async def edit_text(self, text, **k):
        self.out.append((text, k.get("reply_markup")))
        return self

    async def delete(self):
        pass


class _Cb:
    def __init__(self, data, uid=1001):
        self.data, self.from_user, self.message, self.alerts = data, NS(id=uid, full_name="Ali", username="ali"), _Msg(uid), []

    async def answer(self, text="", show_alert=False, **k):
        self.alerts.append(text)


class _St:
    def __init__(self):
        self.d, self.s = {}, None

    async def update_data(self, **k):
        self.d.update(k)

    async def get_data(self):
        return dict(self.d)

    async def set_state(self, s):
        self.s = s

    async def clear(self):
        self.d, self.s = {}, None


def _cbs(m):
    kbm = m.out[-1][1]
    return [b.callback_data for r in kbm.inline_keyboard for b in r if b.callback_data] if kbm else []


@pytest_asyncio.fixture
async def ui(env, monkeypatch):
    import tenant
    from tenant.bots.panel_seller import admin as AM
    from tenant.bots.panel_seller import user as UM
    row = NS(id=7, owner_id=500, field="panel_seller", plan="pro", username="pbot")
    for mod in (tenant, AM, UM):
        monkeypatch.setattr(mod, "Session", env)

    async def gbr(bot):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", gbr)

    async def alert(*a, **k):
        return None
    monkeypatch.setattr(tenant, "_purchase_alert", alert)
    monkeypatch.setattr(SV, "_api", FakeAPI)
    monkeypatch.setattr(AD.PanelAdminAPI, "for_panel", classmethod(lambda cls, p: FakeAPI(p)))
    ui_cache = getattr(tenant, "_SETTING_CACHE", None)
    if ui_cache is not None:
        ui_cache.clear()                                   # کشِ ۱۰ثانیه‌ایِ تنظیمات بینِ تست‌ها نشت نکند
    await GT.grant(500, 10)
    return NS(row=row, AM=AM, UM=UM, maker=env, tenant=tenant)


@pytest.mark.asyncio
async def test_buyer_flow_buy_then_manage_by_permissions(ui):
    UM = ui.UM
    m = _Msg()
    await UM.show_buy(m, None, ui.row, 1001)
    assert "psel:b:1" in _cbs(m)
    cb = _Cb("psel:b:1")
    await UM.buy_detail(cb, None)
    assert "psel:bok:1" in _cbs(cb.message)
    cb = _Cb("psel:bok:1")
    await UM.buy_do(cb, None, _St())
    text = cb.message.out[-1][0]
    assert "پنلِ تو آماده است" in text and "https://p.example.com:8000" in text and "ps" in text
    async with ui.maker() as s:
        a = await s.scalar(select(models.PanelSellerAccount))
    # پیش‌فرض: فقط تمدید مجاز است؛ رمز/خاموش‌کردن ممنوع
    cb = _Cb(f"psel:a:{a.id}")
    await UM.my_detail(cb, None)
    cbs = _cbs(cb.message)
    assert f"psel:rn:{a.id}" in cbs and f"psel:pw:{a.id}" not in cbs and f"psel:tg:{a.id}:0" not in cbs
    cb = _Cb(f"psel:pwok:{a.id}")
    await UM.password_flow(cb, None)
    assert "توسطِ مدیر بسته" in cb.alerts[-1]
    # مالک اجازه می‌دهد ⇒ دکمه‌ها می‌آیند و کار می‌کنند
    await ui.tenant.set_setting(7, "psel_allow_pw", "1")
    await ui.tenant.set_setting(7, "psel_allow_toggle", "1")
    cb = _Cb(f"psel:a:{a.id}")
    await UM.my_detail(cb, None)
    assert f"psel:pw:{a.id}" in _cbs(cb.message) and f"psel:tg:{a.id}:0" in _cbs(cb.message)
    cb = _Cb(f"psel:pwok:{a.id}")
    await UM.password_flow(cb, None)
    assert "رمزِ تازه" in cb.message.out[-1][0]
    # خریدارِ دیگر به پنلِ این‌یکی دسترسی ندارد
    cb = _Cb(f"psel:a:{a.id}", uid=999)
    await UM.my_detail(cb, None)
    assert cb.alerts and not cb.message.out


@pytest.mark.asyncio
async def test_buyer_blocked_when_beta_access_missing(ui):
    await GT.revoke(500)
    m = _Msg()
    await ui.UM.show_buy(m, None, ui.row, 1001)
    assert "تست" in m.out[-1][0]
    cb = _Cb("psel:bok:1")
    await ui.UM.buy_do(cb, None, _St())
    assert cb.alerts and "تست" in cb.alerts[-1]
    async with ui.maker() as s:
        assert await s.scalar(select(models.PanelSellerAccount)) is None


@pytest.mark.asyncio
async def test_owner_menu_package_wizard_and_permissions(ui):
    AM = ui.AM
    cb = _Cb("psel:adm", uid=500)
    await AM.adm_menu(cb, None, _St())
    text, cbs = cb.message.out[-1][0], _cbs(cb.message)
    assert "در حالِ تست" in text and "psel:pk" in cbs and "pyar:menu" in cbs and "psel:rs" in cbs
    # غیرِ مالک رد می‌شود
    cb = _Cb("psel:adm", uid=1001)
    await AM.adm_menu(cb, None, _St())
    assert not cb.message.out
    # ویزاردِ بسته
    st = _St()
    cb = _Cb("psel:pkp:3", uid=500)
    await AM.pk_panel(cb, None, st)
    for step, val in ((AM.pk_title, "ادمین ۵۰ گیگ"), (AM.pk_gb, "۵۰"), (AM.pk_users, "5"), (AM.pk_days, "30"), (AM.pk_price, "150,000")):
        await step(_Msg(500, val), None, st)
    async with ui.maker() as s:
        pks = (await s.scalars(select(models.PanelSellerPackage).order_by(models.PanelSellerPackage.id))).all()
    assert pks[-1].title == "ادمین ۵۰ گیگ" and pks[-1].data_gb == 50 and pks[-1].max_users == 5 and pks[-1].price_toman == 150_000
    # تنظیمِ دسترسی
    cb = _Cb("psel:set:psel_allow_pw", uid=500)
    await AM.set_toggle(cb, None)
    assert await ui.tenant.get_setting(7, "psel_allow_pw", "0") == "1"
    # ریستارت (تأیید ⇒ اجرا)
    cb = _Cb("psel:rsok:3", uid=500)
    await AM.rs_do(cb, None)
    assert any(x[0] == "req" and "/api/core/restart" in x for x in FakeAPI.log) and "ریستارت" in cb.message.out[-1][0]


@pytest.mark.asyncio
async def test_owner_sees_and_manages_sold_accounts(ui):
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    AM = ui.AM
    cb = _Cb("psel:ac:0", uid=500)
    await AM.ac_list(cb, None)
    assert f"psel:ac:v:{r['account_id']}" in _cbs(cb.message)
    cb = _Cb(f"psel:asp:{r['account_id']}", uid=500)
    await AM.ac_suspend(cb, None)
    async with ui.maker() as s:
        assert (await s.get(models.PanelSellerAccount, r["account_id"])).status == "suspended"
    sent = []
    bot = NS(send_message=lambda *a, **k: _noop(sent, a))
    cb = _Cb(f"psel:are:{r['account_id']}", uid=500)
    await AM.ac_resume(cb, bot)
    assert sent and sent[0][0] == 1001 and "رمزِ تازه" in sent[0][1]


async def _noop(sent, a):
    sent.append(a)


@pytest.mark.asyncio
async def test_platform_plan_guard_and_gate(env, monkeypatch):
    import platform_bot as PB
    import platform_panel_seller as PS
    monkeypatch.setattr(PB, "plan_sale_closed", lambda plan: _aw(False))
    st = _St()
    await st.update_data(field="panel_seller")
    cb = _Cb("newplan:growth", uid=5)
    await PB.choose_plan(cb, st)
    assert "حرفه‌ای" in cb.alerts[-1]
    cb = _Cb("newplan:pro", uid=5)                          # حرفه‌ای ولی بدونِ دسترسیِ تست
    await PB.choose_plan(cb, st)
    assert "مرحلهٔ تست" in cb.alerts[-1]
    # دروازه: بدونِ دسترسی صفحهٔ «ثبتِ درخواست»؛ با دسترسی عبور
    cb = _Cb("field:panel_seller", uid=5)
    assert await PS.create_gate(cb) is False and "pselreq" in _cbs(cb.message)
    await GT.grant(5, 3)
    assert await PS.create_gate(_Cb("field:panel_seller", uid=5)) is True


async def _aw(v):
    return v


def test_worker_modules_import_cleanly():
    """worker_tenant مستقیم این حلقه‌ها را import می‌کند؛ هر ImportError کلِ worker را از کار می‌اندازد."""
    import importlib
    for name in ("tenant.bots.panel_seller.loop", "tenant.bots.vpn.autorenew"):
        mod = importlib.import_module(name)
        assert callable(getattr(mod, "panel_seller_loop", None) or getattr(mod, "autorenew_loop"))


def _urls(m):
    kbm = m.out[-1][1]
    return [b.url for r in kbm.inline_keyboard for b in r if getattr(b, "url", None)] if kbm else []


@pytest.mark.asyncio
async def test_login_button_support_button_and_buyer_delete_flow(ui):
    """PATCH_PSEL_LOGIN_BUTTON_V1 / PATCH_PSEL_ACCOUNT_BUTTONS_V1 (Whale Support)."""
    UM = ui.UM
    cb = _Cb("psel:bok:1")
    await UM.buy_do(cb, None, _St())
    assert "https://p.example.com:8000" in _urls(cb.message)                      # «🌐 ورود به پنل» زیرِ پیامِ «پنلِ تو آماده است»
    async with ui.maker() as s:
        a = await s.scalar(select(models.PanelSellerAccount))
    cb = _Cb(f"psel:a:{a.id}")
    await UM.my_detail(cb, None)
    urls, cbs = _urls(cb.message), _cbs(cb.message)
    assert "https://p.example.com:8000" in urls                                                  # ورود به پنل وب
    assert f"psel:cf:{a.id}:s" not in cbs                                                        # پنلِ قدیمی: دکمهٔ آدرسِ پشتیبانیِ ادمین نیست
    assert f"psel:sp:{a.id}" in cbs                                                              # «📊 مشخصات و مصرف» ⇒ صفحهٔ کاملِ مشخصات
    assert f"psel:dl:{a.id}" not in cbs                                                          # حذف: پیش‌فرض ممنوع
    cb = _Cb(f"psel:dlok:{a.id}")
    await UM.delete_flow(cb, None)
    assert "بسته است" in cb.alerts[-1]
    async with ui.maker() as s:
        assert (await s.get(models.PanelSellerAccount, a.id)).status == "active"
    # مالک اجازه می‌دهد ⇒ دکمه با تأییدِ دو مرحله‌ای؛ حذف از پنل و دیتابیس
    await ui.tenant.set_setting(7, "psel_allow_delete", "1")
    ui.tenant._SETTING_CACHE.clear()
    cb = _Cb(f"psel:a:{a.id}")
    await UM.my_detail(cb, None)
    assert f"psel:dl:{a.id}" in _cbs(cb.message)
    cb = _Cb(f"psel:dl:{a.id}")
    await UM.delete_flow(cb, None)
    assert "psel:dlok:%d" % a.id in _cbs(cb.message) and "قابلِ برگشت نیست" in cb.message.out[-1][0]
    cb = _Cb(f"psel:dlok:{a.id}")
    await UM.delete_flow(cb, None)
    async with ui.maker() as s:
        assert (await s.get(models.PanelSellerAccount, a.id)).status == "deleted"
    assert ("del", a.admin_username) in FakeAPI.log
    # خریدارِ دیگر نمی‌تواند پنلِ کسِ دیگر را حذف کند
    cb = _Cb(f"psel:dlok:{a.id}", uid=999)
    await UM.delete_flow(cb, None)
    assert cb.alerts and not cb.message.out


@pytest.mark.asyncio
async def test_buy_page_shows_custom_and_test_even_with_no_packages(ui):
    """PATCH_PSEL_NO_PACKAGES_BUT_CUSTOM_V1 — «فروشِ حجمِ دلخواه فعاله ولی بات هیچی نمیاره»."""
    async with ui.maker() as s:
        pk = await s.get(models.PanelSellerPackage, 1)
        pk.active = False
        await s.commit()
    m = _Msg()
    await ui.UM.show_buy(m, None, ui.row, 1001)
    assert "بسته‌ای برایِ فروش نیست" in m.out[-1][0] and "psel:cvb" not in _cbs(m)                # هیچ‌چیز فعال نیست
    await ui.tenant.set_setting(7, "psel_custom_price_gb", "3000")
    await ui.tenant.set_setting(7, "psel_test_enabled", "1")
    ui.tenant._SETTING_CACHE.clear()
    m = _Msg()
    await ui.UM.show_buy(m, None, ui.row, 1001)
    assert "psel:cvb" in _cbs(m) and "psel:tst" in _cbs(m)


@pytest.mark.asyncio
async def test_new_style_panel_shows_contact_buttons_and_sets_support_url_profile_title_and_name(ui):
    UM = ui.UM
    cb = _Cb("psel:bok:1")
    await UM.buy_do(cb, None, _St())
    async with ui.maker() as s:
        a = await s.scalar(select(models.PanelSellerAccount))
    FakeAPI.new_style = True
    cb = _Cb(f"psel:a:{a.id}")
    await UM.my_detail(cb, None)
    cbs = _cbs(cb.message)
    assert f"psel:cf:{a.id}:s" in cbs and f"psel:cf:{a.id}:t" in cbs and f"psel:rnm:{a.id}" in cbs
    # آدرسِ پشتیبانی ⇒ support_url روی خودِ ادمین در پنل
    st = _St()
    cb = _Cb(f"psel:cf:{a.id}:s")
    await UM.contact_ask(cb, None, st)
    assert st.d == {"acc_id": a.id, "field": "s"}
    m = _Msg(text="@whale_support")
    await UM.contact_save(m, None, st)
    assert ("put", a.admin_username, {"support_url": "https://t.me/whale_support"}) in FakeAPI.log and "ثبت شد" in m.out[-1][0]
    bad = _Msg(text="javascript:alert(1)")
    st2 = _St(); st2.d = {"acc_id": a.id, "field": "s"}
    await UM.contact_save(bad, None, st2)
    assert "نامعتبر" in bad.out[-1][0] and not any(l[0] == "put" and "javascript" in str(l) for l in FakeAPI.log)
    # پاک‌کردن: ارسالِ «-» و دکمهٔ «پاک کردن» ⇒ رشتهٔ خالی (کدِ رسمی فقط None را نادیده می‌گیرد)
    st_c = _St(); st_c.d = {"acc_id": a.id, "field": "s"}
    mc = _Msg(text="-")
    await UM.contact_save(mc, None, st_c)
    assert ("put", a.admin_username, {"support_url": ""}) in FakeAPI.log and "پاک شد" in mc.out[-1][0]
    cbc = _Cb(f"psel:cfc:{a.id}:t")
    await UM.contact_clear(cbc, None, _St())
    assert ("put", a.admin_username, {"profile_title": ""}) in FakeAPI.log and "پاک شد" in cbc.message.out[-1][0]
    # صفحهٔ راهنما مثلِ رباتِ VPN: «این فیلد چیست؟ / چه چیزی بفرستی؟» + دکمهٔ پاک کردن
    cba = _Cb(f"psel:cf:{a.id}:s")
    await UM.contact_ask(cba, None, _St())
    assert "این فیلد چیست" in cba.message.out[-1][0] and f"psel:cfc:{a.id}:s" in _cbs(cba.message)
    # عنوانِ پروفایل
    st3 = _St(); st3.d = {"acc_id": a.id, "field": "t"}
    await UM.contact_save(_Msg(text="Whale VPN"), None, st3)
    assert ("put", a.admin_username, {"profile_title": "Whale VPN"}) in FakeAPI.log
    # خریدارِ دیگر نمی‌تواند فیلدِ پنلِ دیگری را عوض کند
    n0 = len(FakeAPI.log)
    st4 = _St(); st4.d = {"acc_id": a.id, "field": "t"}
    await UM.contact_save(_Msg(uid=999, text="hack"), None, st4)
    assert len(FakeAPI.log) == n0
    # تغییرِ نامِ سرویس (محلی)
    st5 = _St(); st5.d = {"acc_id": a.id}
    await UM.rename_save(_Msg(text="پنلِ فروشگاه"), None, st5)
    async with ui.maker() as s:
        assert (await s.get(models.PanelSellerAccount, a.id)).title == "پنلِ فروشگاه"


@pytest.mark.asyncio
async def test_native_limit_applied_on_new_style_create_renew_and_extra_volume(ui, monkeypatch):
    FakeAPI.new_style = True
    # ساخت: FakeAPI.create_admin نقش نمی‌خواهد؛ سقف را اعمالِ بومی (PUT) می‌کنیم فقط در تمدید/حجم؛ ساخت از مسیرِ role_id است (تستِ جدا)
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    assert r["status"] == "ok"
    FakeAPI.log.clear()
    rr = await SV.renew(7, r["account_id"], 1001, api_factory=FakeAPI)
    assert rr["status"] == "ok" and ("reset", r_username(r)) in FakeAPI.log and ("limit", r_username(r), 100) in FakeAPI.log
    FakeAPI.log.clear()
    await ui.tenant.set_setting(7, "psel_extra_gb_price", "1000")
    ui.tenant._SETTING_CACHE.clear()
    ev = await SV.add_volume(7, r["account_id"], 1001, 20, api_factory=FakeAPI)
    assert ev["status"] == "ok" and ("limit", r_username(r), 120) in FakeAPI.log          # ۱۰۰ + ۲۰ روی خودِ پنل


def r_username(r):
    return r["username"]


def _grid(m):
    kbm = m.out[-1][1]
    return [[(b.callback_data or b.url) for b in r] for r in kbm.inline_keyboard]


@pytest.mark.asyncio
async def test_account_buttons_are_a_tidy_two_column_grid(ui):
    """PATCH_PSEL_ACCOUNT_GRID_V1 — «دکمه‌هایِ بخشِ پنل‌هایِ من مرتب شود»."""
    await ui.tenant.set_setting(7, "psel_allow_pw", "1")
    await ui.tenant.set_setting(7, "psel_allow_toggle", "1")
    await ui.tenant.set_setting(7, "psel_allow_delete", "1")
    await ui.tenant.set_setting(7, "psel_extra_gb_price", "1000")
    ui.tenant._SETTING_CACHE.clear()
    FakeAPI.new_style = True
    await ui.UM.buy_do(_Cb("psel:bok:1"), None, _St())
    async with ui.maker() as s:
        a = await s.scalar(select(models.PanelSellerAccount))
    cb = _Cb(f"psel:a:{a.id}")
    await ui.UM.my_detail(cb, None)
    g = _grid(cb.message)
    assert g[0] == [f"psel:sp:{a.id}", f"psel:xg:{a.id}"]                              # ردیفِ اول: مشخصات | حجمِ اضافه
    assert all(len(r) <= 2 for r in g)                                                  # هیچ ردیفی بیش از ۲ دکمه نیست
    flat = [c for r in g for c in r]
    for want in (f"psel:rn:{a.id}", f"psel:rnm:{a.id}", f"psel:cf:{a.id}:t", f"psel:cf:{a.id}:s", f"psel:pw:{a.id}",
                 f"psel:tg:{a.id}:0", f"psel:tg:{a.id}:1", f"psel:dl:{a.id}", "psel:my"):
        assert want in flat, want
    assert g[-1] == ["psel:my"] and g[-2] == [f"psel:dl:{a.id}"]                        # حذف تک‌دکمه، آخرین قبل از بازگشت


@pytest.mark.asyncio
async def test_spec_page_shows_full_details_usage_bar_and_notification_toggle(ui, monkeypatch):
    """PATCH_PSEL_SPEC_PAGE_V1 — «مشخصات و مصرف» اطلاعاتِ کامل (مثلِ رباتِ VPN) نشان می‌دهد."""
    await ui.UM.buy_do(_Cb("psel:bok:1"), None, _St())
    async with ui.maker() as s:
        a = await s.scalar(select(models.PanelSellerAccount))
    monkeypatch.setattr(FakeAPI, "usage", int(96.5 * 1024 ** 3))     # ۹۶٫۵ از ۱۰۰ گیگ (monkeypatch: به تست‌هایِ دیگر نشت نکند)
    monkeypatch.setattr(FakeAPI, "users", 3)
    cb = _Cb(f"psel:sp:{a.id}")
    await ui.UM.spec_page(cb, None)
    txt = cb.message.out[-1][0]
    assert a.admin_username in txt and "سقفِ حجم" in txt and "۱۰۰ GB" in txt
    assert "مصرف‌شده" in txt and "باقی‌مانده" in txt and "█" in txt and "۹۷٪" in txt          # ۹۶٫۵٪ گرد می‌شود
    assert "اعلانِ پایانِ حجم" in txt and "فعال" in txt and "کاربران" in txt
    assert f"psel:nt:{a.id}" in _cbs(cb.message) and "https://p.example.com:8000" in _urls(cb.message)
    # خاموش‌کردنِ اعلان ⇒ پرچمِ nt0؛ دوباره ⇒ برمی‌گردد
    cb = _Cb(f"psel:nt:{a.id}")
    await ui.UM.spec_page(cb, None)
    assert "🔴 خاموش" in cb.message.out[-1][0]
    async with ui.maker() as s:
        assert "nt0" in (await s.get(models.PanelSellerAccount, a.id)).flags
    cb = _Cb(f"psel:nt:{a.id}")
    await ui.UM.spec_page(cb, None)
    assert "🟢 فعال" in cb.message.out[-1][0]
    # خریدارِ دیگر دسترسی ندارد
    cb = _Cb(f"psel:nt:{a.id}", uid=999)
    await ui.UM.spec_page(cb, None)
    assert cb.alerts and not cb.message.out


@pytest.mark.asyncio
async def test_traffic_warning_at_80_percent_and_suspend_at_100_respect_notification_toggle(ui, monkeypatch):
    """سؤالِ Whale: «اگر حجمِ پنلِ مشتری تموم شد یا نزدیکِ پایان رسید هشدار می‌ره؟» + خاموش‌کردنِ اعلان."""
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    monkeypatch.setattr(FakeAPI, "usage", int(85 * 1024 ** 3))                                                # ۸۵٪
    out = await SV.enforce_once(7, api_factory=FakeAPI)
    assert [m for tg, m in out if tg == 1001 and "۸۰٪" in m]                          # هشدارِ ۸۰٪ به مشتری
    out2 = await SV.enforce_once(7, api_factory=FakeAPI)
    assert not [m for tg, m in out2 if "۸۰٪" in m]                                    # تکراری نمی‌شود
    monkeypatch.setattr(FakeAPI, "usage", int(100 * 1024 ** 3))                                               # تمام
    out3 = await SV.enforce_once(7, api_factory=FakeAPI)
    assert [m for tg, m in out3 if tg == 1001 and "تمام شد" in m] and [1 for tg, _m in out3 if tg == 0]   # مشتری + مالک
    async with ui.maker() as s:
        assert (await s.get(models.PanelSellerAccount, r["account_id"])).status == "suspended"
    # مشتریِ دیگر با اعلانِ خاموش: هشدارِ ۸۰٪ نمی‌رود ولی تعلیقِ ۱۰۰٪ هنوز اطلاع می‌دهد
    r2 = await SV.buy(7, 1001, 1, api_factory=FakeAPI, username="second1")
    async with ui.maker() as s:
        acc = await s.get(models.PanelSellerAccount, r2["account_id"])
        acc.flags = "nt0"
        await s.commit()
    monkeypatch.setattr(FakeAPI, "usage", int(85 * 1024 ** 3))
    out4 = await SV.enforce_once(7, api_factory=FakeAPI)
    assert not [m for tg, m in out4 if "second1" in m and "۸۰٪" in m]
    monkeypatch.setattr(FakeAPI, "usage", int(100 * 1024 ** 3))
    out5 = await SV.enforce_once(7, api_factory=FakeAPI)
    assert [m for tg, m in out5 if tg == 1001 and "second1" in m and "تمام شد" in m]
    # تمدید پرچمِ nt0 را نگه می‌دارد
    await SV.renew(7, r2["account_id"], 1001, api_factory=FakeAPI)
    async with ui.maker() as s:
        assert (await s.get(models.PanelSellerAccount, r2["account_id"])).flags == "nt0"


# ─────────── نمایشِ رمز + ورودِ باز در پایانِ حجم (Whale Support) ───────────
@pytest.fixture
def _key(monkeypatch):
    monkeypatch.setenv("BOTSAZ_SECRET_KEY", "unit-test-key")


@pytest.mark.asyncio
async def test_password_is_stored_encrypted_and_shown_only_to_its_owner(ui, _key):
    """PATCH_PSEL_PASSWORD_VAULT_V1 — «رمزِ پنل را گم می‌کنم»: رمز رمزنگاری‌شده ذخیره می‌شود و فقط به خریدارش نشان داده می‌شود."""
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    aid, pw = r["account_id"], r["password"]
    assert await SV.get_secret(aid) == pw
    async with ui.maker() as s:
        raw = (await s.scalar(select(models.PanelSellerSecret))).pw
    assert raw.startswith("enc1:") and pw not in raw                                    # متنِ ساده در دیتابیس نیست
    cb = _Cb(f"psel:sp:{aid}")
    await ui.UM.spec_page(cb, None)
    assert "psel:pws:%d" % aid in _cbs(cb.message) and pw not in cb.message.out[-1][0]      # صفحهٔ مشخصات رمز را لو نمی‌دهد
    cb = _Cb(f"psel:pws:{aid}")
    await ui.UM.show_password(cb, None)
    assert pw in cb.message.out[-1][0] and "پاک کن" in cb.message.out[-1][0]
    other = _Cb(f"psel:pws:{aid}", uid=999)                                              # خریدارِ دیگر
    await ui.UM.show_password(other, None)
    assert not other.message.out and other.alerts
    await ui.tenant.set_setting(7, "psel_allow_showpw", "0")                             # مالک بسته است
    ui.tenant._SETTING_CACHE.clear()
    cb = _Cb(f"psel:pws:{aid}")
    await ui.UM.show_password(cb, None)
    assert not cb.message.out and "بسته است" in cb.alerts[-1]
    cb = _Cb(f"psel:sp:{aid}")
    await ui.UM.spec_page(cb, None)
    assert "psel:pws:%d" % aid not in _cbs(cb.message)


@pytest.mark.asyncio
async def test_password_updates_on_change_and_vanishes_on_delete(ui, _key):
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    aid = r["account_id"]
    ch = await SV.change_password(7, aid, 1001, api_factory=FakeAPI)
    assert await SV.get_secret(aid) == ch["password"] and ch["password"] != r["password"]
    assert await SV.delete_account(7, aid, api_factory=FakeAPI)
    assert await SV.get_secret(aid) is None


@pytest.mark.asyncio
async def test_volume_end_keeps_customer_login_never_deletes_and_keeps_users_off(ui, _key, monkeypatch):
    """Whale: «بعد از اتمامِ حجم پنلِ مشتری به هیچ وجه حذف نشه» + مشتری هنوز ببیند هر کاربر چقدر حجم دارد."""
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    aid, uname, pw = r["account_id"], r["username"], r["password"]
    monkeypatch.setattr(FakeAPI, "usage", 100 * SV.GB)
    FakeAPI.log.clear()
    await SV.enforce_once(7, FakeAPI)
    async with ui.maker() as s:
        a = await s.get(models.PanelSellerAccount, aid)
    assert a.status == "suspended" and a.suspend_reason == "traffic"                       # حذف نشد
    assert ("off", uname) in FakeAPI.log and not any(x[0] in ("pw", "del") for x in FakeAPI.log)   # کاربران خاموش؛ رمز/حذف دست‌نخورده
    assert await SV.get_secret(aid) == pw                                                  # رمز هنوز قابلِ نمایش
    # دورِ بعد: اگر مشتری کاربرانش را دوباره روشن کرده باشد، دوباره خاموش می‌شوند (دورزدنِ سقف)
    FakeAPI.log.clear()
    await SV.enforce_once(7, FakeAPI)
    assert ("off", uname) in FakeAPI.log
    # حجمِ اضافه: فقط کاربران روشن می‌شوند؛ رمزِ مشتری عوض نمی‌شود
    await ui.tenant.set_setting(7, "psel_extra_gb_price", "1000")
    ui.tenant._SETTING_CACHE.clear()
    FakeAPI.log.clear()
    ev = await SV.add_volume(7, aid, 1001, 20, api_factory=FakeAPI)
    assert ev["status"] == "ok" and ev["new_password"] is None
    assert ("on", uname) in FakeAPI.log and not any(x[0] == "pw" for x in FakeAPI.log)
    assert await SV.get_secret(aid) == pw


@pytest.mark.asyncio
async def test_owner_can_switch_back_to_closing_login_on_volume_end(ui, _key, monkeypatch):
    await ui.tenant.set_setting(7, "psel_keep_login", "0")
    ui.tenant._SETTING_CACHE.clear()
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    monkeypatch.setattr(FakeAPI, "usage", 100 * SV.GB)
    FakeAPI.log.clear()
    await SV.enforce_once(7, FakeAPI)
    assert any(x[0] == "pw" and x[1] == r["username"] for x in FakeAPI.log)                # رفتارِ قبلی: رمز تصادفی
    assert await SV.get_secret(r["account_id"]) is None                                    # رمزِ ناشناخته ⇒ ذخیره‌شده پاک


def test_new_permissions_exist_with_right_defaults():
    from tenant.bots.panel_seller.admin import PERMS
    assert PERMS["psel_allow_showpw"][1] == "1" and PERMS["psel_keep_login"][1] == "1"
