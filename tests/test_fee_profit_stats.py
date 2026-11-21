"""PATCH_FEE_PROFIT_STATS_V1 — آمارِ کارمزد (انتقال/بازی) و سود در «آمار زمانی»."""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant.shared.period_stats as PS

NOW = dt.datetime(2026, 10, 14, 7, 0)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(PS, "Session", m)
    h = lambda **k: NOW - dt.timedelta(**k)
    async with m() as s:
        s.add(models.Bot(id=1, owner_id=1, token="1:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=1, telegram_id=10, full_name="A"))
        # کارمزدِ انتقال (فقط در یادداشت): ۳۰ دقیقه پیش ۱٬۲۰۰ ، ۳ روز پیش ۵۰۰٬۰۰۰ ، ۴۰ روز پیش ۹٬۰۰۰
        for age, fee in [(dict(minutes=30), "1,200"), (dict(days=3), "500,000"), (dict(days=40), "9,000")]:
            s.add(models.WalletTxn(bot_id=1, user_id=1, amount=0, kind="group_transfer_fee",
                                   note=f"کارمزدِ انتقال (2٪): {fee} تومان", created_at=h(**age)))
        s.add(models.WalletTxn(bot_id=1, user_id=1, amount=0, kind="group_transfer_fee", note="بدونِ عدد", created_at=h(minutes=5)))
        await s.commit()
        # کارمزدِ بازی از دفتر
        s.add(models.BetLedger(bot_id=1, bet_id=1, user_id=1, telegram_id=10, amount=-700, kind="fee", created_at=h(minutes=10)))
        s.add(models.BetLedger(bot_id=1, bet_id=1, user_id=1, telegram_id=10, amount=-300, kind="fee", created_at=h(days=2)))
        s.add(models.BetLedger(bot_id=1, bet_id=1, user_id=1, telegram_id=10, amount=5000, kind="win", created_at=h(minutes=10)))
        await s.commit()
        # سفارش‌ها: دو تا با هزینه، یکی بی‌هزینه، یکی ناموفق
        for age, st, price, cost in [(dict(minutes=20), "completed", 10_000, 6_000), (dict(days=3), "sent", 5_000, 4_000),
                                     (dict(minutes=15), "completed", 2_000, None), (dict(minutes=10), "failed", 9_000, 1_000)]:
            s.add(models.SmmOrder(bot_id=1, user_telegram_id=10, service_id="1", link="l", price_toman=price, cost_toman=cost,
                                  status=st, created_at=h(**age)))
        await s.commit()
    yield m
    await engine.dispose()


def test_fee_from_note_parsing():
    assert PS._fee_from_note("کارمزدِ انتقال (2٪): 1,234 تومان") == 1234
    assert PS._fee_from_note("بدونِ عدد") == 0 and PS._fee_from_note(None) == 0


@pytest.mark.asyncio
async def test_fees_report_by_period(db):
    rows = {l: (tr, gm) for l, tr, gm in await PS.fees_report(1, NOW)}
    assert rows["۱ ساعتِ گذشته"][0] == 1_200
    assert rows["۷ روزِ گذشته"][0] == 1_200 + 500_000
    assert rows["کل"][0] == 1_200 + 500_000 + 9_000
    assert rows["۱ ساعتِ گذشته"][1] == 700 and rows["۷ روزِ گذشته"][1] == 1_000 and rows["کل"][1] == 1_000   # فقط kind=fee؛ برد شمرده نمی‌شود


@pytest.mark.asyncio
async def test_profit_uses_only_orders_with_known_cost(db):
    rows = {l: (p, n, miss) for l, p, n, miss in await PS.profit_report(1, NOW)}
    assert rows["۱ ساعتِ گذشته"] == (4_000, 1, 1)            # ۱۰٬۰۰۰−۶٬۰۰۰ ؛ یک سفارشِ بی‌هزینه؛ ناموفق نمی‌شمارد
    assert rows["۷ روزِ گذشته"] == (4_000 + 1_000, 2, 1)
    assert rows["کل"][0] == 5_000


@pytest.mark.asyncio
async def test_build_text_contains_fee_and_profit_sections(db):
    txt = await PS.build_text(1, NOW)
    assert "آمار کارمزد" in txt and "آمار سود" in txt and "بدونِ هزینهٔ ثبت‌شده" in txt
