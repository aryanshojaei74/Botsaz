"""آیتمِ دستیِ مارکتِ NFT: لینکِ t.me در «شماره/تصویر» نرمال می‌شود و ثبتِ تکراری ۴۰۹ می‌دهد (PATCH_NFT_ITEM_LINK_DEDUP_V1)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
from tenant.bots.virtual_service import nft_market, nft_p2p


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(nft_market, "Session", maker)
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:T", username="shop", field="virtual_service", plan="pro", status="active")
        s.add(b)
        await s.commit()
        bid = b.id

    async def ok(*a, **k):
        return None
    monkeypatch.setattr(nft_market, "_owner_only", ok)
    calls = []

    async def fake_meta(link):
        calls.append(link)
        return {"link": link, "ref": "243275", "collection": "Ice Cream", "title": "Ice Cream #243275",
                "image": "https://nft.fragment.com/gift/icecream-243275.large.jpg",
                "model": "Gold Ingot", "color": "English Violet", "symbol": "Trinity Knot"}
    monkeypatch.setattr(nft_p2p, "fetch_gift_meta", fake_meta)
    app = FastAPI()
    app.include_router(nft_market.nft_market_router)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    yield client, maker, bid, calls
    await client.aclose()


BODY = {"kind": "gift", "title": "Ice Cream", "ref": "T.me/Nft/IceCream-243275",
        "image": "T.me/Nft/IceCream-243275", "mode": "buy", "sale_price": 3000000, "status": "available"}


@pytest.mark.asyncio
async def test_link_is_normalised_and_filled(env):
    c, maker, bid, calls = env
    r = await c.post(f"/api/miniapp2/{bid}/admin/item", json=BODY)
    assert r.status_code == 200, r.text
    async with maker() as s:
        it = await s.scalar(select(models.NftItem))
    assert it.ref == "243275"
    assert it.image_url.startswith("https://") and it.image_url.endswith(".jpg")
    assert (it.model, it.color, it.symbol, it.collection) == ("Gold Ingot", "English Violet", "Trinity Knot", "Ice Cream")


@pytest.mark.asyncio
async def test_duplicate_is_rejected_but_sold_one_does_not_block(env):
    c, maker, bid, _ = env
    assert (await c.post(f"/api/miniapp2/{bid}/admin/item", json=BODY)).status_code == 200
    r = await c.post(f"/api/miniapp2/{bid}/admin/item", json=BODY)
    assert r.status_code == 409 and r.json()["detail"] == "duplicate"
    async with maker() as s:
        assert len((await s.scalars(select(models.NftItem))).all()) == 1
        it = await s.scalar(select(models.NftItem))
        it.status = "sold"
        await s.commit()
    assert (await c.post(f"/api/miniapp2/{bid}/admin/item", json=BODY)).status_code == 200   # گیفتِ فروخته‌شده مانع نیست


@pytest.mark.asyncio
async def test_non_url_image_is_dropped_and_plain_number_ref_kept(env):
    c, maker, bid, _ = env
    body = {"kind": "gift", "title": "Plush Pepe", "ref": "55", "image": "not a url", "mode": "buy",
            "sale_price": 1000, "status": "available"}
    assert (await c.post(f"/api/miniapp2/{bid}/admin/item", json=body)).status_code == 200
    async with maker() as s:
        it = await s.scalar(select(models.NftItem))
    assert it.ref == "55" and it.image_url is None
