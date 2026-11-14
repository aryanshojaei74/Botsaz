"""PATCH_GIFT_VIA_FIRST_V1 / PATCH_TRUST_GIFT_COUNT_SUFFIX_V1."""
import types

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.virtual_service import smm


class _Target:
    def __init__(self):
        self.sent = []

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, smm):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.TelegramGiftConfig(bot_id=5, gift_id="g1", star_count=50, emoji="🧸", active=True))
        await s.commit()
    yield maker
    await engine.dispose()


def _cbs(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_dest_buttons_carry_via_and_support_text(db):
    t = _Target()
    await smm._gift_ask_dest(t, 5, "g1", 2, via="s")
    text, kb = t.sent[0]
    assert "g1:2:s" in " ".join(_cbs(kb))
    assert "هر آیدی" in text and "حداقل یک‌بار" not in text


@pytest.mark.asyncio
async def test_dest_bot_mode_explains_unstarted_goes_manual(db):
    t = _Target()
    await smm._gift_ask_dest(t, 5, "g1", 1, via="b")
    text, kb = t.sent[0]
    assert "یک‌بار رباتِ ما را استارت" in text and "پشتیبانی" in text   # PATCH_GIFT_ANY_DEST_V1: دیگر ردّ نمی‌کند، دستی می‌شود
    assert any(c.endswith(":1:b") for c in _cbs(kb))
