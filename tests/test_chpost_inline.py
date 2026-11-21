"""PATCH_CHPOST_INLINE_V1 — ارسالِ اینلاینِ پستِ کانال (ایموجیِ پرمیومِ ادمین سالم می‌ماند)."""
import json
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models

OWNER = 500
TXT = '<b>سلام</b> <tg-emoji emoji-id="5368324170671202286">🙂</tg-emoji> خوش آمدید'


@pytest_asyncio.fixture
async def env(monkeypatch):
    import sys
    import tenant
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    _orig = config.Session
    for _m in list(sys.modules.values()):
        if getattr(_m, "Session", None) is _orig:
            monkeypatch.setattr(_m, "Session", maker, raising=False)
    monkeypatch.setattr(config, "Session", maker)
    tenant._SETTING_CACHE.clear()
    tenant._BOT_ROW_CACHE.clear()
    async with maker() as s:
        b = models.Bot(owner_id=OWNER, token="1:TOK", username="mybot", field="vpn_sales", plan="pro", status="active")
        s.add(b)
        await s.commit()
        bid = b.id
    row_async = None

    async def noop_kb(bot_id, buttons, button_ids, bot=None, guide_keys=None):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        rows = [[InlineKeyboardButton(text=b[0] if isinstance(b, list) else str(b), url="https://example.com")] for b in buttons]
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    monkeypatch.setattr(tenant, "_chpost_full_kb", noop_kb)
    yield NS(maker=maker, bid=bid, T=tenant, bot=NS(token="1:TOK", id=42))
    await engine.dispose()


class FakeQuery:
    def __init__(self, uid, query=""):
        self.from_user, self.query, self.got = NS(id=uid), query, None

    async def answer(self, results, **kw):
        self.got = (results, kw)


@pytest.mark.asyncio
async def test_draft_roundtrip_and_inline_results(env):
    T = env.T
    did = await T._chpost_draft_save(env.bid, OWNER, {"chpost_text": TXT, "chpost_photo": None,
                                                      "chpost_buttons": [["کانال", "https://t.me/x"]]})
    q = FakeQuery(OWNER, f"cp{did}")
    await T.tenant_inline_chpost(q, env.bot)
    results, kw = q.got
    assert kw["is_personal"] is True and kw["cache_time"] == 0 and len(results) == 1
    r = results[0]
    assert r.id == f"cp{did}" and "سلام" in r.title and "<" not in r.title
    msg = r.input_message_content
    assert msg.parse_mode == "HTML" and "tg-emoji" in msg.message_text and "5368324170671202286" in msg.message_text   # ایموجیِ پرمیوم دست‌نخورده
    assert r.reply_markup.inline_keyboard[0][0].url == "https://example.com"


@pytest.mark.asyncio
async def test_photo_draft_uses_cached_photo_with_html_caption(env):
    T = env.T
    did = await T._chpost_draft_save(env.bid, OWNER, {"chpost_text": TXT, "chpost_photo": "AgACfileid"})
    q = FakeQuery(OWNER, f"cp{did}")
    await T.tenant_inline_chpost(q, env.bot)
    r = q.got[0][0]
    assert r.photo_file_id == "AgACfileid" and r.parse_mode == "HTML" and "tg-emoji" in r.caption and r.title.startswith("🖼")


@pytest.mark.asyncio
async def test_empty_query_lists_latest_drafts_newest_first_and_caps_at_10(env):
    T = env.T
    for i in range(12):
        await T._chpost_draft_save(env.bid, OWNER, {"chpost_text": f"پست {i}"})
    q = FakeQuery(OWNER, "")
    await T.tenant_inline_chpost(q, env.bot)
    res = q.got[0]
    assert len(res) == 10 and "پست 11" in res[0].title


@pytest.mark.asyncio
async def test_only_owner_or_admin_sees_drafts(env):
    T = env.T
    did = await T._chpost_draft_save(env.bid, OWNER, {"chpost_text": "محرمانه"})
    for uid in (999, 0):
        q = FakeQuery(uid, f"cp{did}")
        await T.tenant_inline_chpost(q, env.bot)
        assert q.got[0] == []                                              # غریبه: هیچ‌چیز
    # ادمینِ دیگر هم پیش‌نویسِ مالک را نمی‌بیند (به‌ازایِ هر ادمین جداست)
    q = FakeQuery(700, f"cp{did}")
    await T.tenant_inline_chpost(q, env.bot)
    assert q.got[0] == []


@pytest.mark.asyncio
async def test_free_plan_gets_nothing(env):
    T = env.T
    async with env.maker() as s:
        b = await s.get(models.Bot, env.bid)
        b.plan = "free"
        await s.commit()
    T._BOT_ROW_CACHE.clear()
    did = await T._chpost_draft_save(env.bid, OWNER, {"chpost_text": "x"})
    q = FakeQuery(OWNER, f"cp{did}")
    await T.tenant_inline_chpost(q, env.bot)
    assert q.got[0] == []


@pytest.mark.asyncio
async def test_draft_retention_keeps_last_20(env):
    T = env.T
    for i in range(25):
        await T._chpost_draft_save(env.bid, OWNER, {"chpost_text": f"p{i}"})
    async with env.maker() as s:
        n = len((await s.scalars(select(models.ChpostDraft).where(models.ChpostDraft.user_telegram_id == OWNER))).all())
    assert n == 20


class FakeMsg:
    def __init__(self):
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))


class FakeCb:
    def __init__(self):
        self.message, self.from_user, self.data = FakeMsg(), NS(id=OWNER), "tadm:chpost:inline"
        self.alerts = []

    async def answer(self, text=None, show_alert=False, **k):
        self.alerts.append(text)


class FakeState:
    def __init__(self, data):
        self._d = data

    async def get_data(self):
        return dict(self._d)


@pytest.mark.asyncio
async def test_inline_button_flow_and_botfather_hint(env, monkeypatch):
    T = env.T

    class B:
        token = "1:TOK"
        id = 42

        def __init__(self, supports):
            self.supports = supports

        async def get_me(self):
            return NS(username="mybot", supports_inline_queries=self.supports)
    # ۱) حالتِ اینلاین خاموش ⇒ راهنمایِ BotFather، بدونِ پیش‌نویس
    cb = FakeCb()
    await T.tadm_chpost_inline(cb, B(False), FakeState({"chpost_text": TXT}))
    assert "/setinline" in cb.message.sent[0][0]
    async with env.maker() as s:
        assert (await s.scalars(select(models.ChpostDraft))).all() == []
    # ۲) روشن ⇒ پیش‌نویس + دکمهٔ switch_inline_query_chosen_chat
    cb = FakeCb()
    await T.tadm_chpost_inline(cb, B(True), FakeState({"chpost_text": TXT, "chpost_buttons": []}))
    text, kw = cb.message.sent[0]
    btn = kw["reply_markup"].inline_keyboard[0][0]
    sw = btn.switch_inline_query_chosen_chat
    assert sw.query.startswith("cp") and sw.allow_channel_chats and sw.allow_group_chats and "@mybot" in text
    async with env.maker() as s:
        d = (await s.scalars(select(models.ChpostDraft))).all()
    assert len(d) == 1 and d[0].text == TXT and sw.query == f"cp{d[0].id}"
    # ۳) متنِ خالی ⇒ هشدار
    cb = FakeCb()
    await T.tadm_chpost_inline(cb, B(True), FakeState({"chpost_text": "  "}))
    assert cb.alerts and "خالی" in cb.alerts[0]


def test_preview_has_inline_button():
    import inspect
    from tenant import _chpost_preview
    assert "tadm:chpost:inline" in inspect.getsource(_chpost_preview)
