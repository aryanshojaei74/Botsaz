"""PATCH_TRANSFER_BY_USERNAME_V1 — انتقال با ریپلای رویِ پیامِ خودِ فرستنده که یوزرنیمِ گیرنده در آن است (فقط رباتِ فعال‌شده)."""
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
        s.add(models.Bot(id=4, owner_id=1, token="4:T", username="g", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=4, telegram_id=100, balance=50000, username="sender"))
        s.add(models.BotUser(id=2, bot_id=4, telegram_id=200, balance=0, username="Receiver"))
        s.add(models.BotUser(id=3, bot_id=4, telegram_id=300, balance=0, username="third"))
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


def _u(uid):
    return SimpleNamespace(id=uid, is_bot=False, full_name=f"u{uid}", username=None)


def _msg(text, replied_text, replied_author, me=100):
    r = SimpleNamespace(from_user=_u(replied_author), forum_topic_created=None, is_automatic_forward=None,
                        sender_chat=None, text=replied_text, caption=None)
    return _Msg(text=text, from_user=_u(me), sender_chat=None, reply_to_message=r,
                chat=SimpleNamespace(id=-100), replies=[])


async def _bal(db, uid):
    async with db() as s:
        return float((await s.get(models.BotUser, uid)).balance)


@pytest.mark.asyncio
async def test_own_message_with_username_transfers_to_that_username(db):
    await tenant.set_setting(4, "group_transfer_by_username", "1")
    m = _msg("انتقال 1000", "این هم یوزرنیم @receiver سلام", replied_author=100)
    await gw.group_transfer_balance(m, object())
    assert await _bal(db, 1) == 49000 and await _bal(db, 2) == 1000


@pytest.mark.asyncio
async def test_replying_to_someone_elses_message_goes_to_its_author_not_the_username(db):
    await tenant.set_setting(4, "group_transfer_by_username", "1")
    m = _msg("انتقال 1000", "ببین @third چیه", replied_author=200)       # پیامِ نفرِ دیگر (۲۰۰)
    await gw.group_transfer_balance(m, object())
    assert await _bal(db, 2) == 1000 and await _bal(db, 3) == 0


@pytest.mark.asyncio
async def test_feature_is_off_by_default(db):
    m = _msg("انتقال 1000", "@receiver", replied_author=100)
    await gw.group_transfer_balance(m, object())
    assert await _bal(db, 2) == 0 and "به خودت" in m.replies[-1]       # رفتارِ قبلی: به خودت ⇒ خطا


@pytest.mark.asyncio
async def test_unknown_or_multiple_usernames_are_refused(db):
    await tenant.set_setting(4, "group_transfer_by_username", "1")
    m1 = _msg("انتقال 1000", "@nobody", replied_author=100)
    await gw.group_transfer_balance(m1, object())
    m2 = _msg("انتقال 1000", "@receiver و @third", replied_author=100)
    await gw.group_transfer_balance(m2, object())
    assert await _bal(db, 1) == 50000
    assert "پیدا نشد" in m1.replies[-1] and "بیش از یک" in m2.replies[-1]
