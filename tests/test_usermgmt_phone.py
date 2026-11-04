"""PATCH_USERMGMT_PHONE_V1 (پیشنهاد #193) — شمارهٔ کاربر در «مدیریت کاربر» برایِ ادمین/مالک دیده شود."""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant as T


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for m in (config, T):
        monkeypatch.setattr(m, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(bot_id=5, telegram_id=900, balance=Decimal(0), phone_number="989121234567", phone_verified=True))
        s.add(models.BotUser(bot_id=5, telegram_id=901, balance=Decimal(0)))
        await s.commit()
        row = await s.get(models.Bot, 5)

    async def _row(bot):
        return row
    monkeypatch.setattr(T, "get_bot_row", _row)
    yield
    await eng.dispose()


class _Tgt:
    def __init__(self):
        self.text = ""

    async def answer(self, text, **k):
        self.text = text


@pytest.mark.asyncio
async def test_user_card_shows_verified_phone(env):
    t = _Tgt()
    await T._render_user_detail(t, None, 900)
    assert "+989121234567" in t.text and "تاییدشده" in t.text


@pytest.mark.asyncio
async def test_user_card_without_phone_has_no_phone_line_when_gate_off(env):
    t = _Tgt()
    await T._render_user_detail(t, None, 901)
    assert "شماره" not in t.text
