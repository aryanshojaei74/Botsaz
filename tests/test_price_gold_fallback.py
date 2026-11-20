"""PATCH_PRICE_GOLD_FALLBACK_V1 — «قیمت طلا رو نمیگه»: اگر BrsApi جواب نداد، طلا از انس جهانی (PAX Gold) × نرخ زنده."""
import pytest

from tenant.shared import price_lookup as pl
from tenant.shared.coin_data import Coin


@pytest.fixture
def env(monkeypatch):
    from tenant.shared import coin_data as cd
    state = {"brs": [], "paxg": Coin(name="PAX Gold", symbol="PAXG", price=3110.34768, change_pct=1.0)}

    async def brs(bot_id):
        return state["brs"]

    async def cg(cid):
        assert cid == "pax-gold"
        return state["paxg"]

    async def rate():
        return 270_000
    monkeypatch.setattr(pl, "brs_items", brs)
    monkeypatch.setattr(cd, "cg_coin", cg)
    monkeypatch.setattr(pl, "_usd_toman", rate)
    return state


@pytest.mark.asyncio
async def test_gold18_from_global_ounce_when_brs_down(env):
    t = await pl.quote(0, 1, "gold18")
    # 3110.34768/31.1034768 = 100 دلار برای هر گرم ۲۴ ⇒ ۱۸ عیار 75 دلار ⇒ 20,250,000 تومان
    assert "$75.00" in t and "20,250,000 تومان" in t and "محاسبه از انس جهانی" in t


@pytest.mark.asyncio
async def test_coin_is_labeled_without_bubble(env):
    t = await pl.quote(0, 1, "emami")
    assert "بدون حباب" in t and "تومان" in t


@pytest.mark.asyncio
async def test_brs_symbol_preferred_over_fallback(env):
    env["brs"] = pl.parse_brs({"gold": [{"symbol": "IR_GOLD_18K", "name": "18 عیار", "price": 9_000_000, "unit": "تومان"}]})
    t = await pl.quote(0, 2, "gold18")
    assert "18,000,000 تومان" in t and "انس جهانی" not in t


@pytest.mark.asyncio
async def test_unavailable_when_everything_down(env):
    env["paxg"] = None
    assert "در دسترس نیست" in await pl.quote(0, 1, "gold18")


@pytest.mark.asyncio
async def test_fiat_fallback_from_global_rates(env, monkeypatch):
    async def fj(url):
        return {"rates": {"usd": {"value": 100000.0}, "eur": {"value": 80000.0}}}
    monkeypatch.setattr(pl, "_http_json", fj)
    pl._CACHE.pop("cg_fx", None)
    t = await pl.quote(0, 1, "eur")
    assert "$1.25" in t and "337,500 تومان" in t and "نرخ جهانی" in t
