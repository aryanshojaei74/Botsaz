import pytest
from tenant.bots.vpn import vpn_titles as T


def test_building_key_registered_with_previous_default():
    default, desc = T.TITLES["building"]
    assert default == "پرداخت موفق بود. در حال ساخت کانفیگ روی سرور…" and "ساخت" in desc


@pytest.mark.asyncio
async def test_custom_text_roundtrip(monkeypatch):
    import tenant
    store = {}

    async def gs(bot_id, key, default=""): return store.get((bot_id, key), default)
    async def ss(bot_id, key, value): store[(bot_id, key)] = value
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "set_setting", ss)
    assert await T.get_raw(5, "building") == ""
    await T.set_title(5, "building", "  لطفاً چند ثانیه صبر کنید\n در حال آماده‌سازی  ")
    assert await T.get_raw(5, "building") == "لطفاً چند ثانیه صبر کنید در حال آماده‌سازی"
    await T.set_title(5, "building", "")
    assert await T.get_raw(5, "building") == ""
