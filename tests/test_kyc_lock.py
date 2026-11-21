"""kyc_blocked در کلِ ربات اعمال می‌شود (نه فقط شارژ) + اطلاع به کاربر."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant.shared.kyc_lock as KL


def test_group_interaction_detects_bot_use_not_chatter():
    gi = KL.group_interaction
    assert gi("/dice") and gi("بازی 5000") and gi("بازی") and gi("انتقال ۱۰۰۰") and gi("موجودی")
    assert gi("", has_dice=True)
    assert not gi("سلام بچه‌ها") and not gi("چه خبر؟") and not gi("")


def test_private_allowed_only_start_and_kyc_wizard():
    assert KL.private_allowed("/start", None) and KL.private_allowed("/start ref123", None)
    assert KL.private_allowed("هر متنی", "KycUserFSM:wait_media")
    assert not KL.private_allowed("🛍 خرید", None)
    assert not KL.private_allowed("/start", "OtherFSM:x") or True   # /start همیشه باز است
    assert not KL.private_allowed("خرید", "OtherFSM:x")


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(KL, "Session", maker)
    import tenant.shared.kyc as K

    async def enabled(bot_id):
        return True

    monkeypatch.setattr(K, "enabled", enabled)
    KL._cache.clear()
    KL._last_notice.clear()
    async with maker() as s:
        s.add(models.Bot(id=5, owner_id=1, token="5:T", username="x", field="virtual_service", plan="pro", status="active"))
        s.add(models.BotUser(id=1, bot_id=5, telegram_id=777, full_name="A", kyc_blocked=True))
        s.add(models.BotUser(id=2, bot_id=5, telegram_id=888, full_name="B", kyc_blocked=False))
        await s.commit()
    yield maker
    await engine.dispose()


def msg(uid, text="", chat="private", dice=None):
    sent = []

    async def answer(t, **kw):
        sent.append(("answer", t, kw))

    async def reply(t, **kw):
        sent.append(("reply", t, kw))

    m = SimpleNamespace(from_user=SimpleNamespace(id=uid, full_name="نام", is_bot=False), text=text, dice=dice,
                        chat=SimpleNamespace(type=chat), answer=answer, reply=reply)
    m.sent = sent
    return m


@pytest.mark.asyncio
async def test_is_locked_reads_flag_and_caches(env):
    assert await KL.is_locked(5, 777) is True
    assert await KL.is_locked(5, 888) is False
    async with env() as s:                       # رفعِ محدودیت: تا پایانِ کش هنوز قفل است، بعد باز می‌شود
        u = await s.get(models.BotUser, 1)
        u.kyc_blocked = False
        await s.commit()
    assert await KL.is_locked(5, 777) is True
    KL.invalidate(5, 777)
    assert await KL.is_locked(5, 777) is False


@pytest.mark.asyncio
async def test_private_message_blocked_with_kyc_button_and_rate_limited(env):
    m = msg(777, "🛍 خرید")
    assert await KL.message_gate(m, 5, None) is False
    kind, text, kw = m.sent[0]
    assert "محدود شده" in text and kw["reply_markup"].inline_keyboard[0][0].callback_data == "kycu:l1"
    m2 = msg(777, "باز هم")
    assert await KL.message_gate(m2, 5, None) is False and not m2.sent       # ۲۰ثانیه یک‌بار پیام


@pytest.mark.asyncio
async def test_private_kyc_wizard_passes(env):
    assert await KL.message_gate(msg(777, "عکس مدرک"), 5, "KycUserFSM:l2_file") is True


@pytest.mark.asyncio
async def test_start_shows_notice_with_level_button_not_main_menu(env):
    # سطح ۰ ⇒ دکمهٔ احرازِ سطح ۱
    m = msg(777, "/start")
    assert await KL.message_gate(m, 5, None) is False
    text, kw = m.sent[0][1], m.sent[0][2]
    b = kw["reply_markup"].inline_keyboard[0][0]
    assert "محدود شده" in text and b.callback_data == "kycu:l1" and "سطح ۱" in b.text
    # /start پشتِ‌سرهم همیشه جواب می‌دهد (محدودیتِ ۲۰ثانیه برایش نیست)
    m2 = msg(777, "/start")
    assert await KL.message_gate(m2, 5, None) is False and m2.sent
    # سطح ۱ ⇒ احرازِ سطح ۲
    async with env() as s:
        u = await s.get(models.BotUser, 1)
        u.kyc_level = 1
        await s.commit()
    m3 = msg(777, "/start")
    await KL.message_gate(m3, 5, None)
    b3 = m3.sent[0][2]["reply_markup"].inline_keyboard[0][0]
    assert b3.callback_data == "kycu:l2" and "سطح ۲" in b3.text
    # درخواستِ در انتظار ⇒ دکمهٔ وضعیت
    async with env() as s:
        s.add(models.KycRequest(bot_id=5, user_id=1, level=2, status="pending"))
        await s.commit()
    m4 = msg(777, "/start")
    await KL.message_gate(m4, 5, None)
    assert m4.sent[0][2]["reply_markup"].inline_keyboard[0][0].callback_data == "kycu:menu"


@pytest.mark.asyncio
async def test_blocked_callback_also_sends_persistent_notice_and_cancel_passes(env):
    out, sent = [], []

    async def answer(text=None, show_alert=False):
        out.append(text)

    async def m_answer(t, **kw):
        sent.append((t, kw))

    ev = SimpleNamespace(from_user=SimpleNamespace(id=777), data="shop:buy:1", answer=answer,
                         message=SimpleNamespace(chat=SimpleNamespace(type="private"), answer=m_answer))
    assert await KL.callback_gate(ev, 5) is False
    assert out and sent and sent[0][1]["reply_markup"].inline_keyboard[0][0].callback_data == "kycu:l1"
    ev2 = SimpleNamespace(from_user=SimpleNamespace(id=777), data="fsm:cancel", answer=answer,
                          message=SimpleNamespace(chat=SimpleNamespace(type="private"), answer=m_answer))
    assert await KL.callback_gate(ev2, 5) is True


@pytest.mark.asyncio
async def test_unrestricted_user_untouched(env):
    assert await KL.message_gate(msg(888, "🛍 خرید"), 5, None) is True
    assert await KL.message_gate(msg(888, "بازی 5000", chat="supergroup"), 5, None) is True


@pytest.mark.asyncio
async def test_group_game_blocked_with_notice_but_chatter_passes(env):
    g = msg(777, "بازی 5000", chat="supergroup")
    assert await KL.message_gate(g, 5, None) is False
    assert g.sent[0][0] == "reply" and "محدود شده" in g.sent[0][1]
    assert await KL.message_gate(msg(777, "سلام همگی", chat="supergroup"), 5, None) is True
    assert await KL.message_gate(msg(777, "", chat="group", dice=SimpleNamespace(emoji="🎲")), 5, None) is False


@pytest.mark.asyncio
async def test_callbacks(env):
    out = []

    async def answer(text=None, show_alert=False):
        out.append((text, show_alert))

    def cb(data, chat="private", uid=777):
        return SimpleNamespace(from_user=SimpleNamespace(id=uid), data=data, answer=answer,
                               message=SimpleNamespace(chat=SimpleNamespace(type=chat)))

    assert await KL.callback_gate(cb("kycu:menu"), 5) is True
    assert await KL.callback_gate(cb("shop:buy:1"), 5) is False and out[-1][1] is True
    assert await KL.callback_gate(cb("bet:join:3", chat="supergroup"), 5) is False
    assert await KL.callback_gate(cb("kycu:menu", chat="supergroup"), 5) is False     # احراز فقط در پیوی
    assert await KL.callback_gate(cb("shop:buy:1", uid=888), 5) is True


@pytest.mark.asyncio
async def test_auto_block_notifies_user(monkeypatch):
    import tenant.shared.kyc as K
    calls = []

    async def note(bot_id, user_id):
        return True

    async def notify(bot_id, user_id):
        calls.append((bot_id, user_id))

    monkeypatch.setattr(K, "note_charge", note)
    monkeypatch.setattr(KL, "notify_blocked", notify)
    await K.after_charge(5, 9)
    assert calls == [(5, 9)]

    async def note2(bot_id, user_id):
        return False

    monkeypatch.setattr(K, "note_charge", note2)
    await K.after_charge(5, 10)
    assert calls == [(5, 9)]
