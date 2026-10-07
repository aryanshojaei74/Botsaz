"""PATCH_GROUP_FEE_PAYER_V1 — کارمزدِ انتقالِ گروهی: از خودِ مبلغ (قبلی) یا از حسابِ فرستنده."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.shared import group_wallet as gw


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(gw, "Session", maker)
    monkeypatch.setattr(tenant, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=4, owner_id=1, token="4:T", username="g", field="virtual_service",
                         plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=4, telegram_id=100, balance=200000))
        s.add(models.BotUser(id=2, bot_id=4, telegram_id=200, balance=0))
        for k, v in (("group_transfer_fee_enabled", "1"), ("group_transfer_fee_pct", "10")):
            s.add(models.Setting(bot_id=4, key=k, value=v))
        await s.commit()
    row = SimpleNamespace(id=4, owner_id=1, field="virtual_service", plan="pro", status="active")

    async def get_bot_row(_b):
        return row
    monkeypatch.setattr(tenant, "get_bot_row", get_bot_row)
    monkeypatch.setattr(gw, "_group_feature_allowed", lambda r: True)
    yield maker
    await engine.dispose()


class _Msg(SimpleNamespace):
    async def reply(self, text, **kw):
        self.replies.append(text)


def _user(uid):
    return SimpleNamespace(id=uid, is_bot=False, full_name=f"u{uid}", username=None)


def _msg(text, reply_from=_user(200), **reply_kw):
    r = SimpleNamespace(from_user=reply_from, forum_topic_created=None, is_automatic_forward=None,
                        sender_chat=None, **reply_kw)
    return _Msg(text=text, from_user=_user(100), sender_chat=None, reply_to_message=r,
                chat=SimpleNamespace(id=-100), replies=[])


async def _bal(db, uid):
    async with db() as s:
        return float((await s.get(models.BotUser, uid)).balance)


@pytest.mark.asyncio
async def test_fee_from_amount_default(db):
    await gw.group_transfer_balance(_msg("انتقال 100000"), object())
    assert await _bal(db, 1) == 100000 and await _bal(db, 2) == 90000


@pytest.mark.asyncio
async def test_fee_from_sender(db):
    await tenant.set_setting(4, "group_transfer_fee_payer", "sender")
    m = _msg("انتقال 100000")
    await gw.group_transfer_balance(m, object())
    assert await _bal(db, 1) == 90000 and await _bal(db, 2) == 100000
    assert "از حسابِ فرستنده" in m.replies[-1]


@pytest.mark.asyncio
async def test_fee_from_sender_needs_amount_plus_fee(db):
    await tenant.set_setting(4, "group_transfer_fee_payer", "sender")
    m = _msg("انتقال 190000")                     # ۱۹۰ + ۱۹ کارمزد > ۲۰۰ موجودی
    await gw.group_transfer_balance(m, object())
    assert await _bal(db, 1) == 200000 and await _bal(db, 2) == 0
    assert "مبلغ + کارمزد" in m.replies[-1]


@pytest.mark.asyncio
async def test_forum_topic_implicit_reply_is_not_a_target(db):
    m = _msg("انتقال 100000")
    m.reply_to_message.forum_topic_created = object()
    await gw.group_transfer_balance(m, object())
    assert await _bal(db, 1) == 200000
