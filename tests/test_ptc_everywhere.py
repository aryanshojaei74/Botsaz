"""PATCH_PTC_EVERYWHERE_V1 — تخفیفِ «نفراتِ برتر» در همهٔ مسیرهایِ خریدِ پلن (Whale Support)."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_bot as pb


@pytest_asyncio.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for m in (config, pb):
        monkeypatch.setattr(m, "Session", mk)
    import platform_promoters, platform_user_discounts
    for m in (platform_promoters, platform_user_discounts):
        if hasattr(m, "Session"):
            monkeypatch.setattr(m, "Session", mk)
    yield mk
    await eng.dispose()


def test_price_after():
    assert pb.price_after(100_000, 30, 0) == 70_000
    assert pb.price_after(100_000, 0, 15_000) == 85_000
    assert pb.price_after(10_000, 90, 50_000) == 0


@pytest.mark.asyncio
async def test_display_discount_uses_top_customer_percent(db, monkeypatch):
    async def ptc(uid):
        return "percent", 30
    monkeypatch.setattr(pb, "ptc_discount_for", ptc)
    pct, flat, note = await pb.display_plan_discount(111, "growth")
    assert pct == 30 and flat == 0 and "نفراتِ برتر" in note and "۳۰" not in note and "30" in note


@pytest.mark.asyncio
async def test_display_discount_keeps_bigger_other_discount(db, monkeypatch):
    async def ptc(uid):
        return "percent", 10
    monkeypatch.setattr(pb, "ptc_discount_for", ptc)
    monkeypatch.setattr(pb, "referral_discount", lambda n: 20)
    pct, flat, note = await pb.display_plan_discount(111, "growth")
    assert pct == 20 and note == ""            # تخفیفِ بزرگ‌تر می‌ماند و یادداشتِ نفراتِ برتر نمی‌آید


@pytest.mark.asyncio
async def test_display_discount_flat(db, monkeypatch):
    async def ptc(uid):
        return "flat", 25_000
    monkeypatch.setattr(pb, "ptc_discount_for", ptc)
    pct, flat, note = await pb.display_plan_discount(111, "growth")
    assert flat == 25_000 and pct >= 0 and "ماه" in note
