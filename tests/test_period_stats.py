"""PATCH_PERIOD_STATS_V1 — آمار زمانیِ سفارش/شارژ/زیرمجموعه."""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant.shared.period_stats as PS

NOW = dt.datetime(2026, 10, 14, 7, 0)          # = ۱۰:۳۰ به وقتِ ایران


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(PS, "Session", maker)
    h = lambda **k: NOW - dt.timedelta(**k)
    async with maker() as s:
        s.add(models.Bot(id=1, owner_id=1, token="1:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=1, telegram_id=10, full_name="A"))
        for i, (age, st, price) in enumerate([(dict(minutes=20), "sent", 1000), (dict(hours=2), "completed", 2000),
                                              (dict(days=3), "failed", 9999), (dict(days=20), "completed", 3000),
                                              (dict(days=40), "completed", 4000)]):
            s.add(models.SmmOrder(bot_id=1, user_telegram_id=10, service_id="1", link="l", price_toman=price,
                                  status=st, created_at=h(**age)))
        s.add(models.GiftOrder(bot_id=1, buyer_user_id=1, gift_id="premium_3", star_count=0, price_toman=500,
                               dest_telegram_id=1, dest_label="x", status="sent", created_at=h(minutes=5)))
        s.add(models.WalletTxn(bot_id=1, user_id=1, amount=50_000, kind="apay_charge", created_at=h(minutes=30)))
        s.add(models.WalletTxn(bot_id=1, user_id=1, amount=-7_000, kind="purchase", created_at=h(minutes=30)))
        s.add(models.WalletTxn(bot_id=1, user_id=1, amount=20_000, kind="charge", created_at=h(days=2)))
        await s.commit()
    yield maker
    await engine.dispose()


def test_period_labels():
    labels = [l for l, _ in PS.period_starts(NOW)]
    assert labels[0] == "کل" and labels[2] == "امروز (تا 10:30)"


@pytest.mark.asyncio
async def test_orders_count_only_real_orders_across_tables(db):
    rows = {l: (n, t) for l, n, t in await PS.orders_report(1, NOW)}
    assert rows["کل"] == (5, 1000 + 2000 + 3000 + 4000 + 500)          # failed نمی‌شمارد
    assert rows["۱ ساعتِ گذشته"] == (2, 1500)                           # سفارشِ SMM + گیفت
    assert rows["امروز (تا 10:30)"][0] == 3
    assert rows["۱ ماهِ گذشته"][0] == 4


@pytest.mark.asyncio
async def test_topups_only_positive_charge_kinds(db):
    rows = {l: (n, t) for l, n, t in await PS.topups_report(1, NOW)}
    assert rows["کل"] == (2, 70_000) and rows["۱ ساعتِ گذشته"] == (1, 50_000)


@pytest.mark.asyncio
async def test_text_has_three_blocks(db):
    t = await PS.build_text(1, NOW)
    assert "آمار سفارشات" in t and "آمار افزایش موجودی‌ها" in t and "آمار زیرمجموعه‌گیری" in t
