"""لغوِ خودکارِ شمارهٔ مجازیِ OzVinoo اگر ۱۰ دقیقه کد نیامد (درخواستِ ادمین)."""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant.bots.virtual_service.ozvinoo_vnum as OZ


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append(text)


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, OZ):
        monkeypatch.setattr(mod, "Session", maker)
    OZ._AUTOCANCEL_LAST.clear()
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:T", username="x", field="virtual_service", plan="pro", status="active")
        s.add(b); await s.flush()
        u = models.BotUser(bot_id=b.id, telegram_id=555, balance=0)
        svc = models.SmmService(bot_id=b.id, service_id="ozv:p:1", name="n")
        s.add_all([u, svc]); await s.flush()
        o = models.SmmOrder(bot_id=b.id, user_telegram_id=555, smm_service_pk=svc.id, service_id="ozv:p:1",
                            service_name="n", link="+1", quantity=1, price_toman=5000, status="sent",
                            panel_order_id="123", panel_status="pending", tracking_code=1,
                            created_at=dt.datetime.utcnow() - dt.timedelta(minutes=11))
        s.add(o); await s.commit()
        ids = (b.id, o.id)
    async def prov(bot_id, svc): return {"token": "t"}, None
    monkeypatch.setattr(OZ, "_service_provider", prov)
    async def pending(*a, **k): return "pending"
    monkeypatch.setattr(OZ, "_process_code", pending)
    yield maker, ids
    await engine.dispose()


async def _state(maker, oid):
    async with maker() as s:
        o = await s.get(models.SmmOrder, oid)
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 555))
        return o.status, float(u.balance)


@pytest.mark.asyncio
async def test_old_pending_order_is_cancelled_and_refunded(env, monkeypatch):
    maker, (bid, oid) = env
    calls = []
    async def oz(provider, method, path, **kw):
        calls.append(path); return 200, {"status": True}
    monkeypatch.setattr(OZ, "_oz_request", oz)
    bot = FakeBot()
    async with maker() as s:
        order = await s.get(models.SmmOrder, oid)
    await OZ._ozv_check_one(bot, order)
    assert await _state(maker, oid) == ("refunded", 5000.0)
    assert calls == [OZ._OZ_CANCEL] and "5,000" in bot.sent[0] and "10" in bot.sent[0]


@pytest.mark.asyncio
async def test_provider_refusal_gives_no_refund_and_retry_is_throttled(env, monkeypatch):
    maker, (bid, oid) = env
    n = {"c": 0}
    async def oz(provider, method, path, **kw):
        n["c"] += 1; return 400, {"status": False, "message": "Failed to update flood time."}
    monkeypatch.setattr(OZ, "_oz_request", oz)
    bot = FakeBot()
    async with maker() as s:
        order = await s.get(models.SmmOrder, oid)
    await OZ._ozv_check_one(bot, order)
    await OZ._ozv_check_one(bot, order)          # بلافاصله: نباید دوباره درخواست بزند
    assert n["c"] == 1 and await _state(maker, oid) == ("sent", 0.0) and not bot.sent


@pytest.mark.asyncio
async def test_young_order_and_disabled_setting_are_left_alone(env, monkeypatch):
    maker, (bid, oid) = env
    async def oz(*a, **k): raise AssertionError("نباید لغو بزند")
    monkeypatch.setattr(OZ, "_oz_request", oz)
    async with maker() as s:
        order = await s.get(models.SmmOrder, oid)
        order.created_at = dt.datetime.utcnow() - dt.timedelta(minutes=3)
        await s.commit()
    assert await OZ._ozv_maybe_autocancel(FakeBot(), order, {"token": "t"}) is False
    order.created_at = dt.datetime.utcnow() - dt.timedelta(minutes=30)
    async with maker() as s:
        s.add(models.Setting(bot_id=bid, key="ozv_autocancel_min", value="0")); await s.commit()
    assert await OZ._ozv_maybe_autocancel(FakeBot(), order, {"token": "t"}) is False


@pytest.mark.asyncio
async def test_smspool_loop_cancels_and_refunds_after_timeout(monkeypatch):
    import tenant.bots.virtual_service.smm as SMM
    monkeypatch.setattr(SMM, "_VNUM_AUTO_POLL_INTERVAL", 30)
    monkeypatch.setattr(SMM, "_VNUM_AUTO_POLL_MAX_MIN", 1)
    async def _nosleep(*a, **k): return None
    monkeypatch.setattr(SMM.asyncio, "sleep", _nosleep)
    seen = []
    async def check(key, oid): return {"status": 1}
    async def cancel(key, oid): seen.append("cancel"); return {"success": 1}
    monkeypatch.setattr(SMM.smspool_api, "check_sms", check)
    monkeypatch.setattr(SMM.smspool_api, "cancel_sms", cancel)
    applied = []
    async def apply(bot, bot_id, order_id, api_key, sp, chat, price, uid, name, result):
        applied.append(result.get("status"))
        return "pending" if result.get("status") == 1 else "refunded"
    monkeypatch.setattr(SMM, "_vnum_auto_apply_result", apply)
    await SMM._vnum_auto_poll_loop(None, 1, 1, "k", "9", 5, 100.0, 7, "Iran")
    assert seen == ["cancel"] and applied[-1] == 5


@pytest.mark.asyncio
async def test_smspool_loop_does_not_cancel_when_provider_refuses(monkeypatch):
    import tenant.bots.virtual_service.smm as SMM
    monkeypatch.setattr(SMM, "_VNUM_AUTO_POLL_INTERVAL", 30)
    monkeypatch.setattr(SMM, "_VNUM_AUTO_POLL_MAX_MIN", 1)
    async def _nosleep(*a, **k): return None
    monkeypatch.setattr(SMM.asyncio, "sleep", _nosleep)
    async def check(key, oid): return {"status": 1}
    async def cancel(key, oid): return {"success": 0}
    monkeypatch.setattr(SMM.smspool_api, "check_sms", check)
    monkeypatch.setattr(SMM.smspool_api, "cancel_sms", cancel)
    applied = []
    async def apply(bot, bot_id, order_id, api_key, sp, chat, price, uid, name, result):
        applied.append(result.get("status")); return "pending"
    monkeypatch.setattr(SMM, "_vnum_auto_apply_result", apply)
    await SMM._vnum_auto_poll_loop(None, 1, 1, "k", "9", 5, 100.0, 7, "Iran")
    assert 5 not in applied


@pytest.mark.asyncio
async def test_days_old_stuck_order_is_not_retried_and_owner_is_alerted_once(env, monkeypatch):
    maker, (bid, oid) = env
    async def oz(*a, **k): raise AssertionError("سفارشِ چندروزه نباید دوباره لغو زده شود")
    monkeypatch.setattr(OZ, "_oz_request", oz)
    async with maker() as s:
        order = await s.get(models.SmmOrder, oid)
        order.created_at = dt.datetime.utcnow() - dt.timedelta(days=3)
        order.tracking_code = 77
        await s.commit()
    bot = FakeBot()
    assert await OZ._ozv_maybe_autocancel(bot, order, {"token": "t"}) is False
    async with maker() as s:
        order = await s.get(models.SmmOrder, oid)
    assert await OZ._ozv_maybe_autocancel(bot, order, {"token": "t"}) is False
    assert len(bot.sent) == 1 and "77" in bot.sent[0]            # فقط یک‌بار اعلان
