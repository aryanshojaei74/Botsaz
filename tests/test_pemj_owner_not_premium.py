import asyncio
from types import SimpleNamespace as NS

import tenant
import tenant.shared.premium_emoji_admin as PA


def _run(user, store):
    async def gs(bot_id, key, default=""):
        return store.get(key, default)

    async def ss(bot_id, key, value):
        store[key] = value
    return gs, ss


def test_non_premium_owner_defaults_button_icons_off(monkeypatch):
    store = {}
    gs, ss = _run(None, store)
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "set_setting", ss)
    asyncio.run(PA._note_owner_premium(NS(id=1), NS(is_premium=False)))
    assert store == {PA.BTN_KEY: "0"}


def test_premium_or_unknown_owner_or_explicit_choice_is_untouched(monkeypatch):
    store = {}
    gs, ss = _run(None, store)
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "set_setting", ss)
    asyncio.run(PA._note_owner_premium(NS(id=1), NS(is_premium=True)))
    asyncio.run(PA._note_owner_premium(NS(id=1), NS(is_premium=None)))
    assert store == {}
    store[PA.BTN_KEY] = "1"                      # مالک خودش روشن کرده (مثلاً یوزرنیمِ فرگمنت دارد)
    asyncio.run(PA._note_owner_premium(NS(id=1), NS(is_premium=False)))
    assert store[PA.BTN_KEY] == "1"
