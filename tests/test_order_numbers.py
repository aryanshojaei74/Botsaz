"""شناسهٔ واحدِ سفارش: شمارنده دوتا‌دوتا بالا نرود؛ اعلانِ امتیاز همان شمارهٔ سفارشِ پنلِ مدیریت را نشان بدهد."""
from __future__ import annotations

import datetime
import pathlib
import re

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models

ROOT = pathlib.Path(__file__).resolve().parent.parent
USER, OWNER = 555, 1


def test_no_second_allocation_after_flush_in_smm():
    """قبلاً بعد از flush (که before_insert شماره می‌دهد) دوباره شماره می‌گرفت ⇒ هر سفارش دو شماره مصرف می‌کرد."""
    src = (ROOT / "tenant/bots/virtual_service/smm.py").read_text(encoding="utf-8").split("\n")
    for i, line in enumerate(src):
        if "await s.flush()" in line:
            window = "\n".join(src[i:i + 4])
            assert "_alloc_in(" not in window, f"smm.py:{i + 1} بعد از flush دوباره شماره می‌گیرد"


class Cap(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self): ...
    async def stream_content(self, *a, **k): ...

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if type(method).__name__ in ("SendMessage", "EditMessageText"):
            return Message(message_id=900 + len(self.calls), date=datetime.datetime.now(),
                           chat=Chat(id=method.chat_id, type="private"), text=getattr(method, "text", "x")).as_(bot)
        return True


@pytest_asyncio.fixture
async def env(monkeypatch):
    import tenant
    from tenant.shared import product_rating as pr, public_api as _pa
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, _pa, pr, models):
        if hasattr(mod, "Session"):
            monkeypatch.setattr(mod, "Session", maker)
    tenant._BOT_ROW_CACHE.clear()
    import premium_emoji_dynamic as ped

    async def no_premium(_t): return None
    monkeypatch.setattr(ped, "_PROVIDERS", [no_premium])
    async with maker() as s:
        b = models.Bot(owner_id=OWNER, token="123:TESTTOKEN", username="shop", field="virtual_service", plan="pro", status="active")
        s.add(b); await s.flush()
        u = models.BotUser(bot_id=b.id, telegram_id=USER, full_name="Ali", balance=0)
        s.add(u); await s.flush()
        b2 = models.Bot(owner_id=9, token="9:OTHER", username="other", field="virtual_service", plan="pro", status="active")
        s.add(b2); await s.flush()
        s.add(models.SmmOrder(bot_id=b2.id, user_telegram_id=1, service_id="1", quantity=1, link="z"))
        await s.flush()   # pkِ سفارش‌هایِ ربات اصلی را از شمارهٔ ترتیبیِ همان ربات جدا می‌کند
        # چند سفارشِ هم‌زمان از انواعِ مختلف: شماره‌ها باید ۱،۲،۳،… پشت‌سرهم باشند
        o1 = models.SmmOrder(bot_id=b.id, user_telegram_id=USER, service_id="1", quantity=1, link="x", status="completed")
        s.add(o1); await s.flush()
        o2 = models.SmmOrder(bot_id=b.id, user_telegram_id=USER, service_id="1", quantity=1, link="y", status="completed")
        s.add(o2); await s.flush()
        rate = models.ProductRating(bot_id=b.id, order_kind="smm", order_id=o2.id, user_id=u.id, item_key="smm:1")
        s.add(rate)
        await s.commit()
        data = (b.id, o1.id, o2.id, o1.tracking_code, o2.tracking_code)
    monkeypatch.setattr(pr.product_rating_router, "_parent_router", None)
    dp = Dispatcher(storage=MemoryStorage()); dp.include_router(pr.product_rating_router)
    cap = Cap()
    return dp, Bot("123:TESTTOKEN", session=cap), cap, data


@pytest.mark.asyncio
async def test_rating_notice_shows_tracking_number_and_counter_is_consecutive(env):
    dp, bot, cap, (bid, pk1, pk2, t1, t2) = env
    assert (t1, t2) == ("1", "2")                      # پشت‌سرهم، نه ۱ و ۳
    u = User(id=USER, is_bot=False, first_name="U", username="ali")
    m = Message(message_id=42, date=datetime.datetime.now(), chat=Chat(id=USER, type="private"), text="x",
                from_user=User(id=1, is_bot=True, first_name="b"))
    await dp.feed_update(bot, Update(update_id=1, callback_query=CallbackQuery(
        id="1", from_user=u, chat_instance="c", data=f"prate:v:{pk2}:5", message=m)))
    owner_msgs = [c.text for c in cap.calls if type(c).__name__ == "SendMessage" and c.chat_id == OWNER]
    assert owner_msgs and f"<code>{t2}</code>" in owner_msgs[0]
    assert str(pk2) != t2 and not re.search(rf"<code>{pk2}</code>", owner_msgs[0])   # pk داخلی نمایش داده نشود
