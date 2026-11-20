"""PATCH_PRICE_LOOKUP_V1 — قیمت‌یاب (درخواست سینا)."""
import pytest

from tenant.shared import price_lookup as pl


@pytest.mark.parametrize("text,exp", [
    ("تون", (1.0, "ton")), ("5 تون", (5.0, "ton")), ("۲ گرم طلا", (2.0, "gold18")),
    ("5 دلار", (5.0, "usd")), ("سکه امامی", (1.0, "emami")), ("بیت کوین", (1.0, "btc")),
    ("تون 3", (3.0, "ton")), ("2.5 eth", (2.5, "eth")), ("ربع سکه", (1.0, "rob")),
])
def test_parse_hits(text, exp):
    assert pl.parse(text) == exp


@pytest.mark.parametrize("text", ["سلام خوبی", "100", "", None, "تون بخرم؟", "0 تون", "قیمت تون چنده امروز بگو لطفا"])
def test_parse_ignores_chat(text):
    assert pl.parse(text) is None


def test_parse_brs_and_find():
    data = {"gold": [{"name": "طلای 18 عیار", "price": "6,500,000", "change_percent": 1.2},
                     {"name": "سکه امامی", "price": 70000000}, {"name": "نیم سکه", "price": 40000000},
                     {"name": "انس طلا", "price": 2650.5}],
            "currency": [{"name": "یورو", "price": 99000}, {"name": "درهم امارات", "price": 25000, "unit": "ریال"}]}
    rows = pl.parse_brs(data)
    assert pl.find_brs(rows, "gold", "18")["price"] == 6_500_000
    assert pl.find_brs(rows, "gold", "امامی")["price"] == 70_000_000
    assert pl.find_brs(rows, "gold", "نیم")["price"] == 40_000_000
    assert pl.find_brs(rows, "fiat", "درهم")["price"] == 2500
    assert pl.find_brs(rows, "gold", "24") is None


def test_parse_buttons():
    raw = "خرید استارز | https://t.me/x?start=s\nبد\nکانال|t.me/bad\nسایت | https://a.b"
    assert pl.parse_buttons(raw) == [("خرید استارز", "https://t.me/x?start=s"), ("سایت", "https://a.b")]


@pytest.mark.asyncio
async def test_quote_crypto(monkeypatch):
    from tenant.gateways import crypto_pay

    async def rate(*a, **k):
        return 100_000
    async def cq():
        return {"the-open-network": (2.0, 5.0)}
    monkeypatch.setattr(crypto_pay, "get_usd_to_toman_rate", rate)
    monkeypatch.setattr(pl, "crypto_quotes", cq)
    t = await pl.quote(1, 5, "ton")
    assert "قیمت 5 GRAM (ton)" in t and "$10.00" in t and "1,000,000 تومان" in t
    assert "+5.00%" in t and "مقدار سود" in t


@pytest.mark.asyncio
async def test_quote_gold_and_unavailable(monkeypatch):
    from tenant.gateways import crypto_pay

    async def rate(*a, **k):
        return 100_000
    async def brs(bot_id):
        return [{"name": "طلای 18 عیار", "price": 6_500_000, "change": None}]
    async def cq():
        return {}
    monkeypatch.setattr(crypto_pay, "get_usd_to_toman_rate", rate)
    monkeypatch.setattr(pl, "brs_items", brs)
    monkeypatch.setattr(pl, "crypto_quotes", cq)
    t = await pl.quote(1, 2, "gold18")
    assert "2 گرم طلای 18 عیار" in t and "13,000,000 تومان" in t and "$130.00" in t
    assert "در دسترس نیست" in await pl.quote(1, 1, "emami")     # بدون عدد ساختگی
    assert "در دسترس نیست" in await pl.quote(1, 1, "btc")


@pytest.mark.asyncio
async def test_maybe_answer_respects_toggle(monkeypatch):
    sent = []

    class M:
        text = "5 تون"
        async def reply(self, text, **kw):
            sent.append(text)
    store = {}

    async def gs(bid, k, d=""):
        return store.get(k, d)
    async def q(bid, qty, key):
        return f"Q {qty} {key}"
    import tenant
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(pl, "quote", q)
    assert await pl.maybe_answer(M(), None, 1) is False and not sent
    store[pl.ON_KEY] = "1"
    assert await pl.maybe_answer(M(), None, 1) is True and sent == ["Q 5.0 ton"]


def test_menu_registered():
    import inspect
    import tenant
    src = inspect.getsource(tenant)
    assert '"🔎 قیمت‌یاب": _pq_menu_entry' in src and '("🔎 قیمت‌یاب", "pq:menu")' in src
