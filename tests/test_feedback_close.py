"""PATCH_FB_CLOSE_V1 — بستنِ تیکتِ نظر/پیشنهاد: دکمه برایِ مدیر و کاربر + توضیح (از پاسخِ دوم اخطار)."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_bot as PB


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))


def _cb(data, uid):
    calls = {"alert": None, "edited": 0}

    async def answer(t="", show_alert=False, **k):
        calls["alert"] = (t, show_alert)

    async def edit_rm(**k):
        calls["edited"] += 1
    return NS(data=data, from_user=NS(id=uid), answer=answer, message=NS(edit_reply_markup=edit_rm)), calls


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(PB, "Session", mk)
    monkeypatch.setattr(PB, "PLATFORM_ADMIN_ID", 1)
    async with mk() as s:
        s.add(models.PlatformFeedback(id=180, user_telegram_id=555, user_name="Sajad", fb_type="suggest", text="x"))
        await s.commit()
    yield
    await eng.dispose()


def test_hint_is_plain_first_then_warning_from_second_reply():
    assert "⚠️" not in PB._fb_close_hint(1) and "ℹ️" in PB._fb_close_hint(1)
    assert "⚠️" in PB._fb_close_hint(2) and "⚠️" in PB._fb_close_hint(5)


@pytest.mark.asyncio
async def test_user_can_close_only_own_ticket_and_admin_is_told(env):
    b = FakeBot()
    cb, calls = _cb("ufb:close:180", 999)                      # کاربرِ دیگر
    await PB.ufb_close(cb, b)
    assert calls["alert"][1] and b.sent == []
    cb, calls = _cb("ufb:close:180", 555)
    await PB.ufb_close(cb, b)
    assert await PB.get_pset("fbc:180", "") == "1" and calls["edited"] == 1
    assert b.sent and b.sent[0][0] == 1 and "180" in b.sent[0][1]
    cb, calls = _cb("ufb:close:180", 555)                      # بار دوم: «قبلاً بسته شده»
    await PB.ufb_close(cb, b)
    assert "قبلاً" in calls["alert"][0]


@pytest.mark.asyncio
async def test_admin_close_notifies_user_and_non_admin_denied(env):
    b = FakeBot()
    cb, calls = _cb("afb:close:180", 42)
    await PB.afb_close(cb, b)
    assert calls["alert"][1] and await PB.get_pset("fbc:180", "") == ""
    cb, calls = _cb("afb:close:180", 1)
    await PB.afb_close(cb, b)
    assert await PB.get_pset("fbc:180", "") == "1" and b.sent[0][0] == 555


@pytest.mark.asyncio
async def test_reply_counter_increments(env):
    assert await PB._fb_bump_replies(180) == 1 and await PB._fb_bump_replies(180) == 2
