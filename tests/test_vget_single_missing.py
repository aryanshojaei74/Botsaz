"""کانفیگ تکی: کاربر روی پنل نیست (۴۰۴) ⇒ پیامِ روشن به مشتری (نه «از ساب‌لینک استفاده کن») و اعلانِ محدودشده به مالک."""
from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models

USER, OWNER = 555, 1


class Capture(BaseSession):
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

    def sent(self, to):
        return [m.text for m in self.calls if type(m).__name__ == "SendMessage" and m.chat_id == to]


class GonePanel:
    """پنلی که جواب می‌دهد ولی کاربر را نمی‌شناسد."""
    async def get_client_links(self, email): return []
    async def get_client(self, email): return None
    async def close(self): ...


@pytest_asyncio.fixture
async def env(monkeypatch):
    import tenant
    from tenant.bots.vpn import vpn_user
    from tenant.shared import public_api as _pa
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, _pa, vpn_user):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    import premium_emoji_dynamic as ped

    async def no_premium(_t): return None
    monkeypatch.setattr(ped, "_PROVIDERS", [no_premium])
    monkeypatch.setattr(vpn_user, "_client_from_panel", lambda p: GonePanel())

    async def no_native(*a, **k): return 0
    monkeypatch.setattr(vpn_user, "_send_native_wg_files", no_native)
    vpn_user._MISSING_ALERT_TS.clear()
    async with maker() as s:
        b = models.Bot(owner_id=OWNER, token="123:TESTTOKEN", username="shop", field="vpn", plan="pro", status="active")
        s.add(b); await s.flush()
        s.add(models.VpnPanel(bot_id=b.id, panel_key="pasargad#2", enabled=True, base_url="https://p", username="u", password="p"))
        acc = models.PanelAccount(bot_id=b.id, user_telegram_id=USER, email="E1", panel_key="pasargad#2",
                                  status="active", paid_toman=1000, volume_gb=10, days=30)
        s.add(acc); await s.commit()
        aid = acc.id
    monkeypatch.setattr(vpn_user.vpn_user_router, "_parent_router", None)
    dp = Dispatcher(storage=MemoryStorage()); dp.include_router(vpn_user.vpn_user_router)
    cap = Capture()
    return dp, Bot("123:TESTTOKEN", session=cap), cap, aid


def upd(aid, n):
    u = User(id=USER, is_bot=False, first_name="U")
    m = Message(message_id=42, date=datetime.datetime.now(), chat=Chat(id=USER, type="private"), text="x",
                from_user=User(id=8354597347, is_bot=True, first_name="b"))
    return Update(update_id=n, callback_query=CallbackQuery(id="1", from_user=u, chat_instance="c",
                                                           data=f"vget:single:{aid}", message=m))


@pytest.mark.asyncio
async def test_missing_user_gets_clear_message_and_owner_alert_once(env):
    dp, bot, cap, aid = env
    await dp.feed_update(bot, upd(aid, 1))
    cust = cap.sent(USER)
    assert any("روی سرور پیدا نشد" in t for t in cust) and not any("ارسال ساب لینک" in t for t in cust)
    owner = cap.sent(OWNER)
    assert len(owner) == 1 and "وجود ندارد" in owner[0] and "برگشت وجه" in owner[0]
    await dp.feed_update(bot, upd(aid, 2))                 # دوباره: به مالک اعلانِ تکراری نمی‌رود
    assert len(cap.sent(OWNER)) == 1
    assert len([t for t in cap.sent(USER) if "روی سرور پیدا نشد" in t]) == 2
