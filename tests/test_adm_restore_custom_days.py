"""PATCH_ADM_RESTORE_CUSTOM_DAYS_V1 — بازگردانی از بایگانی با «N روز» (به‌جایِ یک دورهٔ کاملِ ۱ ماهه)."""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_bot as pb


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **k):
        self.sent.append((chat_id, text))


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for m in (config, pb):
        monkeypatch.setattr(m, "Session", mk)
    import tenant_status_bus

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(tenant_status_bus, "publish_tenant_invalidate", _noop)
    monkeypatch.setattr(pb, "set_tenant_webhook", _noop)
    async with mk() as s:
        s.add(models.Bot(id=7, owner_id=42, token="7:T", username="zonix", field="virtual_service", plan="pro",
                         status="locked", archived_at=dt.datetime.now() - dt.timedelta(days=3), archive_reason="expired",
                         plan_expires_at=dt.datetime.now() - dt.timedelta(days=5)))
        await s.commit()
    yield mk
    await eng.dispose()


@pytest.mark.asyncio
async def test_restore_with_custom_days_gives_exactly_those_days(db):
    bot = _Bot()
    ok, err, was = await pb._admin_resume_core(bot, 7, custom_days=2)
    assert ok and err is None and was is True
    async with db() as s:
        r = await s.get(models.Bot, 7)
    assert r.status == "active" and r.archived_at is None
    left = r.plan_expires_at - dt.datetime.now()
    assert dt.timedelta(days=1, hours=23) < left < dt.timedelta(days=2, minutes=1)
    assert bot.sent and bot.sent[0][0] == 42                    # مالک خبردار شد


@pytest.mark.asyncio
async def test_plain_restore_still_gives_a_full_period(db):
    ok, _e, was = await pb._admin_resume_core(_Bot(), 7)
    async with db() as s:
        r = await s.get(models.Bot, 7)
    assert ok and was and (r.plan_expires_at - dt.datetime.now()) > dt.timedelta(days=20)


@pytest.mark.asyncio
async def test_restore_unknown_bot_and_days_command_flow(db, monkeypatch):
    ok, err, _ = await pb._admin_resume_core(_Bot(), 999)
    assert not ok and err
    # مسیرِ ادمین: abotdays:restore → عدد → همان نتیجه
    from types import SimpleNamespace
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.fsm.storage.base import StorageKey

    async def _perm(*a, **k):
        return True
    monkeypatch.setattr(pb, "has_perm", _perm)

    async def _kb(*a, **k):
        return None
    monkeypatch.setattr(pb, "smart_home_kb", _kb)

    async def _built(bot_id, src="bots", viewer_id=None):
        return None
    monkeypatch.setattr(pb, "_build_bot_detail", _built)
    st = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=5, user_id=5))
    await st.update_data(op="restore", bot_id=7, src="bots", unit="days")
    answers = []

    class _Msg:
        text = "2"
        from_user = SimpleNamespace(id=5)
        bot = _Bot()

        async def answer(self, text, **k):
            answers.append(text)
    await pb.adm_bot_days_apply(_Msg(), st)
    async with db() as s:
        r = await s.get(models.Bot, 7)
    assert r.status == "active" and (r.plan_expires_at - dt.datetime.now()) < dt.timedelta(days=3)
    assert any("2 روز" in a for a in answers)
