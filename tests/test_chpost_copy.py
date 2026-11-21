"""PATCH_CHPOST_COPY_V1 — ارسالِ پست با کپیِ پیامِ اصلیِ ادمین (copyMessage) تا ایموجیِ پرمیوم سالم بماند؛ در شکست به مسیرِ قدیم."""
from types import SimpleNamespace as NS

import pytest

import chpost_inline_core as CI
import platform_chpost as PC

ADMIN = 424242


class Bot:
    def __init__(self, copy_fail=False):
        self.copies, self.sent, self.photos, self.copy_fail = [], [], [], copy_fail

    async def copy_message(self, **kw):
        if self.copy_fail:
            raise RuntimeError("nope")
        self.copies.append(kw)

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text, kw))

    async def send_photo(self, chat, photo, **kw):
        self.photos.append((chat, photo, kw))


@pytest.mark.asyncio
async def test_copy_post_core():
    b = Bot()
    assert await CI.copy_post(b, -100, {}) is False and b.copies == []                       # منبع ندارد ⇒ مسیرِ قدیم
    ok = await CI.copy_post(b, -100, {"chpost_src_chat": 7, "chpost_src_msg": "55"}, reply_markup="KB")
    assert ok and b.copies == [dict(chat_id=-100, from_chat_id=7, message_id=55, reply_markup="KB")]
    assert await CI.copy_post(Bot(copy_fail=True), -100, {"chpost_src_chat": 7, "chpost_src_msg": 55}) is False


class Msg:
    def __init__(self, text=None, photo=None):
        self.text, self.photo, self.caption = text, photo, None
        self.html_text = f"<b>{text}</b>"
        self.checklist = None
        self.entities = self.caption_entities = None
        self.chat, self.message_id, self.from_user = NS(id=ADMIN), 321, NS(id=ADMIN)
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


class St:
    def __init__(self, data=None):
        self.d = dict(data or {})

    async def get_data(self):
        return dict(self.d)

    async def update_data(self, **kw):
        self.d.update(kw)

    async def clear(self):
        self.d = {}


@pytest.mark.asyncio
async def test_platform_content_recv_remembers_source_message(monkeypatch):
    monkeypatch.setattr(PC, "PLATFORM_ADMIN_ID", ADMIN)

    async def home(uid, show_admin=False):
        return None

    async def prev(*a, **k):
        return None
    monkeypatch.setattr(PC, "smart_home_kb", home)
    monkeypatch.setattr(PC, "_chpost_preview", prev)
    st = St()
    await PC.chpost_content_recv(Msg(text="سلام"), st, Bot())
    assert st.d["chpost_src_chat"] == ADMIN and st.d["chpost_src_msg"] == 321 and st.d["chpost_text"] == "<b>سلام</b>"
    assert st.d["chpost_plain"] == "سلام" and st.d["chpost_entities"] == []                   # PATCH_CHPOST_USERBOT_V1
    st2 = St()
    m = Msg()
    m.text, m.photo = None, [NS(file_id="F1")]
    await PC.chpost_content_recv(m, st2, Bot())
    assert st2.d["chpost_photo"] == "F1" and st2.d["chpost_src_msg"] == 321


class Cb:
    def __init__(self):
        self.from_user, self.data, self.message = NS(id=ADMIN), "chpost:send", Msg()
        self.alerts = []

    async def answer(self, t=None, show_alert=False, **k):
        self.alerts.append(t)


async def _prep(monkeypatch):
    monkeypatch.setattr(PC, "PLATFORM_ADMIN_ID", ADMIN)

    async def gp(k, d=""):
        return {"chpost_chat_id": "-100"}.get(k, d)

    async def home(uid, show_admin=False):
        return None

    async def menu():
        return "m", None

    async def hb(bot, keys, for_channel=True):
        return []
    monkeypatch.setattr(PC, "get_pset", gp)
    monkeypatch.setattr(PC, "smart_home_kb", home)
    monkeypatch.setattr(PC, "_chpost_menu", menu)
    monkeypatch.setattr(PC, "_chpost_home_link_buttons", hb)


@pytest.mark.asyncio
async def test_platform_send_prefers_copy_and_falls_back(monkeypatch):
    await _prep(monkeypatch)
    data = {"chpost_text": "<b>x</b>", "chpost_src_chat": ADMIN, "chpost_src_msg": 321,
            "chpost_buttons": [["دکمه", "https://example.com"]]}
    b = Bot()
    await PC.chpost_send(Cb(), b, St(data))
    assert len(b.copies) == 1 and b.copies[0]["chat_id"] == "-100" and b.copies[0]["message_id"] == 321
    assert b.copies[0]["reply_markup"] is not None and b.sent == []                          # ساختِ دوباره ندارد
    # copy شکست خورد ⇒ مسیرِ قدیم (متنِ ساخته‌شده) و خطا برای ادمین نمی‌آید
    b2 = Bot(copy_fail=True)
    cb = Cb()
    await PC.chpost_send(cb, b2, St(data))
    assert b2.sent and b2.sent[0][0] == "-100" and any("ارسال شد" in a for a in cb.message.answers)
    # بدونِ منبع (مثلاً چک‌لیست) ⇒ مسیرِ قدیم
    b3 = Bot()
    await PC.chpost_send(Cb(), b3, St({"chpost_text": "plain"}))
    assert b3.copies == [] and b3.sent


def test_tenant_flow_uses_copy_helper():
    import inspect
    import tenant
    src = inspect.getsource(tenant.tadm_chpost_send) + inspect.getsource(tenant.tadm_chpost_content_recv) + inspect.getsource(tenant._chpost_preview)
    assert src.count("PATCH_CHPOST_COPY_V1") >= 3 and "copy_post" in src
