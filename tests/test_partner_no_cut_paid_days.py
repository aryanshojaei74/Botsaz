"""PATCH_PARTNER_NO_CUT_PAID_DAYS_V1 — تیکت #۸۳۲: همکار نمی‌تواند روزهایِ پولیِ مشتری را کم کند."""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.partner_bot import partner_customers as pc


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, pc):
        monkeypatch.setattr(mod, "Session", maker)
    exp = dt.datetime.now() + dt.timedelta(days=12)
    async with maker() as s:
        s.add(models.Bot(id=659, owner_id=8343133232, token="1:T", username="DLStarsbot",
                         field="virtual_service", plan="pro", status="active", plan_expires_at=exp))
        await s.commit()
    yield maker, exp
    await engine.dispose()


class _Cb:
    def __init__(self, data):
        self.data = data
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


@pytest.mark.asyncio
async def test_reseller_cannot_cut_days_customer_paid_for(db, monkeypatch):
    maker, exp = db

    async def ok(cb, bot):
        return None, type("PB", (), {"id": 7})()

    async def owned(pb_id, bot_id):
        return True

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(pc, "_partner_ok", ok)
    monkeypatch.setattr(pc, "_owned_sale", owned)
    monkeypatch.setattr(pc, "_show_bot_card", noop)
    # بدونِ خریدِ مستقیم: کم‌کردن مثلِ قبل مجاز است
    cb = _Cb("pcu:d:659:-7")
    await pc.pcu_days(cb, None)
    async with maker() as s:
        assert (await s.get(models.Bot, 659)).plan_expires_at < exp - dt.timedelta(days=6)
        b = await s.get(models.Bot, 659)
        b.plan_expires_at = exp
        s.add(models.PlanOrder(user_telegram_id=8343133232, target_bot_id=659, plan="pro", amount=500000))
        await s.commit()
    # مشتری از پلتفرم خریده → «➖ ۷ روز» رد می‌شود و سررسید دست نمی‌خورد
    cb2 = _Cb("pcu:d:659:-7")
    await pc.pcu_days(cb2, None)
    assert "مستقیم از بات‌ساز" in (cb2.alerts[-1] or "")
    async with maker() as s:
        assert (await s.get(models.Bot, 659)).plan_expires_at == exp
    # افزودن همچنان مجاز است
    await pc.pcu_days(_Cb("pcu:d:659:7"), None)
    async with maker() as s:
        assert (await s.get(models.Bot, 659)).plan_expires_at > exp
