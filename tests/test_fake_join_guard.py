"""PATCH_FJG_CONFIGURABLE_V1 / PERSIST_V1 / RECENT_CMD_V1 — هشدارِ جوینِ انبوه با آستانهٔ قابل‌تنظیم، ذخیرهٔ اخیرها و /fakejoins."""
import asyncio
import json
import time
from collections import deque
from types import SimpleNamespace

import pytest

import fake_join_guard as F

CHAT = -1001111


@pytest.fixture
def g(monkeypatch):
    store: dict = {"group_guard_chat_id": str(CHAT), "group_guard_enabled": "on"}
    sent: list = []

    async def get(key, default=""):
        return store.get(key, default)

    async def put(key, value):
        store[key] = value

    async def save(chat_id, chain):
        store.setdefault("_saved", []).extend(e[1] for e in chain)

    monkeypatch.setattr(F, "_get_pset", get)
    monkeypatch.setattr(F, "_set_pset", put)
    monkeypatch.setattr(F, "_save_candidates", save)
    monkeypatch.setattr(F, "PLATFORM_ADMIN_ID", 42)
    monkeypatch.setattr(F, "_FLUSH_DELAY", 0.01)
    F._join_times.clear(); F._last_alert_at.clear(); F._loaded.clear(); F._flush_pending.clear()

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))
    return SimpleNamespace(store=store, sent=sent, bot=Bot())


async def _join(g, n, start=0):
    for i in range(n):
        await F._register_join(g.bot, CHAT, start + i, f"u{start + i}", None)


@pytest.mark.asyncio
async def test_default_threshold_is_30_so_100_fake_joins_alert(g):
    await _join(g, 29)
    assert not g.sent
    await _join(g, 1, start=29)
    assert len(g.sent) == 1 and g.sent[0][0] == 42 and "30" in g.sent[0][1]


@pytest.mark.asyncio
async def test_threshold_is_configurable_and_bad_values_fall_back(g):
    g.store["fjg_min_count"] = "5"
    await _join(g, 5)
    assert len(g.sent) == 1
    g.store["fjg_min_count"] = "abc"
    assert await F._thresholds() == (F.DEFAULT_MIN_COUNT, F.BURST_GAP_MAX_SEC)


@pytest.mark.asyncio
async def test_recent_joins_survive_a_restart(g):
    await _join(g, 12)
    await asyncio.sleep(0.1)                                  # flush
    saved = json.loads(g.store[f"fjg_recent_{CHAT}"])
    assert len(saved) == 12
    F._join_times.clear(); F._loaded.clear()                  # «ری‌استارت»
    await F._ensure_loaded(CHAT)
    assert {e[1] for e in F._recent(CHAT, 10)} == set(range(12))
    # ورودیِ قدیمی‌تر از پنجره بازیابی نمی‌شود
    g.store[f"fjg_recent_{CHAT}"] = json.dumps([[time.time() - 5000, 999, "old", None]])
    F._join_times.clear(); F._loaded.clear()
    await F._ensure_loaded(CHAT)
    assert F._recent(CHAT, 15) == []


def _cmd(text):
    return SimpleNamespace(args=text)


class _Msg:
    def __init__(self, uid):
        self.from_user = SimpleNamespace(id=uid)
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


@pytest.mark.asyncio
async def test_fakejoins_command_prepares_recent_joiners_and_is_admin_only(g):
    await _join(g, 7)
    # سه نفرِ «قدیمی» (۸ دقیقه پیش)
    now = time.monotonic()
    F._join_times[CHAT] = deque([(now - 480, 100 + i, "old", None) for i in range(3)] + list(F._join_times[CHAT]))
    m = _Msg(42)
    await F.fjg_recent_cmd(m, g.bot, _cmd("3"))               # ۳ دقیقهٔ اخیر ⇒ فقط ۷ نفرِ تازه
    assert sorted(g.store["_saved"]) == list(range(7))
    assert "7" in m.answers[0][0] and m.answers[0][1]["reply_markup"].inline_keyboard[0][0].callback_data == f"fjg:kick:{CHAT}"
    g.store["_saved"] = []
    other = _Msg(7)
    await F.fjg_recent_cmd(other, g.bot, _cmd("10"))
    assert other.answers == [] and g.store["_saved"] == []     # غیرِ مدیر: هیچ


@pytest.mark.asyncio
async def test_fakejoins_when_guard_off_or_empty_explains(g):
    m = _Msg(42)
    await F.fjg_recent_cmd(m, g.bot, _cmd(""))
    assert "هیچ جوینی" in m.answers[0][0]
    g.store["group_guard_enabled"] = "off"
    m2 = _Msg(42)
    await F.fjg_recent_cmd(m2, g.bot, _cmd(""))
    assert "فعال_محافظ" in m2.answers[0][0] and "fjgchat" in m2.answers[0][0]


@pytest.mark.asyncio
async def test_fjgset_shows_and_updates(g):
    m = _Msg(42)
    await F.fjg_set_cmd(m, _cmd(""))
    assert "30" in m.answers[0][0]
    m = _Msg(42)
    await F.fjg_set_cmd(m, _cmd("15 5"))
    assert g.store["fjg_min_count"] == "15" and g.store["fjg_gap_sec"] == "5" and "15" in m.answers[0][0]
    m = _Msg(42)
    await F.fjg_set_cmd(m, _cmd("x"))
    assert g.store["fjg_min_count"] == "15"
    m = _Msg(9)
    await F.fjg_set_cmd(m, _cmd("99"))
    assert g.store["fjg_min_count"] == "15" and m.answers == []


class _ChatBot:
    async def get_chat(self, target):
        if target == "@nope_channel":
            raise RuntimeError("no access")
        return SimpleNamespace(id=-1002222222222 if target in ("@mychannel", -1002222222222) else int(target), title="کانال من")


class _MsgR(_Msg):
    def __init__(self, uid, reply=None):
        super().__init__(uid)
        self.reply_to_message = reply


@pytest.mark.asyncio
async def test_channel_added_by_id_username_or_forwarded_post_and_tracked(g):
    g.store["group_guard_enabled"] = "off"                          # گروهِ گفتگو خاموش؛ فقط کانال
    assert await F._is_guarded_chat(-1002222222222) is False
    m = _MsgR(42)
    await F.fjg_chat_cmd(m, _ChatBot(), _cmd("-1002222222222"))
    assert g.store["fjg_chats"] == "-1002222222222" and "تحتِ نظر" in m.answers[0][0]
    assert await F._is_guarded_chat(-1002222222222) is True and await F._is_guarded_chat(-1009) is False
    # با فورواردِ پست + ریپلای
    fwd = SimpleNamespace(forward_origin=SimpleNamespace(chat=SimpleNamespace(id=-1003333333333)), forward_from_chat=None)
    m = _MsgR(42, reply=fwd)
    await F.fjg_chat_cmd(m, _ChatBot(), _cmd(""))
    assert g.store["fjg_chats"] == "-1002222222222,-1003333333333"
    # بدونِ دسترسی
    m = _MsgR(42)
    await F.fjg_chat_cmd(m, _ChatBot(), _cmd("@nope_channel"))
    assert "دسترسی ندارم" in m.answers[0][0] and g.store["fjg_chats"] == "-1002222222222,-1003333333333"
    # حذف
    m = _MsgR(42)
    await F.fjg_chat_cmd(m, _ChatBot(), _cmd("حذف -1003333333333"))
    assert g.store["fjg_chats"] == "-1002222222222"
    # غیرِ مدیر
    m = _MsgR(7)
    await F.fjg_chat_cmd(m, _ChatBot(), _cmd("-1004444444444"))
    assert m.answers == [] and g.store["fjg_chats"] == "-1002222222222"


@pytest.mark.asyncio
async def test_joins_to_registered_channel_trigger_alert_and_fakejoins(g):
    g.store["group_guard_enabled"] = "off"
    g.store["fjg_chats"] = "-1002222222222"
    ev = lambda i: SimpleNamespace(chat=SimpleNamespace(id=-1002222222222),
                                   old_chat_member=SimpleNamespace(status="left"),
                                   new_chat_member=SimpleNamespace(status="member",
                                                                   user=SimpleNamespace(id=i, is_bot=False, full_name=f"u{i}", username=None)))
    for i in range(30):
        await F.fjg_chat_member_join(ev(i), g.bot)
    assert len(g.sent) == 1 and "-1002222222222" in g.sent[0][1]
    m = _Msg(42)
    await F.fjg_recent_cmd(m, g.bot, _cmd("5"))
    assert any("-1002222222222" in a[0] and "30" in a[0] for a in m.answers)
