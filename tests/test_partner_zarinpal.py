"""Nexivo: زرین‌پال در پنلِ مالیِ رباتِ همکار (هندلر و پلنِ همکار از قبل بود؛ دکمه نبود)."""
import types

import pytest
from aiogram.types import InlineKeyboardMarkup

import tenant
from plans import Feature, has_feature
from tenant.gateways import zarinpal_wallet as ZW
from tenant.partner_bot import partner_menu as PM

pytestmark = pytest.mark.asyncio


class _Cap:
    def __init__(self):
        self.text = self.kb = None
        self.message = self
        self.from_user = types.SimpleNamespace(id=1)

    async def edit_text(self, text, reply_markup=None, **k):
        self.text, self.kb = text, reply_markup

    async def answer(self, *a, **k):
        pass


async def test_partner_plan_has_zarinpal_feature_and_finance_menu_lists_it(monkeypatch):
    assert has_feature("partner", Feature.GATEWAY_ZARINPAL)
    row = types.SimpleNamespace(id=5, plan="partner", owner_id=1)
    pb = types.SimpleNamespace(status="active")

    async def ok(cb, bot): return (row, pb)

    async def gs(bid, key, default=""): return default
    monkeypatch.setattr(PM, "_partner_ok", ok)
    monkeypatch.setattr(tenant, "get_setting", gs)
    cb = _Cap()
    await PM.pp_finance(cb, object())
    cbs = [b.callback_data for r in cb.kb.inline_keyboard for b in r]
    assert "set:zarinpal" in cbs and cbs[-1] == "pp:home"
    assert cbs.index("set:zarinpal") > cbs.index("set:tonpays")       # بعد از درگاه‌هایِ قبلی


async def test_zarinpal_screen_goes_back_to_partner_finance_for_partner_bots(monkeypatch):
    async def row_by_id(bid):
        return types.SimpleNamespace(id=bid, plan=("partner" if bid == 5 else "pro"))
    monkeypatch.setattr(tenant, "get_bot_row_by_id", row_by_id)
    assert await ZW._zp_back_cb(5) == "pp:finance"
    assert await ZW._zp_back_cb(6) == "tadm:sec:finance"                # رباتِ عادی: رفتارِ قبلی
