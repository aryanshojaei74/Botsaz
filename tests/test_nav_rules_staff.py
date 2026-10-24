"""ناوبری: ۱) صفحهٔ سطحِ دوم «برگشت به منوی قبل» ندارد ۲) صفحهٔ قوانین سطحِ منو نیست ۳) مالک/ادمین هم (فقط رویِ صفحه‌هایِ
منویِ مشتری) دکمهٔ برگشت می‌گیرند، نه رویِ پیام‌هایِ تأیید/ردِ مدیریتی ۴) نسخه‌بندیِ قوانین + سابقهٔ تأیید."""
from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models

USER, OWNER = 555, 1


class Cap(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.n = 100

    async def close(self): ...
    async def stream_content(self, *a, **k): ...

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if getattr(self, "fail_edit", False) and type(method).__name__ == "EditMessageText":
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=method, message="Bad Request: message can't be edited")
        if type(method).__name__ in ("SendMessage", "EditMessageText"):
            self.n += 1
            return Message(message_id=getattr(method, "message_id", None) or self.n, date=datetime.datetime.now(),
                           chat=Chat(id=method.chat_id, type="private"), text=getattr(method, "text", "x")).as_(bot)
        return True


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=c) for t, c in r] for r in rows])


HOME = kb([("A", "a"), ("B", "b")])
BACK = [("⬅️ بازگشت به منو", "mbtn:home")]


def cbs(markup):
    return [b.callback_data for r in markup.inline_keyboard for b in r]


@pytest_asyncio.fixture
async def env(monkeypatch):
    import tenant
    from tenant.shared import public_api as _pa
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant, _pa):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._BOT_ROW_CACHE.clear(); tenant._SETTING_CACHE.clear(); tenant._NAV_STAFF_CACHE.clear(); tenant._NAV_MSGS.clear()
    import premium_emoji_dynamic as ped

    async def no_premium(_t): return None
    monkeypatch.setattr(ped, "_PROVIDERS", [no_premium])
    async with maker() as s:
        b = models.Bot(owner_id=OWNER, token="123:TESTTOKEN", username="shop", field="virtual_service", plan="pro", status="active")
        s.add(b); await s.flush()
        s.add(models.BotUser(bot_id=b.id, telegram_id=USER, full_name="Ali", balance=0))
        s.add(models.BotUser(bot_id=b.id, telegram_id=OWNER, full_name="Boss", balance=0))
        await s.commit()
        bid = b.id
    cap = Cap()
    bot = Bot("123:TESTTOKEN", session=cap)
    tenant._nav_install(bot)
    tenant._NAV_HOME_SIGS[(bid, False)] = frozenset({"a", "b"})
    tenant._NAV_HOME_SIGS[(bid, True)] = frozenset({"a", "b"})
    return tenant, bot, cap, bid, maker


async def walk(tenant, bot, cap, uid, pages):
    """home → pages…؛ آخرین markupِ ارسال‌شده را برمی‌گرداند."""
    tok = tenant._CUR_UID.set(uid)
    try:
        m = await bot.send_message(uid, "home", reply_markup=HOME)
        last = None
        for text, markup in pages:
            await bot.edit_message_text(text, chat_id=uid, message_id=m.message_id, reply_markup=markup)
            last = cap.calls[-1].reply_markup
        return m, last
    finally:
        tenant._CUR_UID.reset(tok)


@pytest.mark.asyncio
async def test_level2_has_no_prev_but_level3_has(env):
    tenant, bot, cap, bid, _ = env
    tok = tenant._CUR_UID.set(USER)
    try:
        m = await bot.send_message(USER, "home", reply_markup=HOME)
        await bot.edit_message_text("p2", chat_id=USER, message_id=m.message_id, reply_markup=kb([("x", "x")], BACK))
        assert "nav:back" not in cbs(cap.calls[-1].reply_markup)           # سطحِ دوم: فقط یک دکمهٔ برگشت
        await bot.edit_message_text("p3", chat_id=USER, message_id=m.message_id, reply_markup=kb([("y", "y")], BACK))
        assert "nav:back" in cbs(cap.calls[-1].reply_markup)               # سطحِ سوم: «منوی قبل» هست
    finally:
        tenant._CUR_UID.reset(tok)


@pytest.mark.asyncio
async def test_rules_page_is_not_a_menu_level(env):
    tenant, bot, cap, bid, _ = env
    gate = kb([("✅ قوانین را قبول می‌کنم", "thbtn:ok:5")], [("🏠 بازگشت به خانه", "mbtn:home")])
    tok = tenant._CUR_UID.set(USER)
    try:
        m = await bot.send_message(USER, "home", reply_markup=HOME)
        await bot.edit_message_text("rules", chat_id=USER, message_id=m.message_id, reply_markup=gate)
        await bot.edit_message_text("sub1", chat_id=USER, message_id=m.message_id, reply_markup=kb([("x", "x")], BACK))
        assert "nav:back" not in cbs(cap.calls[-1].reply_markup)           # بعد از قوانین، «قبلی» به قوانین برنمی‌گردد
        ent = tenant._NAV_MSGS[(USER, m.message_id)]
        assert all("rules" != h[0] for h in ent["hist"])
        await bot.edit_message_text("sub2", chat_id=USER, message_id=m.message_id, reply_markup=kb([("y", "y")], BACK))
        assert "nav:back" in cbs(cap.calls[-1].reply_markup)
        assert [h[0] for h in tenant._NAV_MSGS[(USER, m.message_id)]["hist"]][-1] == "sub1"
    finally:
        tenant._CUR_UID.reset(tok)


@pytest.mark.asyncio
async def test_staff_get_back_on_customer_pages_but_not_on_admin_approval(env):
    tenant, bot, cap, bid, _ = env
    tok = tenant._CUR_UID.set(OWNER)
    try:
        m = await bot.send_message(OWNER, "home", reply_markup=HOME)
        await bot.edit_message_text("p2", chat_id=OWNER, message_id=m.message_id, reply_markup=kb([("x", "x")], BACK))
        await bot.edit_message_text("p3", chat_id=OWNER, message_id=m.message_id, reply_markup=kb([("y", "y")], BACK))
        assert "nav:back" in cbs(cap.calls[-1].reply_markup)               # مالک هم «برگشت به منوی قبل» می‌بیند
        # پیامِ تأیید/ردِ مدیریتی (بدونِ دکمهٔ منوی اصلی) هرگز دکمهٔ برگشتِ تاریخچه نمی‌گیرد
        a = await bot.send_message(OWNER, "رسید", reply_markup=kb([("✅", "vcadm:ok:9"), ("❌", "vcadm:no:9")]))
        await bot.edit_message_text("رسید (در حال بررسی)", chat_id=OWNER, message_id=a.message_id,
                                    reply_markup=kb([("🔄", "vcadm:retry:9")]))
        assert "nav:back" not in cbs(cap.calls[-1].reply_markup)
    finally:
        tenant._CUR_UID.reset(tok)


@pytest.mark.asyncio
async def test_button_rules_versioning_and_history(env):
    tenant, bot, cap, bid, maker = env
    async with maker() as s:
        mb = models.MainButton(bot_id=bid, label="شماره مجازی", action="submenu", rules="قانون ۱", row=0, col=0)
        s.add(mb); await s.commit(); await s.refresh(mb)
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == USER))
    assert not await tenant._btn_rules_accepted(bid, mb, u.id)
    await tenant._btn_rules_mark(bid, mb, u.id)
    assert await tenant._btn_rules_accepted(bid, mb, u.id)

    async def set_rules(text):
        mb.rules = text
        async with maker() as s2:
            (await s2.get(models.MainButton, mb.id)).rules = text
            await s2.commit()
    await set_rules("قانون ۲ (تغییر کرد)")
    assert not await tenant._btn_rules_accepted(bid, mb, u.id)             # تغییرِ متن ⇒ تأییدِ مجدد
    await tenant._btn_rules_mark(bid, mb, u.id)
    assert await tenant._btn_rules_accepted(bid, mb, u.id)
    txt = await tenant._rules_history_text(bid, u)
    assert "شماره مجازی" in txt and tenant._rules_ver(mb.rules) in txt and "✅" in txt
    # تأییدِ قدیمیِ «1» یک‌بار به نسخهٔ فعلی ارتقا می‌یابد، بعدش با ویرایش دوباره پرسیده می‌شود
    await tenant.set_setting(bid, f"rules_ok:{mb.id}:{u.id}", "1")
    assert await tenant._btn_rules_accepted(bid, mb, u.id)
    await set_rules("قانون ۳")
    assert not await tenant._btn_rules_accepted(bid, mb, u.id)
    assert "نسخهٔ قدیمی" in await tenant._rules_history_text(bid, u)


@pytest.mark.asyncio
async def test_bot_level_rules_reask_after_change(env):
    tenant, bot, cap, bid, maker = env
    await tenant.set_setting(bid, "vs_rules_required", "1")
    await tenant.set_setting(bid, "vs_rules_text", "متن اول")
    fake = SimpleNamespace(token="123:TESTTOKEN")
    assert await tenant._vso_rules_blocked(fake, USER) == bid               # هنوز تأیید نکرده
    async with maker() as s:
        u = await s.scalar(select(models.BotUser).where(models.BotUser.telegram_id == USER))
        u.rules_accepted = True
        u.rules_accepted_at = datetime.datetime.now()
        u.rules_version = tenant._rules_ver("متن اول")
        await s.commit()
    tenant._SETTING_CACHE.clear(); tenant._BOT_ROW_CACHE.clear()
    assert await tenant._vso_rules_blocked(fake, USER) is None              # تأییدِ نسخهٔ فعلی
    await tenant.set_setting(bid, "vs_rules_text", "متن دوم")
    tenant._SETTING_CACHE.clear()
    assert await tenant._vso_rules_blocked(fake, USER) == bid               # متن عوض شد ⇒ دوباره می‌پرسد
    assert await tenant._vso_rules_blocked(fake, OWNER) is None             # مالک معاف است
