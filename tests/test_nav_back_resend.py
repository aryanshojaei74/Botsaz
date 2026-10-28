"""«بازگشت به منوی قبل»: اگر ویرایشِ پیام ممکن نبود، صفحهٔ قبلی پیامِ تازه می‌شود (نه پرتاب به منوی اصلی)."""
from __future__ import annotations

import datetime

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from tests.test_nav_rules_staff import BACK, HOME, USER, env, kb  # noqa: F401


def _cb(bot, mid, data):
    msg = Message(message_id=mid, date=datetime.datetime.now(), chat=Chat(id=USER, type="private"), text="x").as_(bot)
    return CallbackQuery(id="1", from_user=User(id=USER, is_bot=False, first_name="A"), chat_instance="c",
                         message=msg, data=data).as_(bot)


@pytest.mark.asyncio
async def test_back_edits_previous_page_in_place(env):
    tenant, bot, cap, bid, _ = env
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=USER, user_id=USER))
    tok = tenant._CUR_UID.set(USER)
    try:
        m = await bot.send_message(USER, "home", reply_markup=HOME)
        for t in ("cats", "list", "product"):
            await bot.edit_message_text(t, chat_id=USER, message_id=m.message_id, reply_markup=kb([(t, t)], BACK))
        await tenant.nav_back(_cb(bot, m.message_id, "nav:back"), bot, state)
        assert cap.calls[-2].text == "list"          # (آخرین call = answerCallbackQuery)
    finally:
        tenant._CUR_UID.reset(tok)


@pytest.mark.asyncio
async def test_back_resends_previous_page_when_edit_fails(env):
    tenant, bot, cap, bid, _ = env
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=USER, user_id=USER))
    tok = tenant._CUR_UID.set(USER)
    try:
        m = await bot.send_message(USER, "home", reply_markup=HOME)
        for t in ("cats", "list", "product"):
            await bot.edit_message_text(t, chat_id=USER, message_id=m.message_id, reply_markup=kb([(t, t)], BACK))
        cap.fail_edit = True
        n = len(cap.calls)
        await tenant.nav_back(_cb(bot, m.message_id, "nav:back"), bot, state)
        sent = [c for c in cap.calls[n:] if type(c).__name__ == "SendMessage"]
        assert sent and sent[-1].text == "list"      # صفحهٔ «لیست» دوباره فرستاده شد، نه منوی اصلی
        assert not any(getattr(c, "text", None) == "home" for c in sent)
    finally:
        tenant._CUR_UID.reset(tok)


@pytest.mark.asyncio
async def test_revisiting_a_history_page_via_its_own_back_button_pops_instead_of_pushing(env):
    """PATCH_NAV_REVISIT_POPS_V1 (فدایی): صفحهٔ اصلی → منوی ۲ → منوی ۳ → (دکمهٔ بازگشتِ خودِ منوی ۳) منوی ۲ →
    «بازگشت به منوی قبل» باید صفحهٔ اصلی را بدهد، نه دوباره منوی ۳ را."""
    from tests.test_nav_rules_staff import cbs
    tenant, bot, cap, bid, _ = env
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=USER, user_id=USER))
    tok = tenant._CUR_UID.set(USER)
    try:
        m = await bot.send_message(USER, "home", reply_markup=HOME)
        l2 = kb([("c", "smopen:3")], BACK)
        l3 = kb([("d", "d")], [("⬅️ بازگشت به منوی قبل", "smopen:2"), ("🏠 منوی اصلی", "mbtn:home")])
        await bot.edit_message_text("L2", chat_id=USER, message_id=m.message_id, reply_markup=l2)
        await bot.edit_message_text("L3", chat_id=USER, message_id=m.message_id, reply_markup=l3)
        await bot.edit_message_text("L2", chat_id=USER, message_id=m.message_id, reply_markup=l2)    # دکمهٔ بازگشتِ خودِ منوی ۳
        assert "nav:back" not in cbs(cap.calls[-1].reply_markup)       # قبلِ منوی ۲ همان صفحهٔ اصلی است ⇒ «منوی قبل» نمی‌خواهد
        ent = tenant._NAV_MSGS[(USER, m.message_id)]
        assert [h[0] for h in ent["hist"]] == ["home"]                 # L3 توی تاریخچه نیست
        # ورود به منوی ۳ و برگشتنِ مجدد هم تاریخچه را بزرگ نمی‌کند
        await bot.edit_message_text("L3", chat_id=USER, message_id=m.message_id, reply_markup=l3)
        assert [h[0] for h in ent["hist"]] == ["home", "L2"]
        await bot.edit_message_text("L2", chat_id=USER, message_id=m.message_id, reply_markup=l2)
        assert [h[0] for h in ent["hist"]] == ["home"]
        # صفحهٔ تازه (متنِ دیگر) هنوز push می‌شود
        await bot.edit_message_text("L4", chat_id=USER, message_id=m.message_id, reply_markup=kb([("e", "e")], BACK))
        assert [h[0] for h in ent["hist"]] == ["home", "L2"]
        await tenant.nav_back(_cb(bot, m.message_id, "nav:back"), bot, state)
        assert cap.calls[-2].text == "L2"
    finally:
        tenant._CUR_UID.reset(tok)


@pytest.mark.asyncio
async def test_revisit_does_not_match_a_page_with_same_text_but_different_buttons(env):
    tenant, bot, cap, bid, _ = env
    tok = tenant._CUR_UID.set(USER)
    try:
        m = await bot.send_message(USER, "home", reply_markup=HOME)
        await bot.edit_message_text("pick", chat_id=USER, message_id=m.message_id, reply_markup=kb([("a1", "a1")], BACK))
        await bot.edit_message_text("pick", chat_id=USER, message_id=m.message_id, reply_markup=kb([("b1", "b1")], BACK))
        # متن یکی است و دکمه‌ها فرق دارد ⇒ متنِ cur == متنِ جدید ⇒ اصلاً push نمی‌شود (رفتارِ قبلی)
        ent = tenant._NAV_MSGS[(USER, m.message_id)]
        assert [h[0] for h in ent["hist"]] == ["home"]
        await bot.edit_message_text("other", chat_id=USER, message_id=m.message_id, reply_markup=kb([("z", "z")], BACK))
        await bot.edit_message_text("pick", chat_id=USER, message_id=m.message_id, reply_markup=kb([("q", "q")], BACK))
        assert [h[0] for h in ent["hist"]] == ["home", "pick", "other"]      # دکمه‌هایِ متفاوت ⇒ صفحهٔ تازه، push
    finally:
        tenant._CUR_UID.reset(tok)
