"""PATCH_GUARD_QUIET_V1 — محافظِ خاموش/وصل‌نشده در گروه‌ها ساکت است (فقط دستورِ صریحِ «/…» از ادمین جواب می‌گیرد)."""
from types import SimpleNamespace as NS

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

import tenant_group_guard as G


class Msg:
    def __init__(self, text, uid=5):
        self.text, self.replies = text, []
        self.chat = NS(id=-100, type="supergroup")
        self.from_user = NS(id=uid, full_name="x", username="x")

    async def reply(self, text, **k):
        self.replies.append(text)


class Bot:
    def __init__(self, admin=True):
        self.admin = admin

    async def get_chat_member(self, chat, uid):
        return NS(status="administrator" if self.admin else "member")


@pytest.fixture
def setup(monkeypatch):
    store = {"group_guard_enabled": "0", "group_guard_chat_id": ""}

    async def get_row(bot): return NS(id=1, owner_id=9, plan="pro")

    async def get(row_id, key, default=""): return store.get(key, default)
    monkeypatch.setattr(G, "_get_row", get_row)
    monkeypatch.setattr(G, "_get", get)
    return store


@pytest.mark.asyncio
async def test_bare_words_are_silent_when_guard_off_or_not_connected(setup):
    for word in ("بن", "اخطار", "سکوت", "/بن"):
        m = Msg(word, uid=5)
        with pytest.raises(SkipHandler):
            await G.tenant_guard_commands(m, Bot(admin=False))                 # عضوِ عادی
        assert m.replies == []
    m = Msg("بن")
    with pytest.raises(SkipHandler):
        await G.tenant_guard_commands(m, Bot(admin=True))                      # کلمهٔ ساده حتی از ادمین جواب نمی‌گیرد
    assert m.replies == []
    setup["group_guard_enabled"] = "1"                                         # روشن ولی گروه وصل نیست
    m = Msg("اخطار")
    with pytest.raises(SkipHandler):
        await G.tenant_guard_commands(m, Bot(admin=True))
    assert m.replies == []


@pytest.mark.asyncio
async def test_explicit_slash_command_from_admin_still_gets_guidance(setup):
    m = Msg("/بن")
    await G.tenant_guard_commands(m, Bot(admin=True))
    assert len(m.replies) == 1 and "خاموش" in m.replies[0]
    m2 = Msg("/بن")
    with pytest.raises(SkipHandler):                                           # عضوِ عادی همچنان ساکت
        await G.tenant_guard_commands(m2, Bot(admin=False))
    assert m2.replies == []
    setup["group_guard_enabled"] = "1"
    m3 = Msg("/بن")
    await G.tenant_guard_commands(m3, Bot(admin=True))
    assert len(m3.replies) == 1 and "متصل" in m3.replies[0]
