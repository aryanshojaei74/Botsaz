import asyncio
from tenant.shared import premium_emoji_admin as pe


def test_auto_merges_platform_defaults(monkeypatch):
    async def enabled(_b): return True
    async def auto(_b): return True
    async def plat(): return {"✅": ("111", "✅"), "💰": ("222", "💰")}

    class FakeSess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def scalars(self, _q):
            class R:
                def all(_s):
                    class Row:
                        normal_emoji = "✅"; premium_emoji_id = "999"; premium_display = "✅"
                    return [Row()]
            return R()

    monkeypatch.setattr(pe, "_is_enabled", enabled)
    monkeypatch.setattr(pe, "_auto_enabled", auto)
    monkeypatch.setattr(pe, "_platform_defaults", plat)
    monkeypatch.setattr(pe, "Session", lambda: FakeSess())
    pe._PMAP_CACHE.clear()
    m = asyncio.run(pe.get_premium_map(5))
    assert m["✅"][0] == "999"      # manual wins
    assert m["💰"][0] == "222"      # default fills in


def test_auto_off_keeps_manual_only(monkeypatch):
    async def enabled(_b): return True
    async def auto(_b): return False
    class FakeSess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def scalars(self, _q):
            class R:
                def all(_s): return []
            return R()
    monkeypatch.setattr(pe, "_is_enabled", enabled)
    monkeypatch.setattr(pe, "_auto_enabled", auto)
    monkeypatch.setattr(pe, "Session", lambda: FakeSess())
    pe._PMAP_CACHE.clear()
    assert asyncio.run(pe.get_premium_map(6)) == {}


def test_pack_name_parsing():
    assert pe._pack_name("https://t.me/addemoji/Foo_bar?x=1") == "Foo_bar"
    assert pe._pack_name("  Foo ") == "Foo"


def test_direct_crypto_invoice_has_exit_buttons():
    from tenant.gateways.ton_direct import invoice_exit_row
    assert [b.callback_data for b in invoice_exit_row()] == ["tond:close", "mbtn:home"]


def test_crypto_hub_has_wallet_name_shortcut():
    import asyncio
    from tenant.gateways import tron_direct as td
    async def fake_get(*a, **k): return "1"
    import tenant
    orig = tenant.get_setting
    tenant.get_setting = fake_get
    try:
        _t, kb = asyncio.run(td._hub(1))
    finally:
        tenant.get_setting = orig
    assert "gwl:name:cdirect" in [b.callback_data for r in kb.inline_keyboard for b in r]
