"""ممیزیِ بخشِ شمارهٔ مجازی: مالکیتِ لغو، بازیابی بعد از ری‌استارت، جلوگیری از دو بار خرید/بازگشتِ وجه."""
import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant.bots.virtual_service.ozvinoo_vnum as OZ
import tenant.bots.virtual_service.smm as SMM


class FakeBot:
    def __init__(self, token=None, **kw):
        self.sent = []
        self.session = SimpleNamespace(close=self._close)

    async def _close(self):
        pass

    async def send_message(self, chat, text, **kw):
        self.sent.append(text)


class FakeCb:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = SimpleNamespace(id=uid, full_name="u", username="u")
        self.answers = []
        self.message = SimpleNamespace(answer=self._ans)

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append(text)

    async def _ans(self, *a, **k):
        pass


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, OZ, SMM):
        monkeypatch.setattr(mod, "Session", maker)
    import tenant
    monkeypatch.setattr(tenant, "Session", maker)
    OZ._AUTOCANCEL_LAST.clear()
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:T", username="x", field="virtual_service", plan="pro", status="active")
        s.add(b); await s.flush()
        s.add(models.BotUser(bot_id=b.id, telegram_id=555, balance=0))
        s.add(models.BotUser(bot_id=b.id, telegram_id=666, balance=0))
        svc = models.SmmService(bot_id=b.id, service_id="ozv:p:1", name="n")
        s.add(svc); await s.flush()
        await s.commit()
        ids = (b.id, svc.id)
    yield maker, ids
    await engine.dispose()


async def _order(maker, bid, svc_pk, service_id, status="sent", age_min=30, uid=555, pid="123"):
    async with maker() as s:
        o = models.SmmOrder(bot_id=bid, user_telegram_id=uid, smm_service_pk=svc_pk, service_id=service_id,
                            service_name="n", link="+1", quantity=1, price_toman=5000, status=status,
                            panel_order_id=pid, panel_status="pending", tracking_code=1,
                            created_at=dt.datetime.utcnow() - dt.timedelta(minutes=age_min))
        s.add(o); await s.commit()
        return o.id


@pytest.mark.asyncio
async def test_ozv_cancel_rejects_other_users_and_non_sent_orders(env, monkeypatch):
    maker, (bid, svc) = env
    oid = await _order(maker, bid, svc, "ozv:p:1")
    done_oid = await _order(maker, bid, svc, "ozv:p:1", status="completed")
    async def boom(*a, **k): raise AssertionError("نباید به ارائه‌دهنده درخواست برود")
    monkeypatch.setattr(OZ, "_oz_request", boom)
    async def get_row(bot): return SimpleNamespace(id=bid)
    import tenant
    monkeypatch.setattr(tenant, "get_bot_row", get_row)
    other = FakeCb(f"vnum:ozcancel:{oid}", uid=666)          # کاربرِ دیگر
    await OZ.ozv_cancel_order(other, None)
    assert other.answers == ["سفارش پیدا نشد."]
    mine_done = FakeCb(f"vnum:ozcancel:{done_oid}", uid=555)  # تحویل‌شده
    await OZ.ozv_cancel_order(mine_done, None)
    assert "دیگر قابل لغو نیست" in mine_done.answers[0]


@pytest.mark.asyncio
async def test_process_code_on_refunded_order_is_canceled_not_completed(env, monkeypatch):
    maker, (bid, svc) = env
    oid = await _order(maker, bid, svc, "ozv:p:1", status="refunded")
    async def oz(*a, **k): return 200, {"data": {"code": "12345"}}
    monkeypatch.setattr(OZ, "_oz_request", oz)
    async with maker() as s:
        o = await s.get(models.SmmOrder, oid)
    bot = FakeBot()
    assert await OZ._process_code(bot, o, {"token": "t"}, 1) == "canceled" and not bot.sent


@pytest.mark.asyncio
async def test_recover_refunds_orphaned_smspool_order_after_restart(env, monkeypatch):
    maker, (bid, svc) = env
    oid = await _order(maker, bid, svc, "sp:1:2", age_min=30, pid="777")
    young = await _order(maker, bid, svc, "sp:1:2", age_min=3, pid="778")
    async def key(bot_id, k, d=""): return "KEY"
    monkeypatch.setattr(SMM, "_get_setting", key)
    monkeypatch.setattr(SMM, "Bot", FakeBot)
    async def check(k, oid_): return {"status": 1}
    async def cancel(k, oid_): return {"success": 1}
    monkeypatch.setattr(SMM.smspool_api, "check_sms", check)
    monkeypatch.setattr(SMM.smspool_api, "cancel_sms", cancel)
    assert await SMM.vnum_recover_once() == 1
    async with maker() as s:
        o = await s.get(models.SmmOrder, oid); y = await s.get(models.SmmOrder, young)
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 555))
    assert o.status == "refunded" and y.status == "sent" and float(u.balance) == 5000.0


@pytest.mark.asyncio
async def test_recover_delivers_code_if_it_arrived(env, monkeypatch):
    maker, (bid, svc) = env
    oid = await _order(maker, bid, svc, "sp:1:2", age_min=30, pid="777")
    async def key(bot_id, k, d=""): return "KEY"
    monkeypatch.setattr(SMM, "_get_setting", key)
    monkeypatch.setattr(SMM, "Bot", FakeBot)
    async def check(k, oid_): return {"status": 3, "sms": "4242", "full_sms": "code 4242"}
    async def cancel(k, oid_): raise AssertionError("کد رسیده؛ نباید لغو شود")
    monkeypatch.setattr(SMM.smspool_api, "check_sms", check)
    monkeypatch.setattr(SMM.smspool_api, "cancel_sms", cancel)
    await SMM.vnum_recover_once()
    async with maker() as s:
        o = await s.get(models.SmmOrder, oid)
    assert o.status == "completed" and "4242" in (o.note or "")
