"""PATCH_STOCK_GIVE_TO_USER_V1 — دادن دستیِ کانفیگ انبار (آماده/نامحدود) به کاربر."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.vpn import stock_give as SG


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, SG):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        await s.flush()
        s.add(models.BotUser(id=1, bot_id=9, telegram_id=1001, full_name="Ali", balance=0))
        s.add(models.BotUser(id=2, bot_id=9, telegram_id=1002, full_name="Sara", balance=0))
        s.add(models.Config(bot_id=9, code=1, volume_gb=10, days=30, payload="ready-1", status="in_stock"))
        s.add(models.Config(bot_id=9, code=2, volume_gb=0, days=60, payload="unl-1", status="in_stock", category="vip"))
        s.add(models.Config(bot_id=9, code=3, volume_gb=0, days=30, payload="unl-sold", status="sold", owner_user_id=2))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_counts_and_listing_split_ready_and_unlimited(db):
    assert await SG.counts(9) == (1, 1)
    rows, total = await SG.list_stock(9, True, 0)
    assert total == 1 and [c.code for c in rows] == [2]
    rows, total = await SG.list_stock(9, False, 0)
    assert [c.code for c in rows] == [1]
    assert "♾️" in SG.label(rows and (await SG.list_stock(9, True, 0))[0][0]) and "vip" in SG.label((await SG.list_stock(9, True, 0))[0][0])


@pytest.mark.asyncio
async def test_assign_marks_sold_to_user_once(db):
    cfg, err = await SG.assign(9, 1, 1)
    assert cfg and not err and cfg.status == "sold" and cfg.owner_user_id == 1
    cfg2, err2 = await SG.assign(9, 2, 1)                  # دوباره همان کانفیگ ⇒ رد
    assert cfg2 is None and "انبار نیست" in err2
    async with db() as s:
        c = await s.scalar(select(models.Config).where(models.Config.code == 1))
    assert c.owner_user_id == 1
    assert await SG.counts(9) == (0, 1)
    assert (await SG.assign(9, 99, 2))[0] is None          # کاربر ناموجود
    assert (await SG.assign(8, 1, 2))[0] is None           # ربات دیگر


@pytest.mark.asyncio
async def test_ok_handler_sends_config_and_reports(db, monkeypatch):
    sent, out = [], []

    async def fake_send(bot, tg, cfg):
        sent.append((tg, cfg.payload))

    async def fake_row(cb, bot):
        return SimpleNamespace(id=9)
    monkeypatch.setattr(tenant, "send_config_to_user", fake_send)
    monkeypatch.setattr(SG, "_admin_row", fake_row)

    async def answer(*a, **k):
        out.append(("ans", a))

    async def mans(t, **k):
        out.append(t)
    cb = SimpleNamespace(data="sg:ok:2:2", from_user=SimpleNamespace(id=1), message=SimpleNamespace(answer=mans), answer=answer)
    await SG.sg_ok(cb, SimpleNamespace())
    assert sent == [(1002, "unl-1")]
    assert any("ارسال شد" in str(x) for x in out)
    cb2 = SimpleNamespace(data="sg:ok:1:2", from_user=SimpleNamespace(id=1), message=SimpleNamespace(answer=mans), answer=answer)
    await SG.sg_ok(cb2, SimpleNamespace())                  # بار دوم: دیگر در انبار نیست
    assert len(sent) == 1
