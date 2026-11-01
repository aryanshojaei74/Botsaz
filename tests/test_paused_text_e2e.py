"""PATCH_PAUSED_TEXT_EDIT_V1 — آزمونِ سرتاسری با Dispatcher و روترِ واقعیِ tenant (نه فقط سازندهٔ کیبورد)."""
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

OWNER, USER = 700123, 700555      # آیدی‌هایِ دور از آیدیِ ربات‌هایِ تست‌هایِ دیگر (کشِ _OWN_TG_ID_CACHE در اجرایِ کامل)


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
                           chat=Chat(id=getattr(method, "chat_id", OWNER) or OWNER, type="private"),
                           text=getattr(method, "text", "x")).as_(bot)
        return True

    def shown(self):
        """هر کیبوردی که به مالک نشان داده شد: (متن، [callback یا متنِ دکمه‌ها])."""
        out = []
        for m in self.calls:
            if type(m).__name__ in ("SendMessage", "EditMessageText"):
                rm = getattr(m, "reply_markup", None)
                btns = []
                if rm is not None:
                    rows = getattr(rm, "inline_keyboard", None) or getattr(rm, "keyboard", None) or []
                    btns = [getattr(b, "callback_data", None) or getattr(b, "text", "") for r in rows for b in r]
                out.append((getattr(m, "text", ""), btns))
        return out


def cb_update(data, uid=OWNER):
    u = User(id=uid, is_bot=False, first_name="Owner", username="own")
    m = Message(message_id=42, date=datetime.datetime.now(), chat=Chat(id=uid, type="private"), text="x",
                from_user=User(id=8354597347, is_bot=True, first_name="b"))
    return Update(update_id=1, callback_query=CallbackQuery(id="1", from_user=u, chat_instance="c", data=data, message=m))


def msg_update(text, uid=OWNER):
    u = User(id=uid, is_bot=False, first_name="Owner", username="own")
    return Update(update_id=2, message=Message(message_id=43, date=datetime.datetime.now(), chat=Chat(id=uid, type="private"),
                                                text=text, from_user=u))


@pytest_asyncio.fixture(params=["vpn_sales", "virtual_service", "digital_shop", "ad_bot", "booking", "escrow_bot", "giveaway", "panel_seller"])
async def env(request, monkeypatch):
    import tenant
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    import sys
    _orig = config.Session
    for _mod in list(sys.modules.values()):                      # هر ماژولی که Session را import کرده (vpn_admin، smm، …)
        if getattr(_mod, "Session", None) is _orig:
            monkeypatch.setattr(_mod, "Session", maker, raising=False)
    monkeypatch.setattr(config, "Session", maker)
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    tenant._OWN_TG_ID_CACHE.clear()
    async with maker() as s:
        b = models.Bot(owner_id=OWNER, token="123:TESTTOKEN", username="shop", field=request.param, plan="pro", status="active")
        s.add(b); await s.flush()
        bid = b.id
        await s.commit()
    monkeypatch.setattr(tenant.tenant_router, "_parent_router", None)
    dp = Dispatcher(storage=MemoryStorage()); dp.include_router(tenant.tenant_router)
    cap = Cap()
    yield dict(dp=dp, bot=Bot("123:TESTTOKEN", session=cap), cap=cap, bid=bid, tenant=tenant, field=request.param)
    await engine.dispose()


@pytest.mark.asyncio
async def test_owner_sees_the_button_clicks_it_and_saves_text(env):
    dp, bot, cap, tenant = env["dp"], env["bot"], env["cap"], env["tenant"]
    # ۱) صفحهٔ «دکمه‌ها و تنظیمات» را با روترِ واقعی باز می‌کنیم
    await dp.feed_update(bot, cb_update("tadm:sec:settings"))
    screens = cap.shown()
    assert screens, "هیچ صفحه‌ای نمایش داده نشد"
    LABEL = "✏️ متن خاموش بودن ربات"
    has_cb = any("set:paused_text" in btns for _t, btns in screens)      # منوی شیشه‌ای
    has_label = any(LABEL in btns for _t, btns in screens)               # منوی کیبوردی (پیش‌فرض)
    assert has_cb or has_label, (env["field"], [b for _t, bs in screens for b in bs])
    # ۲) زدنِ دکمه ⇒ درخواستِ متن (همان‌طور که کاربرِ واقعی می‌زند: در حالتِ کیبوردی متنِ دکمه پیام می‌شود)
    cap.calls.clear()
    await dp.feed_update(bot, cb_update("set:paused_text") if has_cb else msg_update(LABEL))
    assert any("متن خاموش بودن ربات" in (t or "") for t, _b in cap.shown()), env["field"]
    # ۳) فرستادنِ متن ⇒ ذخیره
    cap.calls.clear()
    await dp.feed_update(bot, msg_update("ربات تا فردا خاموش است 🌙"))
    tenant._SETTING_CACHE.clear()
    assert await tenant.paused_notice(env["bid"]) == "ربات تا فردا خاموش است 🌙", env["field"]
    assert any("ذخیره شد" in (t or "") for t, _b in cap.shown()), env["field"]


@pytest.mark.asyncio
async def test_non_owner_cannot_open_or_change_it(env):
    dp, bot, cap, tenant = env["dp"], env["bot"], env["cap"], env["tenant"]
    await dp.feed_update(bot, cb_update("set:paused_text", uid=USER))
    await dp.feed_update(bot, msg_update("هک!", uid=USER))
    tenant._SETTING_CACHE.clear()
    assert await tenant.paused_notice(env["bid"]) == tenant.DEFAULT_PAUSED_TEXT


@pytest.mark.asyncio
async def test_version_command_is_owner_only_and_shows_build(env):
    dp, bot, cap, tenant = env["dp"], env["bot"], env["cap"], env["tenant"]
    await dp.feed_update(bot, msg_update("/version", uid=USER))
    assert not any(tenant.BUILD_ID in (t or "") for t, _b in cap.shown())     # غیرمالک: نسخه را نمی‌بیند
    cap.calls.clear()
    await dp.feed_update(bot, msg_update("/version"))
    t = " ".join(x for x, _b in cap.shown())
    assert tenant.BUILD_ID in t and env["field"] in t and "متن خاموش بودن" in t
