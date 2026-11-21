"""PATCH_PRICE_SOURCES_V2 — کوین‌گکو 429 داد و نوبیتکس 403: جایگزین بایننس/OKX، والکس، open.er-api و دادهٔ کهنه."""
import time

import pytest

from tenant.shared import coin_data as cd
from tenant.shared import price_lookup as pl

NOW = time.time()
KL = [[int((NOW - (48 - i) * 1800) * 1000), "1.0", "1.1", "0.9", "1.05", "0"] for i in range(48)]


@pytest.fixture
def net(monkeypatch):
    calls = []
    state = {"binance": True}

    async def fake(url, headers=None):
        calls.append(url)
        if url.startswith(cd.CG):
            return None                                  # کوین‌گکو محدود است
        if "binance" in url and state["binance"]:
            if "ticker/24hr" in url:
                sym = url.split("symbol=")[1]
                return {"lastPrice": "3110.34768" if sym == "PAXGUSDT" else "1.02", "priceChangePercent": "-1.5",
                        "highPrice": "1.1", "lowPrice": "0.9", "quoteVolume": "5000000"}
            if "klines" in url:
                return KL
        if "okx.com" in url and "ticker" in url:
            return {"data": [{"last": "2.0", "open24h": "1.0", "high24h": "2.1", "low24h": "0.9", "volCcy24h": "777"}]}
        if "okx.com" in url and "candles" in url:
            return {"data": [[str(int((NOW - i * 1800) * 1000)), "2", "2.1", "1.9", "2"] for i in range(10)]}
        if "wallex.ir/v1/udf" in url:
            return {"s": "ok", "t": [NOW - 86400, NOW], "o": ["268000", "269000"], "h": ["270000", "271000"],
                    "l": ["267000", "268000"], "c": ["269000", "270000"]}
        if "open.er-api.com" in url:
            return {"rates": {"EUR": 0.8, "AED": 3.6725}}
        return None
    cd._CACHE.clear()
    pl._CACHE.clear()
    monkeypatch.setattr(cd, "_json", fake)
    yield calls, state
    cd._CACHE.clear()
    pl._CACHE.clear()


@pytest.mark.asyncio
async def test_known_coin_from_binance_when_coingecko_blocked(net):
    c = await cd.lookup("ton", cg_id="the-open-network", ex_symbol="TON")
    assert c and c.price == 1.02 and c.source == "binance" and len(c.candles) == 48
    assert c.image == "https://assets.coincap.io/assets/icons/ton@2x.png"


@pytest.mark.asyncio
async def test_okx_when_binance_blocked(net):
    _calls, state = net
    state["binance"] = False
    c = await cd.lookup("ton", cg_id="the-open-network", ex_symbol="TON")
    assert c and c.source == "okx" and c.price == 2.0 and round(c.change_pct) == 100 and c.volume == 777
    assert c.candles and c.candles == sorted(c.candles)


@pytest.mark.asyncio
async def test_prefixed_search_prefers_exchange_over_dex(net):
    calls, _ = net
    c = await cd.lookup("pepe")
    assert c and c.source == "binance" and not any("dexscreener" in u for u in calls)


@pytest.mark.asyncio
async def test_coingecko_429_backs_off(monkeypatch):
    class _R:
        status = 429

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _S:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url, headers=None):
            hits.append(url)
            return _R()
    import aiohttp
    hits = []
    monkeypatch.setattr(aiohttp, "ClientSession", _S)
    monkeypatch.setattr(cd, "_CG_BLOCKED_UNTIL", [0.0])
    assert await cd._json(f"{cd.CG}/ping") is None
    assert await cd._json(f"{cd.CG}/ping") is None
    assert len(hits) == 1                                    # دومی اصلا فرستاده نشد


@pytest.mark.asyncio
async def test_gold_from_binance_paxg(net, monkeypatch):
    async def rate():
        return 270_000
    async def brs(bot_id):
        return []
    monkeypatch.setattr(pl, "_usd_toman", rate)
    monkeypatch.setattr(pl, "brs_items", brs)
    t = await pl.quote(0, 1, "gold18")
    assert "20,250,000 تومان" in t


@pytest.mark.asyncio
async def test_usd_daily_from_wallex_and_fx_from_er_api(net):
    rows = await cd.usdt_irt_daily(90)
    assert rows and rows[-1][4] == 270000
    assert round(await pl._fx_usd_per("eur"), 2) == 1.25
