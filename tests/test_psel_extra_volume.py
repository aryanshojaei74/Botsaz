"""PATCH_PSEL_EXTRA_VOLUME_V1 — خریدِ حجمِ اضافه در رباتِ پنل‌فروشی."""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.panel_seller import service as SV
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
        s.add(models.BotUser(id=1, bot_id=7, telegram_id=1001, full_name="Ali", balance=100_000))
        s.add(models.VpnPanel(id=3, bot_id=7, panel_key="marzban", base_url="https://p", username="s", password="x", enabled=True))
        s.add(models.PanelSellerAccount(id=11, bot_id=7, buyer_tg=1001, panel_id=3, package_id=None, title="t", admin_username="adm1",
                                        data_gb=100, max_users=10, status="active", flags="tr80,exp3",
                                        expires_at=datetime.utcnow() + timedelta(days=10)))
        await s.commit()

    async def _price(bot_id): return 1_000
    monkeypatch.setattr(SV, "extra_gb_price", _price)
    yield m
    await engine.dispose()


async def _acc(m):
    async with m() as s:
        return await s.get(models.PanelSellerAccount, 11), await s.get(models.BotUser, 1)


@pytest.mark.asyncio
async def test_buy_extra_gb_adds_limit_charges_wallet_and_clears_warning(env):
    r = await SV.add_volume(7, 11, 1001, 20, api_factory=FakeAPI)
    assert r["status"] == "ok" and r["new_gb"] == 120 and r["price"] == 20_000
    a, u = await _acc(env)
    assert a.data_gb == 120 and u.balance == 80_000 and "tr80" not in (a.flags or "") and "exp3" in (a.flags or "")


@pytest.mark.asyncio
async def test_insufficient_balance_changes_nothing(env):
    r = await SV.add_volume(7, 11, 1001, 500, api_factory=FakeAPI)        # ۵۰۰٬۰۰۰ > ۱۰۰٬۰۰۰
    assert r["status"] == "insufficient" and r["need"] == 500_000
    a, u = await _acc(env)
    assert a.data_gb == 100 and u.balance == 100_000


@pytest.mark.asyncio
async def test_traffic_suspended_account_reopens_with_new_password(env):
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, 11)
        a.status, a.suspend_reason = "suspended", "traffic"
        await s.commit()
    r = await SV.add_volume(7, 11, 1001, 10, api_factory=FakeAPI)
    assert r["status"] == "ok" and r["new_password"]
    a, _ = await _acc(env)
    assert a.status == "active" and a.suspend_reason is None and ("on", "adm1") in FakeAPI.log


@pytest.mark.asyncio
async def test_expired_suspension_is_not_reopened_by_volume(env):
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, 11)
        a.status, a.suspend_reason = "suspended", "expired"
        await s.commit()
    assert (await SV.add_volume(7, 11, 1001, 10, api_factory=FakeAPI))["status"] == "not_found"
    _, u = await _acc(env)
    assert u.balance == 100_000


@pytest.mark.asyncio
async def test_panel_failure_refunds(env):
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, 11)
        a.status, a.suspend_reason = "suspended", "traffic"
        await s.commit()

    class Bad(FakeAPI):
        async def set_password(self, u, p): raise RuntimeError("down")
    r = await SV.add_volume(7, 11, 1001, 10, api_factory=Bad)
    assert r["status"] == "panel_failed"
    a, u = await _acc(env)
    assert a.data_gb == 100 and a.status == "suspended" and u.balance == 100_000


@pytest.mark.asyncio
async def test_unlimited_other_buyer_and_bad_amount_rejected(env, monkeypatch):
    assert (await SV.add_volume(7, 11, 999, 5, api_factory=FakeAPI))["status"] == "not_found"        # خریدارِ دیگر
    assert (await SV.add_volume(7, 11, 1001, 0, api_factory=FakeAPI))["status"] == "bad_amount"
    assert (await SV.add_volume(7, 11, 1001, 5000, api_factory=FakeAPI))["status"] == "bad_amount"
    async with env() as s:
        a = await s.get(models.PanelSellerAccount, 11)
        a.data_gb = 0
        await s.commit()
    assert (await SV.add_volume(7, 11, 1001, 5, api_factory=FakeAPI))["status"] == "unlimited"


@pytest.mark.asyncio
async def test_disabled_when_price_zero(env, monkeypatch):
    async def zero(bot_id): return 0
    monkeypatch.setattr(SV, "extra_gb_price", zero)
    assert (await SV.add_volume(7, 11, 1001, 5, api_factory=FakeAPI))["status"] == "disabled"
