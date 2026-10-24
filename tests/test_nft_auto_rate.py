"""PATCH_NFT_AUTO_TON_RATE_V1 — نرخِ خودکارِ TON در مارکتِ NFT."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.bots.virtual_service.nft_market as NM
from tenant.gateways import stars_rate


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="virtual_service", plan="pro", status="active"))
        await s.commit()
    NM._AUTO_RATE_CACHE.clear()
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_manual_only_by_default(db):
    await tenant.set_setting(9, "nft_ton_rate", "150000")
    assert await NM._ton_rate(9) == 150_000


@pytest.mark.asyncio
async def test_auto_rate_live_margin_cache_and_fallback(db, monkeypatch):
    await tenant.set_setting(9, "nft_ton_rate", "150000")
    await tenant.set_setting(9, "nft_ton_rate_auto", "1")
    await tenant.set_setting(9, "nft_ton_margin", "10")
    seen = []

    async def live(margin):
        seen.append(margin)
        return 200_000 * (1 + margin / 100)
    monkeypatch.setattr(stars_rate, "ton_toman", live)
    assert await NM._ton_rate(9) == 220_000 and seen == [10.0]
    assert await NM._ton_rate(9) == 220_000 and len(seen) == 1            # کش
    # قطعیِ لحظه‌ای: آخرین نرخِ موفق نگه داشته می‌شود
    NM._AUTO_RATE_CACHE[9] = (0.0, 220_000)

    async def dead(margin):
        return 0.0
    monkeypatch.setattr(stars_rate, "ton_toman", dead)
    assert await NM._ton_rate(9) == 220_000
    NM._AUTO_RATE_CACHE.clear()
    assert await NM._ton_rate(9) == 150_000                                # هیچ نرخی نبود → دستی
    await tenant.set_setting(9, "nft_ton_rate_auto", "0")
    assert await NM._ton_rate(9) == 150_000
