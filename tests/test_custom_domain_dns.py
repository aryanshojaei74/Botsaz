"""دامنهٔ اختصاصی: مقدارهایِ دقیقِ DNS (آی‌پی/CNAME) در خودِ ربات نشان داده می‌شود (PATCH_CDOM_DNS_TARGET_V1)."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.gateways import custom_domain as cd


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    import tenant
    from tenant.shared import public_api as _pa
    for mod in (cd, tenant, _pa):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    cd._DNS_TARGET_CACHE.update({"t": -1e9, "v": ("", [])})
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:T", username="shop", field="virtual_service", plan="pro", status="active")
        s.add(b); await s.flush()
        s.add(models.Setting(bot_id=0, key="webhook_base", value="https://panel.botsaz.example"))
        s.add(models.BotCustomDomain(bot_id=b.id, domain="api.fadayi.com", verify_token="tok123", status="pending"))
        await s.commit()
        bid = b.id
    import asyncio

    async def fake_getaddrinfo(self, host, port, **kw):
        assert host == "panel.botsaz.example"
        return [(2, 1, 6, "", ("188.132.174.150", 0))]
    monkeypatch.setattr(asyncio.get_event_loop_policy().get_event_loop().__class__, "getaddrinfo", fake_getaddrinfo, raising=False)
    return bid, maker


@pytest.mark.asyncio
async def test_pending_domain_shows_exact_a_and_cname_values(env):
    bid, _ = env
    text, _kb = await cd._domain_content(bid, "apikey")
    assert "188.132.174.150" in text                # مقدارِ رکوردِ A
    assert "panel.botsaz.example" in text           # مقدارِ CNAME
    assert "botsaz-verify=tok123" in text           # رکوردِ تأیید
    assert "از پشتیبانی آدرسِ دقیقِ CNAME/آی‌پی را بپرس" not in text
    assert "Proxy" in text


@pytest.mark.asyncio
async def test_apikey_page_shows_dns_values_before_domain_is_verified(env):
    bid, _ = env
    text, _kb = await cd._apikey_content(bid)
    assert "188.132.174.150" in text and "مقدارهایِ DNS" in text


@pytest.mark.asyncio
async def test_platform_owner_can_override_ip(env):
    bid, maker = env
    async with maker() as s:
        s.add(models.Setting(bot_id=0, key="custom_domain_ip", value="203.0.113.7"))
        await s.commit()
    cd._DNS_TARGET_CACHE.update({"t": -1e9, "v": ("", [])})
    host, ips = await cd.get_dns_target()
    assert ips == ["203.0.113.7"] and host == "panel.botsaz.example"
