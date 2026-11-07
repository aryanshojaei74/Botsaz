"""PATCH_BUILD_RESTART_RESUME_V1 — ویزارد «عملکرد دکمه‌ها» بعد از ری‌استارت (state خالی) از روی DB ادامه پیدا کند."""
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_bot as pb


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    orig = config.Session
    for m in list(sys.modules.values()):
        if getattr(m, "Session", None) is orig:
            monkeypatch.setattr(m, "Session", mk, raising=False)
    monkeypatch.setattr(config, "Session", mk)
    yield mk
    await eng.dispose()


class Msg:
    def __init__(self):
        self.chat = SimpleNamespace(id=5)
        self.message_id = 11
        self.sent = []
        self.bot = None

    async def answer(self, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(chat=self.chat, message_id=99)


class Cb:
    def __init__(self, data, uid=5):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.message = Msg()
        self.alerts = []

    async def answer(self, text=None, **kw):
        self.alerts.append(text)


@pytest.mark.asyncio
async def test_batap_without_state_rebuilds_and_continues(db):
    async with db() as s:
        b = models.Bot(owner_id=5, token="t:1", field="booking", plan="pro")
        s.add(b)
        await s.flush()
        s.add(models.MainButton(bot_id=b.id, row=0, col=0, label="A"))
        s.add(models.MainButton(bot_id=b.id, row=0, col=1, label="B", action="book_show"))
        await s.commit()
        ids = [m.id for m in (await s.execute(models.MainButton.__table__.select())).all()]
    st = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=5, user_id=5))
    assert await st.get_state() is None
    cb = Cb(f"batap:{ids[0]}")
    await pb.build_resume_after_restart(cb, st)
    assert await st.get_state() == pb.Build.action.state
    d = await st.get_data()
    assert d["grid_field"] == "booking" and d["cur_btn_id"] == ids[0]
    assert [x["action_set"] for x in d["assign"]] == [False, True]
    assert any("چه اتفاقی" in t for t in cb.message.sent)       # سؤالِ عملکردِ دکمه نشان داده شد


@pytest.mark.asyncio
async def test_foreign_or_missing_bot_gets_expired_alert(db):
    st = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=7, user_id=7))
    cb = Cb("bafin", uid=7)
    await pb.build_resume_after_restart(cb, st)
    assert cb.alerts and "منقضی" in cb.alerts[-1]
    assert await st.get_state() is None
