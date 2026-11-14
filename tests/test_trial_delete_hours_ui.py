"""PATCH_TRIAL_DELETE_HOURS_UI_V1 — مالک مهلتِ حذفِ خودکارِ تست را از تنظیماتِ تست رایگان تغییر می‌دهد (تیکت #878)."""
from types import SimpleNamespace

import pytest

import tenant
from tenant.shared import trial_cleanup as TC

pytestmark = pytest.mark.asyncio


def test_label():
    L = tenant._trial_del_label
    assert L("5") == "5 ساعت بعد از پایانِ تست" and L("24") == "1 روز بعد از پایانِ تست"
    assert L("72") == "3 روز بعد از پایانِ تست" and L("abc") == "5 ساعت بعد از پایانِ تست"
    assert L("9999") == "7 روز بعد از پایانِ تست"                       # بالاتر از سقف ⇒ ۷ روز (همان‌طور که حلقه کلمپ می‌کند)


@pytest.fixture
def env(monkeypatch):
    store = {}
    row = SimpleNamespace(id=9, owner_id=1)

    async def owner_ok(cb, bot):
        return row

    async def owner_ok_msg(msg, bot):
        return row

    async def gs(bot_id, key, default=""):
        return store.get((bot_id, key), default)

    async def ss(bot_id, key, value):
        store[(bot_id, key)] = value

    monkeypatch.setattr(tenant, "_owner_ok", owner_ok)
    monkeypatch.setattr(tenant, "_owner_ok_msg", owner_ok_msg)
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "set_setting", ss)
    return store


def cb(data):
    shown = []

    async def edit_text(t, reply_markup=None, **kw):
        shown.append((t, reply_markup))

    async def answer(*a, **kw):
        return None

    c = SimpleNamespace(data=data, message=SimpleNamespace(edit_text=edit_text, answer=edit_text), answer=answer)
    c.shown = shown
    return c


async def test_menu_shows_current_value_and_presets(env):
    c = cb("set:trial_del")
    await tenant.set_trial_del_menu(c, None)
    text, kb = c.shown[0]
    assert "5 ساعت بعد از پایانِ تست" in text
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert {"set:trial_del_v:1", "set:trial_del_v:5", "set:trial_del_v:24", "set:trial_del_v:168",
            "set:trial_del_custom", "set:trial"} <= set(datas)


async def test_preset_saves_and_cleanup_loop_reads_it(env, monkeypatch):
    await tenant.set_trial_del_preset(cb("set:trial_del_v:12"), None)
    assert env[(9, "trial_delete_hours")] == "12"

    async def sg(bot_id, key, default=""):
        return env.get((bot_id, key), default)
    monkeypatch.setattr(TC, "_setting_get", sg)
    assert await TC._delete_hours(9) == 12.0


async def test_preset_is_clamped(env):
    await tenant.set_trial_del_preset(cb("set:trial_del_v:99999"), None)
    assert env[(9, "trial_delete_hours")] == "168"
    await tenant.set_trial_del_preset(cb("set:trial_del_v:0"), None)
    assert env[(9, "trial_delete_hours")] == "1"


class FakeState:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


def msg(text):
    out = []

    async def answer(t, **kw):
        out.append(t)

    return SimpleNamespace(text=text, answer=answer, out=out)


@pytest.mark.parametrize("text,saved", [("6h", "6"), ("2 روز", "48"), ("1d 12h", "36"), ("7d", "168")])
async def test_custom_value_saved(env, text, saved):
    st = FakeState()
    m = msg(text)
    await tenant.set_trial_del_custom_save(m, None, st)
    assert env[(9, "trial_delete_hours")] == saved and st.cleared and "ذخیره شد" in m.out[0]


@pytest.mark.parametrize("text", ["abc", "", "0h", "8d", "200h"])
async def test_custom_invalid_rejected_and_state_kept(env, text):
    st = FakeState()
    m = msg(text)
    await tenant.set_trial_del_custom_save(m, None, st)
    assert (9, "trial_delete_hours") not in env and not st.cleared and m.out and "❌" in m.out[0]
