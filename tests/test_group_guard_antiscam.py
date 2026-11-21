"""PATCH_GROUP_GUARD_ANTISCAM_V1: اسپمِ «I have a gift for you» در گروه حذف و فرستنده بن می‌شود."""
from types import SimpleNamespace as NS

import pytest

import platform_group_guard as GG


@pytest.mark.parametrize("txt", [
    "I have a gift for you", "i HAVE a GIFT for u 🎁", "🎁 I have a surprise for you 🎁",
    "Claim your free gift now", "claim your reward", "Congratulations! You won a prize",
    "You've been selected", "you have won 1000 USDT", "DM me to get your gift", "free stars click the link to claim",
])
def test_scam_phrases_are_detected(txt):
    assert GG.looks_like_gift_scam(txt), txt


@pytest.mark.parametrize("txt", [
    "سلام، گیفت استارز موجوده؟", "هدیه برای شما دارم", "یه هدیه می‌خوام بخرم", "gift card", "ربات فروش پرمیوم",
    "I want to buy a gift", "how to get premium", "", None,
])
def test_normal_messages_are_not_flagged(txt):
    assert not GG.looks_like_gift_scam(txt)


class _Bot:
    def __init__(self):
        self.deleted, self.banned = [], []

    async def delete_message(self, chat, mid):
        self.deleted.append((chat, mid))

    async def ban_chat_member(self, chat, uid):
        self.banned.append((chat, uid))
        return True

    async def get_chat_member(self, chat, uid):
        return NS(status="kicked")


class _Msg:
    def __init__(self, text=None, caption=None, uid=9, bot=False):
        self.text, self.caption = text, caption
        self.from_user = NS(id=uid, full_name="Spam", username="sp", is_bot=bot)
        self.chat = NS(id=-1)
        self.message_id = 5
        self.sender_chat = None
        self.entities = self.caption_entities = None
        self.answers = []

    async def answer(self, text, **k):
        self.answers.append(text)


@pytest.fixture
def patched(monkeypatch):
    store = {}

    async def gp(key, default=""):
        return store.get(key, default)

    async def auth(msg, bot):
        return msg.from_user.id == 1                      # ادمین
    logged = []

    async def log(text):
        logged.append(text)

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def scalar(self, *a, **k):
            return None

        def add(self, x):
            self.row = x

        async def commit(self):
            logged.append(("db", self.row.reason))
    monkeypatch.setattr(GG, "get_pset", gp)
    monkeypatch.setattr(GG, "_is_authorized", auth)
    monkeypatch.setattr(GG, "_log_action", log)
    monkeypatch.setattr(GG, "Session", lambda: _S())
    return store, logged


@pytest.mark.asyncio
async def test_photo_caption_scam_is_deleted_and_sender_banned(patched):
    _s, logged = patched
    bot, m = _Bot(), _Msg(text=None, caption="I have a gift for you")
    assert await GG._check_antiscam(m, bot) is True
    assert bot.deleted == [(-1, 5)] and bot.banned == [(-1, 9)]
    assert any(isinstance(x, tuple) and "اسپم" in x[1] for x in logged) and m.answers and "بن شد" in m.answers[0]


@pytest.mark.asyncio
async def test_admins_normal_messages_bots_and_off_switch_are_untouched(patched):
    store, _ = patched
    bot = _Bot()
    assert await GG._check_antiscam(_Msg(text="I have a gift for you", uid=1), bot) is False      # ادمین
    assert await GG._check_antiscam(_Msg(text="سلام وقت بخیر"), bot) is False
    assert await GG._check_antiscam(_Msg(text="I have a gift for you", bot=True), bot) is False   # بات/سیستمی
    store["group_guard_antiscam"] = "off"
    assert await GG._check_antiscam(_Msg(text="I have a gift for you"), bot) is False
    assert bot.deleted == [] and bot.banned == []
