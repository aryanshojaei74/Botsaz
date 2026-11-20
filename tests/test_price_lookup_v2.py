"""PATCH_PRICE_LOOKUP_V2 — همهٔ ارزها (نام/نماد/آدرس)، آمار کامل، نمودار کندلی، منبع جایگزین (سینا)."""
import time

import pytest

from tenant.shared import coin_data as cd
from tenant.shared import price_lookup as pl

@pytest.fixture(autouse=True)
def _no_live_rate(monkeypatch):
    """نرخ زنده (والکس/نوبیتکس) در تست به شبکه نرود؛ همان نرخ ساختگی crypto_pay استفاده شود."""
    from tenant.shared import price_lookup as _pl
    monkeypatch.setattr(_pl, "RATE_SOURCES", [])
    monkeypatch.setitem(_pl._LIVE, "v", 0.0)
    monkeypatch.setitem(_pl._LIVE, "ts", 0.0)


T0 = time.time() - 86400
OHLC = [[(T0 + i * 1800) * 1000, 1.0 + i * 0.001, 1.01 + i * 0.001, 0.99 + i * 0.001, 1.005 + i * 0.001] for i in range(48)]
MARKET = [{"id": "the-open-network", "symbol": "ton", "name": "Gram", "current_price": 1.017, "market_cap": 2919748709,
           "market_cap_rank": 25, "total_volume": 218213, "high_24h": 1.038, "low_24h": 1.009,
           "price_change_percentage_24h": -1.35, "circulating_supply": 2870942684}]
PAIR = {"chainId": "ton", "pairAddress": "EQPOOL", "priceUsd": "0.0042", "priceChange": {"h24": 12.5},
        "volume": {"h24": 5000}, "liquidity": {"usd": 9000}, "marketCap": 420000,
        "baseToken": {"address": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs", "name": "NewToken", "symbol": "NEW"}}


@pytest.fixture
def api(monkeypatch):
    calls = []
    state = {"cg_down": False}

    async def fake(url, headers=None):
        calls.append(url)
        if "coingecko" in url and state["cg_down"]:
            return None
        if "/search?query=" in url:
            q = url.rsplit("=", 1)[1]
            return {"coins": [{"id": "pepe", "symbol": "PEPE", "name": "Pepe", "market_cap_rank": 30}] if q == "pepe" else []}
        if "/coins/markets" in url and "order=market_cap_desc" in url:
            return [{"id": "the-open-network", "symbol": "ton", "name": "Gram"}, {"id": "pepe", "symbol": "pepe", "name": "Pepe"}] \
                if "page=1" in url else []
        if "/coins/markets" in url:
            cid = url.rsplit("ids=", 1)[1]
            return [dict(MARKET[0], id=cid, **({"name": "Pepe", "symbol": "pepe"} if cid == "pepe" else {}))] if cid in ("the-open-network", "pepe") else []
        if "/ohlc?" in url:
            return OHLC
        if "dexscreener" in url:
            return {"pairs": [PAIR]} if ("EQCx" in url or "newtoken" in url.lower()) else {"pairs": []}
        if "geckoterminal" in url:
            return {"data": {"attributes": {"ohlcv_list": [[T0 + i * 900, 1, 1.1, 0.9, 1.05, 10] for i in range(40)]}}}
        return None
    cd._CACHE.clear()
    monkeypatch.setattr(cd, "_json", fake)

    async def rate(*a, **k):
        return 274_000
    from tenant.gateways import crypto_pay
    monkeypatch.setattr(crypto_pay, "get_usd_to_toman_rate", rate)
    yield calls, state
    cd._CACHE.clear()


@pytest.mark.asyncio
async def test_known_asset_full_report_with_chart(api):
    text, img = await pl.answer_for(1, pl.parse_query("تون"), "VelnexoBot")
    for part in ("GRAM (ton)", "$1.02", "278,658 تومان", "#25", "-1.35%", "مقدار ضرر", "سقف: 1.038 $", "کف: 1.009 $",
                 "حجم: 218,213 $", "ساپلای: 2,870,942,684", "کپ: 2,919,748,709 $"):
        assert part in text, part
    assert img and img[:2] == b"\xff\xd8"                                  # JPEG


@pytest.mark.asyncio
async def test_symbol_in_chat_uses_top_list_only(api):
    calls, _ = api
    assert pl.parse_query("pepe")[3] is True
    r = await pl.answer_for(1, pl.parse_query("pepe"))
    assert r and "Pepe" in r[0]
    assert not any("/search?" in u for u in calls)                         # کلمهٔ بی‌پیشوند به جست‌وجو نمی‌رود
    assert await pl.answer_for(1, ("coin", 1.0, "zzzz", True)) is None       # کلمهٔ ناشناخته ⇒ سکوت


@pytest.mark.asyncio
async def test_prefixed_search_and_not_found_message(api):
    r = await pl.answer_for(1, pl.parse_query("قیمت pepe"))
    assert "Pepe" in r[0]
    r = await pl.answer_for(1, pl.parse_query("قیمت qqqqx"))
    assert "پیدا نشد" in r[0] and r[1] is None


@pytest.mark.asyncio
async def test_contract_address_via_dexscreener_and_geckoterminal(api):
    calls, _ = api
    text, img = await pl.answer_for(1, pl.parse_query("EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"))
    assert "NewToken (NEW)" in text and "+12.50%" in text and "شبکه: ton" in text
    assert img and any("geckoterminal" in u and "/networks/ton/pools/EQPOOL/" in u for u in calls)


@pytest.mark.asyncio
async def test_fallback_to_dex_when_coingecko_down(api):
    _, state = api
    state["cg_down"] = True
    r = await pl.answer_for(1, pl.parse_query("قیمت newtoken"))
    assert "NewToken" in r[0]
    r = await pl.answer_for(1, pl.parse_query("تون"))                       # بدون عدد ساختگی
    assert "در دسترس نیست" in r[0]


@pytest.mark.asyncio
async def test_amount_multiplies(api):
    text, _ = await pl.answer_for(1, pl.parse_query("5 تون"))
    assert "قیمت 5 GRAM (ton)" in text and "$5.08" in text and "هر واحد: $1.02" in text


@pytest.mark.asyncio
async def test_cache_avoids_repeat_calls(api):
    calls, _ = api
    await pl.answer_for(1, pl.parse_query("تون"))
    n = len(calls)
    await pl.answer_for(1, pl.parse_query("تون"))
    assert len(calls) == n


def test_parse_query_ignores_chat():
    for t in ("hi", "ok", "hello world", "سلام", "ب", "a", "this is a long english sentence"):
        assert pl.parse_query(t) is None, t


@pytest.mark.asyncio
async def test_maybe_answer_sends_photo(monkeypatch):
    sent = {}

    class M:
        text = "تون"
        async def reply_photo(self, photo, **kw):
            sent["photo"] = kw["caption"]
        async def reply(self, text, **kw):
            sent["text"] = text

    async def en(bid):
        return True
    async def af(bid, q, uname=""):
        return "CAP", b"\xff\xd8x"
    async def kb(bid):
        return None
    monkeypatch.setattr(pl, "enabled", en)
    monkeypatch.setattr(pl, "answer_for", af)
    monkeypatch.setattr(pl, "buttons_kb", kb)
    assert await pl.maybe_answer(M(), None, 1) is True and sent == {"photo": "CAP"}


@pytest.mark.asyncio
async def test_not_purchased_means_silent(monkeypatch):
    import bot_addons
    import tenant
    store = {pl.ON_KEY: "1"}

    async def gs(bid, k, d=""):
        return store.get(k, d)
    state = {"v": "none"}

    async def st(bid, addon):
        return state["v"]
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(bot_addons, "addon_state", st)
    assert await pl.enabled(1) is False
    state["v"] = "off"
    assert await pl.enabled(1) is False                  # مدیر غیرفعال کرده
    state["v"] = "on"
    assert await pl.enabled(1) is True
    text, _kb = await pl._menu(1)
    assert "روشن" in text
    state["v"] = "none"
    text, _kb = await pl._menu(1)
    assert "خریداری نشده" in text and "ربات‌های من" in text
