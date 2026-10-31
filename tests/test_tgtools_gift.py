"""گیفتِ خودکار با TGTools (PATCH_TGTOOLS_GIFT_V1): خرید به‌ازایِ هر واحد با کدِ یکتا، کامنت/ناشناس، sold out/خطا ⇒ تحویلِ دستی."""
from __future__ import annotations

import asyncio
import datetime

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models

USER, OWNER = 555, 1
GID = "5170145012310081615"


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

    def markups(self, to):
        return [m.reply_markup for m in self.calls if type(m).__name__ == "SendMessage" and m.chat_id == to]


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
    monkeypatch.setattr(smm, "_TGT_POLL_DELAYS", [0, 0])
    import premium_emoji_dynamic as ped

    async def no_premium(_t): return None
    monkeypatch.setattr(ped, "_PROVIDERS", [no_premium])
    async with maker() as s:
        b = models.Bot(owner_id=OWNER, token="123:TESTTOKEN", username="shop", field="virtual_service", plan="pro", status="active")
        s.add(b); await s.flush()
        s.add(models.BotUser(bot_id=b.id, telegram_id=USER, full_name="Ali", username="aliuser", balance=5_000_000))
        s.add(models.TelegramGiftConfig(bot_id=b.id, gift_id=GID, star_count=15, emoji="💝", active=True))
        s.add(models.TelegramGiftConfig(bot_id=b.id, gift_id="manual1", star_count=0, emoji="🔗", active=True,
                                        is_manual=True, manual_name="NFT", manual_price_toman=1000))
        await s.commit()
        bid = b.id
    await tenant.set_setting(bid, "tgtools_api_key", encrypt_secret("tgt_FAKEKEY123456"))
    await tenant.set_setting(bid, "tgtools_gift", "1")
    await tenant.set_setting(bid, "gift_manual_delivery", "1")

    calls = {"buy": []}
    state = {"gifts": [{"giftId": GID, "name": "Heart", "soldOut": False, "priceTon": 0.16}],
             "buy_exc": None, "fail_on": None, "statuses": {}}

    async def fake_gifts(key=None, lang="en"):
        return state["gifts"]

    async def fake_buy(key, username, gift_id, code, *, message="", hide_sender=False):
        calls["buy"].append(dict(key=key, username=username, gift=gift_id, code=code, message=message, hide=hide_sender))
        idx = len(calls["buy"]) - 1
        if state["buy_exc"] and (state["fail_on"] is None or state["fail_on"] == idx):
            raise state["buy_exc"]
        return {"transactionId": 100 + idx, "status": "Pending"}

    async def fake_get(key, tx):
        return state["statuses"].get(tx, {"status": "Completed"})
    monkeypatch.setattr(tg, "get_gifts", fake_gifts)
    monkeypatch.setattr(tg, "buy_gift", fake_buy)
    monkeypatch.setattr(tg, "get_order", fake_get)
    monkeypatch.setattr(smm.smm_router, "_parent_router", None)
    dp = Dispatcher(storage=MemoryStorage()); dp.include_router(smm.smm_router)
    cap = Cap()
    return dict(dp=dp, bot=Bot("123:TESTTOKEN", session=cap), cap=cap, bid=bid, maker=maker, calls=calls,
                state=state, smm=smm, tg=tg, tenant=tenant)


def cb_update(data, uid=USER, uname="aliuser"):
    u = User(id=uid, is_bot=False, first_name="Ali", username=uname)
    m = Message(message_id=42, date=datetime.datetime.now(), chat=Chat(id=uid, type="private"), text="x",
                from_user=User(id=8354597347, is_bot=True, first_name="b"))
    return Update(update_id=1, callback_query=CallbackQuery(id="1", from_user=u, chat_instance="c", data=data, message=m))


async def set_comment(env, qty, text="سلام", anon=True, scope=0):
    key = StorageKey(bot_id=123, chat_id=USER, user_id=USER)
    await env["dp"].storage.set_data(key, {"gift_comment": {"k": f"{GID}:{USER}:{qty}", "t": text, "anon": anon, "scope": scope}})


async def order(env):
    async with env["maker"]() as s:
        return await s.scalar(select(models.GiftOrder))


async def wait_status(env, *statuses, tries=400):   # تا ۸ ثانیه؛ با رسیدن وضعیت زودتر برمی‌گردد (اولین تست هر اجرا زیر بار CPU گاهی کندتر بود)
    for _ in range(tries):
        await asyncio.sleep(0.02)
        o = await order(env)
        if o and o.status in statuses:
            return o
    return await order(env)


@pytest.mark.asyncio
async def test_wallet_order_is_bought_per_unit_with_unique_codes_comment_and_anon(env):
    await set_comment(env, 2)
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:2:s"))
    o = await wait_status(env, "sent")
    assert o.status == "sent" and o.sent_qty == 2
    buys = env["calls"]["buy"]
    assert len(buys) == 2
    assert [b["code"] for b in buys] == [f"b{env['bid']}-o{o.id}-g0", f"b{env['bid']}-o{o.id}-g1"]
    assert all(b["username"] == "aliuser" and b["gift"] == GID and b["hide"] is True and b["message"] == "سلام" for b in buys)
    assert any("با موفقیت" in t for t in env["cap"].texts(USER))        # خبر به مشتری


@pytest.mark.asyncio
async def test_sold_out_gift_goes_back_to_manual_without_buying(env):
    env["state"]["gifts"] = [{"giftId": GID, "soldOut": True}]
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:1:s"))
    o = await wait_status(env, "awaiting_admin")
    await asyncio.sleep(0.1)
    o = await order(env)
    assert o.status == "awaiting_admin" and env["calls"]["buy"] == []
    assert any("تمام شده" in t for t in env["cap"].texts(OWNER))


@pytest.mark.asyncio
async def test_failure_on_second_unit_keeps_partial_and_notifies_admin(env):
    env["state"]["buy_exc"] = env["tg"].TgToolsError("insufficient balance")
    env["state"]["fail_on"] = 1
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:2:s"))
    for _ in range(60):
        await asyncio.sleep(0.02)
        o = await order(env)
        if o and o.status == "awaiting_admin" and o.sent_qty == 1:
            break
    o = await order(env)
    assert o.status == "awaiting_admin" and o.sent_qty == 1
    admin_txt = " ".join(env["cap"].texts(OWNER))
    assert "1 از 2" in admin_txt and "insufficient balance" in admin_txt
    assert len(env["calls"]["buy"]) == 2          # دومی رد شد؛ هیچ retryِ خودکاری نیست


@pytest.mark.asyncio
async def test_ambiguous_response_never_retries_and_warns(env):
    env["state"]["buy_exc"] = env["tg"].TgToolsAmbiguous("TimeoutError")
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:1:s"))
    o = await wait_status(env, "awaiting_admin")
    await asyncio.sleep(0.1)
    assert len(env["calls"]["buy"]) == 1
    assert any("نامشخص" in t for t in env["cap"].texts(OWNER))
    assert (await order(env)).status == "awaiting_admin"


@pytest.mark.asyncio
async def test_manual_gift_never_goes_to_tgtools(env):
    await env["dp"].feed_update(env["bot"], cb_update("gift:pay:manual1:%d:1:s" % USER))
    await asyncio.sleep(0.2)
    o = await order(env)
    assert o.status == "awaiting_admin" and env["calls"]["buy"] == []


@pytest.mark.asyncio
async def test_auto_off_shows_button_and_admin_press_buys(env):
    await env["tenant"].set_setting(env["bid"], "tgtools_gift", "0")
    env["tenant"]._SETTING_CACHE.clear()
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:1:s"))
    o = await wait_status(env, "awaiting_admin")
    await asyncio.sleep(0.1)
    assert env["calls"]["buy"] == []                        # خودکار خاموش ⇒ نخرید
    kb = [m for m in env["cap"].markups(OWNER) if m][-1]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"gift:tgt:{o.id}" in datas and f"gift:cardok:{o.id}" in datas
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:tgt:{o.id}", uid=OWNER, uname="owner"))
    o = await wait_status(env, "sent")
    assert o.status == "sent" and len(env["calls"]["buy"]) == 1


@pytest.mark.asyncio
async def test_pending_after_polls_waits_for_admin_check_then_finishes(env):
    env["state"]["statuses"] = {100: {"status": "Pending"}}
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:pay:{GID}:{USER}:1:s"))
    for _ in range(60):
        await asyncio.sleep(0.02)
        o = await order(env)
        if o and o.status == "auto_processing":
            await asyncio.sleep(0.15)
            break
    o = await order(env)
    assert o.status == "auto_processing" and len(env["calls"]["buy"]) == 1
    env["state"]["statuses"] = {100: {"status": "Completed"}}
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:tgtchk:{o.id}", uid=OWNER, uname="owner"))
    o = await wait_status(env, "sent")
    assert o.status == "sent" and len(env["calls"]["buy"]) == 1   # دوباره نخرید


@pytest.mark.asyncio
async def test_card_receipt_approval_uses_tgtools_when_auto_on_but_not_for_bot_stars_choice(env):
    async with env["maker"]() as s:
        buyer = await s.scalar(select(models.BotUser))
        for via in ("", "bot"):
            s.add(models.GiftOrder(bot_id=env["bid"], buyer_user_id=buyer.id, gift_id=GID, gift_emoji="💝", star_count=15,
                                   price_toman=1000, dest_telegram_id=USER,
                                   dest_label="خودم\n👤 یوزرنیم: @aliuser", status="awaiting_admin",
                                   pay_method="card", tracking_code="T1" if not via else "T2", quantity=1, deliver_via=via))
        await s.commit()
        ids = [o.id for o in (await s.scalars(select(models.GiftOrder).order_by(models.GiftOrder.id))).all()]
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:cardok:{ids[0]}", uid=OWNER, uname="owner"))
    for _ in range(60):
        await asyncio.sleep(0.02)
        async with env["maker"]() as s:
            if (await s.get(models.GiftOrder, ids[0])).status == "sent":
                break
    assert len(env["calls"]["buy"]) == 1 and env["calls"]["buy"][0]["code"].endswith(f"-o{ids[0]}-g0")
    # مشتری «ارسال با استارزِ ربات» را انتخاب کرده ⇒ مسیرِ قبلی (sendGift با استارزِ ربات)، نه TGTools
    await env["dp"].feed_update(env["bot"], cb_update(f"gift:cardok:{ids[1]}", uid=OWNER, uname="owner"))
    await asyncio.sleep(0.2)
    assert len(env["calls"]["buy"]) == 1


@pytest.mark.asyncio
async def test_post_response_body_is_logged_without_key(monkeypatch, caplog):
    import logging
    from aiohttp import web
    from tenant.bots.virtual_service import tgtools_api as tg

    async def gift(request):
        return web.json_response({"transactionId": 7, "status": "Pending", "message": "queued"})

    app = web.Application()
    app.router.add_post("/api/Purchase/gift", gift)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8882).start()
    monkeypatch.setattr(tg, "BASE_URL", "http://127.0.0.1:8882")
    try:
        with caplog.at_level(logging.INFO, logger=tg.logger.name):
            res = await tg.buy_gift("tgt_SECRETKEY999", "aliuser", GID, "code-1", message="hi", hide_sender=True)
    finally:
        await runner.cleanup()
    assert res["transactionId"] == 7
    assert "transactionId" in caplog.text and "tgt_SECRETKEY999" not in caplog.text


@pytest.mark.asyncio
async def test_real_documented_response_completed_immediately_needs_no_polling(monkeypatch):
    """مستندِ واقعیِ TGTools: POST /api/purchase/gift ⇒ {isSuccess,data:{transactionId,status:"Completed",...,message:null}}."""
    from aiohttp import web
    from tenant.bots.virtual_service import smm, tgtools_api as tg

    async def gift(request):
        body = await request.json()
        assert body["giftId"] == GID and body["recipientUsername"] == "aliuser" and body["trackingCode"] == "b1-o1-g0"
        assert body["hideSenderName"] is False and "message" not in body
        return web.json_response({"isSuccess": True, "data": {
            "transactionId": 44, "status": "Completed", "recipientUsername": "aliuser", "giftId": GID,
            "stars": 15, "quotedTotalTon": 0.2145, "costTon": 0.195, "message": None}})

    app = web.Application()
    app.router.add_post("/api/Purchase/gift", gift)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8880).start()
    monkeypatch.setattr(tg, "BASE_URL", "http://127.0.0.1:8880")

    async def must_not_poll(*a, **k):
        raise AssertionError("وضعیت نباید پول شود؛ پاسخِ اول Completed بود")
    monkeypatch.setattr(tg, "get_order", must_not_poll)
    try:
        res = await tg.buy_gift("tgt_K", "aliuser", GID, "b1-o1-g0")
    finally:
        await runner.cleanup()
    assert res["transactionId"] == 44 and tg.norm_status(res) == "completed"
    st, _cur = await smm._tgt_gift_status("tgt_K", 44, res)
    assert st == "completed"
