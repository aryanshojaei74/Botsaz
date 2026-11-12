"""PATCH_PREMIUM_AUTO_NO_STARS_FILTER_V1 (ARTAVPN): با Fragment API / TGTools / تحویلِ دستی، پلن‌هایِ پرمیوم به‌خاطرِ موجودیِ استارزِ ربات پنهان نشوند."""
from types import SimpleNamespace as NS

import pytest

from tenant.bots.virtual_service import smm


def _patch(monkeypatch, *, settings=None, tgt=False, frag=False, balance=0):
    st = settings or {}

    async def gs(bot_id, key, default=""):
        return st.get(key, default)

    async def tgt_on(bot_id):
        return tgt

    async def frag_on(bot_id):
        return frag

    async def bal(bot):
        return balance

    async def price(bot_id, months, stars, rate, margin):
        return 100000 * months, False

    async def ps(bot_id):
        return 1000.0, 20.0
    monkeypatch.setattr(smm, "_get_setting", gs)
    monkeypatch.setattr(smm, "_tgt_premium_on", tgt_on)
    monkeypatch.setattr(smm._fapi, "premium_on", frag_on)
    monkeypatch.setattr(smm, "_bot_star_balance", bal)
    monkeypatch.setattr(smm, "_premium_effective_price", price)
    monkeypatch.setattr(smm, "_premium_settings", ps)


@pytest.mark.asyncio
async def test_uses_bot_stars_only_for_direct_delivery(monkeypatch):
    _patch(monkeypatch)
    assert await smm._premium_uses_bot_stars(1) is True
    _patch(monkeypatch, frag=True)
    assert await smm._premium_uses_bot_stars(1) is False
    _patch(monkeypatch, tgt=True)
    assert await smm._premium_uses_bot_stars(1) is False
    _patch(monkeypatch, settings={"premium_manual_delivery": "1"})
    assert await smm._premium_uses_bot_stars(1) is False


class _T:
    def __init__(self):
        self.out = []

    async def answer(self, text, reply_markup=None, **k):
        self.out.append((text, reply_markup))


async def _cbs(monkeypatch, **kw):
    _patch(monkeypatch, **kw)

    async def back(bot_id):
        return None
    import tenant
    monkeypatch.setattr(tenant, "inline_back_row", back)
    t = _T()
    await smm.show_premium_shop_to_user(NS(), t, 1)
    kb = t.out[-1][1]
    return [b.callback_data for r in kb.inline_keyboard for b in r]


@pytest.mark.asyncio
async def test_shop_shows_plans_with_fragment_even_if_bot_has_no_stars(monkeypatch):
    got = await _cbs(monkeypatch, frag=True, balance=0)
    assert got == ["prm:pick:3", "prm:pick:6", "prm:pick:12"]


@pytest.mark.asyncio
async def test_shop_still_hides_plans_for_direct_delivery_without_stars(monkeypatch):
    assert await _cbs(monkeypatch, balance=1200) == ["prm:pick:3"]
    assert await _cbs(monkeypatch, balance=0) == []
