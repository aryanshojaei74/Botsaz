"""PATCH_STOCK_MANUAL_DELIVERY_V1 — تحویل دستیِ کانفیگ‌هایِ آمادهٔ انبار (VPN)."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.bots.vpn import stock_delivery as SD


class _St:
    def __init__(self, d=None):
        self.d, self.s = dict(d or {}), None

    async def get_data(self):
        return dict(self.d)

    async def update_data(self, **k):
        self.d.update(k)

    async def set_state(self, s):
        self.s = s

    async def clear(self):
        self.d, self.s = {}, None


class FakeBot:
    token = "9:T"

    def __init__(self):
        self.sent, self.copied = [], []

    async def send_message(self, chat_id, text, **k):
        self.sent.append((chat_id, text, k.get("reply_markup")))
        return SimpleNamespace(message_id=len(self.sent), chat=SimpleNamespace(id=chat_id))

    async def copy_message(self, to, frm, mid, **k):
        self.copied.append((to, frm, mid))


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    import tenant.gateways.card_purchase as CP
    import tenant.shared.extras as EX
    for mod in (config, tenant, SD, CP, EX):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        await s.flush()
        s.add(models.BotUser(id=1, bot_id=9, telegram_id=1001, full_name="Ali", balance=20_000))
        s.add(models.Config(bot_id=9, code=1, volume_gb=10, days=30, user_count=0, price=5000, status="in_stock", payload="cfg-aged"))
        await s.commit()
    yield maker
    await engine.dispose()


def _cb(data, uid=1001):
    out = []

    class M:
        message_id = 5

        async def edit_text(self, t, **k):
            out.append(t)

        async def answer(self, t, **k):
            out.append(t)
    cb = SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid, full_name="Ali", username="ali"), message=M())

    async def ans(*a, **k):
        out.append(("ans", a))
    cb.answer = ans
    return cb, out


@pytest.mark.asyncio
async def test_wallet_purchase_in_manual_mode_queues_and_leaves_stock_untouched(db):
    await tenant.set_setting(9, "stock_manual_delivery", "1")
    tenant._SETTING_CACHE.clear()
    bot = FakeBot()
    cb, out = _cb("mcfg:pay:10:30:0:5000")
    await tenant._manual_cfg_pay_impl(cb, bot, _St())
    async with db() as s:
        cfg = await s.scalar(select(models.Config))
        u = await s.get(models.BotUser, 1)
        o = await s.scalar(select(models.Order))
        d = await s.scalar(select(models.StockDelivery))
    assert cfg.status == "in_stock" and cfg.owner_user_id is None          # انبار دست‌نخورده
    assert float(u.balance) == 15_000 and o.status == "paid"                # پول کم شد، سفارش پرداخت‌شده
    assert d and d.status == "pending" and d.order_id == o.id and d.amount == 5000 and d.volume_gb == 10
    texts = " ".join(str(x[1]) for x in bot.sent)
    assert "صفِ تحویل" in texts and any(m[0] == 1 for m in bot.sent)       # مشتری + مالک خبر گرفتند
    assert not any(str(x[1]).startswith("cfg-aged") for x in bot.sent)      # کانفیگِ قدیمی فرستاده نشد


@pytest.mark.asyncio
async def test_wallet_purchase_manual_off_still_delivers_stock(db, monkeypatch):
    sent = []

    async def fake_send(bot, tg, cfg):
        sent.append(cfg.payload)
    monkeypatch.setattr(tenant, "send_config_to_user", fake_send)
    bot = FakeBot()
    cb, out = _cb("mcfg:pay:10:30:0:5000")
    await tenant._manual_cfg_pay_impl(cb, bot, _St())
    async with db() as s:
        cfg = await s.scalar(select(models.Config))
        d = await s.scalar(select(models.StockDelivery))
    assert cfg.status == "sold" and sent == ["cfg-aged"] and d is None


async def _queued(db, bot):
    await tenant.set_setting(9, "stock_manual_delivery", "1")
    tenant._SETTING_CACHE.clear()
    cb, _ = _cb("mcfg:pay:10:30:0:5000")
    await tenant._manual_cfg_pay_impl(cb, bot, _St())
    async with db() as s:
        return (await s.scalar(select(models.StockDelivery))).id


@pytest.mark.asyncio
async def test_admin_send_copies_messages_then_done_marks_delivered_once(db):
    bot = FakeBot()
    did = await _queued(db, bot)
    st = _St()
    cb, _ = _cb(f"sd:send:{did}", uid=1)
    await SD.sd_send(cb, bot, st)
    assert st.s == SD.SdFSM.sending
    msg = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=SimpleNamespace(id=1), message_id=77)
    replies = []

    async def reply(t, **k):
        replies.append(t)
    msg.answer = reply
    await SD.sd_receive(msg, bot, st)
    assert bot.copied == [(1001, 1, 77)]
    cb2, _ = _cb(f"sd:done:{did}", uid=1)
    await SD.sd_done(cb2, bot, st)
    async with db() as s:
        d = await s.get(models.StockDelivery, did)
    assert d.status == "delivered" and d.sent_count == 1 and d.delivered_by == 1
    assert any("تحویل داده شد" in str(x[1]) and x[0] == 1001 for x in bot.sent)
    cb3, out3 = _cb(f"sd:done:{did}", uid=1)                                # دوباره ⇒ رد
    await SD.sd_done(cb3, bot, st)
    assert any("قبلاً رسیدگی" in str(a) for a in out3)


@pytest.mark.asyncio
async def test_done_requires_at_least_one_sent_and_non_admin_is_denied(db):
    bot = FakeBot()
    did = await _queued(db, bot)
    cb, out = _cb(f"sd:done:{did}", uid=1)
    await SD.sd_done(cb, bot, _St())
    assert any("هنوز چیزی" in str(a) for a in out)
    cb2, out2 = _cb(f"sd:send:{did}", uid=555)                               # مشتریِ عادی
    await SD.sd_send(cb2, bot, _St())
    assert any("دسترسی ندارید" in str(a) for a in out2)
    async with db() as s:
        assert (await s.get(models.StockDelivery, did)).status == "pending"


@pytest.mark.asyncio
async def test_refund_is_atomic_and_credits_wallet_once(db):
    bot = FakeBot()
    did = await _queued(db, bot)
    cb, _ = _cb(f"sd:refund:{did}", uid=1)
    await SD.sd_refund(cb, bot)
    cb2, out2 = _cb(f"sd:refund:{did}", uid=1)
    await SD.sd_refund(cb2, bot)                                             # دوباره ⇒ دوباره پول برنگردد
    async with db() as s:
        u = await s.get(models.BotUser, 1)
        d = await s.get(models.StockDelivery, did)
        o = await s.scalar(select(models.Order))
    assert float(u.balance) == 20_000 and d.status == "refunded" and o.status == "refunded"
    assert any("قبلاً رسیدگی" in str(a) for a in out2)


@pytest.mark.asyncio
async def test_card_receipt_approval_manual_mode_releases_reservation_and_queues(db, monkeypatch):
    sent_cfgs = []

    async def fake_send(bot, tg, cfg):
        sent_cfgs.append(cfg.payload)
    monkeypatch.setattr(tenant, "send_config_to_user", fake_send)
    async with db() as s:
        cfg = await s.scalar(select(models.Config))
        cfg.status = "reserved"
        o = models.Order(bot_id=9, user_id=1, volume_gb=10, days=30, count=1, amount=5000, pay_method="mcfg_card",
                         status="awaiting_admin", reserved_config_id=cfg.id)
        s.add(o)
        await s.commit()
        oid = o.id
    await tenant.set_setting(9, "stock_manual_delivery", "1")
    tenant._SETTING_CACHE.clear()
    import tenant.gateways.card_purchase as CP
    bot = FakeBot()
    cb, out = _cb(f"rcadm:ok:{oid}", uid=1)

    async def edit_caption(*a, **k):
        return None
    cb.message.caption = None
    await CP._rcadm_decide_impl(cb, bot, _St())
    async with db() as s:
        cfg = await s.scalar(select(models.Config))
        o = await s.get(models.Order, oid)
        d = await s.scalar(select(models.StockDelivery))
    assert cfg.status == "in_stock" and cfg.owner_user_id is None and sent_cfgs == []
    assert o.status == "paid" and d and d.status == "pending" and d.order_id == oid
    assert any("صفِ تحویل" in str(x[1]) for x in bot.sent)
