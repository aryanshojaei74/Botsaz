import pytest
from tenant.bots.virtual_service import ozvinoo_vnum as Z

P = {"id": "ivn1", "kind": "ivn", "token": "tok12345", "active": True}


def _patch(monkeypatch, responses):
    calls = []

    async def fake(provider, method, path, *, params=None, body=None):
        calls.append((method, path, body))
        return responses[path]
    monkeypatch.setattr(Z, "_ivn_request", fake)
    return calls


def test_kind_helpers():
    assert Z._is_ivn(P) and Z._phone_keyed(P) and not Z._is_tnumber(P)
    assert not Z._is_ivn({"kind": "tnumber"}) and Z._phone_keyed({"kind": "tnumber"})


@pytest.mark.asyncio
async def test_countries_parse(monkeypatch):
    _patch(monkeypatch, {"/accounts": (200, {"success": True, "data": {"countries": [
        {"code": "us", "name": "United States", "price": 1.5, "available": 45},
        {"code": "XX", "name": "Free", "price": 0, "available": 3}], "total": 2}})})
    ok, err, items = await Z._ivn_countries(P)
    assert ok and len(items) == 1 and items[0]["iso"] == "US" and items[0]["usd"] == 1.5 and items[0]["count"] == 45


@pytest.mark.asyncio
async def test_buy_and_errors(monkeypatch):
    calls = _patch(monkeypatch, {"/account/reserve-number": (200, {"success": True, "data": {
        "phone": "+12345678901", "country": "US", "price": 1.5, "expires_in": 600}})})
    ok, err, ref, phone = await Z._pv_buy(P, "US", None)
    assert ok and ref == phone == "+12345678901" and calls[0][2] == {"country_code": "US"}
    _patch(monkeypatch, {"/account/reserve-number": (402, {"error": "Insufficient balance"})})
    ok, err, *_ = await Z._pv_buy(P, "US", None)
    assert not ok and "کافی نیست" in err
    _patch(monkeypatch, {"/account/reserve-number": (404, {"error": "No numbers available"})})
    ok, err, *_ = await Z._pv_buy(P, "US", None)
    assert not ok and "آزاد نیست" in err


@pytest.mark.asyncio
async def test_code_states_and_password(monkeypatch):
    _patch(monkeypatch, {"/account/get-code": (200, {"success": True, "message": "ok", "data": {
        "phone": "+1555", "codes": {"otp": "12345", "password": "pw123"}}})})
    assert await Z._pv_fetch_code(P, "+15551234567") == ("ok", "12345", "")
    assert "pw123" in Z._code_message("12345", "+15551234567")
    _patch(monkeypatch, {"/account/get-code": (422, {"error": "No new code received"})})
    assert (await Z._pv_fetch_code(P, "+1"))[0] == "pending"
    _patch(monkeypatch, {"/account/get-code": (404, {"error": "Number not reserved or expired"})})
    assert (await Z._pv_fetch_code(P, "+1"))[0] == "missing"
    _patch(monkeypatch, {"/account/get-code": (500, {"error": "Internal server error"})})
    assert (await Z._pv_fetch_code(P, "+1"))[0] == "error"


@pytest.mark.asyncio
async def test_cancel_logout(monkeypatch):
    _patch(monkeypatch, {"/account/cancel-reserve": (200, {"success": True, "data": {"phone": "+1", "status": "cancelled"}}),
                         "/account/logout": (200, {"success": True, "message": "x", "data": {}})})
    assert (await Z._pv_cancel(P, "+1"))[0] is True
    assert (await Z._pv_logout(P, "+1"))[0] == "ok"
    _patch(monkeypatch, {"/account/logout": (404, {"error": "not found"})})
    assert (await Z._pv_logout(P, "+1"))[0] == "missing"
