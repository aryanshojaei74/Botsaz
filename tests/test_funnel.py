"""PATCH_FUNNEL_V1 — قیفِ خرید از دادهٔ موجود."""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import funnel as FN


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, FN):
        monkeypatch.setattr(mod, "Session", maker)
    now = datetime(2026, 10, 31, 12, 0)
    async with maker() as s:
        s.add(models.Bot(id=8, owner_id=1, token="8:T", username="b", field="virtual_service", plan="pro", status="active"))
        for uid, tg, days, pv in ((1, 101, 2, False), (2, 102, 3, False), (3, 103, 4, False), (4, 104, 5, False), (5, 105, 6, False),
                                  (6, 106, 40, False)):
            s.add(models.BotUser(id=uid, bot_id=8, telegram_id=tg, full_name=str(uid), joined_at=now - timedelta(days=days), phone_verified=pv))
        # ۱: شارژ + دو خریدِ کیف‌پولی؛ ۲: فقط شارژ؛ ۳: خریدِ مستقیمِ SMM بدونِ شارژ؛ ۶ (قدیمی): شارژ و خرید
        s.add(models.WalletTxn(bot_id=8, user_id=1, amount=100_000, kind="zarinpal_charge"))
        s.add(models.WalletTxn(bot_id=8, user_id=1, amount=-30_000, kind="purchase"))
        s.add(models.WalletTxn(bot_id=8, user_id=1, amount=-20_000, kind="renew"))
        s.add(models.WalletTxn(bot_id=8, user_id=2, amount=50_000, kind="charge"))
        s.add(models.WalletTxn(bot_id=8, user_id=2, amount=5_000, kind="charge"))
        s.add(models.SmmOrder(bot_id=8, user_telegram_id=103, service_id=1, service_name="s", link="l", quantity=1,
                              price_toman=1000, status="completed"))
        s.add(models.WalletTxn(bot_id=8, user_id=6, amount=9_000, kind="charge"))
        s.add(models.WalletTxn(bot_id=8, user_id=6, amount=-9_000, kind="purchase"))
        await s.commit()
    yield maker, now
    await engine.dispose()


def _d(res):
    return {name.split(" ", 1)[1]: n for name, n in res["stages"]}


@pytest.mark.asyncio
async def test_funnel_counts_cohort_and_dedupes_purchases(env):
    maker, now = env
    r = await FN.compute(8, 30, now)
    d = _d(r)
    assert d["عضو شدند"] == 5                                 # کاربرِ ۴۰روزه خارج از بازه
    assert d["حداقل یک بار شارژ کرد"] == 2                    # ۱ و ۲
    assert d["حداقل یک خرید"] == 2                            # ۱ (کیف‌پول) و ۳ (سفارشِ مستقیم)
    assert d["دو خرید یا بیشتر"] == 1                         # فقط ۱ (خرید + تمدید)
    assert all("شماره" not in n for n, _ in r["stages"])      # گیتِ شماره استفاده نشده
    allr = _d(await FN.compute(8, 0, now))
    assert allr["عضو شدند"] == 6 and allr["حداقل یک خرید"] == 3


@pytest.mark.asyncio
async def test_phone_stage_only_when_gate_in_use_and_analysis(env):
    maker, now = env
    names = [n for n, _ in (await FN.compute(8, 30, now))["stages"]]
    assert all("شماره" not in n for n in names)
    async with maker() as s:
        u = await s.get(models.BotUser, 1)
        u.phone_verified = True
        await s.commit()
    r = await FN.compute(8, 30, now)
    assert r["stages"][1][0].endswith("شماره تأیید شد") and r["stages"][1][1] == 1
    w = FN.analyse([("a", 10), ("b", 8), ("c", 2), ("d", 1)])
    assert w == (2, 75.0)
    assert FN.analyse([("a", 0), ("b", 0)]) is None


@pytest.mark.asyncio
async def test_render_and_view(env):
    maker, now = env
    text = FN.render(await FN.compute(8, 30, now))
    assert "قیفِ خرید" in text and "بزرگ‌ترین ریزش" in text and "30" in text.replace("۳۰", "30")
    empty = FN.render({"stages": [("👥 عضو شدند", 0), ("🛒 حداقل یک خرید", 0)], "days": 7})
    assert "کاربرِ جدیدی نبوده" in empty
    t, kb = await FN._view(8, 7)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert {"fnl:7", "fnl:30", "fnl:0", "pst:menu"} <= set(cbs)
