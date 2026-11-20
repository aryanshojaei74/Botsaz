"""PATCH_PRICE_LIVE_RATE_V1 — نرخ دلار زنده از والکس/نوبیتکس (مدیر: «دلار حدود ۲۷۰ هزار است»)."""
import pytest

from tenant.shared import price_lookup as pl


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setitem(pl._LIVE, "v", 0.0)
    monkeypatch.setitem(pl._LIVE, "ts", 0.0)


def _src(v, calls, name):
    async def f():
        calls.append(name)
        if isinstance(v, Exception):
            raise v
        return v
    return f


@pytest.mark.asyncio
async def test_wallex_first_then_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(pl, "RATE_SOURCES", [("والکس", _src(274_300, calls, "w")), ("نوبیتکس", _src(275_000, calls, "n"))])
    assert await pl.live_rate() == (274_300, "والکس")
    assert await pl.live_rate() == (274_300, "والکس") and calls == ["w"]          # کش ۶۰ ثانیه


@pytest.mark.asyncio
async def test_fallback_order_and_sanity(monkeypatch):
    calls = []
    monkeypatch.setattr(pl, "RATE_SOURCES", [("والکس", _src(RuntimeError("down"), calls, "w")),
                                             ("نوبیتکس", _src(27_430, calls, "n"))])    # عدد خارج بازه رد می‌شود
    from tenant.gateways import crypto_pay

    async def sys_rate(*a, **k):
        return 271_000
    monkeypatch.setattr(crypto_pay, "get_usd_to_toman_rate", sys_rate)
    assert await pl.live_rate() == (271_000, "نرخ ذخیره‌شده") and calls == ["w", "n"]


@pytest.mark.asyncio
async def test_nobitex_rial_to_toman(monkeypatch):
    async def fj(url):
        assert "usdt" in url and "rls" in url
        return {"stats": {"usdt-rls": {"latest": "2743000"}}}
    monkeypatch.setattr(pl, "_http_json", fj)
    assert await pl._rate_nobitex() == 274_300


@pytest.mark.asyncio
async def test_rate_line_in_message(monkeypatch):
    from tenant.shared.coin_data import Coin
    monkeypatch.setattr(pl, "RATE_SOURCES", [("والکس", _src(274_300, [], "w"))])
    rate = await pl._usd_toman()
    t = pl.render_crypto(1, Coin(name="Gram", symbol="TON", price=1.0), rate, title="GRAM (ton)")
    assert "قیمت تومانی: 274,300 تومان" in t and "💱 نرخ دلار: 274,300 تومان (والکس)" in t
