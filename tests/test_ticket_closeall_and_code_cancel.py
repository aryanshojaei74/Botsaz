"""بستنِ همهٔ تیکت‌ها (مالک) + انصرافِ ورودِ کدِ شارژ به صفحهٔ کیف‌پول برمی‌گردد."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import support_ticket as ST
from tenant.shared import wallet_codes as WC


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, ST, WC):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=1, owner_id=500, token="1:T", username="a", field="virtual_service", plan="pro", status="active"))
        s.add(models.Bot(id=2, owner_id=600, token="2:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=1, telegram_id=11, full_name="A"))
        s.add(models.BotUser(id=2, bot_id=2, telegram_id=22, full_name="B"))
        for i, (bot, uid, st) in enumerate([(1, 1, "open"), (1, 1, "answered"), (1, 1, "closed"), (2, 2, "open")]):
            s.add(models.TenantTicket(bot_id=bot, user_id=uid, text=f"t{i}", status=st))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_close_all_closes_only_this_bots_unclosed_tickets(env):
    n = await ST.close_all_tickets(1)
    assert n == 2
    async with env() as s:
        by = {(t.bot_id, t.text): t.status for t in (await s.scalars(select(models.TenantTicket))).all()}
    assert by[(1, "t0")] == by[(1, "t1")] == by[(1, "t2")] == "closed"
    assert by[(2, "t3")] == "open"                         # رباتِ دیگر دست‌نخورده
    assert await ST.close_all_tickets(1) == 0


@pytest.mark.asyncio
async def test_code_prompt_cancel_deletes_prompt_and_returns_to_wallet(env, monkeypatch):
    shown, deleted = [], []

    async def gbr(bot):
        return NS(id=1, owner_id=500)

    async def gcu(bot_id, uid, name="", uname=""):
        return NS(id=1)

    async def sw(msg, bot_id, u, header=""):
        shown.append(bot_id)
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "get_or_create_user", gcu)
    monkeypatch.setattr(tenant, "show_wallet", sw)

    class M:
        async def delete(self):
            deleted.append(1)

    class Cb:
        from_user = NS(id=11, full_name="A", username="a")
        message = M()
        alerts = []

        async def answer(self, text="", **k):
            self.alerts.append(text)

    class St:
        cleared = False

        async def clear(self):
            St.cleared = True
    await WC.cancel(Cb(), None, St())
    assert St.cleared and deleted == [1] and shown == [1]
