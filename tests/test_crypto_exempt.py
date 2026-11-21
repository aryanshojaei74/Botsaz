import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.gateways import wallet_limits as WL
from tenant.shared import kyc as K
from tenant.shared import crypto_exempt as CX


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    async with mk() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="A", balance=0))
        now = dt.datetime.utcnow()
        for k, amt in (("crypto_charge", 5_000_000), ("zarinpal_charge", 1_000_000), ("swap_charge", 2_000_000)):
            s.add(models.WalletTxn(bot_id=5, user_id=1, amount=amt, kind=k, note="x", created_at=now))
        await s.commit()
    store = {}

    async def gs(bid, k, d=""):
        return store.get(k, d)
    monkeypatch.setattr(config, "Session", mk)
    monkeypatch.setattr(K, "Session", mk)
    monkeypatch.setattr(tenant, "get_setting", gs)
    yield mk, store
    await engine.dispose()


@pytest.mark.asyncio
async def test_limits_for_crypto_and_swap_disappear_but_gateways_stay(env):
    mk, store = env
    assert await WL.get_limits(5, "crypto") == (100_000, 0)
    store[CX.KEY] = "1"
    assert await WL.get_limits(5, "crypto") == (1, 0) and await WL.get_limits(5, "swap") == (1, 0)
    assert await WL.check(5, "crypto", 5_000) is None and await WL.check(5, "swap", 100) is None
    assert await WL.get_limits(5, "zarinpal") == (20_000, 0)                     # درگاه مثلِ قبل
    assert "حداقل" in (await WL.check(5, "zarinpal", 100))


@pytest.mark.asyncio
async def test_crypto_deposits_do_not_count_toward_kyc_daily_cap(env, monkeypatch):
    mk, store = env

    async def window(bid):
        return dt.datetime.utcnow() - dt.timedelta(hours=1)
    monkeypatch.setattr(K, "window_start", window)

    async def direct(*a, **k):
        return 0
    monkeypatch.setattr(K, "direct_card_today", direct)
    assert await K.charged_today(5, 1) == 8_000_000
    store[CX.KEY] = "1"
    assert await K.charged_today(5, 1) == 1_000_000                              # فقط درگاه شمرده می‌شود


@pytest.mark.asyncio
async def test_gate_skips_kyc_only_for_crypto_method(env, monkeypatch):
    mk, store = env

    async def enabled(bid):
        return True

    async def check(bid, u, amount):
        return False, "🔒 احراز لازم است"
    monkeypatch.setattr(K, "enabled", enabled)
    monkeypatch.setattr(K, "check_charge", check)
    assert await K.gate(5, 777, 10_000_000, method="crypto") == "🔒 احراز لازم است"      # خاموش ⇒ مثلِ قبل
    store[CX.KEY] = "1"
    assert await K.gate(5, 777, 10_000_000, method="crypto") is None                  # روشن ⇒ رد نمی‌شود
    assert await K.gate(5, 777, 10_000_000) == "🔒 احراز لازم است"                     # کارت/درگاه مثلِ قبل
