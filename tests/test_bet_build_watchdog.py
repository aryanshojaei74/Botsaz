"""PATCH_BET_BUILD_WATCHDOG_V1 — بازیِ گروهی که روی «در حال ساخت بازی…» گیر کند لغو و مسترد می‌شود."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant.shared.group_betting as GB

BOT, TG = 5, 777


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(GB, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=BOT, owner_id=1, token="5:T", username="x", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=BOT, telegram_id=TG, full_name="A", balance=100_000))
        await s.commit()

    async def bfm(bot):
        return SimpleNamespace(id=BOT, owner_id=1, token="5:T", field="virtual_service")

    async def gs(bot_id, key, default=""):
        return {"group_betting_enabled": "1"}.get(key, default)

    class FakeBot:
        edits = []

        def __init__(self, token=None):
            self.session = SimpleNamespace(close=self._close)

        async def _close(self):
            return None

        async def edit_message_text(self, **kw):
            FakeBot.edits.append(kw)

    FakeBot.edits = []
    monkeypatch.setattr(GB, "_bot_for_message", bfm)
    monkeypatch.setattr(GB, "_group_feature_allowed", lambda row: True)
    monkeypatch.setattr(GB, "_get_setting", gs)
    monkeypatch.setattr(GB, "Bot", FakeBot)

    async def goc(bot_id, tg, name, uname):
        async with mk() as s:
            return await s.scalar(select(models.BotUser).where(models.BotUser.bot_id == bot_id, models.BotUser.telegram_id == tg))

    import tenant
    monkeypatch.setattr(tenant, "get_or_create_user", goc)
    yield SimpleNamespace(mk=mk, bot=FakeBot)
    await engine.dispose()


def msg(reply_exc=None, edit_exc=None):
    sent_edits = []

    async def edit_text(t, **kw):
        if edit_exc:
            raise edit_exc
        sent_edits.append(t)

    sent = SimpleNamespace(message_id=900, edit_text=edit_text)
    replies = []

    async def reply(t, **kw):
        if reply_exc and t.startswith("⏳"):
            raise reply_exc
        replies.append(t)
        return sent

    m = SimpleNamespace(text="بازی 5000", chat=SimpleNamespace(id=-100, type="supergroup"), reply_to_message=None,
                        from_user=SimpleNamespace(id=TG, full_name="A", username="a", is_bot=False), reply=reply)
    return m, replies, sent_edits


async def bal(env):
    async with env.mk() as s:
        return int((await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == TG))).balance)


async def the_bet(env):
    async with env.mk() as s:
        return (await s.scalars(select(models.Bet))).one()


@pytest.mark.asyncio
async def test_happy_path_marks_built(env):
    m, replies, edits = msg()
    await GB.create_bet(m, None)
    b = await the_bet(env)
    assert b.status == "pending" and b.unbuilt is False and await bal(env) == 95_000 and edits


@pytest.mark.asyncio
async def test_edit_failure_refunds_immediately(env, monkeypatch):
    async def nosleep(_):
        return None
    monkeypatch.setattr(GB.asyncio, "sleep", nosleep)
    m, replies, edits = msg(edit_exc=RuntimeError("telegram down"))
    await GB.create_bet(m, None)
    b = await the_bet(env)
    assert b.status == "expired" and await bal(env) == 100_000


@pytest.mark.asyncio
async def test_reply_failure_refunds_and_does_not_crash(env):
    m, replies, edits = msg(reply_exc=RuntimeError("timeout"))
    await GB.create_bet(m, None)
    b = await the_bet(env)
    assert b.status == "expired" and await bal(env) == 100_000
    assert any("ساخت بازی ناموفق" in r for r in replies)


@pytest.mark.asyncio
async def test_sweeper_cancels_stuck_unbuilt_but_not_fresh_or_old_rows(env):
    now = datetime.utcnow()
    async with env.mk() as s:
        u = await s.get(models.BotUser, 1)
        u.balance = 0
        mk = lambda **kw: models.Bet(bot_id=BOT, chat_id=-100, creator_user_id=1, creator_telegram_id=TG,
                                      target_telegram_id=0, amount=10_000, status="pending",
                                      expires_at=now + timedelta(hours=1), message_id=11, **kw)
        s.add(mk(unbuilt=True, created_at=now - timedelta(seconds=GB.BUILD_TIMEOUT_SECS + 5)))   # گیرکرده ⇒ مسترد
        s.add(mk(unbuilt=True, created_at=now - timedelta(seconds=10)))                           # تازه ⇒ دست‌نخورده
        s.add(mk(unbuilt=None, created_at=now - timedelta(hours=2)))                              # ردیفِ قدیمی (ساخته‌شده) ⇒ دست‌نخورده
        await s.commit()
    assert await GB.expire_pending_bets_once() == 1
    async with env.mk() as s:
        rows = (await s.scalars(select(models.Bet).order_by(models.Bet.id))).all()
    assert [r.status for r in rows] == ["expired", "pending", "pending"]
    assert await bal(env) == 10_000
    assert env.bot.edits and "ساخت بازی ناموفق" in env.bot.edits[0]["text"]
    assert await GB.expire_pending_bets_once() == 0          # دوباره مسترد نمی‌شود


@pytest.mark.asyncio
async def test_normal_ttl_expiry_still_works(env):
    now = datetime.utcnow()
    async with env.mk() as s:
        u = await s.get(models.BotUser, 1)
        u.balance = 0
        s.add(models.Bet(bot_id=BOT, chat_id=-100, creator_user_id=1, creator_telegram_id=TG, target_telegram_id=0,
                         amount=7_000, status="pending", expires_at=now - timedelta(seconds=1), message_id=11,
                         unbuilt=False, created_at=now - timedelta(minutes=6)))
        await s.commit()
    assert await GB.expire_pending_bets_once() == 1
    assert await bal(env) == 7_000 and "زمان بازی به پایان" in env.bot.edits[0]["text"]
