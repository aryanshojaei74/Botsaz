"""PATCH_TNUMBER_PROVIDER_V1 — ارائه‌دهندهٔ TNumber کنارِ OzVinoo.
پاسخ‌هایِ کشورها/خطاها عیناً از APIِ واقعی (۱۴۰۵/۰۷/۱۵) برداشته شده‌اند."""
import json

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.virtual_service import ozvinoo_vnum as oz

COUNTRIES = {"success": True, "data": [
    {"country": "US", "countrycode": "US", "dial_code": "++++1", "name": " آمریکا2🇺🇸", "count": 6, "sellprice": 90000},
    {"country": "CG", "countrycode": "CG", "dial_code": "+242", "name": "کنگو🇨🇬", "count": 94, "sellprice": 90000},
    {"country": "US", "countrycode": "US", "dial_code": "+1", "name": "آمریکا🇺🇸", "count": 43, "sellprice": 103000},
    {"country": "IR", "countrycode": "IR", "dial_code": "+98", "name": "ایران🇮🇷", "count": 0, "sellprice": 150000},
]}


@pytest_asyncio.fixture
async def api():
    state = {"balance": 0, "bought": [], "canceled": [], "code": None, "buy_resp": None}

    async def balance(req):
        assert req.headers.get("X-API-Key") == "KEY"
        return web.json_response({"success": True, "data": {"balance": state["balance"]}})

    async def countries(req):
        return web.json_response(COUNTRIES)

    async def buy(req):
        if state["balance"] <= 0:
            return web.json_response({"success": False, "error": "موجودی کافی نیست",
                                      "code": "INSUFFICIENT_BALANCE", "balance": 0, "required": 150000})
        state["bought"].append(req.query["country"])
        return web.json_response(state["buy_resp"] or {
            "success": True, "data": {"phonenumber": "+15550001234", "price": 103000, "balance": 1}})

    async def code(req):
        phone = req.match_info["phone"]
        if phone not in ("+15550001234",):
            return web.json_response({"success": False, "error": "اکانت یافت نشد", "code": "ACCOUNT_NOT_FOUND"})
        if state["code"] is None:
            return web.json_response({"success": False, "error": "کد هنوز نیامده", "code": "CODE_PENDING"})
        return web.json_response({"success": True, "data": {"code": state["code"]}})

    async def cancel(req):
        phone = req.query.get("phonenumber")
        if phone != "+15550001234":
            return web.json_response({"success": False, "error": "اکانت یافت نشد", "code": "ACCOUNT_NOT_FOUND"})
        state["canceled"].append(phone)
        return web.json_response({"success": True, "data": {"refunded": 103000}})

    app = web.Application()
    app.router.add_get("/api/v1/account/balance", balance)
    app.router.add_get("/api/v1/buy/available-countries", countries)
    app.router.add_post("/api/v1/buy/account", buy)
    app.router.add_get("/api/v1/account/code/{phone}", code)
    app.router.add_post("/api/v1/order/cancel", cancel)
    srv = TestServer(app)
    await srv.start_server()
    prov = {"id": "tn1", "kind": "tnumber", "token": "KEY", "active": True, "margin_pct": 10,
            "base_url": str(srv.make_url("/api/v1"))}
    yield prov, state
    await srv.close()


@pytest.mark.asyncio
async def test_countries_merge_duplicate_iso_at_highest_price(api):
    prov, _ = api
    ok, err, items = await oz._tn_countries(prov)
    assert ok, err
    by = {i["iso"]: i for i in items}
    assert by["US"]["price"] == 103000 and by["US"]["count"] == 49     # دو ردیفِ US → بالاترین قیمت
    assert set(by["US"]["dials"]) == {"+1"}
    assert "2" not in by["US"]["name"] and by["US"]["name"].count("🇺🇸") == 1


@pytest.mark.asyncio
async def test_buy_errors_with_http_200_are_errors(api):
    prov, state = api
    ok, err, ref, phone = await oz._pv_buy(prov, "IR", None)
    assert not ok and "موجودی" in err and state["bought"] == []


@pytest.mark.asyncio
async def test_buy_code_cancel_flow(api):
    prov, state = api
    state["balance"] = 500000
    ok, err, ref, phone = await oz._pv_buy(prov, "US", None)
    assert ok and phone == ref == "+15550001234" and state["bought"] == ["US"]
    assert await oz._pv_fetch_code(prov, phone) == ("pending", "", "کد هنوز نیامده")
    state["code"] = "54321"
    assert (await oz._pv_fetch_code(prov, phone))[:2] == ("ok", "54321")
    assert (await oz._pv_fetch_code(prov, "+19999999999"))[0] == "missing"
    assert (await oz._pv_cancel(prov, phone))[0] is True
    assert (await oz._pv_cancel(prov, "+19999999999"))[0] is False


@pytest.mark.asyncio
async def test_buy_success_without_phone_is_not_charged(api):
    prov, state = api
    state["balance"] = 500000
    state["buy_resp"] = {"success": True, "data": {"weird": "x"}}
    ok, err, ref, phone = await oz._pv_buy(prov, "US", None)
    assert not ok and err.startswith("UNPARSED:")


def test_code_parser_only_accepts_code_like_values():
    assert oz._tn_extract_code({"code": "12345"}) == "12345"
    assert oz._tn_extract_code({"data": {"otp": "۸۷۶۵۴"}}) == "87654"
    assert oz._tn_extract_code({"message": "Login code: 99881. Do not give"}) == "99881"
    assert oz._tn_extract_code({"code": "ACCOUNT_NOT_FOUND"}) == ""
    assert oz._tn_extract_code(None) == ""


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(oz, "Session", maker)
    monkeypatch.setattr(tenant, "Session", maker)
    for cache in ("_SETTING_CACHE", "_settings_cache", "_SETTINGS_CACHE"):
        if hasattr(tenant, cache) and hasattr(getattr(tenant, cache), "clear"):
            getattr(tenant, cache).clear()
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="v", field="virtual_service",
                         plan="pro", status="active"))
        await s.commit()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_creates_iso_services_and_provider_lookup(api, db):
    prov, _ = api
    await oz._save_providers(9, [prov])
    ok, msg, n = await oz._sync_provider(9, prov)
    assert ok and n == 3, msg
    async with db() as s:
        rows = {r.service_id: r for r in (await s.scalars(select(models.SmmService))).all()}
    us = rows["ozv:tn1:US"]
    assert int(us.sell_rate) == int(round(103000 * 1.10)) * 1000 and us.is_active
    assert rows["ozv:tn1:IR"].is_active is False                     # موجودیِ صفر
    p2, ref = await oz._service_provider(9, us)
    assert p2["kind"] == "tnumber" and ref == "US"
    # کلیدهایِ اضافهٔ provider (مثلِ base_url / cost_adjust) هنگامِ خواندن حفظ می‌شوند
    assert (await oz._providers(9))[0]["base_url"] == prov["base_url"]
