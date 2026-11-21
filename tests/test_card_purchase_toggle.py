"""فدایی: گزینهٔ خاموش‌کردنِ پرداختِ فاکتور با کارت‌به‌کارت، کنارِ دو تیکِ کیف پول (PATCH_CARD_PURCHASE_TOGGLE_V1)."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import vs_pay as VP

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, VP):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:T", username="x", field="virtual_service", plan="pro", status="active")
        s.add(b)
        await s.commit()
        bid = b.id
    yield bid
    await engine.dispose()


async def test_finance_menu_shows_the_toggle_with_current_state(env):
    bid = env
    opts = dict((cb, label) for label, cb in await tenant._sec_finance_opts(bid, "pro"))
    assert "✅ فعال" in opts["set:card_purchase_toggle"]
    await tenant.set_setting(bid, "card_purchase_enabled", "0")
    opts = dict((cb, label) for label, cb in await tenant._sec_finance_opts(bid, "pro"))
    assert "❌ غیرفعال" in opts["set:card_purchase_toggle"]


async def test_toggle_flips_setting_and_hides_card_button_on_invoices(env, monkeypatch):
    bid = env
    refreshed = []

    async def owner_ok(_cb, _bot):
        return SimpleNamespace(id=bid, plan="pro")

    async def refresh(_cb, _row):
        refreshed.append(1)
    monkeypatch.setattr(tenant, "_owner_ok", owner_ok)
    monkeypatch.setattr(tenant, "_refresh_finance_toggle_view", refresh)

    class Cb:
        data = "set:card_purchase_toggle"

        async def answer(self, *a, **k):
            pass
    assert [b.callback_data for r in await VP.pay_rows(bid, 5, 1000, "p", "c") for b in r] == ["p", "c"]
    await tenant.set_card_purchase_toggle(Cb(), object())
    assert await tenant.get_setting(bid, "card_purchase_enabled", "1") == "0" and refreshed
    assert [b.callback_data for r in await VP.pay_rows(bid, 5, 1000, "p", "c") for b in r] == ["p"]
    await tenant.set_card_purchase_toggle(Cb(), object())
    assert await tenant.get_setting(bid, "card_purchase_enabled", "1") == "1"


async def test_vpn_order_keyboard_also_follows_the_toggle(env):
    bid = env
    await tenant.set_setting(bid, "card_enabled", "1")
    await tenant.set_setting(bid, "card_number", "6219861912818753")
    assert "pay:card:5" in [b.callback_data for r in (await tenant.pay_method_kb_full(bid, 5)).inline_keyboard for b in r]
    await tenant.set_setting(bid, "card_purchase_enabled", "0")
    assert "pay:card:5" not in [b.callback_data for r in (await tenant.pay_method_kb_full(bid, 5)).inline_keyboard for b in r]
