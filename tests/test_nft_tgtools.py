"""PATCH_NFT_TGTOOLS_V1 — منبعِ TGTools و خریدِ خودکارِ NFT."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
import tenant.bots.virtual_service.nft_market as NM
import tenant.bots.virtual_service.nft_sources as NS
import tenant.bots.virtual_service.nft_tgtools as NT
import tenant.bots.virtual_service.tgtools_api as TA

ADDR = "UQ" + "a" * 46
NFT = "0:" + "b" * 64


def test_address_and_status_helpers():
    assert NT.valid_ton_address(ADDR) and NT.valid_ton_address(NFT) and not NT.valid_ton_address("hello")
    assert NT.norm_status("Complete") == "done" and NT.norm_status("PaymentReceived") == "pending"
    assert NT.norm_status("Failed") == "failed" and NT.norm_status("Refunded") == "failed"
    assert NS.get_provider("tgtools").ready
    assert round(NS.TgToolsProvider.est_cost_ton(1.0), 3) == 1.115          # مثالِ مستند


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, NM, NS, NT):
        monkeypatch.setattr(mod, "Session", maker)
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=500, token="9:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=9, telegram_id=1001, full_name="Ali", balance=0))
        s.add(models.NftMarketSource(id=1, bot_id=9, provider="tgtools", title="T", markup_percent=10))
        s.add(models.NftItem(id=1, bot_id=9, title="Lol Pop #1", ref=NFT, mode="buy", sale_price=2_000_000,
                             cost_price=1_000_000, status="reserved", source_id=1, source_ref=NFT, holder_user_id=1))
        s.add(models.NftOrder(id=1, bot_id=9, user_id=1, item_id=1, kind="buy", amount=2_000_000, fee=0,
                              status="pending", recipient_ton=ADDR))
        await s.commit()

    async def rate(bot_id):
        return 100_000
    monkeypatch.setattr(NM, "_ton_rate", rate)

    async def key(bot_id, src):
        return "tgt_key"
    monkeypatch.setattr(NT, "_key", key)
    yield maker
    await engine.dispose()


def _fake(monkeypatch, quote, buy=None, order=None, calls=None):
    async def req(api_key, method, path, **kw):
        if calls is not None:
            calls.append((method, path, kw))
        if path.endswith("/quote"):
            return quote
        if path.endswith("/buy/auto"):
            if isinstance(buy, Exception):
                raise buy
            return buy
        return order
    monkeypatch.setattr(TA, "_request", req)


@pytest.mark.asyncio
async def test_buy_queued_then_poll_delivers(env, monkeypatch):
    calls = []
    _fake(monkeypatch, {"canBuy": True, "totalPriceTon": 1.115}, {"orderId": 12, "status": "PaymentReceived"},
          {"orderId": 12, "status": "Complete"}, calls)
    assert await NT.try_tgtools_buy(9, 1) == "queued"
    post = [c for c in calls if c[0] == "POST"][0]
    assert post[2]["json_body"] == {"nftAddress": NFT, "recipientTonAddress": ADDR, "clientOrderId": "b9-n1"}
    async with env() as s:
        o = await s.get(models.NftOrder, 1)
        assert o.source_order_id == "12" and o.transfer_mode == "auto" and o.status == "pending"
    assert await NT.try_tgtools_buy(9, 1) is None                          # دوباره نمی‌خرد
    assert await NT.poll_pending() == 1
    async with env() as s:
        assert (await s.get(models.NftOrder, 1)).status == "delivered"
        assert (await s.get(models.NftItem, 1)).status == "sold"


@pytest.mark.asyncio
async def test_price_guard_blocks_loss(env, monkeypatch):
    calls = []
    _fake(monkeypatch, {"canBuy": True, "totalPriceTon": 25.0}, {"orderId": 1}, None, calls)   # 2.5M تومان > 2M پرداختی
    assert await NT.try_tgtools_buy(9, 1) is None
    assert not [c for c in calls if c[0] == "POST"]
    async with env() as s:
        assert "ضرر" in (await s.get(models.NftOrder, 1)).transfer_note


@pytest.mark.asyncio
async def test_failure_refunds_buyer_and_hides_item(env, monkeypatch):
    _fake(monkeypatch, {"canBuy": True, "totalPriceTon": 1.0}, {"orderId": 5}, {"status": "Failed"})
    assert await NT.try_tgtools_buy(9, 1) == "queued"
    assert await NT.poll_pending() == 1
    async with env() as s:
        assert (await s.get(models.NftOrder, 1)).status == "rejected"
        assert int((await s.get(models.BotUser, 1)).balance) == 2_000_000          # عودتِ وجه
        assert (await s.get(models.NftItem, 1)).status == "hidden"


@pytest.mark.asyncio
async def test_ambiguous_response_marks_unknown_and_never_retries(env, monkeypatch):
    _fake(monkeypatch, {"canBuy": True, "totalPriceTon": 1.0}, TA.TgToolsAmbiguous("timeout"))
    assert await NT.try_tgtools_buy(9, 1) is None
    async with env() as s:
        o = await s.get(models.NftOrder, 1)
        assert o.source_order_id == "?" and "قطعی" in o.transfer_note
    assert await NT.try_tgtools_buy(9, 1) is None and await NT.poll_pending() == 0


@pytest.mark.asyncio
async def test_provider_fetch_parses_collections_and_items(env, monkeypatch):
    async def req(api_key, method, path, **kw):
        if path.endswith("/collections"):
            return {"items": [{"address": "0:c1", "name": "Lol Pop", "imageUrl": "http://i"}]}
        return {"items": [{"address": NFT, "name": "Lol Pop #1", "salePriceTon": 1.5, "isOnSale": True,
                           "displayPriceTon": 1.5},
                          {"address": "0:x", "name": "off", "isOnSale": False, "salePriceTon": 1}]}
    monkeypatch.setattr(TA, "_request", req)
    out = await NS.get_provider("tgtools").fetch("k", limit=10)
    assert len(out) == 1 and out[0].ref == NFT and out[0].collection == "Lol Pop" and out[0].image == "http://i"
    assert round(out[0].price_ton, 4) == round(1.5 * 1.05 + 0.065, 4)
