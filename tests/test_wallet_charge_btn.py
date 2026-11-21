"""#159 (Jahan Service): تیکِ «شارژ کیف‌پول در صفحهٔ خرید» روشن است ولی دکمه‌ای دیده نمی‌شود."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:T", username="x", field="vpn_sales", plan="pro", status="active")
        s.add(b); await s.commit()
        bid = b.id
    yield bid
    await engine.dispose()


def _cbs(rows):
    return [b.callback_data for r in rows for b in r]


@pytest.mark.asyncio
async def test_toggle_on_adds_generic_charge_button_even_without_crypto_or_swap(env):
    bid = env
    await tenant.set_setting(bid, "wallet_charge_in_purchase_enabled", "1")
    await tenant.set_setting(bid, "card_enabled", "1")
    await tenant.set_setting(bid, "card_number", "6219861912818753")
    kb = await tenant.pay_method_kb_full(bid, 5)
    cbs = _cbs(kb.inline_keyboard)
    assert "w:back" in cbs and cbs.count("w:back") == 1
    assert cbs[-1] == "pay:cancel:5"                       # دکمهٔ لغو همچنان آخر است
    assert "pay:card:5" in cbs


@pytest.mark.asyncio
async def test_toggle_off_adds_nothing(env):
    bid = env
    kb = await tenant.pay_method_kb_full(bid, 5)
    assert "w:back" not in _cbs(kb.inline_keyboard)
    assert await tenant.wallet_charge_btn_rows(bid) == []


@pytest.mark.asyncio
async def test_helper_dedupes(env):
    bid = env
    await tenant.set_setting(bid, "wallet_charge_in_purchase_enabled", "1")
    rows = await tenant.wallet_charge_btn_rows(bid)
    assert tenant._has_wallet_charge_btn(rows) and len(rows) == 1
    assert not tenant._has_wallet_charge_btn([])
