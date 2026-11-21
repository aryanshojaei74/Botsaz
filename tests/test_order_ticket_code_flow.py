"""PATCH_ORDER_TICKET_FLOW_V3 — تیکتِ «پیگیریِ سفارش»: کد ← متن ← تیکت با مشخصاتِ سفارش (Fadayi)."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm
from tenant.shared import support_ticket as st


class _Msg:
    def __init__(self, text, uid=200):
        self.text = text
        self.from_user = SimpleNamespace(id=uid)
        self.out = []

    async def answer(self, text, **k):
        self.out.append(text)


class _Cb:
    def __init__(self, data, uid=200):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.message = _Msg("x")
        self.message.edit_reply_markup = self._noop

    async def _noop(self, **k):
        return None

    async def answer(self, *a, **k):
        return None


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for m in (config, st, smm, tenant):
        monkeypatch.setattr(m, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=3, owner_id=1, token="3:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(bot_id=3, telegram_id=200, balance=Decimal(0)))
        await s.commit()
    row = SimpleNamespace(id=3, owner_id=1, field="virtual_service")

    async def gbr(_b):
        return row

    async def kb(*a, **k):
        return None
    notified = []

    async def notify(bot, r, text, kb2, photo_file_id=None):
        notified.append(text)
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "main_kb_or_remove", kb)
    monkeypatch.setattr(tenant, "smart_cancel_kb", kb)
    monkeypatch.setattr(tenant, "_notify_admins", notify)

    async def det(bot_id, trk):
        return "ORDER-DETAILS-" + trk if trk == "AB12" else ""
    monkeypatch.setattr(smm, "_order_details_html", det)
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=200))
    yield mk, state, notified
    await eng.dispose()


@pytest.mark.asyncio
async def test_code_then_text_creates_ticket_with_order_details(env):
    mk, state, notified = env
    await st.ticket_new_category_pick(_Cb("ticket:cat:order"), object(), state)
    assert await state.get_state() == st.TicketFSM.order_code.state
    bad = _Msg("zzzz")
    await st.ticket_order_code_in(bad, object(), state)
    assert "پیدا نشد" in bad.out[-1] and await state.get_state() == st.TicketFSM.order_code.state
    ok = _Msg("ab12")                                    # حروفِ کوچک هم قبول
    await st.ticket_order_code_in(ok, object(), state)
    assert await state.get_state() == st.TicketFSM.new_text.state
    await st.ticket_new(_Msg("بوست نیومد"), state, object())
    async with mk() as s:
        t = await s.scalar(select(models.TenantTicket))
    assert t.category == "order" and t.subject == "پیگیریِ سفارش AB12"
    assert "بوست نیومد" in t.text and "ORDER-DETAILS-AB12" in t.text and "AB12" in t.text
    assert notified and "ORDER-DETAILS-AB12" in notified[0]


@pytest.mark.asyncio
async def test_no_code_falls_back_to_old_flow(env):
    mk, state, _ = env
    await st.ticket_new_category_pick(_Cb("ticket:cat:order"), object(), state)
    await st.ticket_order_nocode(_Cb("ticket:nocode"), object(), state)
    assert await state.get_state() == st.TicketFSM.new_subject.state


@pytest.mark.asyncio
async def test_other_categories_unchanged(env):
    mk, state, _ = env
    await st.ticket_new_category_pick(_Cb("ticket:cat:billing"), object(), state)
    assert await state.get_state() == st.TicketFSM.new_subject.state
