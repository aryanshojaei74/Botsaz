"""خریدِ اولِ همکار حداقل ۳ ربات؛ بعدش یکی‌یکی."""
import asyncio
import re
from pathlib import Path

from partner_rules import FIRST_ORDER_MIN_QTY, first_order_shortfall


def test_first_order_shortfall_rules():
    assert FIRST_ORDER_MIN_QTY == 3
    assert first_order_shortfall(0, 0, 1) == 2
    assert first_order_shortfall(0, 0, 2) == 1
    assert first_order_shortfall(0, 0, 3) == 0
    assert first_order_shortfall(0, 0, 10) == 0
    # بعد از اولین خرید (سفارشِ تأییدشده یا ربات خریداری‌شده) محدودیتی نیست
    assert first_order_shortfall(0, 1, 1) == 0
    assert first_order_shortfall(5, 0, 1) == 0


def test_buy_kb_hides_pay_button_below_minimum():
    from tenant.partner_bot.partner_panel import _buy_kb

    def datas(kb):
        return [b.callback_data for r in kb.inline_keyboard for b in r]

    assert not any(d.startswith("pp:order:") for d in datas(_buy_kb(1, 1, 100, 200, 30, need_min=3)))
    assert any(d == "pp:order:2:1" for d in datas(_buy_kb(2, 1, 100, 200, 30, need_min=3)))
    # همکارِ قدیمی: یک ربات هم قابل خرید است
    assert any(d == "pp:order:1:0" for d in datas(_buy_kb(1, 0, 100, 200, 30, need_min=0)))


def test_buy_text_mentions_minimum_only_when_needed(monkeypatch):
    from tenant.partner_bot import partner_panel as pp

    async def fake_price(_plan):
        return 1000
    monkeypatch.setattr(pp, "base_price", fake_price)
    t1 = asyncio.run(pp._buy_text(30, 700, 700, 3))
    t0 = asyncio.run(pp._buy_text(30, 700, 700, 0))
    assert "حداقل 3" in t1 and "حداقل" not in t0


def test_pay_choose_enforces_minimum_server_side():
    src = Path("tenant/partner_bot/partner_panel.py").read_text(encoding="utf-8")
    seg = src[src.index("async def pp_pay_choose"):src.index("async def pp_pay_wallet")]
    assert re.search(r"_need = await _first_order_need\(pb\)\s+if _need and g \+ p < _need", seg)
