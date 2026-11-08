"""PATCH_CHPOST_INLINE_V1 — ارسالِ اینلاین در رباتِ اصلیِ بات‌ساز + دکمه‌ها در «💬 پشتیبانی و اطلاع‌رسانی»."""
import inspect
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_admin_menu as AM
import platform_chpost as PC

ADMIN = 424242


def test_admin_menu_support_section_has_both_buttons_for_super_admin_only():
    sec = AM._SECTION_BY_KEY["support"]
    cbs = [it[1] for it in sec["items"]]
    assert "chpost:menu" in cbs and "chpost:ua_menu" in cbs                  # PATCH_CHPOST_USERBOT_V1: ارسال با اکانتِ پرمیوم
    items_super = [it[1] for it in AM._section_items(sec, True)]
    items_sub = [it[1] for it in AM._section_items(sec, False)]
    assert "chpost:ua_menu" in items_super and "chpost:ua_menu" not in items_sub and "chpost:menu" not in items_sub


def test_old_settings_entry_moved_not_duplicated():
    import platform_bot
    assert 'callback_data="chpost:menu"' not in inspect.getsource(platform_bot.settings_kb)
    assert "adm:cat:support" in inspect.getsource(PC._chpost_menu)         # بازگشت به همان دسته


@pytest_asyncio.fixture
async def env(monkeypatch):
    import sys
    monkeypatch.setattr(PC, "PLATFORM_ADMIN_ID", ADMIN)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    _orig = config.Session
    for _m in list(sys.modules.values()):
        if getattr(_m, "Session", None) is _orig:
            monkeypatch.setattr(_m, "Session", maker, raising=False)
    monkeypatch.setattr(config, "Session", maker)
    yield NS(maker=maker)
    await engine.dispose()


async def _noop(**kw):
    return None


class Msg:
    def __init__(self):
        self.sent = []
        self.chat = NS(id=ADMIN)

    async def answer(self, text, **kw):
        self.sent.append((text, kw))

    async def edit_reply_markup(self, **k):
        return None


class Cb:
    def __init__(self, uid=ADMIN):
        self.message, self.from_user, self.data, self.alerts = Msg(), NS(id=uid), "x", []

    async def answer(self, text=None, show_alert=False, **k):
        self.alerts.append(text)


class St:
    def __init__(self, data=None):
        self.d, self.state = dict(data or {}), None

    async def get_data(self):
        return dict(self.d)

    async def update_data(self, **kw):
        self.d.update(kw)

    async def set_state(self, s):
        self.state = s


class Q:
    def __init__(self, uid, query=""):
        self.from_user, self.query, self.got = NS(id=uid), query, None

    async def answer(self, results, **kw):
        self.got = (results, kw)


@pytest.mark.asyncio
async def test_inline_start_still_works_as_hidden_path(env):
    st, cb = St(), Cb()
    await PC.chpost_inline_start(cb, st)
    assert st.d["chpost_mode"] == "inline" and st.state is not None and cb.message.sent
    cb2 = Cb(uid=999)
    await PC.chpost_inline_start(cb2, St())
    assert cb2.alerts and "مدیر اصلی" in cb2.alerts[0]                 # غیرِ مدیر اجازه ندارد


@pytest.mark.asyncio
async def test_preview_offers_account_send_only_when_account_connected(env, monkeypatch):
    store = {"chpost_chat_id": "-100", "chpost_group_id": "-200"}

    async def gp(k, d=""):
        return store.get(k, d)
    monkeypatch.setattr(PC, "get_pset", gp)
    for connected in (False, True):
        store["chpost_ua_session"] = "enc" if connected else ""
        st = St({"chpost_text": "x", "chpost_mode": "new"})
        m = Msg()
        await PC._chpost_preview(m, st, NS(copy_message=_noop))
        kb = m.sent[-1][1]["reply_markup"]
        datas = [b.callback_data for r in kb.inline_keyboard for b in r]
        assert ("chpost:ua_send" in datas) is connected and "chpost:send" in datas


@pytest.mark.asyncio
async def test_inline_button_flow_saves_draft_for_bot_zero_and_inline_query_lists_it(env):
    TXT = '<b>سلام</b> <tg-emoji emoji-id="5368324170671202286">🙂</tg-emoji>'

    class B:
        async def get_me(self):
            return NS(username="babotsazbot", supports_inline_queries=True)
    cb = Cb()
    await PC.chpost_inline(cb, B(), St({"chpost_text": TXT, "chpost_buttons": []}))
    text, kw = cb.message.sent[0]
    sw = kw["reply_markup"].inline_keyboard[0][0].switch_inline_query_chosen_chat
    async with env.maker() as s:
        d = (await s.scalars(select(models.ChpostDraft))).all()
    assert len(d) == 1 and d[0].bot_id == 0 and sw.query == f"cp{d[0].id}" and "@babotsazbot" in text
    q = Q(ADMIN, f"cp{d[0].id}")
    await PC.chpost_inline_query(q, B())
    res = q.got[0]
    assert len(res) == 1 and "tg-emoji" in res[0].input_message_content.message_text
    qn = Q(555, f"cp{d[0].id}")                                        # غیرِ مدیر: هیچ
    await PC.chpost_inline_query(qn, B())
    assert qn.got[0] == []


@pytest.mark.asyncio
async def test_inline_mode_off_gives_botfather_hint_and_no_draft(env):
    class B:
        async def get_me(self):
            return NS(username="b", supports_inline_queries=False)
    cb = Cb()
    await PC.chpost_inline(cb, B(), St({"chpost_text": "x"}))
    assert "/setinline" in cb.message.sent[0][0]
    async with env.maker() as s:
        assert (await s.scalars(select(models.ChpostDraft))).all() == []
    cb2 = Cb()
    await PC.chpost_inline(cb2, B(), St({"chpost_text": "  "}))
    assert cb2.alerts and "خالی" in cb2.alerts[0]


def test_inline_query_filter_does_not_swallow_selfsaz_panel_query():
    import re
    src = inspect.getsource(PC)
    pat = re.search(r'inline_query\(F\.query\.regexp\(r"([^"]+)"\)\)', src).group(1)
    assert re.fullmatch(pat, "") and re.fullmatch(pat, "cp12") and re.fullmatch(pat, "  ")
    assert not re.fullmatch(pat, "panel") and not re.fullmatch(pat, "پنل") and not re.fullmatch(pat, "cp12x")
