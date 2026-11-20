"""PATCH_GROUP_ADMIN_CHARGE_V1 — «50000 شارژ» در گروه بات‌ساز، توسط مدیر (اکانت شخصی یا کانال مجاز)."""
import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import platform_group_charge as G

ADMIN = config.PLATFORM_ADMIN_ID
GROUP = -1009


def test_parse():
    assert G.parse("50000 شارژ") == (1, 50000, None)
    assert G.parse("شارژ ۵۰,۰۰۰") == (1, 50000, None)
    assert G.parse("کسر 2000 @Ali_99") == (-1, 2000, "@Ali_99")
    assert G.parse("شارژ 1000 123456789") == (1, 1000, "123456789")
    for t in ("شارژ", "شارژ کردم 5000 تومن", "0 شارژ", "سلام", "شارژ 50000 لطفا"):
        assert G.parse(t) is None, t


class _U:
    def __init__(self, uid, name="u", username=None, is_bot=False):
        self.id, self.full_name, self.username, self.is_bot = uid, name, username, is_bot


class _Chat:
    def __init__(self, cid, title="t"):
        self.id, self.title = cid, title


class _Reply:
    def __init__(self, user):
        self.from_user, self.sender_chat = user, None
        self.forum_topic_created = self.is_automatic_forward = None


class _Msg:
    def __init__(self, text, user=None, sender_chat=None, reply_to=None):
        self.text, self.from_user, self.sender_chat = text, user, sender_chat
        self.chat, self.reply_to_message, self.replies = _Chat(GROUP, "گروه بات‌ساز"), reply_to, []

    async def reply(self, text, **kw):
        self.replies.append(text)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@pytest.fixture
async def db(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    S = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(G, "Session", S)
    pset = {G.CH_KEY: "-1007777"}

    async def gp(k, d=""):
        return pset.get(k, d)

    async def sp(k, v):
        pset[k] = v
    monkeypatch.setattr(G, "_gp", gp)
    monkeypatch.setattr(G, "_sp", sp)
    async with S() as s:
        s.add(models.PlatformUser(telegram_id=55, full_name="Ali", username="ali_99", balance=1000))
        await s.commit()
    yield S, pset
    await eng.dispose()


async def _bal(S, uid):
    async with S() as s:
        u = await s.scalar(select(models.PlatformUser).where(models.PlatformUser.telegram_id == uid))
        return float(u.balance)


@pytest.mark.asyncio
async def test_permissions(db):
    assert await G.can_charge(_Msg("x", _U(ADMIN)))
    assert await G.can_charge(_Msg("x", _U(136817688), sender_chat=_Chat(-1007777)))     # کانال مجاز
    assert not await G.can_charge(_Msg("x", _U(136817688), sender_chat=_Chat(-1008888)))  # کانال غیرمجاز
    assert not await G.can_charge(_Msg("x", _U(1087968824), sender_chat=_Chat(GROUP)))    # ادمین ناشناس
    assert not await G.can_charge(_Msg("x", _U(12345)))


@pytest.mark.asyncio
async def test_channel_admin_charges_by_reply_and_undo_once(db):
    S, _ = db
    bot = _Bot()
    m = _Msg("50000 شارژ", _U(136817688), sender_chat=_Chat(-1007777, "کانال من"), reply_to=_Reply(_U(55, "Ali")))
    await G.group_charge(m, bot)
    assert await _bal(S, 55) == 51000 and "51,000 تومان" in m.replies[-1]
    assert any(c == 55 for c, _t in bot.sent)                                       # به خود شخص خبر رفت
    async with S() as s:
        t = await s.scalar(select(models.PlatformTxn).where(models.PlatformTxn.user_telegram_id == 55))
    class _CbMsg:
        text = "ok"
        async def edit_text(self, *a, **k):
            pass
    class _Cb:
        def __init__(self, uid):
            self.from_user, self.data, self.message, self.alerts = _U(uid), f"pgc:undo:{t.id}", _CbMsg(), []
        async def answer(self, text=None, **k):
            self.alerts.append(text)
    other = _Cb(12345)
    await G.undo(other)
    assert await _bal(S, 55) == 51000 and "فقط مدیر" in other.alerts[-1]
    await G.undo(_Cb(ADMIN))
    assert await _bal(S, 55) == 1000
    again = _Cb(ADMIN)
    await G.undo(again)
    assert await _bal(S, 55) == 1000 and "قبلا برگشت" in again.alerts[-1]


@pytest.mark.asyncio
async def test_admin_by_username_deduct_and_limits(db):
    S, pset = db
    bot = _Bot()
    m = _Msg("کسر 500 @Ali_99", _U(ADMIN))
    await G.group_charge(m, bot)
    assert await _bal(S, 55) == 500
    m = _Msg("کسر 5000 @ali_99", _U(ADMIN))
    await G.group_charge(m, bot)
    assert await _bal(S, 55) == 500 and "موجودی" in m.replies[-1]                  # منفی نمی‌شود
    pset[G.MAX_KEY] = "100000"
    m = _Msg("شارژ 200000 @ali_99", _U(ADMIN))
    await G.group_charge(m, bot)
    assert await _bal(S, 55) == 500 and "سقف" in m.replies[-1]
    m = _Msg("شارژ 1000", _U(ADMIN))                                                # بی‌ریپلای و بی‌آیدی
    await G.group_charge(m, bot)
    assert "ریپلای" in m.replies[-1]


@pytest.mark.asyncio
async def test_unauthorized_user_skipped_unknown_channel_gets_approve_button(db):
    S, pset = db
    bot = _Bot()
    with pytest.raises(SkipHandler):
        await G.group_charge(_Msg("50000 شارژ", _U(12345), reply_to=_Reply(_U(55))), bot)
    m = _Msg("شارژ ۵۰۰۰۰", _U(136817688), sender_chat=_Chat(-1008888, "کانال من"), reply_to=_Reply(_U(55)))
    m.reply_markup = None

    async def reply(text, **kw):
        m.replies.append(text)
        m.reply_markup = kw.get("reply_markup")
    m.reply = reply
    await G.group_charge(m, bot)
    assert await _bal(S, 55) == 1000 and "تایید" in m.replies[-1]
    data = m.reply_markup.inline_keyboard[0][0].callback_data
    assert data.startswith("pgc:ok:")

    class _CbMsg:
        async def delete(self):
            pass

    class _Cb:
        def __init__(self, uid):
            self.from_user, self.data, self.message, self.alerts = _U(uid), data, _CbMsg(), []

        async def answer(self, text=None, **k):
            self.alerts.append(text)
    stranger = _Cb(12345)
    await G.approve(stranger, bot)
    assert await _bal(S, 55) == 1000 and "فقط مدیر" in stranger.alerts[-1]
    await G.approve(_Cb(ADMIN), bot)
    assert await _bal(S, 55) == 51000 and "-1008888" in pset[G.CH_KEY]          # شارژ شد و کانال مجاز شد
    again = _Cb(ADMIN)
    await G.approve(again, bot)
    assert await _bal(S, 55) == 51000 and "منقضی" in again.alerts[-1]           # یک‌بار مصرف
    m2 = _Msg("شارژ 1000", _U(136817688), sender_chat=_Chat(-1008888, "کانال من"), reply_to=_Reply(_U(55)))
    await G.group_charge(m2, bot)                                               # دفعهٔ بعد بدون تایید
    assert await _bal(S, 55) == 52000


@pytest.mark.asyncio
async def test_anonymous_admin_approval_does_not_allowlist_group(db):
    S, pset = db
    bot = _Bot()
    m = _Msg("شارژ 2000", _U(1087968824), sender_chat=_Chat(GROUP), reply_to=_Reply(_U(55)))
    kb = {}

    async def reply(text, **kw):
        kb["k"] = kw.get("reply_markup")
    m.reply = reply
    await G.group_charge(m, bot)
    token_data = kb["k"].inline_keyboard[0][0].callback_data

    class _CbMsg:
        async def delete(self):
            pass

    class _Cb:
        from_user, data, message = _U(ADMIN), token_data, _CbMsg()

        async def answer(self, *a, **k):
            pass
    await G.approve(_Cb(), bot)
    assert await _bal(S, 55) == 3000 and str(GROUP) not in pset[G.CH_KEY]


def test_wired():
    import inspect
    import platform_bot
    import platform_group_guard
    src = inspect.getsource(platform_bot)
    assert src.index("platform_group_charge_router)") < src.index("include_router(group_guard_router)")
    assert inspect.getsource(platform_group_guard).count('callback_data="pgc:menu"') == 2
