"""پرمیومِ خودکار با TGTools (PATCH_TGTOOLS_PREMIUM_V1): قیمت از نرخِ TGTools، خریدِ خودکار، خطا ⇒ تحویلِ دستی، بدونِ خریدِ دوباره."""
from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models

USER, OWNER = 555, 1
PRICES = {"premium_3m_ton": 8.1585, "premium_6m_ton": 10.878, "premium_12m_ton": 19.74}


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

    def texts(self, to):
        return [m.text for m in self.calls if type(m).__name__ == "SendMessage" and m.chat_id == to]


@pytest_asyncio.fixture
async def env(monkeypatch):
    import wallet_crypto
    monkeypatch.setattr(wallet_crypto, "WALLET_ENC_KEY", Fernet.generate_key().decode())
    import tenant
    from tenant.bots.virtual_service import smm, tgtools_api as tg
    from tenant.shared import public_api as _pa
    from wallet_crypto import encrypt_secret
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, _pa, smm):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    smm._TGT_PRICES_CACHE.update({"t": -1e9, "v": {}})
    monkeypatch.setattr(smm, "_TGT_POLL_DELAYS", [0, 0])
    import premium_emoji_dynamic as ped

    async def no_premium(_t): return None
    monkeypatch.setattr(ped, "_PROVIDERS", [no_premium])
    async with maker() as s:
        b = models.Bot(owner_id=OWNER, token="123:TESTTOKEN", username="shop", field="virtual_service", plan="pro", status="active")
        s.add(b); await s.flush()
        s.add(models.BotUser(bot_id=b.id, telegram_id=USER, full_name="Ali", username="aliuser", balance=5_000_000))
        await s.commit()
        bid = b.id
    await tenant.set_setting(bid, "tgtools_api_key", encrypt_secret("tgt_FAKEKEY123456"))
    await tenant.set_setting(bid, "tgtools_premium", "1")
    await tenant.set_setting(bid, "nft_ton_rate", "100000")
    await tenant.set_setting(bid, "premium_margin_pct", "10")

    async def fake_prices():
        return PRICES
    monkeypatch.setattr(tg, "get_prices", fake_prices)
    calls = {"buy": [], "get": 0}
    state = {"buy_result": {"transactionId": 43, "status": "Pending"}, "orders": [{"status": "Pending"}, {"status": "Completed"}],
             "buy_exc": None}

    async def fake_buy(key, username, months, code):
        calls["buy"].append((key, username, months, code))
        if state["buy_exc"]:
            raise state["buy_exc"]
        return state["buy_result"]

    async def fake_get(key, tx):
        calls["get"] += 1
        return state["orders"].pop(0) if state["orders"] else {"status": "Pending"}
    monkeypatch.setattr(tg, "buy_premium", fake_buy)
    monkeypatch.setattr(tg, "get_order", fake_get)
    monkeypatch.setattr(smm.smm_router, "_parent_router", None)
    dp = Dispatcher(storage=MemoryStorage()); dp.include_router(smm.smm_router)
    cap = Cap()
    return dict(dp=dp, bot=Bot("123:TESTTOKEN", session=cap), cap=cap, bid=bid, maker=maker, calls=calls, state=state, smm=smm, tg=tg)


def pay_update(months=3, dest=USER):
    u = User(id=USER, is_bot=False, first_name="Ali", username="aliuser")
    m = Message(message_id=42, date=datetime.datetime.now(), chat=Chat(id=USER, type="private"), text="x",
                from_user=User(id=8354597347, is_bot=True, first_name="b"))
    return Update(update_id=1, callback_query=CallbackQuery(id="1", from_user=u, chat_instance="c",
                                                           data=f"prm:pay:{months}:{dest}", message=m))


async def balance(env):
    async with env["maker"]() as s:
        return float((await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == USER))).balance)


async def order(env):
    async with env["maker"]() as s:
        return await s.scalar(select(models.GiftOrder))


@pytest.mark.asyncio
async def test_price_comes_from_tgtools_rate_times_ton_rate_plus_margin(env):
    smm = env["smm"]
    p = await smm._tgt_premium_price(env["bid"], 3, 10)
    assert p == round(8.1585 * 100000 * 1.10)
    price, custom = await smm._premium_effective_price(env["bid"], 3, 1000, 1.0, 10)
    assert price == p and custom is False
    # نرخِ TON تنظیم نشده ⇒ فرمولِ قبلیِ استارزی
    import tenant
    await tenant.set_setting(env["bid"], "nft_ton_rate", "0")
    tenant._SETTING_CACHE.clear()
    assert await smm._tgt_premium_price(env["bid"], 3, 10) is None


@pytest.mark.asyncio
async def test_pay_triggers_auto_purchase_once_and_completes(env):
    await env["dp"].feed_update(env["bot"], pay_update(3))
    import asyncio
    for _ in range(50):                       # تسک پس‌زمینه
        await asyncio.sleep(0.02)
        o = await order(env)
        if o and o.status == "sent":
            break
    o = await order(env)
    assert o.status == "sent"
    assert len(env["calls"]["buy"]) == 1
    key, username, months, code = env["calls"]["buy"][0]
    assert (key, username, months) == ("tgt_FAKEKEY123456", "aliuser", 3) and code == f"b{env['bid']}-o{o.id}"
    assert await balance(env) == 5_000_000 - round(8.1585 * 100000 * 1.10)
    for _ in range(100):                      # پیامِ «تکمیل شد» بعد از ثبتِ وضعیتِ sent از همان تسکِ پس‌زمینه می‌رود
        if any("تکمیل شد" in t for t in env["cap"].texts(USER)):
            break
        await asyncio.sleep(0.02)
    assert any("تکمیل شد" in t for t in env["cap"].texts(USER))


@pytest.mark.asyncio
async def test_rejected_purchase_falls_back_to_manual_without_second_call(env):
    env["state"]["buy_exc"] = env["tg"].TgToolsError("موجودی کافی نیست", 400)
    await env["dp"].feed_update(env["bot"], pay_update(6))
    import asyncio
    for _ in range(50):
        await asyncio.sleep(0.02)
        o = await order(env)
        if o and o.status == "awaiting_admin" and any("رد کرد" in (t or "") for t in env["cap"].texts(OWNER)):
            break
    o = await order(env)
    assert o.status == "awaiting_admin" and len(env["calls"]["buy"]) == 1
    assert any("رد کرد" in t and "موجودی کافی نیست" in t for t in env["cap"].texts(OWNER))


@pytest.mark.asyncio
async def test_ambiguous_response_warns_and_never_retries(env):
    env["state"]["buy_exc"] = env["tg"].TgToolsAmbiguous("TimeoutError")
    await env["dp"].feed_update(env["bot"], pay_update(12))
    import asyncio
    for _ in range(50):
        await asyncio.sleep(0.02)
        if any("نامشخص" in (t or "") for t in env["cap"].texts(OWNER)):
            break
    assert len(env["calls"]["buy"]) == 1
    assert any("نامشخص" in t and "ممکن است سفارش ثبت شده باشد" in t for t in env["cap"].texts(OWNER))
    assert (await order(env)).status == "awaiting_admin"
