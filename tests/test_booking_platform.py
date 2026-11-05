"""PATCH_BOOKING_V2 — ثبت در ربات مادر: فعال (سبز)، فقط حرفه‌ای، دروازهٔ تست (درخواست/اعطایِ چندروزه)."""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_bot as pb
from tenant.bots.booking import gate


@pytest_asyncio.fixture
async def db(monkeypatch):
    import sys
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


def test_booking_is_active_green_with_pitch_and_in_fields():
    assert "booking" in pb.ACTIVE_FIELDS
    keys = [k for k, _ in pb._fields_ordered()]
    assert "booking" in keys
    # فعال‌ها اول، قفل‌ها آخر (همان قاعدهٔ «سبزها بالا، قرمزها آخر»)
    act = [k in pb.ACTIVE_FIELDS for k in keys]
    assert act == sorted(act, reverse=True)
    assert "رزرو" in pb.FIELD_PITCH["booking"] and "پلنِ حرفه‌ای" in pb.FIELD_PITCH["booking"]


@pytest.mark.asyncio
async def test_gate_closed_then_grant_expiry_revoke(db):
    assert gate.OPEN is False
    assert await gate.status(77) == (False, "none")
    await gate.grant(77, 4)
    assert (await gate.status(77))[0] is True and await gate.remaining_days(77) in (4, 5)
    later = dt.datetime.utcnow() + dt.timedelta(days=5)
    assert await gate.status(77, now=later) == (False, "expired")
    await gate.grant(78, 0)                                   # دائمی
    assert (await gate.status(78, now=later))[0] is True and await gate.remaining_days(78) is None
    assert await gate.revoke(77) is True and await gate.status(77) == (False, "none")
    assert set((await gate.grants()).keys()) == {78}


@pytest.mark.asyncio
async def test_choose_plan_rejects_non_pro_and_ungranted_for_booking(db):
    from types import SimpleNamespace
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    st = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=9, user_id=9))
    await st.update_data(field="booking")
    alerts = []

    class Cb:
        data = "newplan:growth"
        from_user = SimpleNamespace(id=9, full_name="x")

        async def answer(self, text="", show_alert=False, **k):
            alerts.append(text)
    await pb.choose_plan(Cb(), st)
    assert "فقط در پلنِ حرفه‌ای" in alerts[-1]
    Cb.data = "newplan:pro"
    await pb.choose_plan(Cb(), st)
    assert "مرحلهٔ تست" in alerts[-1]                          # حرفه‌ای هست ولی دسترسی نگرفته
