"""PATCH_CHPOST_USERBOT_V1 — ارسالِ پست با اکانتِ پرمیومِ مدیر (Telethon) + چسباندنِ دکمه با ربات."""
import sys
import types
from types import SimpleNamespace as NS

import pytest

import chpost_userbot as UB
import platform_chpost as PC

ADMIN = 424242


@pytest.fixture(autouse=True)
def _telethon_importable(monkeypatch):
    """telethon به pyaes نیاز دارد؛ اگر در محیطِ تست ساخته نشد، یک stub می‌گذاریم (فقط نوع‌ها لازم‌اند). نصب نبود ⇒ skip."""
    try:
        import pyaes  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "pyaes", types.ModuleType("pyaes"))
    pytest.importorskip("telethon")


def test_entities_mapping_keeps_custom_emoji_and_formats():
    from telethon.tl import types as T
    raw = [{"type": "bold", "offset": 0, "length": 5},
           {"type": "custom_emoji", "offset": 6, "length": 2, "custom_emoji_id": "5368324170671202286"},
           {"type": "text_link", "offset": 9, "length": 3, "url": "https://x.y"},
           {"type": "expandable_blockquote", "offset": 13, "length": 4},
           {"type": "pre", "offset": 18, "length": 2, "language": "py"},
           {"type": "weird", "offset": 0, "length": 1}, {"type": "bold"}]       # ناشناخته/ناقص ⇒ رد می‌شود
    ents = UB.entities_to_telethon(raw)
    assert [type(e) for e in ents] == [T.MessageEntityBold, T.MessageEntityCustomEmoji, T.MessageEntityTextUrl,
                                       T.MessageEntityBlockquote, T.MessageEntityPre]
    assert ents[1].document_id == 5368324170671202286 and ents[1].offset == 6 and ents[3].collapsed is True
    assert ents[4].language == "py"


def test_serialize_aiogram_entities_roundtrip():
    from aiogram.types import MessageEntity
    ents = [MessageEntity(type="bold", offset=0, length=3),
            MessageEntity(type="custom_emoji", offset=4, length=2, custom_emoji_id="999"),
            MessageEntity(type="text_link", offset=7, length=2, url="https://a.b")]
    ser = UB.serialize_entities(ents)
    assert ser[1] == {"type": "custom_emoji", "offset": 4, "length": 2, "custom_emoji_id": "999"}
    assert ser[2]["url"] == "https://a.b" and UB.serialize_entities(None) == []
    assert len(UB.entities_to_telethon(ser)) == 3


class FakeClient:
    authorized = True
    resolve_fail_first = False
    resolve_fail_always = False
    last = None

    def __init__(self):
        self.calls, self.resolved = [], 0

    async def connect(self):
        self.calls.append("connect")

    async def disconnect(self):
        self.calls.append("disconnect")

    async def is_user_authorized(self):
        return FakeClient.authorized

    async def get_input_entity(self, cid):
        self.resolved += 1
        if FakeClient.resolve_fail_always or (FakeClient.resolve_fail_first and self.resolved == 1):
            raise ValueError("no entity")
        return ("peer", cid)

    async def get_dialogs(self):
        self.calls.append("dialogs")

    async def send_message(self, target, text, **kw):
        FakeClient.last = ("msg", target, text, kw)
        return NS(id=777)

    async def send_file(self, target, path, **kw):
        FakeClient.last = ("file", target, path, kw)
        return NS(id=778)

    async def get_me(self):
        return NS(first_name="Ali", last_name="", username="ali", premium=True)


@pytest.fixture
def fake(monkeypatch):
    FakeClient.authorized, FakeClient.resolve_fail_first, FakeClient.resolve_fail_always = True, False, False
    c = FakeClient()
    monkeypatch.setattr(UB, "_client", lambda *a: c)
    return c


@pytest.mark.asyncio
async def test_send_post_text_and_photo(fake):
    mid = await UB.send_post(1, "h", "s", "-1001", "سلام 🙂", [{"type": "custom_emoji", "offset": 5, "length": 2, "custom_emoji_id": "42"}])
    kind, target, text, kw = FakeClient.last
    assert mid == 777 and kind == "msg" and target == ("peer", -1001) and text == "سلام 🙂"
    assert kw["formatting_entities"][0].document_id == 42 and kw["link_preview"] is False
    assert fake.calls[-1] == "disconnect"
    mid = await UB.send_post(1, "h", "s", "-1001", "cap", [], photo_path="/tmp/p.jpg")
    assert mid == 778 and FakeClient.last[0] == "file" and FakeClient.last[3]["caption"] == "cap"


@pytest.mark.asyncio
async def test_send_post_resolves_via_dialogs_and_reports_errors(fake):
    FakeClient.resolve_fail_first = True
    assert await UB.send_post(1, "h", "s", "-1001", "x", []) == 777 and "dialogs" in fake.calls    # اولین بار کش خالی ⇒ دیالوگ‌ها پر می‌شود
    FakeClient.resolve_fail_always = True
    with pytest.raises(UB.UserbotError) as e:
        await UB.send_post(1, "h", "s", "-1001", "x", [])
    assert "دسترسی ندارد" in str(e.value)
    FakeClient.resolve_fail_always, FakeClient.authorized = False, False
    with pytest.raises(UB.UserbotError) as e2:
        await UB.send_post(1, "h", "s", "-1001", "x", [])
    assert "منقضی" in str(e2.value) or "نامعتبر" in str(e2.value)


@pytest.mark.asyncio
async def test_validate_session_reports_name_and_premium(fake):
    assert "Ali (@ali)" in await UB.validate_session(1, "h", "s") and "پرمیوم نیست" not in await UB.validate_session(1, "h", "s")
    FakeClient.authorized = False
    with pytest.raises(UB.UserbotError):
        await UB.validate_session(1, "h", "s")


# ───────── جریانِ ربات ─────────
class Bot:
    def __init__(self, attach_fail=False):
        self.edits, self.attach_fail = [], attach_fail

    async def edit_message_reply_markup(self, **kw):
        if self.attach_fail:
            raise RuntimeError("not enough rights")
        self.edits.append(kw)

    async def download(self, file_id, destination=None):
        open(destination, "wb").write(b"jpg")


class Msg:
    def __init__(self):
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


class Cb:
    def __init__(self, uid=ADMIN):
        self.from_user, self.message, self.alerts = NS(id=uid), Msg(), []

    async def answer(self, t=None, show_alert=False, **k):
        self.alerts.append(t)


class St:
    def __init__(self, d=None):
        self.d = dict(d or {})

    async def get_data(self):
        return dict(self.d)

    async def clear(self):
        self.d = {}


async def _prep(monkeypatch, sent):
    monkeypatch.setattr(PC, "PLATFORM_ADMIN_ID", ADMIN)

    async def creds():
        return (1, "h", "s")

    async def gp(k, d=""):
        return {"chpost_chat_id": "-100", "chpost_group_id": "-200"}.get(k, d)

    async def home(uid, show_admin=False):
        return None

    async def menu():
        return "m", None

    async def hb(bot, keys, for_channel=True):
        return []

    async def send_post(api_id, api_hash, session, chat, text, ents, photo_path=None):
        sent.append((chat, text, ents, photo_path))
        return 900 + len(sent)
    monkeypatch.setattr(PC, "_ua_creds", creds)
    monkeypatch.setattr(PC, "get_pset", gp)
    monkeypatch.setattr(PC, "smart_home_kb", home)
    monkeypatch.setattr(PC, "_chpost_menu", menu)
    monkeypatch.setattr(PC, "_chpost_home_link_buttons", hb)
    monkeypatch.setattr(UB, "send_post", send_post)


DATA = {"chpost_plain": "سلام 🙂", "chpost_text": "<b>x</b>", "chpost_entities": [{"type": "custom_emoji", "offset": 5, "length": 2, "custom_emoji_id": "42"}],
        "chpost_buttons": [["دکمه", "https://example.com"]]}


@pytest.mark.asyncio
async def test_ua_send_posts_to_channel_and_group_then_bot_attaches_buttons(monkeypatch):
    sent = []
    await _prep(monkeypatch, sent)
    b, cb = Bot(), Cb()
    await PC.chpost_ua_send(cb, b, St(DATA))
    assert [s[0] for s in sent] == ["-100", "-200"] and sent[0][1] == "سلام 🙂" and sent[0][2][0]["custom_emoji_id"] == "42"
    assert [(e["chat_id"], e["message_id"]) for e in b.edits] == [("-100", 901), ("-200", 902)]
    assert b.edits[0]["reply_markup"].inline_keyboard[0][0].url == "https://example.com"
    assert any("ارسال شد با اکانتِ تو" in a for a in cb.message.answers)


@pytest.mark.asyncio
async def test_ua_send_button_attach_failure_is_reported_not_fatal(monkeypatch):
    sent = []
    await _prep(monkeypatch, sent)
    cb = Cb()
    await PC.chpost_ua_send(cb, Bot(attach_fail=True), St(DATA))
    assert len(sent) == 2 and any("دکمه‌ها نچسبید" in a for a in cb.message.answers)


@pytest.mark.asyncio
async def test_ua_send_photo_downloads_and_cleans_temp_file(monkeypatch):
    import os
    sent = []
    await _prep(monkeypatch, sent)
    await PC.chpost_ua_send(Cb(), Bot(), St({**DATA, "chpost_photo": "FILEID", "chpost_buttons": []}))
    path = sent[0][3]
    assert path and path.endswith(".jpg") and not os.path.exists(path)                     # فایلِ موقت پاک شد


@pytest.mark.asyncio
async def test_ua_send_guards(monkeypatch):
    sent = []
    await _prep(monkeypatch, sent)
    cb = Cb(uid=1)
    await PC.chpost_ua_send(cb, Bot(), St(DATA))
    assert cb.alerts and "مدیر اصلی" in cb.alerts[0] and sent == []
    cb2 = Cb()
    await PC.chpost_ua_send(cb2, Bot(), St({"chpost_text": "old"}))                       # بدونِ متنِ اصلی/entity (پستِ قدیمی)
    assert cb2.alerts and "دوباره بفرست" in cb2.alerts[0] and sent == []
    cb3 = Cb()
    await PC.chpost_ua_send(cb3, Bot(), St({"chpost_plain": "  "}))
    assert cb3.alerts and "خالی" in cb3.alerts[0]
    async def nocreds():
        return None
    monkeypatch.setattr(PC, "_ua_creds", nocreds)
    cb4 = Cb()
    await PC.chpost_ua_send(cb4, Bot(), St(DATA))
    assert cb4.alerts and "اکانت را وصل کن" in cb4.alerts[0]


@pytest.mark.asyncio
async def test_ua_send_reports_userbot_error_per_target(monkeypatch):
    sent = []
    await _prep(monkeypatch, sent)

    async def boom(*a, **k):
        raise UB.UserbotError("اکانت به این کانال دسترسی ندارد")
    monkeypatch.setattr(UB, "send_post", boom)
    cb = Cb()
    await PC.chpost_ua_send(cb, Bot(), St(DATA))
    assert "❌ کانال: اکانت به این کانال دسترسی ندارد" in cb.message.answers[0]


@pytest.mark.asyncio
async def test_connect_flow_stores_encrypted_secrets_and_deletes_messages(monkeypatch):
    monkeypatch.setattr(PC, "PLATFORM_ADMIN_ID", ADMIN)
    store, deleted = {}, []

    async def sp(k, v):
        store[k] = v

    async def home(uid, show_admin=False):
        return None

    async def menu():
        return "m", None

    async def val(api_id, api_hash, session):
        assert (api_id, api_hash, session) == (12345, "H" * 32, "SESSION")
        return "Ali (@ali)"
    monkeypatch.setattr(PC, "set_pset", sp)
    monkeypatch.setattr(PC, "smart_home_kb", home)
    monkeypatch.setattr(PC, "_chpost_menu", menu)
    monkeypatch.setattr(UB, "validate_session", val)
    import wallet_crypto
    monkeypatch.setattr(wallet_crypto, "encrypt_secret", lambda x: "ENC(" + x + ")")

    class State(St):
        async def update_data(self, **kw):
            self.d.update(kw)

        async def set_state(self, s):
            self.s = s

    class M(Msg):
        def __init__(self, text):
            super().__init__()
            self.text, self.from_user = text, NS(id=ADMIN)

        async def delete(self):
            deleted.append(self.text)
    st = State()
    await PC.chpost_ua_api_id(M("12345"), st)
    await PC.chpost_ua_api_hash(M("H" * 32), st)
    await PC.chpost_ua_session(M("SESSION"), st)
    assert store == {"chpost_ua_api_id": "12345", "chpost_ua_api_hash": "ENC(" + "H" * 32 + ")", "chpost_ua_session": "ENC(SESSION)"}
    assert "H" * 32 in deleted and "SESSION" in deleted                                    # پیام‌هایِ حساس پاک شدند
    assert st.d == {}


def test_account_never_leaks_in_menu_text():
    import inspect
    src = inspect.getsource(PC.chpost_ua_menu)
    assert "decrypt_secret" not in src and "chpost_ua_session\", \"\"))" not in src.replace(" ", "")      # فقط «متصل/نیست»؛ هرگز مقدارِ نشست
    assert "_ua_creds()" in src and "creds else" in src
