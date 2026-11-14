import asyncio
from types import SimpleNamespace as NS

from tenant.shared import vs_msg_override as VO
from tenant.shared.text_vars import _order_values


def _q(gift_id, star_count, quantity=1):
    o = NS(id=1, tracking_code="T1", gift_id=gift_id, gift_emoji="⭐", star_count=star_count, price_toman=1000,
           quantity=quantity, dest_label="x", status="sent", created_at=None)
    shim = VO.order_shim(o, 5)
    return asyncio.run(_order_values({"quantity"}, None, shim))["quantity"]


def test_quantity_var_for_stars_and_ton():
    assert _q("starsell_75", 75) == "75"          # قبلاً همیشه «1» بود
    assert _q("tonsell_50", 50) == "0.5"          # ۵۰ صدم تون = ۰٫۵ (قبلاً «1»)
    assert _q("tonsell_150", 150) == "1.5"
    assert _q("tonsell_100", 100) == "1"
    assert _q("srx_10", 10, quantity=3) == "30"
    assert _q("giftx", 15, quantity=2) == "2"      # گیفتِ معمولی مثل قبل
