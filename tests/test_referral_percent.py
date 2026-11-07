"""#Fadayi — پاداشِ درصدیِ زیرمجموعه‌گیری + مدتِ پاداش + خدماتِ مجازی."""
import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.shared import extras


class _Bot:
    def __init__(self):
        self.sent = []
        self.session = type("S", (), {"close": staticmethod(lambda: _noop())})()

    async def send_message(self, chat_id, text, **k):
        self.sent.append((chat_id, text))


async def _noop():
    return None


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", mk)
    monkeypatch.setattr(extras, "Session", mk)
    async with mk() as s:
        s.add(models.Bot(id=3, owner_id=1, token="3:T", username="b", field="virtual_service", plan="pro", status="active"))
        inv = models.BotUser(bot_id=3, telegram_id=100, balance=Decimal(0))
        buyer = models.BotUser(bot_id=3, telegram_id=200, balance=Decimal(0))
        s.add_all([inv, buyer])
        await s.flush()
        link = models.ReferralLink(bot_id=3, user_id=inv.id, code="abc", reward_per_join=0)
        s.add(link)
        await s.flush()
        s.add(models.ReferralJoin(bot_id=3, referral_link_id=link.id, inviter_user_id=inv.id,
                                  referred_telegram_id=200, reward_paid=0, purchase_rewarded=False))
        for k, v in (("referral_reward_type", "percent_all"), ("referral_reward_pct", "5"),
                     ("referral_reward_mode", "join")):          # حالتِ «عضویت» نباید مانع شود
            s.add(models.Setting(bot_id=3, key=k, value=v))
        await s.commit()
    yield mk
    await eng.dispose()


async def _inv_bal(mk):
    async with mk() as s:
        return float(await s.scalar(select(models.BotUser.balance).where(models.BotUser.telegram_id == 100)))


@pytest.mark.asyncio
async def test_percent_all_pays_every_purchase_even_in_join_mode(env):
    b = _Bot()
    await extras.handle_referral_purchase(3, 200, b, amount=1000)
    await extras.handle_referral_purchase(3, 200, b, amount=2000)
    assert await _inv_bal(env) == 150            # ۵٪ از ۳۰۰۰


@pytest.mark.asyncio
async def test_missing_amount_falls_back_to_last_wallet_debit(env):
    async with env() as s:
        uid = await s.scalar(select(models.BotUser.id).where(models.BotUser.telegram_id == 200))
        s.add(models.WalletTxn(bot_id=3, user_id=uid, amount=-4000, kind="purchase", note="x",
                               created_at=dt.datetime.utcnow()))
        await s.commit()
    await extras.handle_referral_purchase(3, 200, _Bot())
    assert await _inv_bal(env) == 200            # ۵٪ از ۴۰۰۰


@pytest.mark.asyncio
async def test_reward_days_limit(env):
    async with env() as s:
        s.add(models.Setting(bot_id=3, key="referral_reward_days", value="7"))
        rj = await s.scalar(select(models.ReferralJoin))
        rj.created_at = dt.datetime.utcnow() - dt.timedelta(days=10)
        await s.commit()
    await extras.handle_referral_purchase(3, 200, _Bot(), amount=1000)
    assert await _inv_bal(env) == 0              # از ۷ روز گذشته
    async with env() as s:
        rj = await s.scalar(select(models.ReferralJoin))
        rj.created_at = dt.datetime.utcnow() - dt.timedelta(days=2)
        await s.commit()
    await extras.handle_referral_purchase(3, 200, _Bot(), amount=1000)
    assert await _inv_bal(env) == 50


@pytest.mark.asyncio
async def test_vs_reconcile_pays_once_per_completed_order(env, monkeypatch):
    monkeypatch.setattr(extras, "Bot", lambda token, default=None: _Bot())
    async with env() as s:
        uid = await s.scalar(select(models.BotUser.id).where(models.BotUser.telegram_id == 200))
        s.add(models.GiftOrder(bot_id=3, buyer_user_id=uid, gift_id="starsell_50", star_count=50,
                               price_toman=10000, dest_telegram_id=200, dest_label="x", status="sent", quantity=1))
        await s.commit()
    assert await extras.referral_vs_reconcile_once() == 0     # اولین اجرا فقط نقطهٔ شروع را ثبت می‌کند
    async with env() as s:
        uid = await s.scalar(select(models.BotUser.id).where(models.BotUser.telegram_id == 200))
        s.add(models.GiftOrder(bot_id=3, buyer_user_id=uid, gift_id="starsell_50", star_count=50,
                               price_toman=20000, dest_telegram_id=200, dest_label="x", status="sent", quantity=1,
                               created_at=dt.datetime.utcnow() + dt.timedelta(seconds=5)))
        await s.commit()
    assert await extras.referral_vs_reconcile_once() == 1
    assert await extras.referral_vs_reconcile_once() == 0     # تکراری نیست
    assert await _inv_bal(env) == 1000                        # ۵٪ از ۲۰۰۰۰
