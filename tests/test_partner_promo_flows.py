"""PATCH_PARTNER_PROMO_V1 — هدیهٔ جشنواره در مسیرهایِ واقعیِ خریدِ عمده (کیف پول و تأییدِ خودکار/دستی)."""
import datetime as dt
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import partner_promo as PP
import platform_partner as PPL
from tenant.partner_bot import partner_panel as PPN

OWNER = 777
START = dt.datetime(2026, 1, 1)
END = dt.datetime(2099, 1, 1)


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (PP, PPL, PPN):
        monkeypatch.setattr(mod, "Session", mk)
    async with mk() as s:
        b = models.Bot(owner_id=OWNER, token="1:T", username="partner", field="partner", plan="partner", status="active")
        s.add(b)
        await s.flush()
        s.add(models.PlatformUser(telegram_id=OWNER, balance=10_000_000))
        pb = models.PartnerBot(bot_id=b.id, owner_id=OWNER, status="active")
        s.add(pb)
        await s.commit()
        pid, bid = pb.id, b.id
    for k, v in {"ppromo_on": "1", "ppromo_start": START.strftime("%Y-%m-%dT%H:%M"), "ppromo_end": END.strftime("%Y-%m-%dT%H:%M"),
                 "ppromo_pro_buy": "2", "ppromo_pro_free": "1"}.items():
        await PP.set_value(k, v)
    sent = []

    async def fake_send(chat_id, text):
        sent.append((chat_id, text))
    monkeypatch.setattr(PPL, "_pp_send_platform", fake_send)
    return NS(mk=mk, pid=pid, bid=bid, sent=sent)


async def _pb(env):
    async with env.mk() as s:
        return await s.get(models.PartnerBot, env.pid)


async def _pending(env, g, p):
    async with env.mk() as s:
        o = models.PartnerStockOrder(partner_bot_id=env.pid, qty_growth=g, qty_pro=p, amount=1000, method="card",
                                     status="pending", created_at=dt.datetime(2026, 6, 1))
        s.add(o)
        await s.commit()
        return o.id


@pytest.mark.asyncio
async def test_auto_approve_adds_bonus_and_logs_it(env):
    oid = await _pending(env, 0, 2)
    assert await PPL.pp_order_auto_approve(oid) is True
    pb = await _pb(env)
    assert pb.stock_pro == 3 and pb.bought_pro == 2               # ۲ خرید + ۱ هدیه؛ آمارِ خرید فقط ۲
    async with env.mk() as s:
        logs = (await s.scalars(select(models.PartnerStockLog).where(models.PartnerStockLog.partner_bot_id == env.pid))).all()
    assert sorted((l.reason, l.delta) for l in logs) == [("buy", 2), ("promo", 1)]
    txt = " ".join(t for _c, t in env.sent if _c == OWNER)
    assert "هدیهٔ جشنوارهٔ خرید" in txt and "حرفه‌ای: +1" in txt


@pytest.mark.asyncio
async def test_two_single_orders_accumulate_to_one_gift_and_replay_is_safe(env):
    o1, o2 = await _pending(env, 0, 1), await _pending(env, 0, 1)
    assert await PPL.pp_order_auto_approve(o1)
    assert (await _pb(env)).stock_pro == 1                          # هنوز هدیه‌ای نیست
    assert await PPL.pp_order_auto_approve(o2)
    assert (await _pb(env)).stock_pro == 3                          # ۱+۱ خرید + ۱ هدیه
    assert await PPL.pp_order_auto_approve(o2) is False             # تأییدِ دوباره هیچ‌چیز اضافه نمی‌کند
    assert (await _pb(env)).stock_pro == 3


@pytest.mark.asyncio
async def test_no_bonus_when_promo_is_off(env):
    await PP.set_value("ppromo_on", "0")
    oid = await _pending(env, 0, 2)
    assert await PPL.pp_order_auto_approve(oid)
    assert (await _pb(env)).stock_pro == 2
    assert "هدیه" not in " ".join(t for _c, t in env.sent)


class _CB:
    def __init__(self, data="pp:pay:wallet"):
        self.data = data
        self.from_user = NS(id=OWNER)
        self.edits = []
        self.message = NS(edit_text=self._edit, answer=self._edit, chat=NS(id=1))

    async def _edit(self, text, **kw):
        self.edits.append(text)

    async def answer(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_wallet_purchase_adds_bonus_and_message(env, monkeypatch):
    async with env.mk() as s:
        row = await s.get(models.Bot, env.bid)
        pb = await s.get(models.PartnerBot, env.pid)

    async def ok(cb, bot):
        return row, pb

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(PPN, "_partner_ok", ok)
    monkeypatch.setattr(PPN, "_notify_platform_admin", noop)
    st = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=OWNER))
    await st.update_data(pp_g=0, pp_p=2, pp_total=500_000, pp_pb=env.pid)
    cb = _CB()
    await PPN.pp_pay_wallet(cb, NS(), st)
    pbn = await _pb(env)
    assert pbn.stock_pro == 3 and pbn.bought_pro == 2
    assert any("هدیهٔ جشنوارهٔ خرید" in t for t in cb.edits)
    async with env.mk() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == OWNER))
        orders = (await s.scalars(select(models.PartnerStockOrder))).all()
    assert float(u.balance) == 10_000_000 - 500_000
    assert len(orders) == 1 and orders[0].method == "wallet" and orders[0].status == "approved"
    # خریدِ تکیِ بعدی در همان بازه: مجموعِ ۳ ⇒ هدیهٔ تازه‌ای نیست (۳//2 - ۲//2 = ۰)
    await st.update_data(pp_g=0, pp_p=1, pp_total=250_000, pp_pb=env.pid)
    await PPN.pp_pay_wallet(_CB(), NS(), st)
    assert (await _pb(env)).stock_pro == 4                           # ۳ + ۱ خرید، بدونِ هدیه
