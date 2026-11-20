"""PATCH_PRICE_WORLD_FIAT_V1 — «حتی افغانی و همهٔ پول‌های جهان»: نسبت جهانی × دلار زندهٔ والکس."""
import pytest

from tenant.shared import price_lookup as pl


@pytest.mark.parametrize("text,key", [("افغانی", "afn"), ("1000 افغانی", "afn"), ("یوان", "cny"), ("5 ین", "jpy"),
                                      ("دینار", "iqd"), ("ریال سعودی", "sar"), ("قیمت روبل", "rub"),
                                      ("100 دلار کانادا", "cad"), ("منات", "azn"), ("لیر", "try")])
def test_parse_world_fiat(text, key):
    assert pl.parse_query(text)[2] == key


def test_common_english_words_not_currency():
    for t in ("try", "php", "amd"):
        assert pl.parse_query(t) in (None,) or pl.parse_query(t)[0] != "asset"


@pytest.mark.asyncio
async def test_afghani_quote_and_card_meta(monkeypatch):
    async def rate():
        return 270_000

    async def brs(bot_id):
        return []

    async def fj(url):
        if "open.er-api.com" in url:
            return {"rates": {"AFN": 70.0, "VND": 26000.0}}
        return None
    monkeypatch.setattr(pl, "_usd_toman", rate)
    monkeypatch.setattr(pl, "brs_items", brs)
    monkeypatch.setattr(pl, "_http_json", fj)
    pl._CACHE.clear()
    t = await pl.quote(0, 1000, "afn")
    assert "🇦🇫" in t and "1000 افغانی" in t and "3,857,143 تومان" in t      # 1000/70 دلار × 270,000
    t = await pl.quote(0, 1, "vnd")
    assert "10.38 تومان" in t                                               # پول خیلی کوچک با اعشار
    assert pl.FLAG_CC["afn"] == "af" and pl._FX_CODE["afn"] == "afn"
    pl._CACHE.clear()
