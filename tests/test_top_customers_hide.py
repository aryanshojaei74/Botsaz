"""حذف افراد از رتبه‌بندیِ «نفراتِ برتر» (PATCH_TOP_CUSTOMERS_HIDE_V1)."""
import types

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
import tenant.shared.top_customers as TC


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(TC, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=5, owner_id=100, token="5:T", username="x", field="digital_shop", plan="pro", status="active"))
        for i, (tg, nm, amt) in enumerate(((100, "ADMIN", 103_000_000), (200, "Hosein", 549_000), (300, "ailia", 119_000)), start=1):
            s.add(models.BotUser(id=i, bot_id=5, telegram_id=tg, full_name=nm, username=nm.lower()))
            s.add(models.Order(bot_id=5, user_id=i, volume_gb=10, days=30, amount=amt, pay_method="card", status="paid"))
        await s.commit()
    yield mk
    await engine.dispose()


@pytest.mark.asyncio
async def test_hidden_users_leave_ranking_and_discount_rank(maker, monkeypatch):
    assert [u.telegram_id for u, _ in await TC.top_buyers(5)] == [100, 200, 300]
    await TC.set_hidden(5, [100])
    assert [u.telegram_id for u, _ in await TC.top_buyers(5)] == [200, 300]
    assert [u.telegram_id for u, _ in await TC.top_ranked(5, 5)] == [200, 300]
    await TC._tset(5, "tc_enabled", "1")
    assert await TC.get_user_rank(5, 100) is None and await TC.get_user_rank(5, 200) == 1     # رتبهٔ تخفیف هم بدونِ او
    text = await TC._build_main_text(5, 100)
    assert "ADMIN" not in text and "Hosein" in text and "خارج از رتبه‌بندی" in text
    await TC.set_hidden(5, [])
    assert [u.telegram_id for u, _ in await TC.top_buyers(5)] == [100, 200, 300]


@pytest.mark.asyncio
async def test_hide_flows_me_add_by_username_and_restore(maker, monkeypatch):
    row = types.SimpleNamespace(id=5, owner_id=100)

    async def owner_ok(cb, bot):
        return row

    async def owner_ok_msg(msg, bot):
        return row
    monkeypatch.setattr(tenant, "_owner_ok", owner_ok)
    monkeypatch.setattr(tenant, "_owner_ok_msg", owner_ok_msg)
    shown = {}

    async def eor(m, text, reply_markup=None, parse_mode="HTML"):
        shown["text"], shown["kb"] = text, reply_markup
    monkeypatch.setattr(tenant, "edit_or_replace", eor)

    class CB:
        data = "tc:hid_me"
        message = object()
        async def answer(self, *a, **k): pass
    await TC.tc_hid_me(CB(), None)
    assert await TC.hidden_ids(5) == [100] and "ADMIN" in shown["text"]
    cbs = [b.callback_data for r in shown["kb"].inline_keyboard for b in r]
    assert "tc:hid_me" not in cbs and "tc:hid_del:100" in cbs and "tc:hid_add" in cbs
    # افزودن با @یوزرنیم
    out = {}

    class State:
        async def clear(self): out["cleared"] = True
    class Msg:
        text = "@Hosein"
        async def answer(self, text, **k): out["t"] = text
    await TC.tc_hid_add_save(Msg(), State(), None)
    assert await TC.hidden_ids(5) == [100, 200] and out["cleared"]
    class Msg2:
        text = "@nobody"
        async def answer(self, text, **k): out["bad"] = text
    await TC.tc_hid_add_save(Msg2(), State(), None)
    assert "پیدا نشد" in out["bad"] and await TC.hidden_ids(5) == [100, 200]
    # برگرداندن
    class CB2(CB):
        data = "tc:hid_del:100"
    await TC.tc_hid_del(CB2(), None)
    assert await TC.hidden_ids(5) == [200]
