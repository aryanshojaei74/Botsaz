"""صفحهٔ «مشخصات کانفیگ»: دکمه‌هایِ کوتاه دوتا-دوتا (PATCH_SVC_KEYBOARD_PAIRS_V1) — گزارشِ WHALE SHOP."""
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

USER = 555


class Cap(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self): ...
    async def stream_content(self, *a, **k): ...

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if type(method).__name__ in ("SendMessage", "EditMessageText", "EditMessageCaption", "SendPhoto"):
            return Message(message_id=900 + len(self.calls), date=datetime.datetime.now(),
                           chat=Chat(id=USER, type="private"), text="x").as_(bot)
        return True


@pytest_asyncio.fixture
async def env(monkeypatch):
    import tenant
    import tenant.bots.vpn.vpn_user as V
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, V):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    V._VC_CACHE.clear()
    import premium_emoji_dynamic as ped

    async def no_premium(_t): return None
    monkeypatch.setattr(ped, "_PROVIDERS", [no_premium])
    async with maker() as s:
        b = models.Bot(owner_id=1, token="123:TESTTOKEN", username="vpn", field="vpn", plan="pro", status="active")
        s.add(b); await s.flush()
        s.add(models.BotUser(bot_id=b.id, telegram_id=USER, full_name="Ali", balance=0))
        s.add(models.PanelAccount(bot_id=b.id, user_telegram_id=USER, email="a@t", panel_key="sanai", plan_title="p",
                                  volume_gb=10, days=30, status="active", sub_link="https://x/sub/1", paid_toman=1000))
        await s.commit()
        bid = b.id
    await tenant.set_setting(bid, "refund_enabled", "1")
    monkeypatch.setattr(V.vpn_user_router, "_parent_router", None)
    dp = Dispatcher(storage=MemoryStorage()); dp.include_router(V.vpn_user_router)
    cap = Cap()
    return dict(dp=dp, bot=Bot("123:TESTTOKEN", session=cap), cap=cap, V=V)


@pytest.mark.asyncio
async def test_service_page_keyboard_is_paired_and_keeps_every_callback(env, monkeypatch):
    V = env["V"]

    async def fake_png(bot_id, link):
        return b"PNG"
    monkeypatch.setattr(V, "_qr_png", fake_png)
    u = User(id=USER, is_bot=False, first_name="Ali")
    m = Message(message_id=42, date=datetime.datetime.now(), chat=Chat(id=USER, type="private"), text="x",
                from_user=User(id=8354597347, is_bot=True, first_name="b"))
    await env["dp"].feed_update(env["bot"], Update(update_id=1, callback_query=CallbackQuery(
        id="1", from_user=u, chat_instance="c", data="vsvc:1", message=m)))
    mk = [c.reply_markup for c in env["cap"].calls if getattr(c, "reply_markup", None)][-1]
    rows = mk.inline_keyboard
    datas = [b.callback_data for r in rows for b in r if b.callback_data]
    # همهٔ دکمه‌هایِ قبلی هنوز هستند
    for want in ("vget:sub:1", "vget:single:1", "vwgcfg:1", "vsvcqr:1", "vreset:1", "vsvcoff:1",
                 "vdel:1", "vrf:1", "vhelp:menu", "vsvcback", "vsvc:home"):
        assert want in datas, want
    assert "vrename:1" not in datas                     # PATCH_SVC_PAGE_TIDY_V1: «تغییر نام» از صفحهٔ سرویس برداشته شد
    assert max(len(r) for r in rows) <= 2
    # قبلاً ۱۳ ردیف بود؛ حالا بسیار کمتر
    assert len(rows) <= 8
    single_rows = [r for r in rows if len(r) == 1]
    assert len(single_rows) <= 3                           # دو دکمهٔ طولانیِ تمدید + (اگر تعدادِ دکمه‌هایِ کوتاه فرد شد) آخرین دکمه
    assert {r[0].callback_data for r in single_rows} <= {"vren:1", "vadd:1", "vhelp:menu"}
