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
    for mod in (config, GT, SV):
        monkeypatch.setattr(mod, "Session", maker)
    FakeAPI.log, FakeAPI.usage, FakeAPI.users, FakeAPI.fail_create = [], 0, 0, False
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
    assert any(x[0] == "pw" and x[1] == uname for x in FakeAPI.log)              # رمز تصادفی ⇒ ورودِ مشتری بسته
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
    await UM.buy_do(cb, None)
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
    await ui.UM.buy_do(cb, None)
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
