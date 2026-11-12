"""PATCH_TON_PROPORTIONAL_V1 + PATCH_TON_AMOUNT_ROUND_UP_V1 (SINA): کم/زیاد پرداخت‌شدنِ TON با memoِ درست."""
from decimal import Decimal

import pytest

from tenant.gateways import ton_direct as TD


def test_uniquify_never_goes_below_the_required_amount():
    for need in ("0.10149", "0.1013", "0.100001", "0.05", "1.23456789", "0.0511"):
        n = Decimal(need)
        for _ in range(300):
            assert TD._uniquify(n) >= n, need


def test_uniquify_still_unique_and_6_decimals():
    outs = {TD._uniquify(Decimal("0.2")) for _ in range(200)}
    assert len(outs) > 50 and all(o == o.quantize(Decimal("0.000001")) for o in outs)


def test_proportional_credit_rules():
    pc = TD.proportional_credit
    assert pc(100_000, "0.1", "0.1")[0] == 100_000                    # دقیق
    assert pc(100_000, "0.1", "0.0985")[0] == 100_000                 # ارفاقِ ۲٪
    assert pc(100_000, "0.1", "0.101")[0] == 100_000                  # ≤ +۲٪
    assert pc(100_000, "0.1", "0.15")[0] == 150_000                   # بیشتر ⇒ به‌نسبت
    assert pc(100_000, "0.1", "0.08")[0] == 80_000                    # کمتر ⇒ به‌نسبت
    assert pc(100_000, "0.1", "0.05")[0] == 50_000                    # دقیقاً ۵۰٪
    assert pc(100_000, "0.1", "0.049")[0] is None                     # زیرِ ۵۰٪ ⇒ دستی
    assert pc(100_000, "0.1", "0.31")[0] is None                      # بیش از ۳ برابر ⇒ دستی
    assert pc(100_000, "0", "0.1")[0] is None


def test_strict_match_charge_is_unchanged():
    import inspect
    src = inspect.getsource(TD._match_charge)
    assert "AMOUNT_TOLERANCE" in src and "PARTIAL" not in src


# ── ادغام: settle_bot با واریزِ کم/زیاد ──
import datetime as dt

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (config, tenant, TD):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._SETTING_CACHE.clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="A", balance=0))
        s.add(models.TonDirectCharge(bot_id=5, user_telegram_id=777, memo="BS111111", asset="TON", amount_toman=100_000,
                                     ton_amount=Decimal("0.1"), rate_toman=1_000_000, wallet_address="UQx", status="waiting",
                                     expires_at=dt.datetime.utcnow() + dt.timedelta(minutes=10)))
        await s.commit()

    async def addr(bot_id):
        return "UQx"
    monkeypatch.setattr(TD, "wallet_address", addr)
    sent = []

    class FakeBot:
        async def send_message(self, chat, text, **k):
            sent.append((chat, text))

        async def delete_message(self, *a, **k):
            pass
    yield maker, FakeBot(), sent
    await eng.dispose()


def _incoming(monkeypatch, value, comment="BS111111", h="tx1"):
    async def fetch(addr):
        return [{"hash": h, "comment": comment, "value_nano": int(Decimal(value) * TD.NANO)}]
    monkeypatch.setattr(TD, "fetch_incoming", fetch)


async def _bal(maker):
    async with maker() as s:
        return float((await s.get(models.BotUser, 1)).balance)


@pytest.mark.asyncio
async def test_exact_underpaid_and_overpaid_payments(env, monkeypatch):
    maker, bot, sent = env
    _incoming(monkeypatch, "0.1")
    assert await TD.settle_bot(5, bot) == 1 and await _bal(maker) == 100_000.0       # دقیق: مثلِ قبل


@pytest.mark.asyncio
async def test_underpaid_is_credited_proportionally_not_ignored(env, monkeypatch):
    maker, bot, sent = env
    _incoming(monkeypatch, "0.08")                      # ۸۰٪ (قبلاً بی‌صدا رد می‌شد)
    assert await TD.settle_bot(5, bot) == 1 and await _bal(maker) == 80_000.0
    assert any("80,000" in t for _c, t in sent)


@pytest.mark.asyncio
async def test_overpaid_is_credited_proportionally(env, monkeypatch):
    maker, bot, sent = env
    _incoming(monkeypatch, "0.15")                      # قبلاً فقط ۱۰۰٬۰۰۰ و بقیه می‌سوخت
    assert await TD.settle_bot(5, bot) == 1 and await _bal(maker) == 150_000.0


@pytest.mark.asyncio
async def test_far_off_amount_is_not_credited_and_owner_is_told_once(env, monkeypatch):
    maker, bot, sent = env
    TD._OFF_AMOUNT_NOTIFIED.clear()
    _incoming(monkeypatch, "0.03")                      # ۳۰٪
    assert await TD.settle_bot(5, bot) == 0 and await _bal(maker) == 0
    assert await TD.settle_bot(5, bot) == 0
    owner_msgs = [t for c, t in sent if c == 1]
    assert len(owner_msgs) == 1 and "BS111111" in owner_msgs[0]


@pytest.mark.asyncio
async def test_same_tx_never_credits_twice(env, monkeypatch):
    maker, bot, sent = env
    _incoming(monkeypatch, "0.08")
    await TD.settle_bot(5, bot)
    await TD.settle_bot(5, bot)
    assert await _bal(maker) == 80_000.0
