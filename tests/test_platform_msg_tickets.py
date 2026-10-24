"""سیستمِ تیکتِ روبیکا/سروش: منویِ دکمه‌ای، ساختِ تیکت، رشتهٔ گفتگو، بستن و نظرسنجی، پاسخِ مدیر."""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import platform_msg_tickets as T
import platform_rubika as R


class FakeChannel(T.Channel):
    key = "rubika"
    label = "روبیکا"

    def __init__(self):
        self.sent, self.acks, self.files = [], [], []

    async def send(self, chat_id, text, menu=None):
        self.sent.append((chat_id, text, menu))

    async def ack(self, chat_id, text):
        self.acks.append((chat_id, text))

    async def send_file(self, chat_id, data, name, ftype, caption=""):
        self.files.append((chat_id, name, ftype, caption))
        return True

    async def download(self, file_id):
        return b"\xff\xd8img"


class FakeBot:
    def __init__(self):
        self.msgs, self.photos = [], []

    async def send_message(self, chat, text, **kw):
        self.msgs.append((chat, text, kw))

    async def send_photo(self, chat, inp, caption=None, **kw):
        self.photos.append((chat, caption, kw))


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(T, "Session", maker)
    monkeypatch.setattr(R, "Session", maker)
    monkeypatch.setattr(T, "PLATFORM_ADMIN_ID", 42)
    ch, bot = FakeChannel(), FakeBot()

    async def get_channel(key):
        return ch

    monkeypatch.setattr(T, "get_channel", get_channel)
    yield SimpleNamespace(ch=ch, bot=bot, maker=maker)
    await engine.dispose()


def ev(text="", kind="text", **kw):
    base = {"chat_id": "c1", "user_id": "u1", "name": "علی رضایی", "username": "ali_r", "text": text,
            "kind": kind, "phone": "", "file_id": "", "file_name": "", "file_type": ""}
    base.update(kw)
    return base


async def say(env, text="", kind="text", **kw):
    return await T.handle_user(env.bot, env.ch, ev(text, kind, **kw))


async def new_ticket(env, body="پولم نیومد"):
    await say(env, T.BTN_NEW)
    await say(env, T.CAT_LABEL["pay"])
    return await say(env, body)


@pytest.mark.asyncio
async def test_start_shows_welcome_menu(env):
    await say(env, kind="start")
    chat, text, menu = env.ch.sent[-1]
    assert "پشتیبانی" in text and [T.BTN_NEW] in menu and [T.BTN_PHONE] in menu


@pytest.mark.asyncio
async def test_full_ticket_flow_creates_ticket_and_notifies_admin(env):
    await say(env, T.BTN_NEW)
    assert env.ch.sent[-1][2][-1] == [T.BTN_CANCEL] and len(env.ch.sent[-1][2]) == 4      # ۶ دسته در ۳ ردیف + انصراف
    await say(env, T.CAT_LABEL["pay"])
    assert "بنویس" in env.ch.sent[-1][1]
    assert await say(env, "پولم نیومد <b>") == 1
    async with env.maker() as s:
        t = await s.scalar(select(models.PlatformTicket))
    assert t.category == "pay" and t.status == "open" and t.name == "علی رضایی" and t.channel == "rubika"
    chat, card, kw = env.bot.msgs[-1]
    assert chat == 42 and f"BS-{t.id}" in card.split("\n")[0] and "&lt;b&gt;" in card and "@ali_r" in card
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == f"mtk:close:{t.id}"
    assert f"BS-{t.id}" in env.ch.sent[-1][1]                       # رسیدِ دریافت برایِ کاربر
    assert [T.BTN_CLOSE] in env.ch.sent[-1][2]                      # حالا دکمهٔ بستن در منو هست


@pytest.mark.asyncio
async def test_followup_is_appended_to_open_ticket(env):
    await new_ticket(env)
    n = len(env.bot.msgs)
    assert await say(env, "ضمناً کد رهگیری ۱۲۳") == 1
    assert len(env.bot.msgs) == n + 1 and "پیام جدید در تیکت" in env.bot.msgs[-1][1]
    assert env.ch.acks and "اضافه شد" in env.ch.acks[-1][1]
    async with env.maker() as s:
        assert len((await s.scalars(select(models.PlatformTicket))).all()) == 1
        assert len((await s.scalars(select(models.PlatformTicketMsg))).all()) == 2


@pytest.mark.asyncio
async def test_message_without_ticket_hints_menu(env):
    assert await say(env, "سلام") == 0
    assert "ثبت تیکت" in env.ch.sent[-1][1] and not env.bot.msgs


@pytest.mark.asyncio
async def test_open_ticket_cap(env):
    for _ in range(T.MAX_OPEN):
        await new_ticket(env)
    await say(env, T.BTN_NEW)
    assert "تیکتِ باز" in env.ch.sent[-1][1]
    async with env.maker() as s:
        assert len((await s.scalars(select(models.PlatformTicket))).all()) == T.MAX_OPEN


@pytest.mark.asyncio
async def test_cancel_during_write(env):
    await say(env, T.BTN_NEW)
    await say(env, T.CAT_LABEL["bug"])
    await say(env, T.BTN_CANCEL)
    assert await say(env, "متنِ بعد از انصراف") == 0
    async with env.maker() as s:
        assert not (await s.scalars(select(models.PlatformTicket))).all()


@pytest.mark.asyncio
async def test_shared_phone_is_saved_and_stamped_on_new_ticket(env):
    await say(env, kind="contact", phone="98912")
    assert env.ch.acks and "ثبت شد" in env.ch.acks[-1][1]
    assert [T.BTN_PHONE] not in env.ch.sent[-1][2]                  # دکمهٔ شماره دیگر لازم نیست
    await new_ticket(env)
    assert "98912" in env.bot.msgs[-1][1]


@pytest.mark.asyncio
async def test_user_close_notifies_admin_without_rating(env):
    await new_ticket(env)
    assert await say(env, T.BTN_CLOSE) == 1
    assert "کاربر تیکت را بست" in env.bot.msgs[-1][1]
    async with env.maker() as s:
        assert (await s.scalar(select(models.PlatformTicket))).status == "closed"
    assert await say(env, "پیام بعد از بستن") == 0                  # دیگر به تیکتِ بسته اضافه نمی‌شود


@pytest.mark.asyncio
async def test_admin_close_asks_rating_and_stores_it(env):
    await new_ticket(env)
    await T.close_ticket(env.bot, 1, by="admin")
    chat, text, menu = env.ch.sent[-1]
    assert "راضی" in text and list(T.RATES) in menu
    assert await say(env, "😍 عالی") == 1
    async with env.maker() as s:
        assert (await s.scalar(select(models.PlatformTicket))).rating == 3
    assert "رضایتِ کاربر" in env.bot.msgs[-1][1]


@pytest.mark.asyncio
async def test_file_ticket_goes_to_admin_as_photo(env):
    await say(env, T.BTN_NEW)
    await say(env, T.CAT_LABEL["pay"])
    assert await say(env, "رسید", kind="file", file_id="F1", file_name="r.jpg", file_type="photo") == 1
    assert env.bot.photos and env.bot.photos[0][0] == 42 and "BS-1" in env.bot.photos[0][1]


def test_ticket_id_read_from_first_line_only():
    assert T.ticket_id_from("🎫 تیکت جدید BS-12\n...\nکاربر نوشت BS-99") == 12
    assert T.ticket_id_from("متنِ بی‌ربط") is None


@pytest.mark.asyncio
async def test_admin_text_reply_goes_to_user_and_marks_answered(env):
    await new_ticket(env)
    replies = []

    async def reply(text, **kw):
        replies.append(text)

    msg = SimpleNamespace(text="درست شد", from_user=SimpleNamespace(id=42), reply=reply,
                          reply_to_message=SimpleNamespace(text="🎫 تیکت جدید BS-1\nx", caption=None))
    assert T._is_ticket_text_reply(msg)
    await T.tk_reply(msg)
    chat, text, menu = env.ch.sent[-1]
    assert chat == "c1" and "BS-1" in text and "درست شد" in text and menu is None
    async with env.maker() as s:
        assert (await s.scalar(select(models.PlatformTicket))).status == "answered"
    assert replies and "فرستاده شد" in replies[0]
    # پیامِ بعدیِ کاربر تیکت را دوباره «باز» می‌کند
    await say(env, "ممنون ولی هنوز مشکل دارم")
    async with env.maker() as s:
        assert (await s.scalar(select(models.PlatformTicket))).status == "open"


@pytest.mark.asyncio
async def test_non_admin_reply_is_ignored(env):
    msg = SimpleNamespace(text="x", from_user=SimpleNamespace(id=7),
                          reply_to_message=SimpleNamespace(text="BS-1", caption=None))
    assert not T._is_ticket_text_reply(msg)


@pytest.mark.asyncio
async def test_tickets_command_lists_open_and_stats(env):
    await new_ticket(env)
    out = []

    async def answer(text, **kw):
        out.append(text)

    await T.tk_list(SimpleNamespace(answer=answer))
    assert "BS-1" in out[0] and "روبیکا" in out[0] and "کل تیکت‌ها: 1" in out[0]


@pytest.mark.asyncio
async def test_my_tickets_lists_status(env):
    await new_ticket(env)
    await say(env, T.BTN_MY)
    assert "BS-1" in env.ch.sent[-1][1] and "باز" in env.ch.sent[-1][1]


async def _add_faq(env, q="چطور شارژ کنم؟", a="از منوی کیف پول شارژ بزن."):
    await T._save_faq((await T._faq()) + [{"q": q, "a": a}])


@pytest.mark.asyncio
async def test_faq_button_appears_only_when_faq_exists(env):
    await say(env, kind="start")
    assert [T.BTN_FAQ] not in env.ch.sent[-1][2]
    await _add_faq(env)
    await say(env, kind="start")
    assert [T.BTN_FAQ] in env.ch.sent[-1][2]


@pytest.mark.asyncio
async def test_faq_flow_answers_without_ticket(env):
    await _add_faq(env)
    await say(env, T.BTN_FAQ)
    assert ["چطور شارژ کنم؟"] in env.ch.sent[-1][2]
    assert await say(env, "چطور شارژ کنم؟") == 0
    assert "کیف پول" in env.ch.sent[-1][1] and T.BTN_NEW in env.ch.sent[-1][1]
    async with env.maker() as s:
        assert not (await s.scalars(select(models.PlatformTicket))).all()


@pytest.mark.asyncio
async def test_faq_state_falls_through_to_normal_flow(env):
    await _add_faq(env)
    await say(env, T.BTN_FAQ)
    await say(env, T.BTN_NEW)                       # از وسطِ FAQ مستقیم تیکت می‌سازد
    assert "موضوعِ تیکت" in env.ch.sent[-1][1]


@pytest.mark.asyncio
async def test_custom_welcome_text(env):
    await T._set(T.K_WELCOME, "خوش اومدی به فروشگاه ما")
    await say(env, kind="start")
    assert env.ch.sent[-1][1] == "خوش اومدی به فروشگاه ما"


@pytest.mark.asyncio
async def test_settings_panel_add_and_delete_faq(env):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.fsm.storage.base import StorageKey
    out = []

    async def answer(text, **kw):
        out.append(text)

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    adm = SimpleNamespace(id=42)
    await T.tks_cmd(SimpleNamespace(from_user=adm, answer=answer), state)
    assert "تنظیماتِ تیکت" in out[-1]
    await state.set_state(T.TkSetFSM.faq_q)
    await T.tks_faq_q(SimpleNamespace(text="ساعت کاری؟", from_user=adm, answer=answer), state)
    assert await state.get_state() == T.TkSetFSM.faq_a.state
    await T.tks_faq_a(SimpleNamespace(text="۹ تا ۱۸", from_user=adm, answer=answer), state)
    assert [x["q"] for x in await T._faq()] == ["ساعت کاری؟"]

    async def cb_answer(*a, **k):
        pass

    async def edit_text(text, **kw):
        out.append(text)

    cb = SimpleNamespace(data="mtks:del:0", from_user=adm, answer=cb_answer,
                         message=SimpleNamespace(edit_text=edit_text, answer=answer))
    await T.tks_cb(cb, state)
    assert await T._faq() == []


@pytest.mark.asyncio
async def test_faq_question_cannot_shadow_menu_buttons(env):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.fsm.storage.base import StorageKey
    out = []

    async def answer(text, **kw):
        out.append(text)

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    await state.set_state(T.TkSetFSM.faq_q)
    await T.tks_faq_q(SimpleNamespace(text=T.BTN_NEW, from_user=SimpleNamespace(id=42), answer=answer), state)
    assert "یکتا" in out[-1] and await state.get_state() == T.TkSetFSM.faq_q.state


from datetime import datetime, timedelta

DAY = datetime(2026, 10, 22, 8, 30)       # ۱۲:۰۰ به وقتِ تهران
NIGHT = datetime(2026, 10, 21, 22, 30)    # ۲:۰۰ بامداد تهران


async def _age_ticket(env, hours, tid=1, status=None):
    async with env.maker() as s:
        t = await s.get(models.PlatformTicket, tid)
        t.updated_at = DAY - timedelta(hours=hours)
        if status:
            t.status = status
        await s.commit()


@pytest.mark.asyncio
async def test_reminder_digest_for_stale_open_tickets(env):
    await new_ticket(env)
    await _age_ticket(env, 5)
    n = len(env.bot.msgs)
    assert await T.remind_once(env.bot, DAY) == 1
    text = env.bot.msgs[n][1]
    assert env.bot.msgs[n][0] == 42 and "BS-1" in text and "بی‌پاسخ" in text and "3 ساعت" in text
    assert await T.remind_once(env.bot, DAY + timedelta(hours=1)) == 0     # در همان بازه تکرار نمی‌شود
    assert await T.remind_once(env.bot, DAY + timedelta(hours=4)) == 1     # بعد از N ساعت دوباره


@pytest.mark.asyncio
async def test_reminder_skips_fresh_answered_closed_and_quiet_hours(env):
    await new_ticket(env)
    await _age_ticket(env, 1)                                   # تازه
    assert await T.remind_once(env.bot, DAY) == 0
    await _age_ticket(env, 9, status="answered")                # جواب داده‌شده
    assert await T.remind_once(env.bot, DAY) == 0
    await _age_ticket(env, 9, status="closed")                  # بسته
    assert await T.remind_once(env.bot, DAY) == 0
    await _age_ticket(env, 9, status="open")
    assert await T.remind_once(env.bot, NIGHT) == 0             # ساعتِ خواب
    assert await T.remind_once(env.bot, DAY) == 1


@pytest.mark.asyncio
async def test_reminder_can_be_turned_off_and_cycled(env):
    await new_ticket(env)
    await _age_ticket(env, 9)
    await T._set(T.K_REMIND, "0")
    assert await T.remind_once(env.bot, DAY) == 0
    out = []

    async def cb_answer(*a, **k):
        pass

    async def edit_text(text, **kw):
        out.append(text)

    cb = SimpleNamespace(data="mtks:remind", from_user=SimpleNamespace(id=42), answer=cb_answer,
                         message=SimpleNamespace(edit_text=edit_text, answer=edit_text))
    await T.tks_cb(cb, SimpleNamespace())
    assert await T._remind_hours() == 1 and "هر 1 ساعت" in out[-1]
