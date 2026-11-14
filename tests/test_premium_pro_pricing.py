"""PATCH_PREMIUM_PRO_PRICING_V1 (Nexivo): سودِ مبلغی + پلنِ ۱ ماههٔ دستی، ویژهٔ حرفه‌ای."""
from types import SimpleNamespace as NS

import pytest

from tenant.bots.virtual_service import smm


@pytest.fixture
def cfg(monkeypatch):
    st = {}
    plan = {"v": "pro"}

    async def gs(bot_id, key, default=""):
        return st.get(key, default)

    async def tgt_on(bot_id):
        return False

    async def frag_on(bot_id):
        return False

    async def row(bot_id):
        return NS(plan=plan["v"])
    import tenant
    monkeypatch.setattr(smm, "_get_setting", gs)
    monkeypatch.setattr(smm, "_tgt_premium_on", tgt_on)
    monkeypatch.setattr(smm._fapi, "premium_on", frag_on)
    monkeypatch.setattr(tenant, "get_bot_row_by_id", row)
    return st, plan


@pytest.mark.asyncio
async def test_pct_margin_is_unchanged_by_default(cfg):
    price, custom = await smm._premium_effective_price(1, 3, 1000, 1000.0, 20.0)
    assert (price, custom) == (1_200_000, False)


@pytest.mark.asyncio
async def test_fixed_profit_adds_amount_to_cost_for_pro(cfg):
    st, plan = cfg
    st.update(premium_margin_mode="fixed", premium_margin_fixed="50000")
    price, _ = await smm._premium_effective_price(1, 3, 1000, 1000.0, 20.0)
    assert price == 1_000_000 + 50_000
    assert await smm._premium_cost_price(1, 3, 1000, 1000.0) == 1_000_000


@pytest.mark.asyncio
async def test_fixed_profit_is_ignored_on_growth(cfg):
    st, plan = cfg
    st.update(premium_margin_mode="fixed", premium_margin_fixed="50000")
    plan["v"] = "growth"
    price, _ = await smm._premium_effective_price(1, 3, 1000, 1000.0, 20.0)
    assert price == 1_200_000                      # برگشت به درصدی


@pytest.mark.asyncio
async def test_custom_price_still_wins(cfg):
    st, _ = cfg
    st.update(premium_margin_mode="fixed", premium_margin_fixed="50000", premium_custom_price_3="777000")
    assert await smm._premium_effective_price(1, 3, 1000, 1000.0, 20.0) == (777000, True)


@pytest.mark.asyncio
async def test_one_month_needs_pro_manual_delivery_and_cost(cfg):
    st, plan = cfg
    assert [m for m, _ in await smm._premium_tiers_for(1)] == [3, 6, 12]
    st.update(premium_m1="1", premium_cost_1="300000")
    assert [m for m, _ in await smm._premium_tiers_for(1)] == [3, 6, 12]        # تحویلِ دستی خاموش
    st["premium_manual_delivery"] = "1"
    assert [m for m, _ in await smm._premium_tiers_for(1)] == [1, 3, 6, 12]
    plan["v"] = "growth"
    assert [m for m, _ in await smm._premium_tiers_for(1)] == [3, 6, 12]        # رشد: قفل
    plan["v"] = "pro"
    del st["premium_cost_1"]
    assert [m for m, _ in await smm._premium_tiers_for(1)] == [3, 6, 12]        # بدونِ قیمتِ خرید


@pytest.mark.asyncio
async def test_one_month_price_pct_and_fixed(cfg):
    st, _ = cfg
    st.update(premium_m1="1", premium_cost_1="300000", premium_manual_delivery="1")
    assert (await smm._premium_effective_price(1, 1, 0, 1000.0, 20.0))[0] == 360_000
    st.update(premium_margin_mode="fixed", premium_margin_fixed="40000")
    assert (await smm._premium_effective_price(1, 1, 0, 1000.0, 20.0))[0] == 340_000


def test_feature_is_pro_only():
    from plans import has_feature, Feature
    assert has_feature("pro", Feature.PREMIUM_PRO_PRICING) and not has_feature("growth", Feature.PREMIUM_PRO_PRICING)


@pytest.mark.asyncio
async def test_admin_screen_renders_cost_profit_and_pro_buttons(cfg, monkeypatch):
    st, plan = cfg
    st.update(premium_margin_mode="fixed", premium_margin_fixed="50000", premium_m1="1",
              premium_cost_1="300000", premium_manual_delivery="1")
    text, kb = await smm.premium_admin_content(1, None)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "خرید 1,000,000 + سود 50,000" in text and "1 ماهه" in text
    assert {"prm:modetoggle", "prm:setfixed", "prm:m1toggle", "prm:setcost1"} <= set(cbs)
    plan["v"] = "growth"
    _t, kb = await smm.premium_admin_content(1, None)
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "prm:prolock" in cbs and "prm:modetoggle" not in cbs
