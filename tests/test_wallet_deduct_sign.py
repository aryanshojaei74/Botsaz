"""#855 — «➖ کسر کیف پول» باید موجودی را کم کند."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant


class _Msg:
    def __init__(self, text=None):
        self.text = text
        self.out = []

    async def answer(self, text, **k):
        self.out.append(text)

    async def edit_reply_markup(self, **k):
        return None


class _Bot:
    id = 1

    async def send_message(self, *a, **k):
        return None


class _Cb:
    def __init__(self, data):
        self.data = data
        self.message = _Msg()

    async def answer(self, *a, **k):
        return None


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="b", field="vpn_sales",
                         plan="pro", status="active"))
        s.add(models.BotUser(bot_id=7, telegram_id=99, balance=Decimal(300)))
        await s.commit()
    row = SimpleNamespace(id=7, owner_id=1, field="vpn_sales", plan="pro")

    async def get_bot_row(_b):
        return row

    async def owner_ok(cb, bot):
        return row

    async def tkb(_i):
        return None
    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    monkeypatch.setattr(tenant, "_owner_ok", owner_ok)
    monkeypatch.setattr(tenant, "tadmin_kb", tkb)
    monkeypatch.setattr(tenant, "Message", _Msg)
    monkeypatch.setattr(tenant, "smart_cancel_kb", lambda b: asyncio.sleep(0))
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))
    yield maker, state
    await engine.dispose()


async def _bal(maker):
    async with maker() as s:
        return (await s.get(models.BotUser, 1)).balance


@pytest.mark.asyncio
async def test_deduct_with_reason(env):
    maker, state = env
    await tenant.admin_user_action(_Cb("usr:sub:99"), state, _Bot())
    await tenant.admin_adjust(_Msg("200"), state, _Bot())
    await tenant.admin_adjust_deduct_reason(_Msg("test"), state, _Bot())
    assert await _bal(maker) == 100


@pytest.mark.asyncio
async def test_deduct_skip_reason(env):
    maker, state = env
    await tenant.admin_user_action(_Cb("usr:sub:99"), state, _Bot())
    await tenant.admin_adjust(_Msg("200"), state, _Bot())
    await tenant.admin_adjust_deduct_skip(_Cb("adjdeduct:skip"), state, _Bot())
    assert await _bal(maker) == 100


@pytest.mark.asyncio
async def test_add(env):
    maker, state = env
    await tenant.admin_user_action(_Cb("usr:add:99"), state, _Bot())
    await tenant.admin_adjust(_Msg("200"), state, _Bot())
    assert await _bal(maker) == 500


@pytest.mark.asyncio
async def test_result_message_states_direction_and_old_new(env):
    maker, state = env
    m = _Msg("200")
    await tenant.admin_user_action(_Cb("usr:sub:99"), state, _Bot())
    await tenant.admin_adjust(m, state, _Bot())
    r = _Msg("x")
    await tenant.admin_adjust_deduct_reason(r, state, _Bot())
    out = "\n".join(r.out)
    assert "کسر شد" in out and "300" in out and "100" in out
