"""سینا ۲۹ آبان: کارت تصویری طلا/دلار با پرچم و رنگ لوگو، نقل‌قول جمع‌شونده، حذف «ارسال دستی»، کپی در بلوپال."""
import io
import time

import pytest
from PIL import Image

from tenant.shared import coin_data as cd
from tenant.shared import price_lookup as pl
from tenant.shared.coin_data import Coin


def _png(color):
    im = Image.new("RGBA", (64, 64), color)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


@pytest.fixture
def env(monkeypatch):
    t0 = time.time() - 90 * 86400
    usdt = [(t0 + i * 86400, 260000.0 + i * 100, 261000.0 + i * 100, 259000.0 + i * 100, 260500.0 + i * 100) for i in range(90)]
    paxg = [(t0 + i * 14400, 3000.0, 3010.0, 2990.0, 3005.0) for i in range(180)]
    calls = []

    async def daily(days=90):
        return usdt

    async def candles(cid, days=1):
        calls.append((cid, days))
        return paxg

    async def img(url):
        calls.append(url)
        return _png((10, 60, 160, 255))

    async def coin(cid):
        return Coin(name="PAX Gold", symbol="PAXG", price=3110.34768)

    async def rate():
        return 270_000.0, "x"

    async def brs(bot_id):
        return []
    monkeypatch.setattr(cd, "usdt_irt_daily", daily)
    monkeypatch.setattr(cd, "cg_candles", candles)
    monkeypatch.setattr(cd, "fetch_image", img)
    monkeypatch.setattr(cd, "cg_coin", coin)
    monkeypatch.setattr(pl, "live_rate", rate)
    monkeypatch.setattr(pl, "brs_items", brs)
    return calls


@pytest.mark.asyncio
async def test_usd_card_has_flag_and_chart(env):
    img = await pl.asset_card(0, "usd", "adsbotsaz")
    assert img and img[:2] == b"\xff\xd8"
    assert any("flagcdn.com" in str(c) and "/us.png" in str(c) for c in env)


@pytest.mark.asyncio
async def test_gold_card_uses_paxg_candles_without_flag(env):
    img = await pl.asset_card(0, "gold18", "")
    assert img and ("pax-gold", 30) in env and not any("flagcdn" in str(c) for c in env)


def test_day_change():
    cs = [(0, 1, 1, 1, 100.0), (86400, 1, 1, 1, 110.0)]
    pct, diff = pl._day_change(cs)
    assert round(pct, 2) == 10.0 and diff == 10.0
    assert pl._day_change(cs[:1]) == (None, None)


def test_accent_follows_logo_color():
    from tenant.shared.price_chart import BLUE, accent_of
    assert accent_of(_png((200, 30, 30, 255)))[0] > 150
    assert accent_of(None) == BLUE


def test_expandable_quote_survives_premium_conversion():
    import premium_emoji
    import premium_emoji_dynamic as d
    p = d._DynHXParser({"🪙": ("1", "🪙")})
    p.feed("<b>t</b>\n<blockquote expandable>a\nb 🪙</blockquote><blockquote>c</blockquote>")
    p.close()
    types = sorted(e.type for e in p.entities)
    assert "expandable_blockquote" in types and "blockquote" in types
    import inspect
    assert '"expandable_blockquote"' in inspect.getsource(premium_emoji)


def test_manual_note_hidden_from_customer():
    import inspect
    from tenant.bots.virtual_service import smm
    from tenant.shared import public_api_special
    for mod in (smm, public_api_special):
        src = inspect.getsource(mod)
        assert "ارسال کاملاً دستی" not in src and "ارسال دستی است" not in src
