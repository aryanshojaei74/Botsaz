"""PATCH_TEXT_FORMATTING_V1 — متن‌های اطلاع‌رسانی سفارش: قالب تلگرام (بولد/…) حفظ شود و HTML ناقص ذخیره نشود."""
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import tenant
from tenant.bots.virtual_service import smm
from tenant.bots.virtual_service import smm_texts_admin as TA


def test_html_problem_accepts_valid_and_rejects_broken_markup():
    ok = ("سلام <b>{{user_name}}</b> <i>x</i> <u>y</u> <s>z</s> <code>1</code> <pre>p</pre> "
          '<a href="https://t.me/x">لینک</a> <tg-spoiler>s</tg-spoiler> <blockquote>q</blockquote> '
          "<b><i>تو در تو</i></b> &lt; و &amp; 5 &gt; 3 {{balance}}")
    assert TA.html_problem(ok) is None and TA.html_problem("متن ساده") is None
    for bad, needle in (("<b>باز", "بسته نشده"), ("بسته</b>", "جای درستی نیست".replace("", "")),
                        ("<b><i>x</b></i>", "جای درستی نیست".replace("", "")), ("<foo>x</foo>", "نمی‌شناسد"),
                        ("اگر a < b باشد", "تنها"), ("5 <", "تنها")):
        msg = TA.html_problem(bad)
        assert msg and needle.replace("", "") in msg.replace("", ""), (bad, msg)


class _Msg:
    def __init__(self, text, entities=None, html=None):
        self.text, self.entities, self.html_text = text, entities or [], html or text
        self.from_user = SimpleNamespace(id=1)
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.sent.append(text)
        return self


@pytest.fixture
def env(monkeypatch):
    store = {}

    async def set_setting(bot_id, key, value):
        store[key] = value

    async def get_setting(bot_id, key, default=""):
        return store.get(key, default)

    async def owner(msg, bot):
        return SimpleNamespace(id=7)
    monkeypatch.setattr(tenant, "set_setting", set_setting)
    monkeypatch.setattr(tenant, "get_setting", get_setting)
    monkeypatch.setattr(smm, "_owner", owner)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    return SimpleNamespace(store=store, state=state)


async def _save(env, msg, key="completed"):
    await env.state.update_data(smmtxt_key=key)
    await env.state.set_state(TA.TxtFSM.edit)
    await TA.smmtxt_save(msg, object(), env.state)


@pytest.mark.asyncio
async def test_telegram_formatting_is_saved_as_html(env):
    ent = [SimpleNamespace(type="bold"), SimpleNamespace(type="mention")]
    await _save(env, _Msg("سفارشت تکمیل شد", ent, html="سفارشت <b>تکمیل</b> شد"))
    assert env.store["smm_txt_completed"] == "سفارشت <b>تکمیل</b> شد"
    assert await env.state.get_state() is None


@pytest.mark.asyncio
async def test_plain_text_and_typed_tags_are_kept_verbatim(env):
    await _save(env, _Msg("سلام <b>{{user_name}}</b> & خوش آمدی", html="سلام &lt;b&gt;…"))      # بدون entity: html_text نادیده است
    assert env.store["smm_txt_completed"] == "سلام <b>{{user_name}}</b> & خوش آمدی"


@pytest.mark.asyncio
async def test_broken_html_is_refused_and_editing_stays_open(env):
    m = _Msg("سلام <b>بدون بستن")
    await _save(env, m)
    assert "smm_txt_completed" not in env.store
    assert any("ایراد دارد" in t and "بسته نشده" in t for t in m.sent)
    assert await env.state.get_state() == TA.TxtFSM.edit.state                   # کاربر می‌تواند دوباره بفرستد
    ok = _Msg("درست <b>شد</b>")
    await TA.smmtxt_save(ok, object(), env.state)
    assert env.store["smm_txt_completed"] == "درست <b>شد</b>" and await env.state.get_state() is None


@pytest.mark.asyncio
async def test_default_keyword_still_resets(env):
    env.store["smm_txt_completed"] = "x"
    await _save(env, _Msg("پیشفرض"))
    assert env.store["smm_txt_completed"] == ""
