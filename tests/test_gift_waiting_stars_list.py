"""سفارش‌هایِ «منتظرِ موجودیِ استارز» در لیستِ سفارش‌هایِ گیفت با دکمهٔ ارسال/لغو (گزارشِ فدایی)."""
import asyncio
import datetime

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models

USER, OWNER = 555, 1


class Cap(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.gift_error = None

    async def close(self): ...
    async def stream_content(self, *a, **k): ...

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name == "SendGift" and self.gift_error:
            raise self.gift_error
        if name in ("SendMessage", "EditMessageText"):
            return Message(message_id=900 + len(self.calls), date=datetime.datetime.now(),
                           chat=Chat(id=method.chat_id, type="private"), text=getattr(method, "text", "x")).as_(bot)
        return True


@pytest_asyncio.fixture
async def env(monkeypatch):
    import tenant
    from tenant.bots.virtual_service import smm
    from tenant.shared import public_api as _pa
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, _pa, smm):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    import premium_emoji_dynamic as ped

    async def no_premium(_t): return None
    monkeypatch.setattr(ped, "_PROVIDERS", [no_premium])
    async with maker() as s:
        b = models.Bot(owner_id=OWNER, token="123:TESTTOKEN", username="shop", field="virtual_service", plan="pro", status="active")
        s.add(b); await s.flush()
        u = models.BotUser(bot_id=b.id, telegram_id=USER, full_name="Ali", username="aliuser", balance=0)
        s.add(u); await s.flush()
        s.add(models.GiftOrder(bot_id=b.id, buyer_user_id=u.id, gift_id="5170145012310081615", gift_emoji="💝", star_count=15,
                               price_toman=1000, dest_telegram_id=USER, dest_label="@aliuser", status="awaiting_stars",
                               pay_method="wallet", tracking_code="T77", quantity=1, deliver_via="bot"))
        await s.commit()
    monkeypatch.setattr(smm.smm_router, "_parent_router", None)
    dp = Dispatcher(storage=MemoryStorage()); dp.include_router(smm.smm_router)
    cap = Cap()
    return dict(dp=dp, bot=Bot("123:TESTTOKEN", session=cap), cap=cap, maker=maker)


def cb(data, uid=OWNER):
    u = User(id=uid, is_bot=False, first_name="Owner", username="owner")
    m = Message(message_id=42, date=datetime.datetime.now(), chat=Chat(id=uid, type="private"), text="x",
                from_user=User(id=8354597347, is_bot=True, first_name="b"))
    return Update(update_id=1, callback_query=CallbackQuery(id="1", from_user=u, chat_instance="c", data=data, message=m))


def last_markup(cap):
    return [m.reply_markup for m in cap.calls if type(m).__name__ in ("EditMessageText", "SendMessage") and m.reply_markup][-1]


def datas(mk):
    return [b.callback_data for row in mk.inline_keyboard for b in row]


async def order(env):
    async with env["maker"]() as s:
        return await s.scalar(select(models.GiftOrder))


@pytest.mark.asyncio
async def test_list_shows_waiting_order_with_retry_and_cancel_buttons(env):
    await env["dp"].feed_update(env["bot"], cb("gift:orders:0"))
    o = await order(env)
    d = datas(last_markup(env["cap"]))
    assert f"gift:lr:{o.id}:0" in d and f"gift:lc:{o.id}:0" in d and "w:botstars" in d
    txt = [m.text for m in env["cap"].calls if type(m).__name__ == "EditMessageText"][-1]
    assert "منتظرِ موجودیِ استارزِ ربات: 1 سفارش" in txt and "T77" in txt


@pytest.mark.asyncio
async def test_retry_from_list_sends_gift_and_marks_sent(env):
    await env["dp"].feed_update(env["bot"], cb(f"gift:lr:{(await order(env)).id}:0"))
    o = await order(env)
    assert o.status == "sent" and o.sent_qty == 1
    assert any(type(m).__name__ == "SendGift" for m in env["cap"].calls)


@pytest.mark.asyncio
async def test_retry_still_without_stars_keeps_order_waiting(env):
    from aiogram.exceptions import TelegramBadRequest
    env["cap"].gift_error = TelegramBadRequest(method=None, message="Bad Request: BALANCE_TOO_LOW")
    await env["dp"].feed_update(env["bot"], cb(f"gift:lr:{(await order(env)).id}:0"))
    assert (await order(env)).status == "awaiting_stars"


@pytest.mark.asyncio
async def test_cancel_from_list_refunds_wallet_and_rejects(env):
    oid = (await order(env)).id
    await env["dp"].feed_update(env["bot"], cb(f"gift:lc:{oid}:0"))
    o = await order(env)
    assert o.status == "rejected"
    async with env["maker"]() as s:
        bal = float((await s.scalar(select(models.BotUser))).balance)
    assert bal == 1000.0          # وجهِ مشتری برگشت


@pytest.mark.asyncio
async def test_non_owner_cannot_use_list_buttons(env):
    oid = (await order(env)).id
    await env["dp"].feed_update(env["bot"], cb(f"gift:lc:{oid}:0", uid=USER))
    assert (await order(env)).status == "awaiting_stars"
