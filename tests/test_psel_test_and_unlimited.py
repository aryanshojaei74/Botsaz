"""PATCH_PSEL_FREE_TEST_V1 / PATCH_PSEL_UNLIMITED_DAYS_V1 / PATCH_PSEL_OWNER_FAIL_NOTICE_V1 — Whale Support."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.panel_seller import common as PC
from tenant.bots.panel_seller import service as SV
from tenant.bots.panel_seller import user as PU
from tests.test_panel_seller import FakeAPI


@pytest_asyncio.fixture
async def env(monkeypatch):
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
        s.add(models.PanelSellerPackage(id=1, bot_id=7, panel_id=3, title="نامحدود", data_gb=100, max_users=10, days=0,
                                        price_toman=300_000, active=True))
        await s.commit()
    yield m
    await engine.dispose()


def test_days_text_and_pk_line_show_unlimited():
    assert PC.days_text(0) == "♾ نامحدود" and PC.days_text(None) == "♾ نامحدود" and "۳۰" in PC.days_text(30)
    pk = NS(title="x", data_gb=10, max_users=5, days=0, price_toman=1000)
    assert "♾ نامحدود" in PC.pk_line(pk) and "روز" not in PC.pk_line(pk)
    assert PC.exp_text(None) == "♾ نامحدود"


@pytest.mark.asyncio
async def test_zero_day_package_creates_account_without_expiry_and_renews_without_expiry(env):
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    assert r["status"] == "ok"
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, r["account_id"])
    assert a.expires_at is None                                       # قبلاً «۰ ⇒ ۳۰ روز» می‌شد
    r2 = await SV.renew(7, r["account_id"], 1001, api_factory=FakeAPI)
    assert r2["status"] == "ok" and r2["expires_at"] is None


@pytest.mark.asyncio
async def test_unlimited_account_is_never_suspended_for_expiry(env):
    r = await SV.buy(7, 1001, 1, api_factory=FakeAPI)
    out = await SV.enforce_once(7, api_factory=FakeAPI, now=datetime.utcnow() + timedelta(days=5000))
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, r["account_id"])
    assert a.status == "active" and not any("مهلت" in m for _tg, m in out)


# ─────────── تستِ رایگان ───────────
async def _enable(m, **kw):
    from tenant import set_setting
    await set_setting(7, "psel_test_enabled", "1")
    for k, v in kw.items():
        await set_setting(7, f"psel_test_{k}", str(v))
    tenant._SETTING_CACHE.clear()


@pytest.mark.asyncio
async def test_test_section_off_by_default(env):
    assert await SV.test_available(7, 1001) is False
    assert (await SV.buy_test(7, 1001, api_factory=FakeAPI))["status"] == "disabled"


@pytest.mark.asyncio
async def test_free_test_is_one_per_user_with_owner_limits_and_no_charge(env):
    await _enable(env, hours=48, gb=2, users=4)
    assert await SV.test_available(7, 1001) is True
    r = await SV.buy_test(7, 1001, api_factory=FakeAPI)
    assert r["status"] == "ok" and (r["hours"], r["gb"], r["users"]) == (48, 2, 4)
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, r["account_id"])
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 1001))
    assert a.title == SV.TEST_TITLE and a.data_gb == 2 and a.max_users == 4 and a.package_id is None and a.price_toman == 0
    assert 47 <= (a.expires_at - datetime.utcnow()).total_seconds() / 3600 <= 48.1 and u.balance == 1_000_000
    assert ("create", r["username"], r["password"]) in FakeAPI.log
    assert await SV.test_available(7, 1001) is False
    assert (await SV.buy_test(7, 1001, api_factory=FakeAPI))["status"] == "test_used"
    assert await SV.test_available(7, 2002) is True                    # مشتریِ دیگر می‌تواند


@pytest.mark.asyncio
async def test_deleted_test_panel_still_counts_as_used(env):
    await _enable(env)
    r = await SV.buy_test(7, 1001, api_factory=FakeAPI)
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, r["account_id"])
        a.status = "deleted"
        await s.commit()
    assert await SV.test_available(7, 1001) is False


@pytest.mark.asyncio
async def test_test_panel_failure_returns_error_and_does_not_burn_the_test(env):
    await _enable(env)

    class Bad(FakeAPI):
        async def create_admin(self, u, p): raise RuntimeError("403 Forbidden: sudo required")
    r = await SV.buy_test(7, 1001, api_factory=Bad)
    assert r["status"] == "panel_failed" and "sudo" in r["error"]
    assert await SV.test_available(7, 1001) is True                    # حساب ساخته نشد ⇒ تست سوخت نشده


# ─────────── اعلانِ علتِ خطا به مالک ───────────
@pytest.mark.asyncio
async def test_owner_gets_exact_panel_error_throttled(monkeypatch):
    sent = []

    async def _na(bot, row, text, *a, **k): sent.append(text)
    monkeypatch.setattr(tenant, "_notify_admins", _na)
    import log_throttle
    monkeypatch.setattr(log_throttle, "should_log", lambda key, secs: not sent)     # دومی throttle می‌شود
    row = NS(id=7)
    await PU.tell_owner_failure(object(), row, 1001, "ساختِ پنل", "HTTP 403: not sudo")
    await PU.tell_owner_failure(object(), row, 1002, "ساختِ پنل", "again")
    assert len(sent) == 1 and "HTTP 403: not sudo" in sent[0] and "1001" in sent[0] and "sudo" in sent[0]
