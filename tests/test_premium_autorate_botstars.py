"""PATCH_PREMIUM_AUTO_RATE_V1 + PATCH_BOT_STARS_TOPUP_V1"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, smm):
        monkeypatch.setattr(mod, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service",
                         plan="pro", status="active"))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_premium_auto_rate(db, monkeypatch):
    from tenant.gateways import stars_rate

    async def fake(margin, bot_id=None):
        return 3976.0
    monkeypatch.setattr(stars_rate, "star_toman", fake)
    await tenant.set_setting(5, "premium_toman_rate", "1000")
    assert (await smm._premium_settings(5))[0] == 1000.0
    await tenant.set_setting(5, "premium_auto", "1")
    assert (await smm._premium_settings(5))[0] == 3976.0
    text, kb = await smm.premium_admin_content(5)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "sauto:premium" in cbs


@pytest.mark.asyncio
async def test_bot_stars_invoice_payload():
    from tenant.gateways import stars_admin
    sent = {}

    class _B:
        async def send_invoice(self, **kw):
            sent.update(kw)
    row = type("R", (), {"id": 5})()
    await stars_admin._send_bot_topup_invoice(_B(), 1, row, 250)
    assert sent["currency"] == "XTR" and sent["payload"] == "bot_stars:5:250"
    assert sent["prices"][0].amount == 250


def test_admin_buttons_use_bot_topup():
    import inspect
    src = inspect.getsource(smm)
    assert 'افزایشِ موجودیِ استارزِ ربات", callback_data="w:stars"' not in src
    assert src.count('callback_data="w:botstars"') >= 4


@pytest.mark.asyncio
async def test_premium_fragment_price(db, monkeypatch):
    """PATCH_PREMIUM_FRAGMENT_PRICE_V1 — قیمت = دلارِ فرگمنت × نرخِ دلار × (۱+سود)."""
    from tenant.gateways import stars_rate

    async def fake_usd():
        return 100_000.0
    monkeypatch.setattr(stars_rate, "usd_toman", fake_usd)

    async def no_tgt(*a, **k):
        return None
    monkeypatch.setattr(smm, "_tgt_premium_price", no_tgt)
    await tenant.set_setting(5, "premium_auto", "fragment")
    await tenant.set_setting(5, "premium_margin_pct", "10")
    rate, margin = await smm._premium_settings(5)
    p3, custom = await smm._premium_effective_price(5, 3, 1000, rate, margin)
    assert (p3, custom) == (round(11.99 * 100_000 * 1.1), False)
    await tenant.set_setting(5, "premium_frag_usd", '{"3": 12, "6": 16, "12": 29}')
    assert (await smm._premium_effective_price(5, 12, 2500, rate, margin))[0] == round(29 * 100_000 * 1.1)
    await tenant.set_setting(5, "stars_usd_toman", "150000")      # نرخِ دستیِ دلار مقدم است
    assert (await smm._premium_effective_price(5, 3, 1000, rate, margin))[0] == round(12 * 150_000 * 1.1)
    await tenant.set_setting(5, "premium_custom_price_3", "999")  # قیمتِ شخصی همچنان اولویت دارد
    assert await smm._premium_effective_price(5, 3, 1000, rate, margin) == (999, True)
    text, kb = await smm.premium_admin_content(5)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "prm:fragusd" in cbs and "فرگمنت" in text
