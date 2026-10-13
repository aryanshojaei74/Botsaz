"""PATCH_PLATFORM_USER_DISCOUNT_V1 — تخفیفِ دائمیِ اختصاصیِ کاربر در رباتِ مادر."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_user_discounts as UD


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, UD):
        monkeypatch.setattr(mod, "Session", maker)
    monkeypatch.setattr(UD, "PLATFORM_ADMIN_ID", 1)
    async with maker() as s:
        s.add(models.PlatformUser(telegram_id=55, full_name="Ali", username="ali", balance=0))
        await s.commit()
    yield maker
    await engine.dispose()


def test_parse_percent():
    assert UD.parse_percent("۲۰٪") == 20 and UD.parse_percent("0") == 0 and UD.parse_percent("90") == 90
    assert UD.parse_percent("91") is None and UD.parse_percent("abc") is None and UD.parse_percent("-5") is None


@pytest.mark.asyncio
async def test_set_update_remove_and_active_flag(db):
    assert await UD.percent_for(55) == 0
    assert await UD.set_discount(55, 15) == "created" and await UD.percent_for(55) == 15
    assert await UD.set_discount(55, 25) == "updated" and await UD.percent_for(55) == 25
    async with db() as s:
        d = await s.scalar(select(models.PlatformUserDiscount))
        d.active = False
        await s.commit()
    assert await UD.percent_for(55) == 0                       # خاموش = اثری ندارد
    assert await UD.set_discount(55, 30) == "updated" and await UD.percent_for(55) == 30   # دوباره روشن
    assert await UD.set_discount(55, 0) == "removed" and await UD.percent_for(55) == 0
    assert await UD.set_discount(55, 0) == "none"


@pytest.mark.asyncio
async def test_admin_flow_creates_discount_and_notifies_user(db):
    sent = []

    class Bot:
        async def send_message(self, chat, text, **k):
            sent.append((chat, text))

    class St:
        def __init__(self):
            self.d = {}

        async def update_data(self, **k):
            self.d.update(k)

        async def get_data(self):
            return dict(self.d)

        async def set_state(self, s):
            pass

        async def clear(self):
            self.d = {}

    class Msg:
        def __init__(self, text, uid=1):
            self.text, self.from_user, self.bot, self.out = text, SimpleNamespace(id=uid), Bot(), []

        async def answer(self, text, **k):
            self.out.append(text)
    st = St()
    m1 = Msg("۵۵")
    await UD.ud_user(m1, st)
    assert "Ali" in m1.out[0] and st.d["ud_uid"] == 55
    m2 = Msg("20")
    await UD.ud_percent(m2, st)
    assert await UD.percent_for(55) == 20 and "20" in m2.out[0]
    assert sent and sent[0][0] == 55 and "20" in sent[0][1]     # به کاربر خبر می‌رسد
    # غیرِ مدیر هیچ اثری ندارد
    await UD.ud_user(Msg("56", uid=999), st)
    m3 = Msg("0")
    st.d = {"ud_uid": 55}
    await UD.ud_percent(m3, st)
    assert await UD.percent_for(55) == 0
