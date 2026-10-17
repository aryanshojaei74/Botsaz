"""PATCH_BROADCAST_DURABLE_V1 — ارسالِ همگانی بعد از ری‌استارت از همان‌جا ادامه می‌دهد."""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import broadcast_runner as br


class FakeBot:
    sent: list = []
    edits: list = []
    fail_at: int | None = None

    def __init__(self, *a, **k):
        class _S:
            async def close(self_inner):
                pass
        self.session = _S()

    async def send_message(self, chat_id, text, **kw):
        if FakeBot.fail_at is not None and len(FakeBot.sent) == FakeBot.fail_at:
            raise RuntimeError("process died")   # شبیه‌سازیِ مردنِ پروسه وسطِ کار
        FakeBot.sent.append(chat_id)

    async def edit_message_text(self, text, **kw):
        FakeBot.edits.append(text)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, br):
        monkeypatch.setattr(mod, "Session", maker)
    monkeypatch.setattr(br, "_make_bot", lambda token: FakeBot())
    monkeypatch.setattr(br, "_SEND_GAP", 0)
    monkeypatch.setattr(br, "_SAVE_EVERY", 3)
    FakeBot.sent, FakeBot.edits, FakeBot.fail_at = [], [], None
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="vpn_sales",
                         plan="pro", status="active"))
        for i in range(10):
            s.add(models.BotUser(id=100 + i, bot_id=5, telegram_id=1000 + i, full_name="u", balance=0))
        s.add(models.Broadcast(id=1, bot_id=5, text="سلام", status="sending", chat_id=1, msg_id=9,
                               cursor_uid=0, total_count=10))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_sends_everyone_and_finishes(db):
    await br.run_broadcast(1)
    assert FakeBot.sent == [1000 + i for i in range(10)]
    async with db() as s:
        bc = await s.get(models.Broadcast, 1)
    assert (bc.status, bc.sent_count, bc.failed_count, bc.cursor_uid) == ("done", 10, 0, 109)
    assert "تمام شد" in FakeBot.edits[-1]


@pytest.mark.asyncio
async def test_resumes_after_crash_without_duplicates(db, monkeypatch):
    # اجرایِ اول بعد از ۴ نفر «می‌میرد»: ذخیرهٔ آخر روی نفرِ سوم بوده
    async def boom(*a, **k):
        raise SystemExit
    orig_save = br._save
    calls = {"n": 0}

    async def save_then_die(bc_id, cursor, sent, failed):
        calls["n"] += 1
        st = await orig_save(bc_id, cursor, sent, failed)
        if calls["n"] == 1:
            raise RuntimeError("worker killed")
        return st
    monkeypatch.setattr(br, "_save", save_then_die)
    await br.run_broadcast(1)
    assert FakeBot.sent == [1000, 1001, 1002]
    monkeypatch.setattr(br, "_save", orig_save)
    # تا ضربان کهنه نشده، اجراکنندهٔ دوم نمی‌تواند ادعا کند
    await br.run_broadcast(1)
    assert FakeBot.sent == [1000, 1001, 1002]
    async with db() as s:
        bc = await s.get(models.Broadcast, 1)
        bc.updated_at = dt.datetime.utcnow() - dt.timedelta(minutes=5)
        await s.commit()
    await br.run_broadcast(1)
    assert FakeBot.sent == [1000 + i for i in range(10)]          # از نفرِ چهارم ادامه، بدونِ تکرار
    async with db() as s:
        bc = await s.get(models.Broadcast, 1)
    assert bc.status == "done" and bc.sent_count == 10


@pytest.mark.asyncio
async def test_cancel_stops(db):
    async with db() as s:
        (await s.get(models.Broadcast, 1)).status = "cancelled"
        await s.commit()
    await br.run_broadcast(1)
    assert FakeBot.sent == []


@pytest.mark.asyncio
async def test_legacy_stuck_rows_marked_interrupted_and_owner_told(db):
    async with db() as s:
        s.add(models.Broadcast(id=2, bot_id=5, text="old", status="sending",
                               created_at=dt.datetime.utcnow()))
        await s.commit()
    await br._mark_legacy_interrupted()
    async with db() as s:
        assert (await s.get(models.Broadcast, 2)).status == "interrupted"
        assert (await s.get(models.Broadcast, 1)).status == "sending"
    assert FakeBot.sent == [1]     # پیام به مالک (owner_id=1)
