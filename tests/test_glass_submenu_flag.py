"""ABOL: زیرمنوی «شیشه‌ای» در رباتِ کیبوردی (منویِ اصلی کیبوردی، زیرمنو اینلاین)."""
from types import SimpleNamespace

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import tenant

pytestmark = pytest.mark.asyncio


class _Msg:
    def __init__(self):
        self.sent = []
        self.from_user = SimpleNamespace(id=5)

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))


class _State:
    def __init__(self):
        self.data = {}

    async def update_data(self, **kw):
        self.data.update(kw)


@pytest.fixture
def store(monkeypatch):
    st = {}

    async def gs(bid, key, default=""):
        return st.get(key, default)

    async def ss(bid, key, val):
        st[key] = val
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "set_setting", ss)
    return st


async def test_toggle_roundtrip_and_ids(store):
    assert await tenant.glass_submenu_ids(1) == set()
    await tenant.set_glass_submenu(1, 12, True)
    await tenant.set_glass_submenu(1, 7, True)
    assert await tenant.glass_submenu_ids(1) == {7, 12}
    assert store["glass_submenus"] == "7,12"
    await tenant.set_glass_submenu(1, 12, False)
    assert await tenant.glass_submenu_ids(1) == {7}


async def test_glass_submenu_sends_inline_and_not_reply_keyboard(store, monkeypatch):
    await tenant.set_glass_submenu(1, 12, True)
    ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="a", callback_data="mbtn:3")]])

    async def fake_inline(bot_id, parent_id):
        return ikb

    async def reply_kb_must_not_be_built(*a, **k):
        raise AssertionError("reply keyboard must not be built for a glass submenu")
    monkeypatch.setattr(tenant, "submenu_inline_kb", fake_inline)
    monkeypatch.setattr(tenant, "submenu_kb", reply_kb_must_not_be_built)
    msg, st = _Msg(), _State()
    btn = SimpleNamespace(id=12, label="سرویس‌ها", message="", support_btn=False,
                          support_contact=None, parent_id=None)
    await tenant._render_reply_submenu(msg, 1, btn, st)
    assert len(msg.sent) == 1 and msg.sent[0][1] is ikb
    assert st.data == {"cur_submenu_id": None}


async def test_non_glass_submenu_still_uses_reply_keyboard(store, monkeypatch):
    called = []

    async def fake_reply(bot_id, parent_id, **kw):
        called.append(parent_id)
        return None
    monkeypatch.setattr(tenant, "submenu_kb", fake_reply)
    msg, st = _Msg(), _State()
    btn = SimpleNamespace(id=13, label="x", message="hi", support_btn=False,
                          support_contact=None, parent_id=None)
    await tenant._render_reply_submenu(msg, 1, btn, st)
    assert called == [13]


async def test_owner_look_menu_has_glass_toggle_only_for_submenus(store):
    sub = SimpleNamespace(id=12, bot_id=1, action="submenu", label="x", message="",
                          color="", icon_emoji_id="", parent_id=None)
    other = SimpleNamespace(id=14, bot_id=1, action="buy_config", label="x", message="",
                            color="", icon_emoji_id="", parent_id=None)

    def cbs(kb):
        return [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "thb:glass:12" in cbs(await tenant._thb_kb_look_glass(sub))
    assert "thb:glass:14" not in cbs(await tenant._thb_kb_look_glass(other))
    # ردیفِ «بازگشت» همچنان آخر است
    assert cbs(await tenant._thb_kb_look_glass(sub))[-1].startswith("thb:pick:")
