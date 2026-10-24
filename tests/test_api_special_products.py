"""PATCH_API_SPECIAL_PRODUCTS_V1 — استارز/پرمیوم/گیفت/ری‌اکشنِ استارزی در وب‌سرویسِ v2."""
import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant
from tenant.shared import public_api as pa
from tenant.shared import public_api_special as sp


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    from tenant.bots.virtual_service import smm
    for mod in (pa, sp, config, tenant, smm):
        monkeypatch.setattr(mod, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    sent = []

    async def _notify(bot, row, text, kb=None, photo_file_id=None):
        sent.append(text)

    async def _noop(*a, **k):
        return None

    async def _off(*a, **k):
        return False
    monkeypatch.setattr(smm, "_notify_manual_order", _notify)
    monkeypatch.setattr(smm, "_notify_gift_order_trust_success", _noop)
    monkeypatch.setattr(smm, "_tgt_premium_on", _off)
    monkeypatch.setattr(smm, "_tgt_gift_on", _off)
    monkeypatch.setattr(smm, "_tgt_premium_price", _noop)
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service",
                         plan="pro", status="active", api_key="BOTKEY"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="Ali", username="ali",
                             balance=10_000_000, api_key="UKEY", api_access=True))
        s.add(models.BotUser(id=2, bot_id=5, telegram_id=888, full_name="Reza", username="reza", balance=0))
        for i, act in enumerate(("stars_shop", "premium_shop", "gift_shop", "star_reaction_shop")):
            s.add(models.MainButton(bot_id=5, row=i, col=0, label=act, action=act))
        for k, v in (("stars_sell_rate", "1500"), ("stars_sell_min", "50"), ("stars_sell_max", "5000"),
                     ("srx_rate", "2000"), ("srx_min", "5"), ("premium_custom_price_3", "900000")):
            s.add(models.Setting(bot_id=5, key=k, value=v))
        s.add(models.TelegramGiftConfig(id=3, bot_id=5, gift_id="5170145012310081615", star_count=15,
                                        emoji="💝", active=True))
        s.add(models.TelegramGiftConfig(id=4, bot_id=5, gift_id="manual_x", star_count=0, active=True,
                                        is_manual=True, manual_price_toman=50000))
        await s.commit()
    yield maker, sent
    await engine.dispose()


class _Req:
    query_params = {}
    headers = {}


async def _v2(monkeypatch, **params):
    async def fake(_r):
        return {k: str(v) for k, v in params.items()}
    monkeypatch.setattr(pa, "_v2_params", fake)
    r = await pa.smm_v2(_Req())
    return json.loads(r.body)


@pytest.mark.asyncio
async def test_services_list_special(db, monkeypatch):
    rows = {x["service"]: x for x in await _v2(monkeypatch, key="UKEY", action="services")}
    assert rows[sp.STARS_ID]["rate"] == "1500000" and rows[sp.STARS_ID]["min"] == "50"
    assert rows[sp.STARS_ID]["category"] == "تلگرام - استارز"
    assert rows[sp.REACTION_ID]["rate"] == "2000000"
    assert rows[sp.PREMIUM_BASE + 3]["price"] == "900000" and rows[sp.PREMIUM_BASE + 3]["max"] == "1"
    assert sp.GIFT_BASE + 3 in rows
    assert sp.GIFT_BASE + 4 not in rows            # گیفتِ NFT/سفارشی تک‌نسخه است


@pytest.mark.asyncio
async def test_hidden_when_button_removed(db, monkeypatch):
    maker, _ = db
    from sqlalchemy import delete
    async with maker() as s:
        await s.execute(delete(models.MainButton).where(models.MainButton.action == "stars_shop"))
        await s.commit()
    rows = {x["service"] for x in await _v2(monkeypatch, key="UKEY", action="services")}
    assert sp.STARS_ID not in rows
    r = await _v2(monkeypatch, key="UKEY", action="add", service=sp.STARS_ID, link="@reza", quantity=100)
    assert r == {"error": "Incorrect service ID"}


@pytest.mark.asyncio
async def test_reaction_order_and_status(db, monkeypatch):
    maker, sent = db
    r = await _v2(monkeypatch, key="UKEY", action="add", service=sp.REACTION_ID,
                  link="https://t.me/mychan/12", quantity=10, anonymous=1)
    oid = r["order"]
    assert oid > sp.ORDER_BASE
    async with maker() as s:
        u = await s.get(models.BotUser, 1)
        o = await s.get(models.GiftOrder, oid - sp.ORDER_BASE)
    assert float(u.balance) == 10_000_000 - 20_000
    assert o.gift_id.startswith("srx_") and o.anonymous and o.status == "awaiting_admin"
    assert "ری‌اکشن" in sent[-1] and "ناشناس" in sent[-1]
    st = await _v2(monkeypatch, key="UKEY", action="status", order=oid)
    assert st["status"] == "Pending" and st["charge"] == "20000.0"
    # کاربرِ دیگر سفارشِ این کاربر را نمی‌بیند؛ کلیدِ ربات می‌بیند
    async with maker() as s:
        (await s.get(models.BotUser, 2)).api_key = "RKEY"
        (await s.get(models.BotUser, 2)).api_access = True
        await s.commit()
    assert await _v2(monkeypatch, key="RKEY", action="status", order=oid) == {"error": "Incorrect order ID"}
    assert (await _v2(monkeypatch, key="BOTKEY", action="status", orders=f"{oid},1"))[str(oid)]["status"] == "Pending"


@pytest.mark.asyncio
async def test_reaction_bad_link(db, monkeypatch):
    r = await _v2(monkeypatch, key="UKEY", action="add", service=sp.REACTION_ID, link="@chan", quantity=10)
    assert "Incorrect link" in r["error"]


@pytest.mark.asyncio
async def test_premium_order_unknown_dest(db, monkeypatch):
    maker, sent = db
    r = await _v2(monkeypatch, key="UKEY", action="add", service=sp.PREMIUM_BASE + 3,
                  link="https://t.me/someone_new", quantity=1)
    async with maker() as s:
        o = await s.get(models.GiftOrder, r["order"] - sp.ORDER_BASE)
    assert o.gift_id == "premium_3" and o.price_toman == 900000 and o.dest_telegram_id == 0
    assert "@someone_new" in o.dest_label
    assert (await _v2(monkeypatch, key="UKEY", action="add", service=sp.PREMIUM_BASE + 3,
                      link="@reza", quantity=2)) == {"error": "Quantity must be 1"}


@pytest.mark.asyncio
async def test_gift_order_known_dest_and_discount(db, monkeypatch):
    maker, _ = db
    async with maker() as s:
        (await s.get(models.BotUser, 1)).api_discount_pct = 10
        await s.commit()
    from tenant.bots.virtual_service import smm
    unit, _ = await smm._gift_price_and_label(
        type("G", (), {"is_manual": False, "star_count": 15, "emoji": "💝"})(), 5)
    r = await _v2(monkeypatch, key="UKEY", action="add", service=sp.GIFT_BASE + 3, link="@Reza", quantity=2)
    async with maker() as s:
        o = await s.get(models.GiftOrder, r["order"] - sp.ORDER_BASE)
        u = await s.get(models.BotUser, 1)
    disc_unit = round(unit * 90 / 100)
    assert o.dest_telegram_id == 888 and o.quantity == 2 and o.deliver_via == ""
    assert o.price_toman == disc_unit
    assert float(u.balance) == 10_000_000 - disc_unit * 2
    assert (await _v2(monkeypatch, key="UKEY", action="add", service=sp.GIFT_BASE + 4,
                      link="@reza", quantity=1)) == {"error": "Incorrect service ID"}


@pytest.mark.asyncio
async def test_stars_uses_bot_flow_with_price(db, monkeypatch):
    from tenant.bots.virtual_service import smm
    seen = {}

    async def fake_place(bot, row, **kw):
        seen.update(kw)
        return {"order_id": 42, "tracking": "1", "price": kw["price_override"], "dest_label": ""}
    monkeypatch.setattr(smm, "place_stars_order", fake_place)
    r = await _v2(monkeypatch, key="UKEY", action="add", service=sp.STARS_ID, link="@unknown_guy", quantity=100)
    assert r == {"order": sp.ORDER_BASE + 42}
    assert seen["dest_id"] == 0 and seen["dest_username"] == "unknown_guy"
    assert seen["price_override"] == 150_000 and seen["buyer_tg_id"] == 777
    r = await _v2(monkeypatch, key="UKEY", action="add", service=sp.STARS_ID, link="@reza", quantity=10)
    assert "between" in r["error"]


@pytest.mark.asyncio
async def test_insufficient_balance(db, monkeypatch):
    maker, _ = db
    async with maker() as s:
        (await s.get(models.BotUser, 1)).balance = 100
        await s.commit()
    r = await _v2(monkeypatch, key="UKEY", action="add", service=sp.REACTION_ID,
                  link="https://t.me/mychan/12", quantity=10)
    assert r == {"error": "Not enough funds on balance"}
    async with maker() as s:
        assert float((await s.get(models.BotUser, 1)).balance) == 100


def test_docs_mention_special():
    assert "ری‌اکشنِ استارزی" in pa.render_docs_html("https://x")
