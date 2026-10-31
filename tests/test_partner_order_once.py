"""PATCH_PARTNER_ORDER_ONCE_V1 — یک پرداخت = یک ربات؛ توکنِ دوم با همان سفارش نباید ربات دومی بسازد."""
import contextlib
import datetime as dt
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bot_utils
import config
import models
from tenant.partner_bot import partner_sales as ps


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, ps):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=1, owner_id=10, token="1:P", username="partnerbot", field="virtual_service",
                         plan="partner", status="active"))
        await s.flush()
        s.add(models.PartnerBot(id=5, bot_id=1, owner_id=10, status="active", stock_growth=3, stock_pro=3))
        s.add(models.PartnerCardOrder(id=9, partner_bot_id=5, buyer_telegram_id=77, plan="growth",
                                      price=1000, status="approved"))
        await s.commit()

    @contextlib.asynccontextmanager
    async def temp_bot(token):
        async def get_me():
            return NS(username="b" + token[:3].replace(":", "x"))
        yield NS(get_me=get_me)

    async def reclaim(s, token, uid):
        return 0, False

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(bot_utils, "_temp_bot", temp_bot)
    monkeypatch.setattr(bot_utils, "reclaim_token_if_archived", reclaim)
    for name in ("set_tenant_webhook", "refresh_low_stock_timer", "log_stock"):
        monkeypatch.setattr(ps, name, noop)
    monkeypatch.setattr(ps, "invalidate_pb_cache", lambda *_: None)
    import tenant
    monkeypatch.setattr(tenant, "_purchase_alert", noop)
    yield maker
    await engine.dispose()


class _State:
    def __init__(self, data): self.data, self.cleared = data, False
    async def get_data(self): return dict(self.data)
    async def clear(self): self.cleared = True
    async def update_data(self, **k): self.data.update(k)
    async def set_state(self, *_): pass


class _Msg:
    def __init__(self, token):
        self.text, self.from_user, self.out = token, NS(id=77), []
    async def answer(self, text, **k): self.out.append(text)


@pytest.mark.asyncio
async def test_second_token_with_same_paid_order_is_refused(env):
    maker = env
    data = dict(ps_plan="growth", ps_kind="sold", ps_price=1000, ps_pb=5, ps_bot=1, ps_paid_by="card", ps_order=9)
    bot = NS(send_message=lambda *a, **k: noop())

    async def noop():
        return None
    for tok in ("111:" + "A" * 40, "222:" + "B" * 40):
        m = _Msg(tok)
        await ps.ps_token(m, bot, _State(dict(data)))
    async with maker() as s:
        sold = await s.scalar(select(func.count()).select_from(models.Bot).where(models.Bot.plan == "growth"))
        pb = await s.get(models.PartnerBot, 5)
    assert sold == 1 and pb.stock_growth == 2
    assert "قبلاً ربات ساخته شده" in m.out[-1]


class _Cb:
    def __init__(self):
        self.from_user, self.alerts, self.message = NS(id=77), [], NS(edit_text=self._noop)
    async def _noop(self, *a, **k): return None
    async def answer(self, text=None, show_alert=False): self.alerts.append(text)


@pytest.mark.asyncio
async def test_wallet_double_tap_charges_once(env, monkeypatch):
    maker = env
    async with maker() as s:
        s.add(models.BotUser(bot_id=1, telegram_id=77, balance=5000))
        await s.commit()

    async def partner_of(bot):
        async with maker() as s:
            return await s.get(models.Bot, 1), await s.get(models.PartnerBot, 5)
    monkeypatch.setattr(ps, "_partner_of", partner_of)
    st = _State(dict(ps_plan="growth", ps_price=1000))
    for _ in range(2):
        await ps.ps_pay_wallet(_Cb(), NS(), st)
    async with maker() as s:
        bu = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == 77))
        n = await s.scalar(select(func.count()).select_from(models.PartnerCardOrder).where(
            models.PartnerCardOrder.status == "approved", models.PartnerCardOrder.receipt_file_id.is_(None)))
    assert float(bu.balance) == 4000 and n == 2      # ۱ = سفارشِ از پیش‌ساخته‌شدهٔ fixture
